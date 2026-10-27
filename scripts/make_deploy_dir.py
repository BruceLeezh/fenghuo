"""打包一个干净的部署目录（只含运行必需文件），供 workbuddy_sites_deploy 上传。

排除：历史截图 *_*.png、E2E 测试脚本 scripts/、__pycache__、_exported_report.html、
      _c.json/_w.json 等临时文件。

⚠️ 本脚本是**增量覆盖、从不删除**（避免误删 deploy/ 里的人为内容），
   代价是**陈旧的残留项会永远留在 deploy/ 里并随部署包一起上传**。
   实测踩到过：`deploy/.git/`（早期 git 推送尝试的遗留，现走 REST API）滞留了 95 个文件，
   每次部署都被白白上传。因此这里显式维护一份 _PURGE 清单做定点清理。

用法：python scripts/make_deploy_dir.py
输出：D:/金融科技专项赛/deploy/
"""
import os
import shutil

SRC = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
DEPLOY = os.path.abspath(os.path.join(SRC, "..", "deploy"))

# 单文件：直接复制
# ⚠️ requirements.lock.txt 必须在内：README 与部署说明都让用户跑
#    `pip install -r requirements.lock.txt`（可复现构建），漏了它会当场报文件不存在。
SINGLE_FILES = ["server.py", "classified_server.py", "pipeline.py",
                "requirements.txt", "requirements.lock.txt", "README.md"]
# 隐藏文件 .env 单独处理（含凭据，不打印内容）
# 目录：整目录复制（排除 __pycache__ / *.pyc）
DIRS = ["core", "adapters", "data", "integrations", "llm", "security", "static"]

_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", "*.bak", "*.bak_*")

# 需要从 deploy/ 里**剔除**的陈旧残留（复制之后执行，定点清理）
_PURGE = [
    ".git",                 # 早期 git 推送遗留；现用 push_to_github.py 走 REST API
    "__pycache__",          # 字节码缓存
    "render.yaml",          # Render 部署已弃用（2026-09-15 确认不再使用）
    os.path.join("core", "ewma.py"),   # 已删除的零引用死代码（能力由 servicelevel.py 覆盖）
    os.path.join("data", "mock", "test_供应商.xlsx"),  # 早期 mock 残留，app/ 与交付包均无
]
# 需要剔除的临时产物模式（E2E 写出的截图/导出物，可能被误带进来）
_PURGE_GLOBS = ["_*.png", "_exported_report.html", "_c.json", "_w.json", "_r.json", "*.pyc",
                "*.bak", "*.bak_*"]


def _purge():
    """清理 deploy/ 里的陈旧残留；返回被清理的条目名。"""
    import glob
    removed = []
    for name in _PURGE:
        p = os.path.join(DEPLOY, name)
        if os.path.isdir(p):
            shutil.rmtree(p); removed.append(name + "/")
        elif os.path.isfile(p):
            os.remove(p); removed.append(name)
    for root, dirs, files in os.walk(DEPLOY):
        for d in list(dirs):
            if d == "__pycache__":
                shutil.rmtree(os.path.join(root, d))
                removed.append(os.path.relpath(os.path.join(root, d), DEPLOY) + "/")
                dirs.remove(d)
        for f in files:
            if any(glob.fnmatch.fnmatch(f, g) for g in _PURGE_GLOBS):
                os.remove(os.path.join(root, f))
                removed.append(os.path.relpath(os.path.join(root, f), DEPLOY))
    return removed


def _sanitize_credentials():
    """把 deploy/data/source_config.json 里的凭据字段清零。

    ⚠️ 为什么必须做：配置中心（core/datasource_config.py）会把用户在界面上填的凭据
    **持久化进 source_config.json**，这条路径**绕过了只对 `.env` 做的脱敏**。
    而 deploy/ 会被 make_server_bundle.py 打成部署包对外分发 ——
    实测就抓到过 ifind.mcp_key（930 字符）与 wind.api_key 的真实值躺在里面。
    运行时凭据由服务器上的 `.env` 提供，这里清零不影响功能。
    """
    import json
    p = os.path.join(DEPLOY, "data", "source_config.json")
    if not os.path.exists(p):
        return None
    try:
        d = json.load(open(p, encoding="utf-8"))
    except Exception:
        return None
    n = 0
    for _name, s in (d.get("sources") or {}).items():
        cred = s.get("credential") if isinstance(s, dict) else None
        if isinstance(cred, dict):
            for k in list(cred):
                if cred[k]:
                    cred[k] = ""
                    n += 1
    if n:
        # 原地覆写（不用 os.replace：WPS/编辑器占用时会 WinError 5）
        with open(p, "r+b") as fh:
            fh.seek(0)
            fh.write(json.dumps(d, ensure_ascii=False, indent=2).encode("utf-8"))
            fh.truncate()
    return n


def main():
    os.makedirs(DEPLOY, exist_ok=True)  # 增量覆盖，不删除已有文件

    for f in SINGLE_FILES:
        src = os.path.join(SRC, f)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(DEPLOY, f))
            print(f"  file   {f}")
        else:
            print(f"  SKIP   {f} (不存在)")

    # .env：**不覆盖已存在的 deploy/.env**
    #
    # ⚠️ 这里以前是直接 shutil.copy2(app/.env → deploy/.env)，会把 app/.env 里的
    #    真实凭据（企查查 token、iFinD key 等）写进 deploy/。而 deploy/ 会被
    #    make_server_bundle.py 打成部署包，等于**把密钥打进了要外发的产物**。
    #    现在改为：首次创建才复制；已存在则保留，避免把脱敏过的占位符重新污染成真凭据。
    #    服务器上真正的凭据由部署时手填（见 README_部署.txt），不靠这个文件传递。
    env = os.path.join(SRC, ".env")
    env_dst = os.path.join(DEPLOY, ".env")
    if os.path.exists(env) and not os.path.exists(env_dst):
        shutil.copy2(env, env_dst)
        print("  file   .env (首次创建，内容保密)")
    elif os.path.exists(env_dst):
        print("  keep   .env (已存在，保留不动 —— 防止真实凭据被打进部署包)")
    else:
        print("  SKIP   .env (app/.env 不存在)")

    for d in DIRS:
        src = os.path.join(SRC, d)
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(DEPLOY, d), ignore=_IGNORE, dirs_exist_ok=True)
            print(f"  dir    {d}/")

    # 定点清理陈旧残留
    purged = _purge()
    if purged:
        print(f"  purge  {len(purged)} 项：{', '.join(purged[:8])}"
              + (" …" if len(purged) > 8 else ""))
    else:
        print("  purge  无残留")

    # 凭据净化（必须在复制 data/ 之后）
    ncred = _sanitize_credentials()
    if ncred:
        print(f"  cred   source_config.json 已清零 {ncred} 个凭据字段")
    elif ncred == 0:
        print("  cred   source_config.json 凭据本就为空")
    else:
        print("  cred   source_config.json 不存在，跳过")

    # 统计体积
    total = 0
    for root, dirs, files in os.walk(DEPLOY):
        for f in files:
            total += os.path.getsize(os.path.join(root, f))
    print(f"\n部署目录就绪：{DEPLOY}")
    print(f"总大小：{total/1024:.1f} KB")


if __name__ == "__main__":
    main()

