"""企查查 MCP 客户端 —— 把官方 MCP Server 的真实风控数据接入本项目。

为什么需要这一层：
  企查查 MCP 是"流式 HTTP + Bearer Token"协议，不是普通 REST API。
  本模块封装了 MCP 握手（initialize）/ 工具调用（tools/call）/
  SSE 响应解析，让上层（适配器）像调普通函数一样取到真实数据。

设计要点：
  - 纯标准库实现（urllib + json），零第三方依赖，与项目风格一致
  - 10 个 Server 按需路由：company / risk / ipr / operation / history
    / executive / legal-regulation / legal-case / tender
  - 失败全部优雅降级（返回 None），绝不阻断主流程
  - Token 从环境变量 QCC_API_TOKEN 读取（见 app/.env）

对应本项目五大风险维度的真实数据映射：
  legal       ← qcc-risk: dishonest / judgment_debtor / judicial_documents
                / administrative_penalty / business_exception / serious_violation
  operating   ← qcc-company: registration / change_records
  financial   ← qcc-company: financial_data
  reputation  ← qcc-risk: risk_scan（35 项因子汇总）+ 负面舆情
  concentration ← qcc-company: shareholder / external_investments
"""
from core.quiet_log import log_swallow as _log_swallow
import json
import os
import threading
import urllib.request
import urllib.error

from integrations import qcc_cache

QCC_MCP_BASE = "https://agent.qcc.com/mcp"
# Server 名 → MCP endpoint 路径
SERVERS = {
    "company": "company",
    "risk": "risk",
    "ipr": "ipr",
    "operation": "operation",
    "history": "history",
    "executive": "executive",
    "legal-regulation": "regulation",
    "legal-case": "case",
    "tender": "tender",
}

_TIMEOUT = int(os.environ.get("QCC_TIMEOUT", "30"))

# MCP 握手复用：同一 server 的 initialize 只需做一次（省一次 HTTP 往返与潜在额度）
_SHARED = {}
_SHARED_LOCK = threading.Lock()

# 失败熔断：遇到"额度/鉴权"这类**不会因为重试而好转**的错误时，
# 在冷却期内直接短路，避免批量扫描时反复发起无效请求（既费时也显失专业）。
_BREAKER = {"until": 0.0, "reason": ""}
_BREAKER_LOCK = threading.Lock()
_BREAKER_COOLDOWN = int(os.environ.get("QCC_BREAKER_COOLDOWN", "300"))  # 默认 5 分钟


def _fatal_reason(err) -> str:
    """判断错误是否为"重试也无用"的致命错误（额度/鉴权），返回可读原因或空串。"""
    s = str(err)
    for code in ("credit_balance_insufficient", "quota_exceeded", "unauthorized", "forbidden"):
        if code in s:
            return _BIZ_ERR[code]
    if "积分余额不足" in s:
        return _BIZ_ERR["credit_balance_insufficient"]
    return ""


def breaker_state() -> dict:
    """当前熔断状态（供界面/自检展示）。"""
    import time as _t
    with _BREAKER_LOCK:
        left = max(0, int(_BREAKER["until"] - _t.time()))
        return {"open": left > 0, "cooldown_left": left, "reason": _BREAKER["reason"]}


def reset_breaker():
    """手动解除熔断（充值后立即恢复用）。"""
    with _BREAKER_LOCK:
        _BREAKER["until"] = 0.0
        _BREAKER["reason"] = ""


def _first(v):
    """取列表首项（企查查部分字段返回数组，如法定代表人名称）。"""
    if isinstance(v, list):
        return v[0] if v else None
    return v


class QCCError(Exception):
    """企查查 MCP 调用异常。"""


