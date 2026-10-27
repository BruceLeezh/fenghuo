"""万得 Wind MCP 客户端 —— 接入 Wind 金融数据（A股行情/财务基本面/行业对标）。

协议要点
--------
1. **Endpoint**：`https://mcp.wind.com.cn/vserver_<service>_data/mcp/`
   service ∈ stock / fund / index / bond / financial_docs / economic / analytics
2. **鉴权**：`Authorization: Bearer <ak_xxx>` —— **要加 Bearer 前缀**（与企查查相同，与 iFinD 相反）
3. **会话**：标准 MCP JSON-RPC 2.0；initialize 响应头可能返回 Mcp-Session-Id（复用，若无则不依赖）
4. **日期格式**：`yyyyMMdd`（不含短横线）；**windcode**：如 `600519.SH` / `000001.SZ` / `AAPL.O`

额度优化
--------
- 与企查查/iFinD 共用 `qcc_cache` 缓存层（key 带 `wind-<service>` 前缀）
- 财务/档案低频变化缓存久，行情短 —— 重复查询不重复消耗额度
"""
from core.quiet_log import log_swallow as _log_swallow
import json
import os
import ssl
import threading
import urllib.error
import urllib.request

from integrations import qcc_cache

WIND_MCP_BASE = os.environ.get(
    "WIND_MCP_BASE", "https://mcp.wind.com.cn/vserver_{service}_data/mcp/")

# 服务域 → 端点（service 名直接拼进 vserver_<service>_data）
SERVERS = {
    "stock": WIND_MCP_BASE.format(service="stock"),
    "fund": WIND_MCP_BASE.format(service="fund"),
    "index": WIND_MCP_BASE.format(service="index"),
    "bond": WIND_MCP_BASE.format(service="bond"),
    "financial_docs": WIND_MCP_BASE.format(service="financial_docs"),
    "economic": WIND_MCP_BASE.format(service="economic"),
    "analytics": WIND_MCP_BASE.format(service="analytics"),
}

_TIMEOUT = int(os.environ.get("WIND_TIMEOUT", "60"))

_SESSIONS = {}
_REQ_IDS = {}
_LOCK = threading.Lock()

# TLS 校验：默认**开启**（2026-10-12 实测 mcp.wind.com.cn:443 证书链完整，
# 由 GlobalSign RSA OV SSL CA 2018 签发，可正常校验）。
# 若个别环境下确因证书链问题连不上，可临时置 WIND_INSECURE_TLS=1 放宽（会打警告）。
_SSL_CTX = ssl.create_default_context()
if os.environ.get("WIND_INSECURE_TLS") == "1":
    _SSL_CTX.check_hostname = False
    _SSL_CTX.verify_mode = ssl.CERT_NONE
    print("[warn] WIND_INSECURE_TLS=1：已关闭 Wind TLS 证书校验，仅应在排障时临时使用。")


class WindError(Exception):
    """Wind MCP 调用异常。"""


_BIZ_ERR = {
    "401": "Wind 密钥无效或已过期，请在 https://aifinmarket.wind.com.cn 重新获取",
    "403": "Wind 密钥无权访问该服务（可能缺少对应权益）",
    "429": "Wind 请求过于频繁，请稍后重试",
}


def explain_error(err) -> str:
    s = str(err)
    for code, msg in _BIZ_ERR.items():
        if f"HTTP {code}" in s or code in s[:20]:
            return msg
    for kw in ("密钥", "未授权", "unauthorized", "invalid token", "Bearer"):
        if kw.lower() in s.lower():
            return _BIZ_ERR["401"]
    if "网络" in s or "timed out" in s.lower():
        return "Wind 接口网络不可达（mcp.wind.com.cn）"
    return s[:200]


def _key() -> str:
    from core.credential import real_secret
    return real_secret((os.environ.get("WIND_API_KEY")
                        or os.environ.get("WIND_MCP_KEY") or "").strip())


def is_configured() -> bool:
    return bool(_key())


def _next_id(t: str) -> int:
    with _LOCK:
        _REQ_IDS[t] = _REQ_IDS.get(t, 0) + 1
        return _REQ_IDS[t]


