"""安全边界回归验证 —— 逐条核对「涉密通道不出网」的实现是否真的生效。

为什么单独成文：安全承诺如果只写在文档里，改一次代码就可能悄悄失效，
而且**不会报错**。本脚本把每条承诺落成一条可执行的断言，改动后跑一遍即可。

覆盖的断言（15 条）：
  A1  网络守卫：守卫内建连抛 NetworkBlockedError，守卫后原函数被精确还原
  A2  网络守卫：可重入（嵌套），嵌套退出不提前卸载
  B1  跨域白名单：不含通配符 "*"
  B2  涉密密级：由路由自身固定为 classified
  B3  密级自检：正常配置下放行
  B4  密级自检：密级路由被改坏（classified→云端）时必须拒绝
  C1  涉密实例路由表：只含 /api/upload，不含任何公开查询/出网接口
  C2  独立部署开关：CLASSIFIED_STANDALONE=1 时主实例不再注册 /api/upload
  D1~D4 端到端：真实起两个实例，实测上传成功 + 涉密实例不暴露公开接口 + CORS 生效
  D5  端到端：把解析器换成"会联网的实现"，守卫必须拦住并如实报错
  E1  本地脱敏：嵌套结构逐字段脱敏，零泄漏 + 占位符唯一 + 可完整还原
  E2  本地脱敏：跑一次 /api/query 截获真实上云报文，验证已脱敏且返回值本地还原

用法：
    python scripts/verify_security.py
退出码：0 = 全部通过；1 = 有失败。
"""
import io
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
sys.path.insert(0, APP)

