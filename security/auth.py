"""写接口鉴权与操作人识别。

设计取舍
--------
一套要在行内落地的系统不能"谁连上端口谁就能删台账"，但决赛现场又必须零配置
可演示（评委自己点前端、14 个 UI 冒烟脚本也全是本机直连）。因此采用**按来源分
级**的策略，而不是一刀切登录：

* 本机（127.0.0.1 / ::1）——单机演示形态，免密，操作人记为「本机操作员」
* 跨主机（局域网/反代/公网）——写操作必须带 `Authorization: Bearer <FENGHUO_API_TOKEN>`；
  未配置 token 时**拒绝**（fail-closed），不允许"没配就等于全开"

读接口不鉴权（演示要看数据），敏感写操作才拦；被拦的操作不产生副作用。
操作人身份会随处置动作落盘（预警状态、工单推进/备注），用于满足《办法》对
处置留痕的要求。前端可用 `X-Operator` 头声明当前操作人（缺省「值班员」）。
"""
import os

__all__ = ["client_ip", "is_local_request", "identity", "authorize_write"]

TOKEN_ENV = "FENGHUO_API_TOKEN"
OPERATOR_HEADER = "x-operator"
DEFAULT_TOKENS = ("", " ", "changeme")


def client_ip(request) -> str:
    """取客户端 IP。转发头只在明确开启 TRUST_PROXY=1 时才认（默认关闭）。

    否则直连的客户端自己发一个 `X-Real-IP: 127.0.0.1` 就能冒充本机。
    """
    if os.environ.get("TRUST_PROXY", "").strip().lower() in ("1", "true", "yes"):
        rip = (request.headers.get("x-real-ip") or "").strip()
        if rip:
            return rip
        xff = (request.headers.get("x-forwarded-for") or "").strip()
        if xff:
            return xff.split(",")[-1].strip()
    return request.client.host if request.client else ""


# 回环地址白名单。`::ffff:127.0.0.1` 是双栈监听下的 IPv4 映射形式，同样是本机。
# ⚠️ 这里**不能**把空串放进来：`request.client` 为 None（Unix socket、部分 ASGI
#    适配器/代理）时 client_ip 返回 ""，若把 "" 视为本机，等于让"拿不到来源"的
#    请请求直接获得写权限 —— 安全判断必须 fail-closed。
_LOOPBACK = ("127.0.0.1", "::1", "::ffff:127.0.0.1")


def is_local_request(request) -> bool:
    return client_ip(request) in _LOOPBACK


def identity(request) -> dict:
    """当前请求的操作人。mode=local|token，operator 用于落盘留痕。"""
    operator = (request.headers.get(OPERATOR_HEADER) or "").strip()[:40]
    local = is_local_request(request)
    if local:
        return {"mode": "local", "operator": operator or "本机操作员"}
    return {"mode": "token", "operator": operator or "远程令牌调用"}


def authorize_write(request) -> tuple:
    """判断一次写操作是否放行。返回 (ok, status_code, detail, identity)。"""
    ident = identity(request)
    if ident["mode"] == "local":
        return True, 200, "", ident
    token = os.environ.get(TOKEN_ENV, "").strip()
    if token in DEFAULT_TOKENS:
        return False, 403, ("跨主机写操作已禁用：服务端未配置 %s。"
                            "请在本机操作，或由管理员设置该环境变量后重试。" % TOKEN_ENV), ident
    auth = (request.headers.get("authorization") or "").strip()
    provided = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if provided and _same(provided, token):
        return True, 200, "", ident
    return False, 403, "写操作需要携带 Authorization: Bearer <令牌>。", ident


def _same(a: str, b: str) -> bool:
    """定长比较，避免按字节提前返回泄露前缀信息。"""
    import hmac
    return hmac.compare_digest(a.encode("utf-8", "ignore"), b.encode("utf-8", "ignore"))
