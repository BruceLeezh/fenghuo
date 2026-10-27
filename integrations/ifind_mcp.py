"""同花顺 iFinD MCP 客户端 —— 接入 iFinD 金融数据服务（行情/财务/公告/宏观）。

协议要点（与企查查不同，容易踩坑）
----------------------------------
1. **Endpoint**：`https://api-mcp.51ifind.com:8643/ds-mcp-servers/<service>-mcp`
   service ∈ stock / fund / edb / news / bond / global_stock / index / future
2. **鉴权**：`Authorization: <MCP_KEY>` —— **不加 Bearer 前缀**（与企查查相反）
3. **会话**：initialize 的**响应头**返回 `Mcp-Session-Id`，后续请求必须带上它
   并发送 `notifications/initialized` 通知；否则服务端会拒绝
4. **证书**：服务端证书链不完整，需 `verify=False`（与官方 skill 的 call.py 一致）
5. **并发**：免费用户 2 QPS、个人版 5、企业版 10 —— 本项目顺序调用，不并发打

数据映射（本项目用途）
----------------------
    market      ← stock.stock_highfreq_quotes（最新价/涨跌幅/成交额）
    financials  ← stock.get_stock_financials（资产负债率/净利率/ROE/现金流）
    news        ← news.search_news（负面/风险资讯）
"""
from core.quiet_log import log_swallow as _log_swallow
import json
import os
import ssl
import threading
import urllib.error
import urllib.request

from integrations import qcc_cache

IFIND_MCP_BASE = os.environ.get(
    "IFIND_MCP_BASE", "https://api-mcp.51ifind.com:8643/ds-mcp-servers")

SERVERS = {
    "stock": f"{IFIND_MCP_BASE}/hexin-ifind-ds-stock-mcp",
    "fund": f"{IFIND_MCP_BASE}/hexin-ifind-ds-fund-mcp",
    "edb": f"{IFIND_MCP_BASE}/hexin-ifind-ds-edb-mcp",
    "news": f"{IFIND_MCP_BASE}/hexin-ifind-ds-news-mcp",
    "bond": f"{IFIND_MCP_BASE}/hexin-ifind-ds-bond-mcp",
    "global_stock": f"{IFIND_MCP_BASE}/hexin-ifind-ds-global-stock-mcp",
    "index": f"{IFIND_MCP_BASE}/hexin-ifind-ds-index-mcp",
    "future": f"{IFIND_MCP_BASE}/hexin-ifind-ds-futures-mcp",
}

_TIMEOUT = int(os.environ.get("IFIND_TIMEOUT", "60"))

# 会话与请求计数（按服务维度复用；线程安全）
_SESSIONS = {}
_REQ_IDS = {}
_LOCK = threading.Lock()

# TLS 校验：默认**开启**（2026-10-12 实测 api-mcp.51ifind.com:8643 证书链完整，
# 由 DigiCert GeoTrust G2 签发，可正常校验）。早期注释称"证书链不完整"已过时。
# 若个别环境下确因证书链问题连不上，可临时置 IFIND_INSECURE_TLS=1 放宽（会打警告）。
_SSL_CTX = ssl.create_default_context()
if os.environ.get("IFIND_INSECURE_TLS") == "1":
    _SSL_CTX.check_hostname = False
    _SSL_CTX.verify_mode = ssl.CERT_NONE
    print("[warn] IFIND_INSECURE_TLS=1：已关闭 iFinD TLS 证书校验，仅应在排障时临时使用。")


class IfindError(Exception):
    """iFinD MCP 调用异常。"""


_BIZ_ERR = {
    "401": "iFinD 密钥无效或已过期，请在 https://mcp.51ifind.com/#/profile 重新获取",
    "403": "iFinD 密钥无权访问该服务（可能缺少对应权益）",
    "429": "iFinD 请求过于频繁（免费版 2 次/秒），请稍后重试",
}


def explain_error(err) -> str:
    """把异常翻译成一句人话，供界面展示。"""
    s = str(err)
    for code, msg in _BIZ_ERR.items():
        if f"HTTP {code}" in s or code in s[:20]:
            return msg
    for kw in ("密钥", "未授权", "unauthorized", "invalid token"):
        if kw.lower() in s.lower():
            return _BIZ_ERR["401"]
    if "网络" in s or "timed out" in s.lower():
        return "iFinD 接口网络不可达（api-mcp.51ifind.com:8643）"
    return s[:200]


def _key() -> str:
    """读取 iFinD MCP 密钥（环境变量 IFIND_MCP_KEY）。占位符视作未配置。"""
    from core.credential import real_secret
    return real_secret((os.environ.get("IFIND_MCP_KEY") or "").strip())


def is_configured() -> bool:
    return bool(_key())


def _next_id(t: str) -> int:
    with _LOCK:
        _REQ_IDS[t] = _REQ_IDS.get(t, 0) + 1
        return _REQ_IDS[t]


def _parse_response(body: str):
    """响应可能是 SSE（data: {...}）或纯 JSON —— 两种都要能解析。"""
    if "data:" in body:
        for line in body.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                chunk = line[5:].strip()
                try:
                    return json.loads(chunk)
                except json.JSONDecodeError:
                    _log_swallow("integrations/ifind_mcp.py:109")
                    continue
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        raise IfindError(f"响应解析失败: {body[:200]}")