def _parse_response(body: str):
    if "data:" in body:
        for line in body.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                chunk = line[5:].strip()
                try:
                    return json.loads(chunk)
                except json.JSONDecodeError:
                    _log_swallow("integrations/wind_mcp.py:102")
                    continue
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        raise WindError(f"响应解析失败: {body[:200]}")


class WindClient:
    """Wind MCP 客户端：initialize 后复用会话，tools/call 取数。"""

    def __init__(self, service: str = "stock", key: str = None):
        if service not in SERVERS:
            raise WindError(f"未知 service: {service}，可选: {list(SERVERS)}")
        self.service = service
        self.url = SERVERS[service]
        self.key = (key or _key()).strip()

    def _headers(self) -> dict:
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": "Bearer " + self.key,   # ★ Wind 加 Bearer 前缀
        }
        with _LOCK:
            sid = _SESSIONS.get(self.service)
        if isinstance(sid, str) and sid:
            h["Mcp-Session-Id"] = sid
        return h

    def _post(self, payload: dict, timeout: int = None, capture_session: bool = False):
        if not self.key:
            raise WindError("未配置 Wind 密钥（WIND_API_KEY）")
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self.url, data=data, method="POST", headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=timeout or _TIMEOUT,
                                        context=_SSL_CTX) as resp:
                body = resp.read().decode("utf-8", "ignore")
                if capture_session:
                    sid = resp.headers.get("Mcp-Session-Id")
                    if sid:
                        with _LOCK:
                            _SESSIONS[self.service] = sid
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "ignore")[:200]
            raise WindError(f"HTTP {e.code}: {detail}")
        except Exception as e:
            raise WindError(f"网络错误: {e}")
        return _parse_response(body) if body.strip() else {}

    def _ensure_session(self):
        # Wind MCP 标记为 stateless，但仍需 initialize 握手（不返回 Mcp-Session-Id）。
        # ★ 关键：不能发 notifications/initialized 通知 —— stateless 不认识，会返回"无效的请求"，
        #   并导致后续 tools/list、tools/call 全部被拒。
        with _LOCK:
            if self.service in _SESSIONS:
                return
        self._post({
            "jsonrpc": "2.0", "id": _next_id(self.service), "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                       "clientInfo": {"name": "workbuddy-wind-client", "version": "1.0"}},
        }, timeout=30, capture_session=True)
        with _LOCK:
            _SESSIONS[self.service] = True   # 仅标记"已握手"，无真实 session id

    def list_tools(self) -> list:
        """列出该服务当前密钥可用的工具（不消耗取数额度）。"""
        self._ensure_session()
        res = self._post({"jsonrpc": "2.0", "id": _next_id(self.service),
                          "method": "tools/list", "params": {}})
        tools = (((res or {}).get("result") or {}).get("tools")) or []
        return [t.get("name") for t in tools if isinstance(t, dict) and t.get("name")]

    def call_tool(self, tool_name: str, params: dict = None, timeout: int = None,
                  refresh: bool = False):
        """调用工具并返回已解包数据（复用 qcc_cache 缓存层）。"""
        args = params or {}
        if not refresh:
            cached = qcc_cache.get(f"wind-{self.service}", tool_name, args)
            if cached is not None:
                return cached

        self._ensure_session()
        res = self._post({
            "jsonrpc": "2.0", "id": _next_id(self.service), "method": "tools/call",
            "params": {"name": tool_name, "arguments": args},
        }, timeout=timeout)

        if isinstance(res, dict) and "error" in res:
            err = res["error"]
            msg = err.get("message") if isinstance(err, dict) else str(err)
            raise WindError(f"{tool_name} 调用失败: {msg}")

        result = (res or {}).get("result") or {}
        content = result.get("content")
        out = result
        if isinstance(content, list):
            texts = [c.get("text", "") for c in content
                     if isinstance(c, dict) and c.get("type") == "text"]
            joined = "\n".join(texts)
            if joined.strip():
                try:
                    out = json.loads(joined)
                except (json.JSONDecodeError, TypeError):
                    out = joined

        if out is not None:
            qcc_cache.put(f"wind-{self.service}", tool_name, args, out)
        return out