# 业务错误码 → 用户可读原因（用于前端提示，避免"查无此企业"式误导）
_BIZ_ERR = {
    "credit_balance_insufficient": "企查查 MCP 积分余额不足，请前往 https://agent.qcc.com/ 充值后重试",
    "quota_exceeded": "企查查 MCP 调用额度已用尽",
    "unauthorized": "企查查 Token 无效或已过期，请检查 app/.env 中的 QCC_API_TOKEN",
    "forbidden": "企查查 Token 无权访问该接口",
}


def explain_error(err) -> str:
    """把 QCCError 的原始信息翻译成一句人话。

    识别常见的配额/鉴权错误，其余原样返回。用于让"数据源失败"在界面上
    可见且有可操作建议，而不是被静默吞掉。
    """
    s = str(err)
    for code, msg in _BIZ_ERR.items():
        if code in s:
            return msg
    if "积分余额不足" in s or "充值" in s:
        return _BIZ_ERR["credit_balance_insufficient"]
    if "网络错误" in s:
        return "企查查接口网络不可达（请检查网络/代理）"
    return s[:200]


def _token() -> str:
    t = (os.environ.get("QCC_API_TOKEN") or "").strip()
    if t.lower().startswith("bearer "):
        t = t[7:].strip()
    # 占位符（your_qcc_api_token）视作未配置，否则会拿假凭据真调接口 → 401
    from core.credential import real_secret
    return real_secret(t)


def is_configured() -> bool:
    """是否已配置企查查 Token。"""
    return bool(_token())