class IfindClient:
    """iFinD MCP 客户端：一次 initialize 拿 Mcp-Session-Id，之后复用会话。"""

    def __init__(self, service: str = "stock", key: str = None):
        if service not in SERVERS:
            raise IfindError(f"未知 service: {service}，可选: {list(SERVERS)}")
        self.service = service
        self.url = SERVERS[service]
        self.key = (key or _key()).strip()

    # ---------- 底层 ----------
    def _headers(self) -> dict:
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": self.key,   # ★ iFinD 不加 Bearer 前缀
        }
        with _LOCK:
            sid = _SESSIONS.get(self.service)
        if sid:
            h["Mcp-Session-Id"] = sid
        return h

    def _post(self, payload: dict, timeout: int = None, capture_session: bool = False):
        if not self.key:
            raise IfindError("未配置 iFinD MCP 密钥（IFIND_MCP_KEY）")
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self.url, data=data, method="POST",
                                     headers=self._headers())
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
            raise IfindError(f"HTTP {e.code}: {detail}")
        except Exception as e:
            raise IfindError(f"网络错误: {e}")
        return _parse_response(body) if body.strip() else {}

    def _ensure_session(self):
        with _LOCK:
            if self.service in _SESSIONS:
                return
        self._post({
            "jsonrpc": "2.0", "id": _next_id(self.service), "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                       "clientInfo": {"name": "workbuddy-ifind-client", "version": "1.0"}},
        }, timeout=30, capture_session=True)
        # 必须补发 initialized 通知，否则后续调用会被拒
        try:
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, timeout=15)
        except Exception:
            _log_swallow("integrations/ifind_mcp.py:174")

    # ---------- 对外 ----------
    def list_tools(self) -> list:
        """列出该服务当前密钥可用的工具（不消耗取数额度）。"""
        self._ensure_session()
        res = self._post({"jsonrpc": "2.0", "id": _next_id(self.service),
                          "method": "tools/list", "params": {}})
        tools = (((res or {}).get("result") or {}).get("tools")) or []
        return [t.get("name") for t in tools if isinstance(t, dict) and t.get("name")]

    def call_tool(self, tool_name: str, params: dict = None, timeout: int = None,
                  refresh: bool = False):
        """调用工具并返回已解包数据。

        ★ 额度优化：与企查查共用 `qcc_cache` 缓存层（key 带 ifind-<service> 前缀）。
          实时行情缓存 30 分钟、财务 7 天、资讯 24 小时 —— 重复查询不重复消耗取数额度。
          传 refresh=True 可跳过缓存强制拉取。
        """
        args = params or {}
        if not refresh:
            cached = qcc_cache.get(f"ifind-{self.service}", tool_name, args)
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
            raise IfindError(f"{tool_name} 调用失败: {msg}")

        result = (res or {}).get("result") or {}
        # MCP 标准：result.content = [{"type":"text","text":"..."}]
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
            qcc_cache.put(f"ifind-{self.service}", tool_name, args, out)
        return out

    # ---------- 便捷封装 ----------
    def quotes(self, symbols: str, indicators: str = "最新价,涨跌幅,换手率,市净率,总市值",
               data_mode: str = "real_time", refresh: bool = False):
        """实时行情快照（A股/基金/债券/指数）。"""
        return self.call_tool("stock_highfreq_quotes", {
            "symbols": symbols, "indicators": indicators, "data_mode": data_mode,
        }, refresh=refresh)

    def financials(self, query: str, refresh: bool = False):
        """财务数据与指标（自然语言问句）。"""
        return self.call_tool("get_stock_financials", {"query": query}, refresh=refresh)

    def summary(self, query: str, refresh: bool = False):
        """股票信息摘要。"""
        return self.call_tool("get_stock_summary", {"query": query}, refresh=refresh)

    def news(self, query: str, size: int = 5, time_start: str = "", time_end: str = "",
             refresh: bool = False):
        """新闻资讯语义检索（time_start / time_end 为服务端必填）。"""
        from datetime import date, timedelta
        p = {
            "query": query, "size": size,
            "time_start": time_start or (date.today() - timedelta(days=365)).isoformat(),
            "time_end": time_end or date.today().isoformat(),
        }
        return self.call_tool("search_news", p, refresh=refresh)

    def notices(self, query: str, size: int = 5, time_start: str = "", time_end: str = "",
                refresh: bool = False):
        """上市公司公告语义检索。"""
        from datetime import date, timedelta
        p = {
            "query": query, "size": size,
            "time_start": time_start or (date.today() - timedelta(days=365)).isoformat(),
            "time_end": time_end or date.today().isoformat(),
        }
        return self.call_tool("search_notice", p, refresh=refresh)


def quick_probe(company: str = "恒生电子") -> dict:
    """一行探测 iFinD 连通性与可用工具（调试用，消耗少量额度）。"""
    out = {"company": company, "key_configured": is_configured()}
    if not is_configured():
        out["error"] = "未配置 IFIND_MCP_KEY"
        return out
    try:
        c = IfindClient("stock")
        out["tools"] = c.list_tools()[:12]
        out["quotes"] = c.quotes(company)
    except Exception as e:
        out["error"] = explain_error(e)
    return out


if __name__ == "__main__":
    import sys
    print(json.dumps(quick_probe(sys.argv[1] if len(sys.argv) > 1 else "恒生电子"),
                     ensure_ascii=False, indent=2)[:2500])
