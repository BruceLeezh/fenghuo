"""万得 Wind 适配器 —— 专业财务基本面 + 实时行情（权威源，交叉验证用）。

定位
----
Wind 与 iFinD 数据有重叠（都是行情/财务），故 Wind 的定位是：
  - **财务交叉验证**：当 iFinD / 企查查财务缺失时，用 Wind 财务兜底
  - **权威财务**：ROE / 资产负债率 / 净利润等，比公开免费源更权威

额度优化（重要）
----------------
Wind 按调用计费，且**没有"公司名 → windcode"的精确搜索工具**（search_stocks 是选股筛选）。
故做两道防护：
  1. **名称 → windcode 零成本映射**：优先查本地预采集库内置的 stock_code + market
     （如 恒生电子 → 600570.SH），本地库没有的主体直接返回"未覆盖"，不调 Wind —— 避免无效查询
  2. **复用 qcc_cache**：财务缓存 7 天、行情缓存 30 分钟（见 qcc_cache._TOOL_TTL 的 wind 段）
"""
from core.quiet_log import log_swallow as _log_swallow
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.schemas import SupplierProfile, SourceInfo, FinancialMetrics, MarketData
from core.adapter import DataSourceAdapter, register
from integrations.wind_mcp import WindClient, WindError, is_configured

WIND_SOURCE = SourceInfo(
    key="wind", label="万得 Wind", category="专业财务 / 行情",
    authoritative=True, url="https://www.wind.com.cn",
)


def _f(v):
    """字符串/数字 → float（去逗号、百分号）。"""
    if v is None:
        return None
    s = str(v).replace(",", "").replace("%", "").strip()
    if not s or s in ("—", "-", "N/A", "null", "None"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _table_of(resp):
    """Wind 返回 {"data":{"data":[{...}]}} 或 {"data":{...}}。统一取表格 dict。"""
    if not isinstance(resp, dict):
        return None
    d = resp.get("data") or {}
    if isinstance(d.get("data"), list) and d.get("data"):
        return d["data"][0]
    if isinstance(d.get("columns"), list):
        return d
    return None


def _parse_table(resp):
    """表格 → {列名: 值}。"""
    table = _table_of(resp)
    if not isinstance(table, dict):
        return {}
    columns = table.get("columns") or []
    rows = table.get("rows") or []
    if not rows or not isinstance(rows[0], list):
        return {}
    out = {}
    for i, col in enumerate(columns):
        name = col.get("name") if isinstance(col, dict) else str(col)
        out[name] = rows[0][i] if i < len(rows[0]) else None
    return out


def _units_of(resp):
    """取各列单位。Wind 两种结构：fundamentals 的单位在 columns[i].unit，
    price_indicators 的单位在 data.unit。统一收集成 {列名: 单位}。"""
    units = {}
    table = _table_of(resp)
    if isinstance(table, dict):
        for col in (table.get("columns") or []):
            if isinstance(col, dict) and col.get("name") and col.get("unit"):
                units[col["name"]] = col["unit"]
    d = (resp or {}).get("data") or {}
    if isinstance(d.get("unit"), dict):
        units.update(d["unit"])
    return units


@register("wind")
class WindAdapter(DataSourceAdapter):
    """万得 Wind 数据源（真实接入：财务基本面 + 实时行情）。"""

    def __init__(self):
        self._client = None

    def _c(self):
        if self._client is None:
            self._client = WindClient("stock")
        return self._client

    def is_available(self) -> bool:
        return is_configured()

    def source_info(self) -> dict:
        return {"label": "万得 Wind", "category": "专业财务 / 行情",
                "authoritative": True}

    def fetch_supplier(self, name: str) -> SupplierProfile:
        windcode = self._resolve_code(name)
        if not windcode:
            raise ValueError(
                f"Wind 未覆盖「{name}」：本地预采集库无其股票代码，"
                f"且 Wind 无按公司名精确检索工具（避免无效查询消耗额度）。"
            )

        fin = self._fetch_financials(windcode)
        mkt = self._fetch_market(windcode)
        if fin is None and mkt is None:
            raise ValueError(f"Wind 对「{name}」({windcode}) 无财务与行情数据。")

        profile = SupplierProfile(name=name)
        profile.registration = {"_windcode": windcode}
        profile.financials = fin
        profile.market = mkt
        profile.sources = ["wind"]
        profile.source_meta = {"wind": WIND_SOURCE}
        return profile

    # ---------- 名称 → windcode（零成本，本地库兜底）----------
    def _resolve_code(self, name: str):
        raw = self._match_local(name)
        if not raw:
            return None
        reg = raw.get("registration") or {}
        code, market = reg.get("stock_code"), reg.get("market")
        if code and market:
            return f"{code}.{market}"
        return None

    def _match_local(self, name):
        base = os.path.join(os.path.dirname(__file__), "..", "data", "public")
        if not os.path.isdir(base):
            return None

        def norm(s):
            return re.sub(r"[\s（）()·]", "", s or "")

        target = norm(name)
        exact, contains = None, []
        for fn in os.listdir(base):
            if not fn.endswith(".json"):
                continue
            try:
                with open(os.path.join(base, fn), encoding="utf-8") as f:
                    raw = json.load(f)
            except Exception:
                _log_swallow("adapters/wind_adapter.py:158")
                continue
            nf = norm(raw.get("name", ""))
            if nf == target:
                exact = raw
                break
            if target and (target in nf or nf in target):
                contains.append(raw)
        if exact:
            return exact
        if len(contains) == 1:
            return contains[0]
        return None  # 多候选不猜

    # ---------- 财务 / 行情 ----------
    def _fetch_financials(self, windcode: str):
        try:
            resp = self._c().call_tool("get_stock_fundamentals", {
                "question": f"查询{windcode}最新一期的资产负债率、ROE、净利率、净利润、营业收入",
            })
        except WindError:
            return None
        row = _parse_table(resp)
        if not row:
            return None
        units = _units_of(resp)

        def col(kw):
            for k, v in row.items():
                if kw in k:
                    return v
            return None

        def to_yuan(kw):
            v = col(kw)
            if v is None:
                return None
            num = _f(v)
            if num is None:
                return None
            unit = ""
            for k, u in units.items():
                if kw in k:
                    unit = u or ""
                    break
            if "亿" in unit:
                return num * 1e8
            if "万" in unit:
                return num * 1e4
            return num

        return FinancialMetrics(
            debt_ratio=_f(col("资产负债率")),
            roe=_f(col("ROE")),
            net_margin=_f(col("净利率")),
            net_profit=to_yuan("净利润"),
            revenue=to_yuan("营业收入"),
        )

    def _fetch_market(self, windcode: str):
        try:
            resp = self._c().call_tool("get_stock_price_indicators", {
                "windcode": windcode,
                "indexes": "中文简称,最新成交价,涨跌幅,换手率",
            })
        except WindError:
            return None
        row = _parse_table(resp)
        if not row:
            return None
        return MarketData(
            stock_code=windcode,
            stock_name=row.get("中文简称") or row.get("Wind代码"),
            price=_f(row.get("最新成交价")),
            change_pct=_f(row.get("涨跌幅")),
            turnover_rate=_f(row.get("换手率")),
            source="万得 Wind（实时）",
        )