try:
    sys.stdout.reconfigure(line_buffering=True, encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

PASS, FAIL, SKIP = [], [], []


def check(name, ok, detail=""):
    """ok=None 表示跳过（环境不支持），既不算通过也不算失败，但会打出来。"""
    if ok is None:
        SKIP.append(name)
        print("  %-4s %s%s" % ("SKIP", name, ("  — " + detail) if detail else ""))
        return ok
    (PASS if ok else FAIL).append(name)
    print("  %-4s %s%s" % ("OK" if ok else "FAIL", name,
                           ("  — " + detail) if detail else ""))
    return ok


# ---------------------------------------------------------------- A. 网络守卫

def test_netguard():
    print("\n[A] 网络守卫 security/netguard.py")
    from security.netguard import no_network, NetworkBlockedError, is_active, blocked_count

    orig = socket.socket.connect

    # A1：守卫内拒绝、守卫后还原
    raised = False
    try:
        with no_network():
            inside = socket.socket.connect is not orig
            try:
                socket.socket.connect(socket.socket(), ("example.com", 80))
            except NetworkBlockedError:
                raised = True
    except Exception as e:  # noqa: BLE001
        check("A1 守卫内建连被拒 + 守卫后还原", False, "意外异常 %r" % e)
        return
    after = socket.socket.connect is orig
    check("A1 守卫内建连被拒 + 守卫后还原", raised and inside and after,
          "拦截=%s 替换=%s 还原=%s" % (raised, inside, after))

    # A2：可重入
    ok_nested = True
    try:
        with no_network():
            with no_network():
                pass
            ok_nested = is_active()          # 内层退出不应卸载
        ok_nested = ok_nested and (not is_active())
    except Exception as e:  # noqa: BLE001
        ok_nested = False
    check("A2 可重入（嵌套退出不提前卸载）", ok_nested)

    # A3：线程隔离 —— 守卫只约束进入守卫的线程，不能牵连并发的公开查询
    #     （历史缺陷：进程级开关 + 守卫套在 await 外层，上传大文件会掐断所有出网）
    import threading
    seen = {}

    def _other():
        s = socket.socket()
        s.settimeout(2)
        try:
            s.connect(("127.0.0.1", 9))      # 本机必然拒绝/超时，但**不应被守卫拦截**
            seen["r"] = "connected"
        except NetworkBlockedError:
            seen["r"] = "blocked"
        except OSError:
            seen["r"] = "allowed"
        finally:
            s.close()

    with no_network():
        th = threading.Thread(target=_other)
        th.start()
        th.join()
    check("A3 守卫不牵连其它线程（线程本地作用域）", seen.get("r") == "allowed",
          "其它线程结果=%s" % seen.get("r"))

    return blocked_count()


# ------------------------------------------------------- B. 配置与密级自检

def test_config():
    print("\n[B] 配置与密级自检 app/server.py")
    import server as srv

    # B1：CORS 白名单
    origins = None
    for mw in srv.app.user_middleware:
        if "CORSMiddleware" in str(mw.cls):
            origins = list(mw.kwargs.get("allow_origins") or [])
    ok = origins is not None and "*" not in origins and len(origins) > 0
    check("B1 跨域白名单不含通配符", ok, "allow_origins=%s" % origins)

    # B2：涉密密级由路由固定
    check("B2 涉密密级由路由自身固定",
          getattr(srv, "_UPLOAD_CHANNEL_LEVEL", None) == "classified",
          "_UPLOAD_CHANNEL_LEVEL=%r" % getattr(srv, "_UPLOAD_CHANNEL_LEVEL", None))

    # B3：正常配置放行
    ok3 = True
    try:
        srv._assert_classified_route()
    except Exception as e:  # noqa: BLE001
        ok3 = False
    check("B3 密级自检在正常配置下放行", ok3)

    # B4：路由被改坏必须拒绝（模拟有人把 classified 指向云端）
    import llm.client as lc
    orig_get = lc.get_llm_client
    try:
        lc.get_llm_client = lambda level: lc.CloudLLM()
        from fastapi import HTTPException
        blocked = False
        try:
            srv._assert_classified_route()
        except HTTPException:
            blocked = True
        except Exception:  # noqa: BLE001
            blocked = False
    finally:
        lc.get_llm_client = orig_get
    check("B4 密级被改指向云端时拒绝服务", blocked)


# ------------------------------------------------------ C. 涉密实例路由表

def test_classified_routes():
    print("\n[C] 涉密实例隔离 classified_server.py")
    import classified_server as cs

    paths = {getattr(r, "path", None) for r in cs.app.routes}
    has_upload = "/api/upload" in paths
    leaked = sorted(p for p in paths
                    if p and p not in ("/api/upload", "/openapi.json",
                                       "/docs", "/docs/oauth2-redirect", "/redoc"))
    check("C1 涉密实例只含 /api/upload", has_upload and not leaked,
          "额外路由=%s" % (leaked or "无"))

    # C2：CLASSIFIED_STANDALONE=1 → 主实例移除该路由
    code = (
        "import os;os.environ['CLASSIFIED_STANDALONE']='1';"
        "import server;"
        "print(sum(1 for r in server.app.routes if getattr(r,'path','')=='/api/upload'))"
    )
    p = subprocess.run([sys.executable, "-c", code], cwd=APP,
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    got = (p.stdout or "").strip().splitlines()
    ok = bool(got) and got[-1] == "0"
    check("C2 CLASSIFIED_STANDALONE=1 时主实例移除该路由", ok,
          "剩余条数=%s" % (got[-1] if got else p.stderr[:80]))


# ------------------------------------------------------------- D. 端到端

def _multipart(field, filename, blob):
    b = b"--BOUNDARY\r\n"
    b += ('Content-Disposition: form-data; name="%s"; filename="%s"\r\n'
          % (field, filename)).encode()
    b += b"Content-Type: application/octet-stream\r\n\r\n" + blob + b"\r\n"
    b += b"--BOUNDARY--\r\n"
    return b, "multipart/form-data; boundary=BOUNDARY"


def _post_upload(port, origin=None):
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["供应商名称", "XX测试科技有限公司"])
    ws.append(["资产负债率", "61.2%"])
    ws.append(["营业收入", "12000万元"])
    buf = io.BytesIO()
    wb.save(buf)
    body, ctype = _multipart("files", "_sec_verify.xlsx", buf.getvalue())
    req = urllib.request.Request("http://127.0.0.1:%d/api/upload" % port,
                                 data=body, method="POST")
    req.add_header("Content-Type", ctype)
    if origin:
        req.add_header("Origin", origin)
    # ⚠️ 返回原始 headers 对象而非 dict()：
    #    uvicorn 发出的响应头是小写（access-control-allow-origin），
    #    转成 dict 会丢掉大小写不敏感性，导致 .get("Access-Control-...") 恒为 None。
    #    email.message.Message 的 get() 本身不区分大小写，直接用即可。
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode("utf-8")), r.headers
    except urllib.error.HTTPError as e:
        return e.code, {}, e.headers