class QCCClient:
    """企查查 MCP 客户端：一次握手，多次调用。"""

    def __init__(self, server: str = "company", token: str = None):
        if server not in SERVERS:
            raise QCCError(f"未知 server: {server}，可选: {list(SERVERS)}")
        self.server = server
        self.url = f"{QCC_MCP_BASE}/{SERVERS[server]}/stream"
        self.token = token or _token()
        self._inited = False
        self._req_id = 0
        # 复用同 server 已完成的握手（省一次往返；并发安全）
        with _SHARED_LOCK:
            shared = _SHARED.get(self.server)
            if shared:
                self._inited = shared.get("inited", False)
                self._req_id = shared.get("req_id", 0)

    # ---------- MCP 协议底层 ----------
    def _post(self, payload: dict) -> dict:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.url, data=data, method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "Authorization": "Bearer " + self.token,
                "User-Agent": "WorkBuddy-QCC-Client/1.0",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
                body = resp.read().decode("utf-8", "ignore")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "ignore")[:200]
            raise QCCError(f"HTTP {e.code}: {detail}")
        except Exception as e:
            raise QCCError(f"网络错误: {e}")

        # 响应可能是 SSE（data: {...}）或纯 JSON
        if "data:" in body:
            for line in body.splitlines():
                line = line.strip()
                if line.startswith("data:"):
                    try:
                        return json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        _log_swallow("integrations/qcc_mcp.py:180")
                        continue
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            raise QCCError(f"响应解析失败: {body[:200]}")

    def _call(self, method: str, params: dict = None) -> dict:
        self._req_id += 1
        with _SHARED_LOCK:
            _SHARED.setdefault(self.server, {})["req_id"] = self._req_id
        return self._post({
            "jsonrpc": "2.0", "id": self._req_id,
            "method": method, "params": params or {},
        })

    def init(self):
        """MCP 握手（幂等，跨实例复用）。"""
        if self._inited:
            return
        with _SHARED_LOCK:
            shared = _SHARED.get(self.server)
            if shared and shared.get("inited"):
                self._inited = True
                return
        self._call("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "workbuddy-qcc-client", "version": "1.0"},
        })
        self._inited = True
        with _SHARED_LOCK:
            _SHARED.setdefault(self.server, {})["inited"] = True

    def list_tools(self):
        """列出本 server 全部工具。"""
        self.init()
        res = self._call("tools/list")
        return (res.get("result") or {}).get("tools") or []

    def call_tool(self, name: str, arguments: dict = None, refresh: bool = False):
        """调用工具并返回结构化数据（自动解包 MCP content 文本）。

        ★ 积分优化：命中缓存直接返回，不再发请求。
          工商/年报类缓存 7 天、风险类 12 小时、实体锚定 30 天。
          传 refresh=True 可跳过缓存强制拉取（"刷新此公司"场景）。
        """
        args = arguments or {}
        if not refresh:
            cached = qcc_cache.get(self.server, name, args)
            if cached is not None:
                return cached

        # 熔断：额度/鉴权类错误在冷却期内直接短路，不再发起无效请求
        st = breaker_state()
        if st["open"]:
            raise QCCError(f"{st['reason']}（已暂停企查查调用 {st['cooldown_left']}s，避免无效请求）")

        try:
            self.init()
            res = self._call("tools/call", {"name": name, "arguments": args})
            # 业务错误（含额度/鉴权）也必须在 try 内判定，才能走同一熔断路径
            if "error" in res:
                raise QCCError(f"{name} 调用失败: {res['error']}")
        except QCCError as e:
            reason = _fatal_reason(e)
            if reason:
                import time as _t
                with _BREAKER_LOCK:
                    _BREAKER["until"] = _t.time() + _BREAKER_COOLDOWN
                    _BREAKER["reason"] = reason
            raise

        result = res.get("result") or {}
        # MCP 标准：result.content = [{"type":"text","text":"..."}]
        content = result.get("content")
        out = result
        if content:
            texts = []
            for c in content:
                if isinstance(c, dict) and c.get("type") == "text":
                    texts.append(c.get("text", ""))
            joined = "\n".join(texts)
            # 尝试解析为 JSON，失败则原样返回文本
            try:
                out = json.loads(joined)
            except (json.JSONDecodeError, TypeError):
                out = joined

        # 只缓存非空结果（失败/空内容不写，避免空结果被固化整个 TTL）
        if out:
            qcc_cache.put(self.server, name, args, out)
        return out

    # ---------- 本项目专用的高层封装 ----------
    def search_candidates(self, keyword: str, refresh: bool = False):
        """实体检索：返回 (匹配结果, 候选列表)。

        匹配结果 ∈ {"唯一精确匹配", "多候选", "未匹配"}
        候选列表 = [{"name":..., "credit_code":..., "established":..., "status":...}]

        注意：调用本身失败（网络/配额/鉴权）时**向上抛出 QCCError**，
        不再伪装成"未匹配"。否则上层会把"欠费"误判为"查无此企业"，
        静默降级到单源，导致风险漏报。
        """
        data = self.call_tool("get_company_by_query", {"searchKey": keyword}, refresh=refresh)
        if isinstance(data, str):
            return "未匹配", []

        status = data.get("匹配结果") or ""
        info = data.get("企业信息")

        # 唯一精确匹配：企业信息是 dict
        if isinstance(info, dict):
            name = info.get("企业名称")
            if name:
                return "唯一精确匹配", [{
                    "name": name,
                    "credit_code": info.get("统一社会信用代码"),
                    "established": info.get("成立日期"),
                    "status": info.get("状态"),
                    "legal_person": _first(info.get("法定代表人名称")),
                }]
            return "未匹配", []

        # 多候选：企业信息是 list
        if isinstance(info, list):
            cands = []
            for it in info:
                if not isinstance(it, dict):
                    continue
                nm = it.get("企业名称")
                if not nm:
                    continue
                cands.append({
                    "name": nm,
                    "credit_code": it.get("统一社会信用代码"),
                    "established": it.get("成立日期"),
                    "status": it.get("状态"),
                    "legal_person": _first(it.get("法定代表人名称")),
                })
            return ("多候选" if cands else "未匹配"), cands

        return "未匹配", []

    def resolve_company(self, keyword: str):
        """实体锚定：返回 (企业名称, 统一社会信用代码) 或 None。

        仅"唯一精确匹配"时自动返回；多候选返回 None（由上层展示候选让用户选定）。
        """
        status, cands = self.search_candidates(keyword)
        if status == "唯一精确匹配" and cands:
            return cands[0]["name"], cands[0]["credit_code"]
        return None

    def registration(self, search_key: str, refresh: bool = False):
        """工商登记信息。"""
        return self._safe("get_company_registration_info", {"searchKey": search_key}, refresh=refresh)

    def financials(self, search_key: str, refresh: bool = False):
        """核心财务数据（资产负债/利润/成长/比率）。"""
        return self._safe("get_financial_data", {"searchKey": search_key}, refresh=refresh)

    def change_records(self, search_key: str, refresh: bool = False):
        """工商变更记录（用于"变更频繁"风险信号）。"""
        return self._safe("get_change_records", {"searchKey": search_key}, refresh=refresh)

    def _safe(self, tool: str, args: dict, refresh: bool = False):
        """安全调用：失败返回 None，不抛异常。"""
        try:
            return self.call_tool(tool, args, refresh=refresh)
        except Exception:
            return None

    def call_tool_ex(self, tool: str, args: dict = None, refresh: bool = False):
        """★ 区分「调用失败」与「确实为空」的安全调用。

        返回 `(ok, data, err)`：
          · ok=True  → data 为工具返回值（可能是空 dict/list，那是**确实为空**）
          · ok=False → data 为 None，err 为失败原因（网络/鉴权/额度/超时…）

        ⚠️ 为什么必须有这个：`_safe()` 把所有异常吞成 None，调用方无法区分
        「这家公司确实没有股东记录」与「股东接口调用失败了」。
        后者若被当成前者，该维度会以「零扣分」计入评分 → **分数虚高、风险漏报**。
        """
        try:
            return True, self.call_tool(tool, args or {}, refresh=refresh), None
        except Exception as e:
            return False, None, "%s: %s: %s" % (tool, type(e).__name__, str(e)[:200])

    # 公开的安全调用别名（供适配器使用）
    def call_tool_safe(self, tool: str, args: dict = None, refresh: bool = False):
        """安全调用工具：失败返回 None，不抛异常。"""
        return self._safe(tool, args or {}, refresh=refresh)

    def shareholder_info(self, search_key: str, refresh: bool = False):
        """股东信息（集中度维度）。"""
        return self._safe("get_shareholder_info", {"searchKey": search_key}, refresh=refresh)

    def external_investments(self, search_key: str, refresh: bool = False):
        """对外投资（关联方识别）。"""
        return self._safe("get_external_investments", {"searchKey": search_key}, refresh=refresh)

    def actual_controller(self, search_key: str, refresh: bool = False):
        """实际控制人。"""
        return self._safe("get_actual_controller", {"searchKey": search_key}, refresh=refresh)

    def branches(self, search_key: str, refresh: bool = False):
        """分支机构。"""
        return self._safe("get_branches", {"searchKey": search_key}, refresh=refresh)


