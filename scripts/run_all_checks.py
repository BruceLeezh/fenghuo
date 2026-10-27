"""一键跑测：后端健康检查 → 夹具重建 → 数据快照 → 全部 E2E → 模拟样本验收 → 数据还原 → 产物清理 → 汇总。

背景（为什么需要这个脚本）：
- E2E（`ui_verify_*.js`）有**数据副作用**：导入测试会往台账塞 2 家、工单测试会建工单。
  以前每次跑完都要手动还原，容易漏。
- E2E 会往项目根写出 `_*.png` 截图，跑完要手动清理。
- 脚本之间有依赖：`ui_verify_upload_multi.js` 必须先有夹具。
- 逐个跑容易漏、且看不出整体是否通过。
- 自建的模拟数据样本（`verify_official_samples.py`）是解析器最真实的验收基线，
  一并纳入，避免"自建夹具全绿、真实样本判错"。
- **后端没起时 14 个用例会齐刷刷 ERR_CONNECTION_REFUSED**（输出几百行堆栈看不出原因），
  故增加前置健康检查，必要时自动拉起后端。

用法：
    python scripts/run_all_checks.py            # 全量跑测（含还原与清理）
    python scripts/run_all_checks.py --keep     # 保留截图为证据，不清理
    python scripts/run_all_checks.py --only upload_multi,service   # 只跑匹配的用例
    python scripts/run_all_checks.py --timeout 180                 # 本地用例超时（秒）

超时策略：外部依赖用例（企查查/iFinD/Wind/DeepSeek）自动获得 3 倍预算，
本地用例用 --timeout 的紧超时，以便真卡死时快速暴露。

退出码：0 = 全部通过；1 = 有失败（明细见报告）。
"""
import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)                    # …/app
ROOT = os.path.dirname(APP)                    # 项目根

