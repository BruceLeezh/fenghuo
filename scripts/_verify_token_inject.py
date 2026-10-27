"""本地验证令牌注入链路（三种部署形态）。

用法：python _verify_token_inject.py
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")
APP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app")
PY = sys.executable
PORT = 8022
TOKEN = "local-verify-token-abc"


def boot(env_extra, label):
    env = dict(os.environ)
    env.update({"PORT": str(PORT), "BIND_HOST": "127.0.0.1"})
    env.update(env_extra)
    p = subprocess.Popen([PY, "server.py"], cwd=APP, env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        time.sleep(0.5)
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/api/sources" % PORT, timeout=2)
            return p
        except Exception:
            if p.poll() is not None:
                print("  !! 进程提前退出 rc=%s" % p.returncode)
                return None
    p.terminate()
    return None


def get_home():
    with urllib.request.urlopen("http://127.0.0.1:%d/" % PORT, timeout=10) as r:
        return r.read().decode("utf-8"), dict(r.headers)


def post(path, body, headers=None, method="POST"):
    req = urllib.request.Request(
        "http://127.0.0.1:%d%s" % (PORT, path),
        data=json.dumps(body).encode("utf-8"),
        headers=dict({"Content-Type": "application/json"}, **(headers or {})),
        method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read().decode("utf-8", "replace")[:200]
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:200]


def stop(p):
    if not p:
        return
    p.terminate()
    try:
        p.wait(timeout=8)
    except Exception:
        p.kill()


def has_injection(html):
    """精确判据：只看服务端注入的 <script>window.FH_API_TOKEN = "...";</script>。

    ⚠️ 不能用 `"FH_API_TOKEN" in html`——前端源码本来就有这个词
    （fetchWithTimeout 里的读取处与注释），那样永远为真。
    """
    return re.search(r'<script>window\.FH_API_TOKEN = "[^"]*";</script>', html) is not None


def main():
    print("=" * 72)
    print("形态 A：单机演示（不设任何令牌）")
    print("=" * 72)
    p = boot({}, "A")
    if not p:
        return 1
    html, hdr = get_home()
    has = has_injection(html)
    print("  首页大小 %d B ｜ 服务端注入令牌: %s" % (len(html), has))
    print("  （期望：False —— 单机形态页面不带令牌）")
    code, body = post("/api/roster", {"name": ""})
    print("  本机 POST /api/roster 空名 -> %s %s（期望 400 业务校验，说明免密放行）"
          % (code, body[:60]))
    print("  判定: %s" % ("通过" if (not has and code == 400) else "**失败**"))
    stop(p)

    print()
    print("=" * 72)
    print("形态 B：公网部署 + 前端令牌注入（FENGHUO_FRONTEND_TOKEN）")
    print("=" * 72)
    p = boot({"FENGHUO_API_TOKEN": TOKEN, "FENGHUO_FRONTEND_TOKEN": TOKEN}, "B")
    if not p:
        return 1
    html, hdr = get_home()
    m = re.search(r'window\.FH_API_TOKEN = ("[^"]*")', html)
    injected = json.loads(m.group(1)) if m else None
    print("  首页大小 %d B" % len(html))
    print("  注入的令牌 = %r" % injected)
    print("  （期望：等于配置值 %r）" % TOKEN)
    ok_html = (injected == TOKEN)
    # 本机请求仍免密（client.host 是 127.0.0.1）
    code, body = post("/api/roster", {"name": ""})
    print("  本机 POST /api/roster -> %s（本机仍免密，期望 400）" % code)
    # 带令牌的写操作（模拟公网带令牌）
    code2, body2 = post("/api/roster", {"name": ""},
                        {"Authorization": "Bearer " + TOKEN})
    print("  带令牌 POST -> %s（期望 400，说明令牌被接受）" % code2)
    print("  判定: %s" % ("通过" if ok_html else "**失败**"))
    stop(p)

    print()
    print("=" * 72)
    print("形态 C：公网部署 + 不注入前端令牌（默认安全形态）")
    print("=" * 72)
    p = boot({"FENGHUO_API_TOKEN": TOKEN}, "C")
    if not p:
        return 1
    html, hdr = get_home()
    has = has_injection(html)
    print("  首页大小 %d B ｜ 服务端注入令牌: %s" % (len(html), has))
    print("  （期望：False —— 未配 FENGHUO_FRONTEND_TOKEN 时不注入）")
    code, body = post("/api/roster", {"name": ""})
    print("  本机 POST /api/roster -> %s（本机免密，期望 400）" % code)
    print("  判定: %s" % ("通过" if (not has and code == 400) else "**失败**"))
    stop(p)

    print()
    print("=" * 72)
    print("全部完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())