def _start(script, port, extra_env=None):
    env = dict(os.environ)
    env["CLASSIFIED_PORT"] = str(port)
    if extra_env:
        env.update(extra_env)
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | \
        getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    p = subprocess.Popen([sys.executable, script], cwd=APP, env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=flags)
    for _ in range(60):
        time.sleep(0.5)
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            return p
        except OSError:
            continue
    return p


def _kill(p):
    try:
        p.terminate()
    except Exception:  # noqa: BLE001
        pass
    time.sleep(1.0)
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)],
                       capture_output=True)
    except Exception:  # noqa: BLE001
        pass


def test_write_auth():
    """写接口鉴权：本机免密、跨主机必须令牌（未配置令牌时失败关闭）。

    F1 走真实 HTTP（本机够用）；F2/F3 直接测判定函数——Windows 内核会把
    127.0.0.2 的对端地址归一成 127.0.0.1，回环别名造不出"跨主机"，硬测只会测出假象。
    F5 验证处置动作确实记到了操作人（写临时状态文件，不污染演示数据）。
    """
    print("\n[F] 写接口鉴权 security/auth.py")
    token = "verify-only-token"

    # ---- F1：真实端到端，本机 DELETE 不存在的台账 → 免密放行 → 404 业务语义 ----
    p = _start("server.py", 8000, {"FENGHUO_API_TOKEN": token})
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:8000/api/roster/NOPE-DOES-NOT-EXIST", method="DELETE")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                code = r.status
        except urllib.error.HTTPError as e:
            code = e.code
        except Exception as e:  # noqa: BLE001
            code = "conn-error:%s" % type(e).__name__
        if isinstance(code, str):
            check("F1 本机写操作免密放行", False, code)
        else:
            check("F1 本机写操作免密放行（404=查无此条）", code == 404, "HTTP %s" % code)
    finally:
        _kill(p)

    # ---- F2/F3：判定函数（不依赖网络与端口）----
    from security import auth as auth_mod

    class _Req:
        def __init__(self, host, headers=None):
            class _C:
                pass
            self.client = _C()
            self.client.host = host
            self.headers = headers or {}

    old_env = os.environ.pop("FENGHUO_API_TOKEN", None)
    try:
        ok, status, detail, ident = auth_mod.authorize_write(_Req("192.168.1.20"))
        check("F2 未配置令牌时跨主机写被拒（fail-closed）",
              (not ok) and status == 403 and "FENGHUO_API_TOKEN" in detail,
              "ok=%s status=%s" % (ok, status))
    finally:
        if old_env is not None:
            os.environ["FENGHUO_API_TOKEN"] = old_env

    os.environ["FENGHUO_API_TOKEN"] = token
    try:
        ok_no, st_no, _, _ = auth_mod.authorize_write(_Req("192.168.1.20"))
        ok_wrong, st_wrong, _, _ = auth_mod.authorize_write(
            _Req("192.168.1.20", {"authorization": "Bearer not-the-token"}))
        ok_right, st_right, _, ident = auth_mod.authorize_write(
            _Req("192.168.1.20", {"authorization": "Bearer " + token,
                                  "x-operator": "张审核"}))
        check("F3 无令牌/错令牌被拒、正确令牌放行",
              (not ok_no) and st_no == 403 and (not ok_wrong) and st_wrong == 403
              and ok_right and st_right == 200,
              "无=%s 错=%s 对=%s" % (st_no, st_wrong, st_right))
        check("F4 跨主机操作人身份与留痕口径",
              ident.get("mode") == "token" and ident.get("operator") == "张审核",
              "identity=%r" % (ident,))
    finally:
        os.environ.pop("FENGHUO_API_TOKEN", None)

    # ---- F5：中间件真的调用了鉴权（接线检查）----
    src = open(os.path.join(APP, "server.py"), encoding="utf-8").read()
    check("F5 写请求在中间件里过鉴权（接线存在）",
          "authorize_write(" in src and "_touches_writes(" in src)

    # ---- F5b：拿不到客户端来源时不得按本机放行（fail-closed）----
    # 历史缺陷：client_ip 对 request.client=None 返回 ""，而 is_local_request 把 ""
    # 也算本机 → Unix socket / 部分 ASGI 适配器下，远程可无令牌调用写接口。
    class _NoClient:
        client = None
        headers = {}

    check("F5b 无客户端来源按远程处理（不放行）",
          auth_mod.is_local_request(_NoClient()) is False,
          "is_local=%r" % auth_mod.is_local_request(_NoClient()))

    # ---- F6：处置动作落盘带操作人 ----
    import tempfile
    from core import alerts as alerts_mod
    tmpdir = tempfile.mkdtemp(prefix="fenghuo_state_")
    old_path = alerts_mod.STATE_PATH
    alerts_mod.STATE_PATH = os.path.join(tmpdir, "alert_states.json")
    try:
        alerts_mod.update_state("demo-key", state="处置中", note="已联系供应商",
                                operator="张审核")
        data = alerts_mod.load_states()
        got = (data.get("demo-key") or {}).get("operator")
        check("F6 预警状态变更记录操作人", got == "张审核", "operator=%r" % got)
    finally:
        alerts_mod.STATE_PATH = old_path