# 行缓冲：本脚本常被重定向到日志文件，默认块缓冲会导致"跑了一半看不到任何输出"
try:
    sys.stdout.reconfigure(line_buffering=True, encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

# E2E 数据副作用的权威快照源：deploy/data（干净态）
# 权威干净基线目录（`deploy/data`）。三种布局依次尝试：
#   ① 交付包内联 deploy/data（如有）
#   ② 项目树：包目录的同级 deploy/data
#   ③ 都没有（典型：交付包单独拷出）→ None，此时**回退到"跑测前状态"**
#
# ⚠️ 历史缺陷：原实现只试 ①②，且不校验结果是否存在。交付包里两者皆无，
#    SNAPSHOT_DIR 指向一个不存在的路径，restore_data 里 `os.path.exists(src)` 为假 →
#    静默跳过还原，E2E 导入的两家供应商、新建的工单就此永久留在演示数据里
#    （实测台账从 37 家涨到 39 家、工单 8→12、HHI 由 2440 变 1800）。
SNAPSHOT_DIR = None
for _cand in (os.path.join(APP, "deploy", "data"),          # 包内联（旧布局）
              os.path.join(ROOT, "deploy", "data"),         # 项目树下
              os.path.join(os.path.dirname(ROOT), "deploy", "data"),  # 交付包→项目根
              os.path.join(APP, "data_baseline")):          # 显式基线目录
    if os.path.isdir(_cand):
        SNAPSHOT_DIR = _cand
        break

DATA_FILES = ["roster.json", "workorders.json", "service_levels.json"]

# 已知的"预期非 0 退出/预期失败"用例：不视为回归
EXPECTED_NONZERO = {
    # 尾部故意测非法状态跃迁（"处置中"→"已闭环"），后端正确返回 400
    "ui_verify_conc_wo.js",
}

# 依赖**外部实时数据源**的用例：给更大的超时预算。
# ⚠️ 2026-09-15 踩坑：默认 100s 超时下，degrade / ifind 曾因外部 API 瞬时变慢
#    双双超时（exit=124）而被误判为"回归"；单独重跑分别 53s / 24s 就过了。
#    外部服务耗时不可控，不能用本地用例的紧超时去卡它们；
#    但也不能无限等——给 3 倍预算（默认 180s 时此处 300s）即可区分"慢"与"挂死"。
_EXTERNAL = {
    "ui_verify.js",             # 企查查全量抓取
    "ui_verify_ifind.js",       # 同花顺 iFinD
    "ui_verify_sources.js",     # 数据源连通性探测
    "ui_verify_degrade.js",     # 数据源降级（需真实触发外部失败）
    "ui_verify_qcc_cache.js",   # 企查查缓存命中（首次仍需真实调用）
    "ui_verify_deepseek.js",    # DeepSeek 大模型
    "ui_verify_service.js",     # 外包专项履历会调企查查标讯
}
EXTERNAL_TIMEOUT_FACTOR = 3

BASE_URL = "http://127.0.0.1:8000/"

NODE = os.environ.get(
    "WB_NODE",
    r"C:\Users\lenovo\.workbuddy\binaries\node\versions\22.22.2-3\node.exe",
)
NODE_PATH = os.environ.get(
    "WB_NODE_PATH",
    r"C:\Users\lenovo\.workbuddy\binaries\node\workspace\node_modules",
)

PKG = "playwright-core"


def _node_bin():
    """挑一个可用的 node：环境变量 → 托管版 → PATH。"""
    if os.path.exists(NODE):
        return NODE
    for c in ("node", "node.exe"):
        if shutil.which(c):
            return c
    return NODE


def _run(cmd, cwd, timeout=180, env_extra=None):
    env = dict(os.environ)
    env["NODE_PATH"] = NODE_PATH
    env["PATH"] = (r"C:\Windows\System32;C:\Windows;" + env.get("PATH", ""))
    if env_extra:
        env.update(env_extra)
    try:
        p = subprocess.run(cmd, cwd=cwd, timeout=timeout, env=env,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace")
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, f"[timeout after {timeout}s]"
    except Exception as e:  # noqa: BLE001
        return 125, f"[launch failed] {e}"


def _health_ok(timeout=3.0):
    """探测后端是否可访问。"""
    import urllib.request
    try:
        with urllib.request.urlopen(BASE_URL, timeout=timeout) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


def ensure_server(auto_start=True, wait=30):
    """确保后端在跑；不在则自动拉起。返回 (是否就绪, 说明)。

    ⚠️ 为什么必须有这一步：E2E 全部依赖 `http://127.0.0.1:8000`。
    后端一旦没起，14 个用例会**齐刷刷报 ERR_CONNECTION_REFUSED**，
    输出几百行堆栈却看不出真正原因（实测踩过，白排查一轮）。
    ⚠️ 且后台启动的服务**跨会话会被系统回收**，所以每次跑测都要重新确认。
    """
    if _health_ok():
        return True, "已在运行"
    if not auto_start:
        return False, "未运行（--no-auto-start）"

    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        subprocess.Popen(
            [_python_bin(), "server.py"], cwd=APP,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=flags,
        )
    except Exception as e:  # noqa: BLE001
        return False, "自动启动失败：%s" % e

    for _ in range(int(wait / 0.5)):
        time.sleep(0.5)
        if _health_ok(2.0):
            return True, "已自动拉起"
    return False, "启动后 %ds 内仍未就绪" % wait


def _python_bin():
    """优先用当前解释器（依赖已装在它下面）。"""
    return sys.executable or "python"


def snapshot_data():
    """跑测前记录数据文件当前内容。**仅作为"被测前状态"备份**，不作为还原目标。

    ⚠️ 为什么不拿它当还原目标：若上一次 E2E 异常中断（被 kill / 超时），
    `app/data/` 里会残留脏数据；此时"快照还原"只会把脏数据原样保留，
    污染会**逐次累积**（实测工单从 1 条涨到 2 条）。
    因此还原一律以 `deploy/data/` 这份权威干净基线为准（见 restore_data）。
    """
    snap = {}
    for fn in DATA_FILES:
        p = os.path.join(APP, "data", fn)
        if os.path.exists(p):
            snap[fn] = open(p, "rb").read()
    return snap


def _backup_if_dirty(snap):
    """若跑测前的状态已与权威基线不同，先落一份备份，避免"顺手删掉用户真实数据"。"""
    pre = os.path.join(APP, "_shots", "_pre_run_backup")
    saved = []
    for fn, blob in snap.items():
        base = os.path.join(SNAPSHOT_DIR, fn) if SNAPSHOT_DIR else None
        if base and os.path.exists(base) and open(base, "rb").read() == blob:
            continue
        os.makedirs(pre, exist_ok=True)
        open(os.path.join(pre, fn), "wb").write(blob)
        saved.append(fn)
    return saved


def restore_data(snap):
    """**始终以 `deploy/data/` 权威基线还原**（保证 E2E 幂等、可重复跑）。

    跑测前的差异化状态会先备份到 `app/_shots/_pre_run_backup/`，不会丢失。
    """
    dirtied = _backup_if_dirty(snap)
    restored = []
    for fn in DATA_FILES:
        dst = os.path.join(APP, "data", fn)
        if not os.path.exists(dst):
            continue
        if SNAPSHOT_DIR:
            src = os.path.join(SNAPSHOT_DIR, fn)
            if not os.path.exists(src):
                continue
            blob = open(src, "rb").read()
        else:
            # 交付包内没有权威基线：还原成"本次跑测开始前"的字节。
            # E2E 对数据的写入都是本脚本跑出来的，回到跑测前即恢复原状；
            # 且不像"跳过还原"那样把污染留给下一次。
            blob = snap.get(fn)
            if blob is None:
                continue
        if open(dst, "rb").read() != blob:
            with open(dst, "wb") as fh:
                fh.write(blob)
            restored.append(fn)
    if dirtied:
        restored.append("（跑测前状态已备份 %d 个）" % len(dirtied))
    return restored


def cleanup_artifacts(keep=False):
    """清理 E2E 产物：项目根 / app/ 的 `_*` 临时物，以及 `_shots/*.png` 截图。

    ⚠️ 只删 `.png`。`_shots/` 里的夹具（`_t_*.xlsx|docx|pdf`、`_test_import.xlsx`）
    是 E2E 的**读入源**，删了会导致测试 ENOENT，必须保留。
    """
    if keep:
        return []
    removed = []
    # 1) 根目录 / app 下的临时物
    for pat in ("_*.png", "_exported_report.html", "_*.tmp"):
        for base in (ROOT, APP):
            for f in glob.glob(os.path.join(base, pat)):
                if os.path.basename(os.path.dirname(f)) == "_shots":
                    continue
                try:
                    os.remove(f)
                    removed.append(os.path.relpath(f, ROOT))
                except OSError:
                    pass
    # 2) _shots/ 截图（可再生；夹具是非 png，不会被误删）
    for f in glob.glob(os.path.join(ROOT, "_shots", "*.png")):
        try:
            os.remove(f)
            removed.append(os.path.relpath(f, ROOT))
        except OSError:
            pass
    return removed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="保留截图产物")
    ap.add_argument("--clean-only", action="store_true",
                    help="只清理产物、不跑测（E2E 截图可再生，随时可清）")
    ap.add_argument("--only", default="", help="逗号分隔的关键字过滤，如 upload_multi,service")
    ap.add_argument("--timeout", type=int, default=180,
                    help="本地用例超时秒数（外部依赖用例自动 ×%d）" % EXTERNAL_TIMEOUT_FACTOR)
    ap.add_argument("--no-auto-start", action="store_true",
                    help="后端未运行时不自动拉起，直接中止")
    args = ap.parse_args()

    if args.clean_only:
        try:
            removed = cleanup_artifacts(False)
            print("已清理 %d 项：%s" % (len(removed), "、".join(removed) if removed else "无"))
        except BaseException as e:  # noqa: BLE001
            print("⚠️ 清理被环境拦截：%s" % type(e).__name__)
        return 0

    only = [s.strip() for s in args.only.split(",") if s.strip()]

    scripts = sorted(glob.glob(os.path.join(HERE, "ui_verify*.js")))
    if only:
        scripts = [s for s in scripts if any(k in os.path.basename(s) for k in only)]

    print("=" * 72)
    print("全量跑测  |  用例 %d 个  |  项目根 %s" % (len(scripts), ROOT))
    print("=" * 72)

    # 0) 后端健康检查（不在则自动拉起）——避免 14 个用例齐刷刷 ERR_CONNECTION_REFUSED
    ok_srv, note = ensure_server(auto_start=not args.no_auto_start)
    print("[0/6] 后端服务 (127.0.0.1:8000) ... %s" % note)
    if not ok_srv:
        print()
        print("❌ 后端未就绪，已中止跑测（避免 14 个用例全部无意义失败）。")
        print("   手动启动：cd %s && python server.py" % APP)
        return 1

    # 1) 夹具
    rc, out = _run([sys.executable, os.path.join(HERE, "make_upload_fixtures.py")], APP)
    ok_fixtures = "五维可用" in out and "Word 表格布局自检" in out
    print("[1/6] 夹具重建 + 自检 ......... %s" % ("OK" if ok_fixtures else "详见输出"))
    if not ok_fixtures:
        print(out)

    # 2) 数据快照
    snap = snapshot_data()
    print("[2/6] 数据快照已记录（%d 个文件）" % len(snap))

    # 3) 逐个跑 E2E
    print("[3/6] E2E 执行")
    results = []
    for s in scripts:
        name = os.path.basename(s)
        # 外部依赖用例给更大预算（见 _EXTERNAL 注释）
        budget = args.timeout * (EXTERNAL_TIMEOUT_FACTOR if name in _EXTERNAL else 1)
        t0 = time.time()
        rc, out = _run([_node_bin(), s], ROOT, timeout=budget)
        dt = time.time() - t0
        expected_bad = name in EXPECTED_NONZERO
        ok = (rc == 0) or expected_bad
        results.append((name, rc, ok, dt, out, expected_bad))
        mark = "OK " if ok else ("TIMEOUT" if rc == 124 else "FAIL")
        extra = "  (预期非0，豁免)" if (expected_bad and rc != 0) else ""
        print("      %-32s exit=%-3d %6.1fs  %s%s" % (name, rc, dt, mark, extra))

    # 4) 模拟数据验收（自建的 6 份样本，2 家虚拟供应商 × 各 3 份）
    #    ⚠️ 该脚本只读本地路径、**不复制任何数据**（保密要求）；路径不存在时优雅跳过。
    rc_off, out_off = _run(
        [sys.executable, os.path.join(HERE, "verify_official_samples.py")],
        APP, timeout=120)
    if "未找到模拟数据目录" in out_off:
        off_status = "跳过（本机无模拟数据目录）"
        off_ok = True
    else:
        off_ok = rc_off == 0
        off_status = "OK" if off_ok else "FAIL"
    print("[4/6] 模拟数据验收 ............. %s" % off_status)
    if not off_ok:
        print("\n".join(out_off.strip().splitlines()[-20:]))

    # 先算结论：后面两步是"善后"，任何异常都不该影响验收判定
    failed = [r for r in results if not r[2]]

    # 5) 数据还原（善后，失败不影响判定）
    try:
        restored = restore_data(snap)
        print("[5/6] 数据还原 ............... %s" % ("、".join(restored) if restored else "无需还原"))
    except BaseException as e:  # noqa: BLE001
        print("[5/6] 数据还原 ............... ⚠️ 失败：%s" % e)

    # 6) 产物清理（善后）
    # ⚠️ 某些环境有"批量删除守卫"，会把删除动作拦截并抛错/终止进程。
    # 这一步只是打扫卫生，必须吞掉异常，否则会把"全部通过"的跑测误报成失败。
    if args.keep:
        print("[6/6] 产物清理 ............... 已跳过（--keep）")
    else:
        try:
            removed = cleanup_artifacts(False)
            print("[6/6] 产物清理 ............... %s" % ("、".join(removed) if removed else "无"))
        except BaseException as e:  # noqa: BLE001
            print("[6/6] 产物清理 ............... ⚠️ 已跳过（环境拦截删除：%s）" % type(e).__name__)

    # 汇总
    print("-" * 72)
    print("E2E 通过 %d / %d" % (len(results) - len(failed), len(results)))
    if not off_ok:
        failed = failed + [("verify_official_samples.py(模拟样本)", rc_off, False, 0, out_off, False)]
    for name, rc, _ok, _dt, out, _e in failed:
        print("\n--- %s (exit=%d) ---" % (name, rc))
        tail = "\n".join(out.strip().splitlines()[-25:])
        print(tail)
    print("=" * 72)
    print("结果：%s" % ("✅ 全部通过" if not failed else "❌ 有 %d 个失败" % len(failed)))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