class QCCRiskClient(QCCClient):
    """风控大脑（qcc-risk，38 个工具）——本项目的法律风险数据核心。"""

    def __init__(self, token: str = None):
        super().__init__("risk", token=token)

    def risk_scan(self, search_key: str, refresh: bool = False):
        """一次扫描 35 项风险因子（分诊整体风险面）。★核心★"""
        return self._safe("get_company_risk_scan", {"searchKey": search_key}, refresh=refresh)

    def dishonest(self, search_key: str):
        """失信被执行人。"""
        return self._safe("get_dishonest_info", {"searchKey": search_key})

    def judgment_debtor(self, search_key: str):
        """被执行人（红线信号）。"""
        return self._safe("get_judgment_debtor_info", {"searchKey": search_key})

    def administrative_penalty(self, search_key: str):
        """行政处罚。"""
        return self._safe("get_administrative_penalty", {"searchKey": search_key})

    def business_exception(self, search_key: str):
        """经营异常名录。"""
        return self._safe("get_business_exception", {"searchKey": search_key})

    def serious_violation(self, search_key: str):
        """严重违法失信名单。"""
        return self._safe("get_serious_violation", {"searchKey": search_key})

    def judicial_documents(self, search_key: str, page_size: int = 10):
        """裁判文书（诉讼记录）。"""
        return self._safe("get_judicial_documents",
                          {"searchKey": search_key, "pageSize": page_size})

    def equity_freeze(self, search_key: str):
        """股权冻结。"""
        return self._safe("get_equity_freeze", {"searchKey": search_key})

    def tax_arrears(self, search_key: str):
        """欠税公告。"""
        return self._safe("get_tax_arrears_notice", {"searchKey": search_key})

    def collect_legal_risks(self, search_key: str) -> dict:
        """汇总采集全部法律风险维度数据，返回 {维度: 数据}。"""
        return {
            "dishonest": self.dishonest(search_key),
            "judgment_debtor": self.judgment_debtor(search_key),
            "administrative_penalty": self.administrative_penalty(search_key),
            "business_exception": self.business_exception(search_key),
            "serious_violation": self.serious_violation(search_key),
            "judicial_documents": self.judicial_documents(search_key),
            "equity_freeze": self.equity_freeze(search_key),
            "tax_arrears": self.tax_arrears(search_key),
        }