def test_ssrf_guard():
    """自定义数据源出网守卫：保存前与取数时都要拦内网/元数据/非 http 协议。"""
    print("\n[G] 自定义数据源出网守卫 adapters/generic_adapter.py")
    sys.path.insert(0, APP)
    try:
        from adapters.generic_adapter import _assert_public_url
    except Exception as e:  # noqa: BLE001
        check("G1 出网守卫可导入", False, repr(e))
        return

    bad = ["file:///etc/passwd", "http://127.0.0.1:8000/api/scan",
           "http://169.254.169.254/latest/meta-data/", "http://10.0.0.7/",
           "gopher://example.com:70/"]
    rejected = []
    for u in bad:
        try:
            _assert_public_url(u)
        except ValueError:
            rejected.append(u)
    check("G1 内网/元数据/非 http 地址被拒", len(rejected) == len(bad),
          "拒绝 %d/%d" % (len(rejected), len(bad)))

    # 写入路径也要校验：源码里 add/update 两个入口必须出现守卫调用
    src = open(os.path.join(APP, "server.py"), encoding="utf-8").read()
    wired = src.count("_assert_public_url(")
    check("G2 保存自定义源前已挂守卫（≥2 处）", wired >= 2, "server.py 命中 %d 处" % wired)


def test_e2e():
    print("\n[D] 端到端（真实起服务，仅本机回环）")
    main_p = _start("server.py", 8000)
    cls_p = _start("classified_server.py", 8001)
    try:
        # D1：公开实例上传正常
        try:
            st, js, _ = _post_upload(8000)
            ok = st == 200 and js.get("channel") == "classified" and js.get("ok_count", 0) >= 1
            check("D1 公开实例的涉密上传可用", ok, "HTTP %s ok_count=%s" % (st, js.get("ok_count")))
        except Exception as e:  # noqa: BLE001
            check("D1 公开实例的涉密上传可用", False, repr(e))

        # D2：涉密实例上传正常
        try:
            st, js, _ = _post_upload(8001)
            ok = st == 200 and js.get("channel") == "classified"
            check("D2 涉密独立实例上传可用", ok, "HTTP %s" % st)
        except Exception as e:  # noqa: BLE001
            check("D2 涉密独立实例上传可用", False, repr(e))

        # D3：涉密实例不暴露公开查询接口
        try:
            req = urllib.request.Request(
                "http://127.0.0.1:8001/api/query", data=b"{}", method="POST")
            req.add_header("Content-Type", "application/json")
            urllib.request.urlopen(req, timeout=10)
            code = 200
        except urllib.error.HTTPError as e:
            code = e.code
        except Exception:  # noqa: BLE001
            code = 0
        check("D3 涉密实例不暴露 /api/query", code == 404, "HTTP %s" % code)

        # D4：CORS —— 白名单内返回 ACAO，白名单外不返回
        try:
            _, _, h_ok = _post_upload(8000, origin="http://127.0.0.1:8000")
            h_bad = _post_upload(8000, origin="http://evil.example.com")[2]
            allow_ok = h_ok.get("Access-Control-Allow-Origin") == "http://127.0.0.1:8000"
            allow_bad = "Access-Control-Allow-Origin" not in h_bad
            check("D4 CORS 仅对白名单来源放行", allow_ok and allow_bad,
                  "白名单内=%r 白名单外=%r" % (h_ok.get("Access-Control-Allow-Origin"),
                                             h_bad.get("Access-Control-Allow-Origin")))
        except Exception as e:  # noqa: BLE001
            check("D4 CORS 仅对白名单来源放行", False, repr(e))
    finally:
        _kill(main_p)
        _kill(cls_p)


