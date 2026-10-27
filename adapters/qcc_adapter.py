"""企查查实时适配器 —— 通过 MCP 拉取真实工商/财务/司法风险数据。

这是本项目"公开公司查询"的**真实数据源**，与 public_adapter 互补：
  - qcc_adapter：输入任意公司名 → 真实企查查数据（工商+财务+司法风险+股东）
  - public_adapter：本地预采集库 + 东方财富免费实时源（离线可用）

切换方式：`get_adapter("qcc")`。上层评级引擎零改动。

数据映射（企查查 → 统一 schema）：
  工商登记      → SupplierProfile.registration
  年报财务      → FinancialMetrics
  35 项风险扫描 → LegalEvent 列表（被执行人/裁判文书/行政处罚/经营异常/欠税…）
  股东信息      → Shareholder 列表（集中度维度）
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.schemas import (
    SupplierProfile, LegalEvent, NewsItem, FinancialMetrics,
    Shareholder, SourceInfo,
)
from core.adapter import DataSourceAdapter, register
from integrations.qcc_mcp import QCCClient, QCCRiskClient, is_configured, QCCError, explain_error


class MultiCandidateError(ValueError):
    """命中多个企业主体，需用户指定更精确的名称。"""

    def __init__(self, message: str, candidates: list = None):
        super().__init__(message)
        self.candidates = candidates or []


def _looks_like_credit_code(s: str) -> bool:
    """判断是否为统一社会信用代码（18 位，大写字母+数字）。"""
    return bool(re.fullmatch(r"[0-9A-HJ-NPQRTUWXY]{18}", (s or "").strip().upper()))


# 风险因子 → (统一事件标题, 严重度, 监管条款依据, 归属维度)
# 顺序即优先级，越靠前越严重
RISK_FACTOR_MAP = {
    "失信信息": ("被列入失信被执行人", "high", "办法第16条(准入)/第23条/第30条(风险识别)", "legal"),
    "被执行人": ("被列为被执行人", "high", "办法第23条(持续监控)/第30条(风险识别)", "legal"),
    "严重违法": ("被列入严重违法失信名单", "high", "办法第16条(准入)/第26条(提供商财务与内控监控)", "legal"),
    "经营异常": ("被列入经营异常名录", "high", "办法第16条(准入)/第26条(提供商财务与内控监控)", "operating"),
    "行政处罚": ("受到行政处罚", "medium", "办法第17条(尽职调查)/第30条(合规风险)", "legal"),
    "裁判文书": ("涉诉裁判文书", "medium", "办法第30条(风险识别)", "legal"),
    "立案信息": ("法院立案（未结案）", "medium", "办法第30条(风险识别)", "legal"),
    "开庭公告": ("开庭公告", "low", "办法第30条(风险识别)", "legal"),
    "股权冻结": ("股权被司法冻结", "high", "办法第26条(提供商财务与内控监控)", "legal"),
    "欠税公告": ("欠税公告", "medium", "办法第26条(提供商财务与内控监控)", "operating"),
    "税收违法": ("税收违法", "high", "办法第26条(提供商财务与内控监控)", "operating"),
    "税务非正常": ("税务非正常户", "high", "办法第26条(提供商财务与内控监控)", "operating"),
    "破产重整": ("破产重整", "high", "办法第17条(尽调)/第26条(提供商财务与内控监控)", "operating"),
    "终本案件": ("终结本次执行程序", "medium", "办法第30条(风险识别)", "legal"),
    "限制高消费": ("限制高消费", "medium", "办法第30条(风险识别)", "legal"),
    "司法拍卖": ("资产被司法拍卖", "medium", "办法第26条(提供商财务与内控监控)", "operating"),
    "股权出质": ("股权出质", "low", "办法第33条(集中度风险)", "concentration"),
    "股权质押": ("股权质押", "low", "办法第33条(集中度风险)", "concentration"),
    "动产抵押": ("动产抵押", "low", "办法第33条(集中度风险)", "concentration"),
    "土地抵押": ("土地抵押", "low", "办法第33条(集中度风险)", "concentration"),
    "清算信息": ("进入清算程序", "high", "办法第17条(尽调)/第26条(提供商财务与内控监控)", "operating"),
    "注销备案": ("注销备案", "high", "办法第16条(准入)/第26条(提供商财务与内控监控)", "operating"),
    "简易注销": ("简易注销", "high", "办法第26条(提供商财务与内控监控)", "operating"),
}

# 企查查数据源元信息
QCC_SOURCE = SourceInfo(
    key="qcc", label="企查查 MCP", category="工商/司法/财务",
    authoritative=True, url="https://www.qcc.com",
)

# 子工具调用失败时，**受影响（需标 na）的维度**。
#
# ⚠️ 判定依据是「哪些**评分规则**会读这个工具产出的数据」，而不是「这个工具的事件标了哪个维度」：
#   · get_company_risk_scan 产出的是 `legal_events`，消费它的规则落在两个维度 ——
#       法律维度 5 条全部；经营维度 2 条（经营异常名录 30 分、工商变更频繁 12 分）。
#     扫描失败若只标 legal，经营维度就会以 100 分（零扣分）计入 → 照样虚高。
#     （注意：RISK_FACTOR_MAP 里另有几项标了 concentration，如股权出质/质押，
#       但集中度的 3 条规则读的是 important_outsourcing / cross_border / non_resident
#       标记，**并不消费 legal_events**，故不计入。）
#   · get_shareholder_info 产出股权结构，供集中度维度的股权集中度部分使用。
_TOOL_FAILED_DIMS = {
    "get_company_risk_scan": ["legal", "operating"],
    "get_shareholder_info": ["concentration"],
}


@register("qcc")
class QCComAdapter(DataSourceAdapter):
    """企查查实时数据源（真实工商 + 财务 + 司法风险 + 股东）。"""

    def __init__(self, with_risk: bool = True, with_shareholder: bool = True):
        self.with_risk = with_risk
        self.with_shareholder = with_shareholder
        self._company = None
        self._risk = None

    def is_available(self) -> bool:
        """未配置 Token（或仍是占位符）时不可用。

        基类默认 `True`；若这里不覆盖，数据源清单会把企查查显示成"可用"，
        而实际一调就抛"未配置企查查 Token" —— 界面上就是"已启用但每次都失败"。
        """
        return is_configured()

    # ---------- 统一入口 ----------
    def fetch_supplier(self, name: str, refresh: bool = False) -> SupplierProfile:
        """获取供应商档案。

        name 支持三种形态：
          - 公司名称（唯一匹配时自动锚定）
          - 统一社会信用代码（强制锚定该主体，用于多候选后用户选定）
          - 已选定的候选项 —— 由 `fetch_supplier_by_key` 直接调用

        refresh=False（默认）时优先读本地缓存，命中即不再消耗企查查积分；
        refresh=True 强制重新拉取（"刷新这家公司"场景）。
        """
        if not is_configured():
            raise ValueError(
                "未配置企查查 Token。请在 app/.env 设置 QCC_API_TOKEN，"
                "或改用公开查询（本地预采集库）。"
            )

        self._company = QCCClient("company")
        # 1) 实体锚定
        #    若传入的本身就是统一社会信用代码（18 位），直接锚定，跳过候选判断
        if _looks_like_credit_code(name):
            return self._fetch_by_key(name, refresh=refresh)

        # 检索失败（配额/鉴权/网络）→ 原样抛出，让聚合层记入 failed 并在界面显示，
        # 绝不伪装成"未匹配"（否则会静默降级为单源，造成风险漏报）
        try:
            status, cands = self._company.search_candidates(name, refresh=refresh)
        except QCCError as e:
            raise ValueError(f"企查查数据源不可用：{explain_error(e)}") from e

        if status == "多候选":
            # 单一候选也视为"可自动锚定"，避免用户被一个选项卡住
            if len(cands) == 1:
                return self._fetch_by_key(cands[0].get("credit_code") or cands[0]["name"],
                                          fallback_name=cands[0]["name"], refresh=refresh)
            raise MultiCandidateError(
                f"「{name}」命中多个主体，请选择目标主体。",
                candidates=cands,
            )
        if status == "未匹配" or not cands:
            raise ValueError(f"企查查未找到公司「{name}」，请检查名称或改用全称。")
        return self._fetch_by_key(cands[0].get("credit_code") or cands[0]["name"],
                                  fallback_name=cands[0]["name"], refresh=refresh)

    def fetch_supplier_by_key(self, key: str, fallback_name: str = "",
                              refresh: bool = False) -> SupplierProfile:
        """按统一社会信用代码（或精确名称）直接锚定并抓取档案。

        用于前端多候选弹窗中用户选定某个主体后，绕过模糊检索直取该主体。
        """
        if not is_configured():
            raise ValueError("未配置企查查 Token。")
        self._company = QCCClient("company")
        return self._fetch_by_key(key, fallback_name=fallback_name, refresh=refresh)

    def _fetch_by_key(self, key: str, fallback_name: str = "",
                      refresh: bool = False) -> SupplierProfile:
        """核心抓取流程：以 key（信用代码或精确名称）为锚点拉取全部数据。

        每个工具调用都走缓存层（见 integrations/qcc_cache.py）：
        工商/年报类缓存 7 天、风险扫描 12 小时 —— 重复查询同一公司不再重复扣积分。
        """
        real_name = fallback_name or key

        # 2) 工商登记
        #    ⚠️ 必须区分「调用失败」与「确实为空」：注册信息失败时若仍产出
        #       {credit_code: None, ...} 这样的真值字典，`dim_has_data("operating")`
        #       会判为"有数据"，经营维度以零扣分拿 100 分 —— 风险被静默放过。
        reg_ok, reg_raw, reg_err = self._company.call_tool_ex(
            "get_company_registration_info", {"searchKey": key}, refresh=refresh)
        registration = self._parse_registration(reg_raw) if reg_ok else {}

        # 3) 年报财务
        fin_ok, fin_raw, fin_err = self._company.call_tool_ex(
            "get_financial_data", {"searchKey": key}, refresh=refresh)
        financials = self._parse_financials(fin_raw) if fin_ok else None
        # 4) 股东信息（集中度维度）
        #    ⚠️ 必须用 call_tool_ex 区分「接口调用失败」与「确实无股东记录」：
        #       前者登记为维度不可用（评分层标 na），后者才是真的空。
        shareholders = []
        unavailable_dims = []
        fetch_errors = []
        if not reg_ok:
            unavailable_dims.append("operating")
            fetch_errors.append(f"get_company_registration_info: {reg_err}")
        if not fin_ok:
            unavailable_dims.append("financial")
            fetch_errors.append(f"get_financial_data: {fin_err}")
        if self.with_shareholder:
            ok, raw_sh, err = self._company.call_tool_ex(
                "get_shareholder_info", {"searchKey": key}, refresh=refresh)
            if ok:
                shareholders = self._parse_shareholders(raw_sh)
            else:
                unavailable_dims.extend(_TOOL_FAILED_DIMS["get_shareholder_info"])
                fetch_errors.append(err)

        # 5) 司法风险（可选，耗时较长）
        legal_events = []
        if self.with_risk:
            self._risk = QCCRiskClient()
            legal_events, risk_ok = self._collect_risk_events(key, refresh=refresh)
            if not risk_ok:
                unavailable_dims.extend(_TOOL_FAILED_DIMS["get_company_risk_scan"])
                fetch_errors.append("get_company_risk_scan: 调用失败")

        # 去重（同一维度可能被多个失败工具重复登记）
        unavailable_dims = list(dict.fromkeys(unavailable_dims))

        profile = SupplierProfile(
            name=real_name,
            # ⚠️ 不能硬编码 True：企查查只提供工商/财务/司法事实，**不提供**
            #    「本行是否把它评定为重要外包」——那是我方台账的判断（第13条）。
            #    早期实现一律置 True，导致每次实时查询都被打上"重要外包"标签并扣 10 分，
            #    属凭空捏造风险口径。未知即按 False（不扣分），由台账/文件提供时再置真。
            important_outsourcing=False,
            legal_events=legal_events,
            news=[],
            financials=financials,
            shareholders=shareholders,
            registration=registration,
            sources=["qcc"],
            source_meta={"qcc": QCC_SOURCE},
            unavailable_dims=unavailable_dims,
            # 企查查不返回「重要/非驻场/跨境」三个外包标记，集中度维度无标记依据；
            # 显式声明未提供，避免把"未提供"当成"明确为否"而给出 100 分。
            conc_flags_provided=False,
        )
        if fetch_errors:
            # 在工商字段里留痕，便于界面/报告显示"本次有子项未取到"
            profile.registration = profile.registration or {}
            profile.registration["_partial"] = True
            profile.registration["_fetch_errors"] = fetch_errors
        self._attach_alerts(profile)
        return profile

    # ---------- 解析 ----------
    def _parse_registration(self, raw: dict) -> dict:
        """把企查查工商数据映射成统一字段。

        ⚠️ 空输入必须返回空 dict：返回一份「所有字段都是 None + _data_source」的字典
        会被 `dim_has_data("operating")` 判为"有数据"，经营维度以 100 分计入综合分。
        """
        if not isinstance(raw, dict) or not raw:
            return {}
        reg = {
            "credit_code": raw.get("统一社会信用代码"),
            "legal_person": raw.get("法定代表人"),
            "registered_capital": raw.get("注册资本"),
            "paid_capital": raw.get("实缴资本"),
            "established": raw.get("成立日期"),
            "status": raw.get("登记状态"),
            "company_type": raw.get("企业类型"),
            "industry": raw.get("国标行业"),
            "region": raw.get("所属地区"),
            "address": raw.get("注册地址"),
            "staff_scale": raw.get("人员规模"),
            "insured_count": raw.get("参保人数"),
            "business_scope": raw.get("经营范围"),
        }
        # 一个实际字段都没取到 → 视为无工商数据（不要靠 _data_source 撑成真值）
        if all(v in (None, "", []) for v in reg.values()):
            return {}
        reg["_data_source"] = "企查查MCP(实时)"
        return reg

    def _parse_financials(self, raw) -> FinancialMetrics:
        """把企查查年报数据转成统一 FinancialMetrics（取最新报告期）。"""
        if not isinstance(raw, dict):
            return None
        periods = raw.get("财务数据信息") or []
        if not periods:
            return None
        p = periods[0]  # 已按报告期倒序
        detail = (p.get("指标详情") or {})
        analysis = detail.get("分析数据") or {}
        profit = (analysis.get("盈利能力") or {})
        repay = (analysis.get("偿还能力") or {})
        growth = (analysis.get("成长能力") or {})
        stmt = (detail.get("财务报表") or {})
        cash = (stmt.get("现金流量表") or {})
        income = (stmt.get("利润表") or {})
        balance = (stmt.get("资产负债表") or {})

        def f(v):
            try:
                return float(str(v).replace(",", "").replace("%", "").strip())
            except (TypeError, ValueError):
                return None

        return FinancialMetrics(
            debt_ratio=f(repay.get("资产负债率")),
            current_ratio=f(repay.get("流动比率")),
            quick_ratio=f(repay.get("速动比率")),
            net_margin=f(profit.get("净利率")),
            gross_margin=f(profit.get("毛利率")),
            roe=f(profit.get("加权净资产收益率")),
            roa=f(profit.get("总资产收益率")),
            operating_cashflow=f(cash.get("经营活动产生的现金流")),
            net_profit=f(income.get("净利润")),
            revenue=f(income.get("营业收入")),
            total_assets=f(balance.get("资产总额")),
            revenue_yoy=f(growth.get("营业收入同比")),
            profit_yoy=f(growth.get("净利润同比")),
            report_period=p.get("报告期"),
        )

    def _parse_shareholders(self, raw) -> list:
        """解析股东信息（不同返回结构做兼容）。"""
        if not isinstance(raw, dict):
            return []
        items = (raw.get("股东信息") or raw.get("股东") or
                 raw.get("data") or raw.get("list") or [])
        if isinstance(items, dict):
            items = [items]
        out = []
        for it in items[:10]:
            if not isinstance(it, dict):
                continue
            nm = (it.get("股东名称") or it.get("股东") or it.get("名称") or
                  it.get("shareholderName"))
            if not nm:
                continue
            def g(*keys):
                for k in keys:
                    if it.get(k) not in (None, ""):
                        return it.get(k)
                return None
            ratio_raw = g("持股比例", "出资比例", "比例", "shareholdingRatio")
            ratio = None
            if ratio_raw is not None:
                try:
                    ratio = float(str(ratio_raw).replace("%", "").replace(",", "").strip())
                except (TypeError, ValueError):
                    ratio = None
            out.append(Shareholder(
                name=str(nm),
                ratio=ratio,
                amount=g("认缴出资额", "认缴金额", "出资额"),
                type=g("股东类型", "类型") or "",
            ))
        return out

    def _collect_risk_events(self, search_key: str, refresh: bool = False):
        """调一次 35 项风险扫描，把有记录的因子转成 LegalEvent。

        返回 `(events, ok)`：ok=False 表示**风险扫描调用失败**（网络/额度/超时），
        与「扫到了但一家风险都没有」是两回事 —— 前者必须登记为维度不可用，
        否则会把"没取到"当成"没风险"，导致法律维度拿满分、总分虚高。
        """
        events = []
        ok, scan, _err = self._risk.call_tool_ex(
            "get_company_risk_scan", {"searchKey": search_key}, refresh=refresh)
        if not ok or not isinstance(scan, dict):
            return events, False

        factors = scan.get("风险因子扫描") or []
        for fac in factors:
            factor_name = fac.get("风险因子")
            count = fac.get("条目数") or 0
            if count <= 0:
                continue
            mapping = RISK_FACTOR_MAP.get(factor_name)
            if not mapping:
                continue
            title, severity, clause, dim = mapping
            events.append(LegalEvent(
                title=f"{title}（{count}条）",
                severity=severity,
                amount=None,
                occurred_at=None,
                source="企查查风控大脑",
                evidence=f"{factor_name}：命中 {count} 条记录。监管依据：{clause}",
                dimension=dim,
                count=int(count),
            ))
        # 按严重度排序：high > medium > low
        order = {"high": 0, "medium": 1, "low": 2}
        events.sort(key=lambda e: order.get(e.severity, 3))
        return events, True

    # ---------- 综合风险提示 ----------
    def _attach_alerts(self, profile: SupplierProfile):
        """根据采集到的数据生成"人话版"综合风险提示。"""
        alerts = []
        for e in profile.legal_events:
            if e.severity == "high":
                alerts.append({
                    "level": "high",
                    "title": e.title,
                    "detail": e.evidence or "",
                })
        f = profile.financials
        if f:
            if f.audit_opinion and "标准无保留" not in str(f.audit_opinion):
                alerts.append({
                    "level": "high",
                    "title": "审计意见非标准",
                    "detail": f"审计意见为「{f.audit_opinion}」，财务报表存在重大不确定性。",
                })
            if f.net_margin is not None and f.net_margin < 0:
                alerts.append({
                    "level": "medium",
                    "title": "主营业务亏损",
                    "detail": f"销售净利率 {f.net_margin:.2f}%，公司处于亏损状态。",
                })
            if f.debt_ratio is not None and f.debt_ratio > 70:
                alerts.append({
                    "level": "medium",
                    "title": "资产负债率偏高",
                    "detail": f"资产负债率 {f.debt_ratio:.2f}%，高于 70% 警戒线，偿债压力较大。",
                })
            if f.operating_cashflow is not None and f.operating_cashflow < 0:
                alerts.append({
                    "level": "medium",
                    "title": "经营现金流为负",
                    "detail": f"经营活动现金流 {f.operating_cashflow:,.0f} 元，自身造血能力不足。",
                })
        profile.risk_alerts = alerts[:8]