class QCCTenderClient(QCCClient):
    """招投标（标讯）客户端 —— 外包过往记录的数据基础。

    对应《办法》：
      第18条 服务提供商选择与招标评审
      第29条 服务评价结果作为后续准入参考
      第30条(五) 服务水平下降 / 第28条 关联外包

    用于给"外包专项"提供真实的履约履历：
    - 该供应商给哪些机构做过外包、金额多少、什么方式中标
    - 客户是否高度集中在金融行业、是否存在单一来源偏多
    - 中标频次与规模的时间趋势（反映其外包业务健康度）
    """

    def __init__(self, token: str = None):
        super().__init__("tender", token=token)

    def search_company(self, keyword: str):
        """按企业名检索，返回企业ID与标讯画像（中标数/投标数/招采数）。"""
        return self._safe("search_companies", {"keywords": [keyword]})

    def company_tenders(self, enterprise_id: str, role: str = None,
                        info_types: list = None, keywords: list = None,
                        page_size: int = 20, page: int = 1):
        """检索某企业的标讯记录。

        role: 1=招采方 3=中标方 4=代理方 8=投标方 6=被提及
        info_types: ["招标公告", "中标公告"]
        """
        args = {"id": enterprise_id, "pageSize": page_size, "page": page}
        if role:
            args["role"] = role
        if info_types:
            args["infoTypes"] = info_types
        if keywords:
            args["keywords"] = keywords
        return self._safe("search_company_tenders", args)

    def tender_detail(self, tender_id: str):
        """标讯详情。"""
        return self._safe("get_tender_detail", {"id": tender_id})


# ---------- 便捷函数 ----------
def quick_risk_probe(company_name: str) -> dict:
    """一行拿到某公司的核心风险数据（用于快速验证/演示）。"""
    out = {"company": company_name, "token_ok": is_configured()}
    if not is_configured():
        out["error"] = "未配置 QCC_API_TOKEN"
        return out
    try:
        c = QCCClient("company")
        resolved = c.resolve_company(company_name)
        out["resolved"] = resolved
        if resolved:
            key = resolved[1] or resolved[0]
            r = QCCRiskClient()
            out["risk_scan"] = r.risk_scan(key)
            out["dishonest"] = r.dishonest(key)
            out["judgment_debtor"] = r.judgment_debtor(key)
    except Exception as e:
        out["error"] = str(e)
    return out


if __name__ == "__main__":
    import sys
    name = sys.argv[1] if len(sys.argv) > 1 else "北京科蓝软件系统股份有限公司"
    print(json.dumps(quick_risk_probe(name), ensure_ascii=False, indent=2)[:3000])