def test_guard_in_path():
    """端到端取证：把解析器换成"会联网的实现"，守卫必须拦住并如实报错。

    这比"守卫自身能拦"更强：它证明守卫确实**套在涉密解析路径上**，
    而不是一个写好却没人调用的装饰。
    """
    print("\n[D5] 涉密解析路径确实启用了网络守卫")
    import asyncio
    from io import BytesIO
    from starlette.datastructures import UploadFile
    import server as srv

    def _evil_parser(path):          # 模拟"解析器被改成会出网"
        s = socket.socket()
        s.settimeout(2)
        s.connect(("example.com", 80))
        return None

    orig = srv.parse_supplier_file
    srv.parse_supplier_file = _evil_parser
    detail = ""
    ok = False
    try:
        uf = UploadFile(file=BytesIO(b"dummy"), filename="_guard_probe.xlsx")
        res = asyncio.run(srv.upload(files=[uf], supplier_id=""))
        item = (res.get("files") or [{}])[0]
        detail = "ok=%s error=%s" % (item.get("ok"), item.get("error"))
        ok = item.get("ok") is False and "安全守卫拦截" in (item.get("error") or "")
    except Exception as e:  # noqa: BLE001
        detail = repr(e)
    finally:
        srv.parse_supplier_file = orig
    check("D5 解析路径启用网络守卫（出网被拦并如实报错）", ok, detail)


