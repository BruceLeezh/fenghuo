"""本地预采集库适配器 —— 离线可用的银行 IT 外包公司档案。

定位
----
覆盖银行 IT 外包典型上市公司（data/public/*.json），数据来自公开工商信息
与公开年报，秒级响应、离线可用。行情部分复用东方财富（用本地库内置的
股票代码直查，规避东财 search 只认简称的问题）。

从 public_adapter 拆分而来（2026-09）：本地库与东方财富解耦，各自独立可开关。
"""
from core.quiet_log import log_swallow as _log_swallow
import json
import os
import re

from core.schemas import (
    SupplierProfile, LegalEvent, NewsItem, FinancialMetrics,
    SourceInfo,
)
from core.adapter import DataSourceAdapter, register

PUBLIC_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "public")

LOCAL_SOURCE = SourceInfo(
    key="local", label="本地预采集库", category="工商/财务/司法",
    authoritative=True, url="",
)


@register("local")
class LocalAdapter(DataSourceAdapter):
    """本地预采集库数据源：工商 + 年报财务 + 司法 + 舆情（离线）。"""

    def __init__(self):
        self._cache = None

    def is_available(self) -> bool:
        return True

    def source_info(self) -> dict:
        return {"label": "本地预采集库", "category": "工商/财务/司法",
                "authoritative": True}

    def fetch_supplier(self, name: str) -> SupplierProfile:
        name = (name or "").strip()
        if not name:
            raise ValueError("请输入公司名称。")

        raw, exact, cands = self._match_local(name)
        if raw is None and cands:
            # 模糊命中（名称不完整/简称）
            if len(cands) > 1:
                # 多个候选 → 让用户自主选择（实体锚定，不擅自替用户选）
                from adapters.qcc_adapter import MultiCandidateError
                clist = []
                for c in cands:
                    reg = c.get("registration") or {}
                    clist.append({
                        "name": c.get("name", ""),
                        "credit_code": reg.get("credit_code") or "",
                        "legal_person": reg.get("legal_person") or "—",
                        "established": reg.get("established") or "—",
                        "status": reg.get("status") or "—",
                    })
                raise MultiCandidateError(
                    f"「{name}」名称不完整，命中多个主体，请选择目标供应商。",
                    candidates=clist,
                )
            # 单一候选（简称唯一匹配）→ 采用，但标记"模糊匹配"供上层提示
            raw = cands[0]
            exact = False

        if raw is None:
            names = "、".join(self._available_names()[:12])
            raise ValueError(
                f"本地预采集库未找到「{name}」。\n"
                f"当前覆盖：{names} 等 {len(self._available_names())} 家。"
            )

        profile = self._dict_to_profile(raw)
        profile.registration = profile.registration or {}
        profile.registration["_data_source"] = "本地预采集库(公开工商+年报)"
        if not exact:
            # 模糊匹配标记：供上层在结果里提示"名称可能不完整"
            profile.registration["_fuzzy_matched"] = raw.get("name", "")

        # 行情补充：用本地库内置股票代码直查东方财富（复用 EastmoneyAdapter）
        code = profile.registration.get("stock_code")
        if code:
            try:
                from adapters.eastmoney_adapter import EastmoneyAdapter, EM_SOURCE
                em = EastmoneyAdapter()
                mkt = em._em_market(str(code), profile.registration.get("market"))
                if mkt:
                    profile.market = mkt
                    if mkt.stock_name:
                        profile.registration.setdefault("short_name", mkt.stock_name)
                    profile.sources = ["local", "eastmoney"]
                    profile.source_meta = {"local": LOCAL_SOURCE, "eastmoney": EM_SOURCE}
                else:
                    profile.sources = ["local"]
                    profile.source_meta = {"local": LOCAL_SOURCE}
            except Exception:
                profile.sources = ["local"]
                profile.source_meta = {"local": LOCAL_SOURCE}
        else:
            profile.sources = ["local"]
            profile.source_meta = {"local": LOCAL_SOURCE}

        self._attach_alerts(profile)
        return profile

    # ---------- 本地预采集库 ----------
    def _load_local(self) -> dict:
        if self._cache is not None:
            return self._cache
        cache = {}
        if os.path.isdir(PUBLIC_DIR):
            for fn in os.listdir(PUBLIC_DIR):
                if not fn.endswith(".json"):
                    continue
                try:
                    with open(os.path.join(PUBLIC_DIR, fn), "r", encoding="utf-8") as f:
                        raw = json.load(f)
                    cache[raw["name"]] = raw
                except Exception:
                    _log_swallow("adapters/local_adapter.py:126")
                    continue
        self._cache = cache
        return cache

    def _available_names(self) -> list:
        return list(self._load_local().keys())

    def _match_local(self, name: str):
        """返回 (matched_raw, exact, candidates)。

        - matched_raw：精确或唯一模糊命中的原始数据（dict 或 None）
        - exact：是否精确匹配（True=精确，False=模糊/简称）
        - candidates：模糊命中的**全部**候选（空列表表示无模糊命中）
        """
        cache = self._load_local()
        if not cache:
            return None, True, []

        if name in cache:
            return cache[name], True, []

        def norm(s: str) -> str:
            return re.sub(r"[\s（）()·]", "", s or "")

        target = norm(name)
        for full, raw in cache.items():
            if norm(full) == target:
                return raw, True, []

        candidates = []
        for full, raw in cache.items():
            nf = norm(full)
            if target and (target in nf or nf in target):
                candidates.append(raw)
        if candidates:
            return None, False, candidates
        return None, True, []

    def _dict_to_profile(self, raw: dict) -> SupplierProfile:
        legal = [LegalEvent(**e) for e in raw.get("legal_events", [])]
        news = [NewsItem(**n) for n in raw.get("news", [])]
        fin = FinancialMetrics(**raw["financials"]) if raw.get("financials") else None
        # 本地快照 JSON 显式写了 important_outsourcing / non_resident / cross_border，
        # 其中 False 是"明确为非重要外包"，不是"未提供"。若不给 conc_flags_provided，
        # 集中度维度会被判 na，三条规则（重要/非驻场/跨境）的扣分被整块丢弃 ——
        # 明确判为重要外包的主体反而少扣 10 分，风险被低估。
        flags_provided = any(k in raw for k in
                             ("important_outsourcing", "non_resident", "cross_border"))
        return SupplierProfile(
            name=raw["name"],
            important_outsourcing=raw.get("important_outsourcing", False),
            non_resident=raw.get("non_resident", False),
            cross_border=raw.get("cross_border", False),
            legal_events=legal,
            news=news,
            financials=fin,
            registration=raw.get("registration"),
            conc_flags_provided=flags_provided,
        )

    def _attach_alerts(self, profile: SupplierProfile):
        """生成"人话版"综合风险提示。"""
        alerts = []
        for e in profile.legal_events:
            if e.severity == "high":
                alerts.append({"level": "high", "title": e.title,
                               "detail": e.evidence or ""})
        f = profile.financials
        if f:
            if f.net_margin is not None and f.net_margin < 0:
                alerts.append({"level": "medium", "title": "主营业务亏损",
                               "detail": f"销售净利率 {f.net_margin:.2f}%，公司处于亏损状态。"})
            if f.debt_ratio is not None and f.debt_ratio > 70:
                alerts.append({"level": "medium", "title": "资产负债率偏高",
                               "detail": f"资产负债率 {f.debt_ratio:.2f}%，高于 70% 警戒线。"})
            if f.operating_cashflow is not None and f.operating_cashflow < 0:
                alerts.append({"level": "medium", "title": "经营现金流为负",
                               "detail": f"经营活动现金流 {f.operating_cashflow:,.0f} 元。"})
            if f.revenue_yoy is not None and f.revenue_yoy < -20:
                alerts.append({"level": "medium", "title": "营收大幅下滑",
                               "detail": f"营业收入同比 {f.revenue_yoy:.2f}%，降幅超过 20%。"})
        m = profile.market
        if m and m.change_pct is not None and abs(m.change_pct) > 5:
            alerts.append({"level": "low", "title": "股价异动",
                           "detail": f"当日涨跌幅 {m.change_pct:+.2f}%，建议关注市场预期变化。"})
        profile.risk_alerts = alerts[:8]
