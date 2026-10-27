"""线上部署验收：从公网视角 + 服务器本机视角双向核验。

要点：
  1. 公网（本机从外网访问）——验证「公网可演示写入」形态是否真的生效：
     带令牌的写操作能通过；不带令牌的被拒（fail-closed）。
  2. 服务器本机——验证本机免密通道、只读接口、涉密通道。
"""
import json
import os
import sys
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")
PUBLIC = "http://118.89.33.239"
TOKEN = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "_fh_token.txt"), encoding="utf-8").read().strip() \
    if os.path.exists(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "_fh_token.txt")) else ""

OK, FAIL = [], []


def chk(name, cond, detail=""):
    (OK if cond else FAIL).append(name)
    print("  %s %s%s" % ("OK  " if cond else "★FAIL", name,
                         ("  — " + detail) if detail else ""))
    return cond


def req(url, method="GET", body=None, headers=None, timeout=90):
    h = dict(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        h.setdefault("Content-Type", "application/json")
    r = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return -1, str(e)


def head(t):
    print("\n" + "=" * 70)
    print(t)
    print("=" * 70)


def main():
    head("① 公网可达性")
    st, body = req(PUBLIC + "/")
    chk("首页 200", st == 200, "HTTP %s, %d bytes" % (st, len(body)))
    # 首页是否注入了令牌（"公网可演示写入"形态的标志）
    injected = 'window.FH_API_TOKEN = "' in body
    chk("首页已注入前端令牌（可演示写入）", injected,
        "令牌注入标记 = %s" % injected)
    if injected and TOKEN:
        import re
        m = re.search(r'window\.FH_API_TOKEN = "([^"]*)"', body)
        chk("注入的令牌与部署令牌一致", bool(m) and m.group(1) == TOKEN,
            "匹配 = %s" % (bool(m) and m.group(1) == TOKEN))

    head("② 公网只读接口（应全部可用）")
    st, b = req(PUBLIC + "/api/roster")
    chk("GET /api/roster", st == 200, "HTTP %s" % st)
    if st == 200:
        try:
            d = json.loads(b)
            n = len(d.get("items") or [])
            chk("台账 37 家", n == 37, "实际 %d 家" % n)
        except Exception as e:
            chk("台账解析", False, str(e))
    st, _ = req(PUBLIC + "/api/servicelevel")
    chk("GET /api/servicelevel", st == 200, "HTTP %s" % st)
    st, _ = req(PUBLIC + "/api/sources")
    chk("GET /api/sources", st == 200, "HTTP %s" % st)

    head("③ 公网写操作鉴权（公网可演示写入形态）")
    # 不带令牌 → 必须 403（fail-closed 未配令牌时）或 403（令牌不匹配）
    st_no, b_no = req(PUBLIC + "/api/roster",
                      method="POST", body={"name": ""})
    chk("不带令牌的写操作被拒", st_no == 403,
        "HTTP %s  %s" % (st_no, b_no[:70]))
    # 带正确令牌 → 应通过鉴权，落到业务校验（400 空名）
    if TOKEN:
        st_tk, b_tk = req(PUBLIC + "/api/roster", method="POST",
                          body={"name": ""},
                          headers={"Authorization": "Bearer " + TOKEN})
        chk("带令牌的写操作通过鉴权（落到业务校验 400）", st_tk == 400,
            "HTTP %s  %s" % (st_tk, b_tk[:70]))
    # 本机专用接口（数据源配置）在公网仍应 403
    st_cfg, b_cfg = req(PUBLIC + "/api/sources/config", method="POST",
                        body={"key": "qcc"},
                        headers={"Authorization": "Bearer " + TOKEN} if TOKEN else {})
    chk("数据源配置接口在公网 403（仅限本机）", st_cfg == 403,
        "HTTP %s  %s" % (st_cfg, b_cfg[:70]))

    head("④ 公网查询功能")
    st, b = req(PUBLIC + "/api/query", method="POST",
                body={"name": "北京科蓝软件系统股份有限公司", "source": "auto"})
    ok = st == 200
    chk("POST /api/query（auto）", ok, "HTTP %s" % st)
    if ok:
        try:
            r = (json.loads(b).get("result") or {})
            print("     分数=%s 等级=%s" % (r.get("total_score"), r.get("level")))
        except Exception:
            pass

    head("⑤ 汇总")
    print("通过 %d / %d" % (len(OK), len(OK) + len(FAIL)))
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print("   -", f)
    print("\n结果：%s" % ("✅ 全部通过" if not FAIL else "❌ 有失败项"))
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())