def test_desensitize():
    """本地脱敏：证明「出网前脱敏、返回后还原」真的生效，而不是模块躺着没人调。"""
    print("\n[E] 本地脱敏 wire-up security/desensitize.py + server 掩码助手")
    import re
    import server as srv
    from core.schemas import SupplierProfile, NewsItem, FinancialMetrics
    from core import aggregator
    import llm.deepseek as ds

    # --- E1：嵌套结构逐字段脱敏；用「往返全等」当判据 ---
    # 为什么不是「各字段都在」就算过：曾因占位符撞名，还原时 A 的名字被写成 B 的，
    # 逐个 contains 检查照样通过。只有「还原后与原文逐字相同」才能抓住这类串味。
    payload = {
        "audit_opinion": "标准无保留意见",
        "note": "甲方科技有限公司与乙方银行股份有限公司签约，金额 5000万元，账号 6217000012345678",
        "nested": ["丙方集团有限公司", "另一家科技有限公司"],
    }
    origin_blob = json.dumps(payload, ensure_ascii=False)
    mapping = {}
    masked = srv._mask_strings(payload, mapping)
    flat = json.dumps(masked, ensure_ascii=False)
    secrets = ("甲方科技有限公司", "丙方集团有限公司", "另一家科技有限公司",
               "5000万元", "6217000012345678")
    leaked = [s for s in secrets if s in flat]
    tokens = set(re.findall(r"<M\d+_[A-Z]+>", flat))
    back = srv._restore_text(flat, mapping)
    check("E1 嵌套脱敏：零泄漏 + 占位符一一对应 + 往返全等",
          (not leaked) and len(tokens) == len(mapping) and back == origin_blob,
          "泄漏=%s 占位符=%d 映射=%d 往返相等=%s"
          % (leaked or "无", len(tokens), len(mapping), back == origin_blob))

    # --- E2：跑一次 /api/query，截获真正发给云端的报文 ---
    NAME = "深圳市长亮科技股份有限公司"
    SHORT = "长亮科技"          # 新闻里真正会出现的写法
    prof = SupplierProfile(
        name=NAME,
        news=[
            NewsItem(title=NAME + "被列为被执行人，涉案金额 1200万元",
                     sentiment="negative", severity="high"),
            # ⚠️ 这一条专门盯"简称漏出"：正则只认带后缀的全称，简称得靠字面遮蔽
            NewsItem(title=SHORT + "因合同纠纷被诉，账号 6217000012345678 被冻结",
                     sentiment="negative", severity="medium"),
        ],
        financials=FinancialMetrics(debt_ratio=61.2, revenue=1.2e9,
                                    audit_opinion="标准无保留意见"),
    )
    res = aggregator.MultiSourceResult(profile=prof, used=["fake"], failed={}, verified={})

    captured = {}

    def fake_chat(self, messages, temperature=0.1):
        prompt = "\n".join(m.get("content", "") for m in messages)
        captured["prompt"] = captured.get("prompt", "") + prompt
        if "事件抽取器" in prompt:
            ph = re.search(r"<M\d+_(?:COMPANY|ALIAS)>", prompt)
            amt = re.search(r"<M\d+_AMOUNT>", prompt)
            # 云端只看得见占位符，所以它返回的标题里也只会是占位符
            return json.dumps([{
                "title": "%s被列为被执行人" % (ph.group(0) if ph else "（未脱敏）"),
                "event_type": "legal", "severity": "high",
                "amount": amt.group(0) if amt else None,
                "occurred_at": None, "evidence": "生效法律文书",
            }], ensure_ascii=False)
        return "财务健康度一般，负债率偏高。"

    orig_fetch, orig_chat = srv._fetch_profile, ds.DeepseekClient._chat
    srv._fetch_profile = lambda *a, **k: res
    ds.DeepseekClient._chat = fake_chat
    try:
        srv.query(srv.QueryRequest(name=NAME))
    finally:
        srv._fetch_profile, ds.DeepseekClient._chat = orig_fetch, orig_chat

    prompt = captured.get("prompt", "")
    short_leak = SHORT in prompt
    sent_leak = (NAME in prompt) or (SHORT in prompt) or ("1200万元" in prompt) \
        or ("6217000012345678" in prompt)
    sent_masked = "<M" in prompt
    titles = [e.title for e in prof.legal_events if e.source == "LLM抽取"]
    restored_ok = any(NAME in t for t in titles)
    check("E2 上云报文已脱敏（含简称）且返回内容本地还原",
          (not sent_leak) and sent_masked and restored_ok,
          "全称泄漏=%s 简称泄漏=%s 含占位符=%s 还原后标题=%s"
          % (NAME in prompt, short_leak, sent_masked, titles[:1]))


def main():
    print("=" * 68)
    print("安全边界回归验证")
    print("=" * 68)
    test_netguard()
    test_config()
    test_classified_routes()
    test_e2e()
    test_guard_in_path()
    test_desensitize()
    test_write_auth()
    test_ssrf_guard()

    print("\n" + "-" * 68)
    extra = ("（跳过 %d 项）" % len(SKIP)) if SKIP else ""
    print("通过 %d / %d%s" % (len(PASS), len(PASS) + len(FAIL), extra))
    if FAIL:
        print("失败：%s" % "、".join(FAIL))
    print("结果：%s" % ("✅ 全部通过" if not FAIL else "❌ 有 %d 项失败" % len(FAIL)))
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
