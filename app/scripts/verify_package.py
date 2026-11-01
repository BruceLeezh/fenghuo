# -*- coding: utf-8 -*-
"""交付包自检 —— 提交/打包前跑一遍，防止"代码与方案书对不上"和"凭据随包外发"。

背景（为什么需要它）：
  2026-09-24 审查正式提交包时抓到两类问题，都不是靠肉眼能发现的——
  1) `data/source_config.json` 里躺着真实的 ifind/wind 密钥（配置中心持久化绕过了
     只对 `.env` 做的脱敏），而该目录会被打进对外分发的部署包；
  2) 方案书 7.1 已经写入「网络守卫 / 独立实例 / 出网前脱敏」，但包里 `server.py` 是
     改动前的快照，`security/netguard.py` 与 `classified_server.py` 干脆不在包里。
  两类问题都会在"照着源码核验"的评委面前直接暴露，故固化成脚本，每次交付前跑。

用法：
    python scripts/verify_package.py            # 校验脚本所在目录（app/ 或交付包根）
    python scripts/verify_package.py <路径>      # 校验指定目录

退出码：0 = 全部通过；1 = 有失败项。
"""
import json
import os
import re
import subprocess
import sys

ROOT = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else \
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable

try:
    sys.stdout.reconfigure(line_buffering=True, encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

OK, FAIL = [], []


def chk(name, cond, detail=""):
    (OK if cond else FAIL).append(name)
    print("   %s %s%s" % ("OK  " if cond else "★FAIL", name,
                         ("  — " + detail) if detail else ""))
    return cond


def head(t):
    print("\n" + "=" * 68)
    print(t)
    print("=" * 68)


# ---------------------------------------------------------------- ① 语法
head("① 语法编译（全部 .py）")
pys = []
for dp, dirs, files in os.walk(ROOT):
    dirs[:] = [d for d in dirs if d not in ("node_modules", "__pycache__", ".venv", ".git")]
    pys += [os.path.join(dp, f) for f in files if f.endswith(".py")]
bad = []
for p in pys:
    try:
        # 用内置 compile 在内存里编译；py_compile 的 cfile=os.devnull 在 Windows 上会抛错
        compile(open(p, encoding="utf-8").read(), p, "exec")
    except SyntaxError as e:
        bad.append("%s: %s" % (os.path.relpath(p, ROOT), e))
chk("编译 %d 个 .py" % len(pys), not bad, "；".join(bad))

# ---------------------------------------------------------------- ② 导入与路由
head("② 模块导入与路由（含适配器注册链路）")
code = (
    "import sys;sys.path.insert(0,sys.argv[1]);"
    "import pipeline;"
    "from core.adapter import DATA_SOURCE_REGISTRY as R;"
    "import server as s;"
    "import classified_server as c;"
    "D={'/openapi.json','/docs','/docs/oauth2-redirect','/redoc'};"
    "m=[r.path for r in s.app.routes if getattr(r,'path','') not in D];"
    "cm=[r.path for r in c.app.routes if getattr(r,'path','') not in D];"
    "print(len(R));print(len(m));print(cm)"
)
r = subprocess.run([PY, "-c", code, ROOT], cwd=ROOT, capture_output=True,
                   text=True, encoding="utf-8", errors="replace")
o = (r.stdout or "").strip().splitlines()
if len(o) >= 3:
    chk("适配器注册数 = 7", o[0] == "7", "实为 %s" % o[0])
    chk("公开实例对外路由 = 50", o[1] == "50", "实为 %s" % o[1])
    chk("涉密实例只含 /api/upload", o[2] == "['/api/upload']", "实为 %s" % o[2])
else:
    chk("模块导入", False, (r.stderr or "")[-300:])

# ---------------------------------------------------------------- ③ 关键实现标记
head("③ 关键实现标记（与方案书 7.1 / README 的口径对齐）")
srv = open(os.path.join(ROOT, "server.py"), encoding="utf-8").read()
ng = open(os.path.join(ROOT, "security", "netguard.py"), encoding="utf-8").read()
cs = open(os.path.join(ROOT, "classified_server.py"), encoding="utf-8").read()
html = open(os.path.join(ROOT, "static", "index.html"), encoding="utf-8").read()
chk("server.py 解析入口套网络守卫", "no_network()" in srv)
chk("server.py 密级自检 _assert_classified_route", "_assert_classified_route" in srv)
chk("server.py 出网前脱敏 _mask_text/_mask_terms", "_mask_text" in srv and "_mask_terms" in srv)
# ⚠️ 不能用 `'allow_origins=["*"]' not in srv`：源码注释里就写着这串，会误报
chk("server.py 跨域白名单已收紧", "_allowed_origins or _DEFAULT_ORIGINS" in srv)
chk("server.py 独立部署开关", "CLASSIFIED_STANDALONE" in srv)
chk("classified_server.py 绑定 127.0.0.1", 'CLASSIFIED_HOST", "127.0.0.1"' in cs)
chk("netguard.py 拦 socket 建连", "socket.socket.connect" in ng)
chk("index.html 含 CLASSIFIED_API", "CLASSIFIED_API" in html)

# ---------------------------------------------------------------- ④ 凭据残留
head("④ 凭据残留扫描（对外交付前必过）")
# 本地开发树（app/ 与 deploy/ 同级）本来就持有真实凭据，用于本地联调；
# 该场景下降级为告警，不判失败——脚本的硬判据是"对外交付物"。
IS_DEV_TREE = (os.path.basename(ROOT) == "app"
               and os.path.isdir(os.path.join(os.path.dirname(ROOT), "deploy")))
hits = []
for dp, dirs, files in os.walk(ROOT):
    dirs[:] = [d for d in dirs if d not in ("node_modules", "__pycache__", ".venv", ".git")]
    for f in files:
        if not f.endswith((".py", ".js", ".json", ".env", ".md", ".bat", ".html", ".txt")):
            continue
        p = os.path.join(dp, f)
        if os.path.getsize(p) > 3_000_000:
            continue
        rel = os.path.relpath(p, ROOT)
        t = open(p, encoding="utf-8", errors="replace").read()
        if f == ".env":
            for line in t.splitlines():
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    k, v = k.strip(), v.strip()
                    if any(w in k.upper() for w in ("TOKEN", "KEY", "SECRET", "PASSWORD")) \
                            and len(v) >= 24 and not v.startswith(("http", "你的")):
                        hits.append("%s: %s 值疑似真实" % (rel, k))
        else:
            for m in re.finditer(r"(?i)(sk-[A-Za-z0-9]{16,}|bearer\s+[A-Za-z0-9_\-]{24,})", t):
                hits.append("%s: %s" % (rel, m.group(0)[:16]))
        if f.endswith("source_config.json"):
            try:
                cfg = json.loads(t)
                for nm, s in (cfg.get("sources") or {}).items():
                    cred = (s.get("credential") or {}) if isinstance(s, dict) else {}
                    for k, v in cred.items():
                        if k in ("mcp_key", "api_key", "token") and v:
                            hits.append("%s: %s.%s 非空" % (rel, nm, k))
            except Exception:  # noqa: BLE001
                pass
if IS_DEV_TREE and hits:
    print("   ⚠ WARN 检测到凭据 %d 处 —— 当前是本地开发树（app/），持有真实凭据属预期；" % len(hits))
    print("           对外交付前请对交付包执行本脚本，那里必须为 0。")
    for h in hits[:6]:
        print("           · %s" % h)
else:
    chk("无凭据残留", not hits, "；".join(hits[:6]))

# ---------------------------------------------------------------- ⑤ 安全回归
head("⑤ 安全边界端到端回归")
vs = os.path.join(ROOT, "scripts", "verify_security.py")
if os.path.exists(vs):
    out = subprocess.run([PY, vs], cwd=ROOT, capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=420)
    tail = (out.stdout or "") + (out.stderr or "")
    for line in tail.splitlines():
        if line.strip().startswith(("OK ", "FAIL", "通过", "结果")):
            print("   " + line.strip())
    chk("verify_security 通过", out.returncode == 0)
else:
    chk("verify_security.py 存在", False, "未找到")

# ---------------------------------------------------------------- 汇总
print("\n" + "-" * 68)
print("通过 %d / %d" % (len(OK), len(OK) + len(FAIL)))
if FAIL:
    print("失败：%s" % "、".join(FAIL))
print("结果：%s" % ("✅ 全部通过" if not FAIL else "❌ 有 %d 项失败" % len(FAIL)))
sys.exit(0 if not FAIL else 1)
