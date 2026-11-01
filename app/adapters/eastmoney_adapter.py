"""东方财富公开接口适配器 —— 免费实时行情 / 估值数据源。

定位
----
东方财富公开接口（无需 Key）覆盖沪深 A 股实时行情、估值与基础财务，
作为独立的"行情/估值"数据源参与多源聚合（与企查查/本地库交叉验证）。

从 public_adapter 拆分而来（2026-09）：让东方财富成为独立可插拔源，
在「数据源管理」页可单独开关。
"""
import json
import re
import urllib.error
import urllib.parse
import urllib.request

from core.schemas import (
    SupplierProfile, FinancialMetrics, MarketData, SourceInfo,
)
from core.adapter import DataSourceAdapter, register

EM_SOURCE = SourceInfo(
    key="eastmoney", label="东方财富公开接口", category="行情/估值",
    authoritative=False, url="https://quote.eastmoney.com",
)

# 东方财富公开行情/基本信息接口（无需 Key）
EM_SEARCH_URL = "https://searchapi.eastmoney.com/api/suggest/get"
# push2 主站偶发拒连，改用延迟行情站，并保留主站作为备选
EM_QUOTE_HOSTS = [
    "https://push2delay.eastmoney.com",
    "https://push2.eastmoney.com",
]
EM_QUOTE_PATH = "/api/qt/stock/get"


@register("eastmoney")
class EastmoneyAdapter(DataSourceAdapter):
    """东方财富公开接口数据源：按公司名/代码查 A 股，返回行情+估值+基础财务。"""

    def is_available(self) -> bool:
        return True

    def source_info(self) -> dict:
        return {"label": "东方财富公开接口", "category": "行情/估值",
                "authoritative": False}

    def fetch_supplier(self, name: str) -> SupplierProfile:
        """按公司名/代码查 A 股，返回真实行情+财务数据（非 A 股抛错）。"""
        code, market = self._em_search(name)
        if not code:
            raise ValueError(f"东方财富未找到 A 股「{name}」（仅支持沪深上市公司）。")
        info = self._em_basic(code, market)
        if not info:
            raise ValueError(f"东方财富未取到「{name}」的行情数据。")

        profile = SupplierProfile(
            name=info.get("name") or name,
            legal_events=[],
            news=[],
            financials=self._em_financials(code, market),
            market=self._em_market(code, market),
            registration={
                "stock_code": code,
                "market": market,
                "industry": info.get("industry", ""),
                "total_market_cap": info.get("market_cap"),
                "pe": info.get("pe"),
                "pb": info.get("pb"),
                "_data_source": "东方财富公开接口(实时)",
            },
            sources=["eastmoney"],
            source_meta={"eastmoney": EM_SOURCE},
        )
        return profile

    # ---------- 行情/估值 ----------
    def _em_market(self, code: str, market: str = None) -> MarketData:
        if not market:
            market = "SH" if str(code).startswith(("6", "9")) else "SZ"
        secid = f"{'1' if market == 'SH' else '0'}.{code}"
        fields = "f57,f58,f43,f170,f162,f167,f116,f168,f169,f171"
        try:
            d = self._em_quote(secid, fields)
        except Exception:
            return None
        if not d:
            return None

        def num(v, scale=False):
            if not isinstance(v, (int, float)):
                return None
            return round(v / 100, 2) if scale else v

        return MarketData(
            stock_code=str(code),
            stock_name=d.get("f58"),
            price=num(d.get("f43"), scale=True),
            change_pct=num(d.get("f170"), scale=True),
            pe=num(d.get("f162"), scale=True),
            pb=num(d.get("f167"), scale=True),
            total_market_cap=d.get("f116"),
            turnover_rate=num(d.get("f168"), scale=True),
            source="东方财富公开接口(实时)",
        )

    @staticmethod
    def _scale100(v):
        if isinstance(v, (int, float)):
            return round(v / 100, 2)
        return v

    def _http_json(self, url: str, timeout: int = 6):
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                "Referer": "https://quote.eastmoney.com/",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            txt = resp.read().decode("utf-8", errors="ignore")
        txt = re.sub(r"^[^(]*\(", "", txt.strip())
        txt = re.sub(r"\)\s*;?\s*$", "", txt)
        return json.loads(txt)

    def _em_quote(self, secid: str, fields: str):
        last_err = None
        for host in EM_QUOTE_HOSTS:
            url = f"{host}{EM_QUOTE_PATH}?secid={secid}&fields={fields}"
            try:
                return (self._http_json(url) or {}).get("data") or {}
            except Exception as e:
                last_err = e
                continue
        if last_err:
            raise last_err
        return {}

    def _em_search(self, keyword: str):
        if re.fullmatch(r"\d{6}", keyword):
            market = "SH" if keyword.startswith(("6", "9")) else "SZ"
            return keyword, market
        q = urllib.parse.quote(keyword)
        url = (
            f"{EM_SEARCH_URL}?input={q}&type=14&token=D43BF722C8E33BDC906FB84D85E326E8"
            f"&count=10"
        )
        try:
            data = self._http_json(url)
        except (urllib.error.URLError, OSError, TimeoutError,
                json.JSONDecodeError) as e:
            # 断网/超时/非 JSON 响应 ≠「东财没有这只 A 股」。吞掉会让行情维度
            # 静默缺失，故上抛给聚合层，由 failed_sources 如实记录。
            # （HTTPError⊂URLError，socket.timeout⊂OSError，一并覆盖）
            raise ValueError(
                "东方财富公开接口检索失败（网络或响应异常）："
                f"{type(e).__name__}: {e}") from e
        items = (data.get("QuotationCodeTable") or {}).get("Data") or []
        for it in items:
            code = it.get("Code")
            mkt = str(it.get("MktNum"))
            if not code or not re.fullmatch(r"\d{6}", code):
                continue
            if mkt not in ("1", "2"):
                continue
            if not code.startswith(("6", "0", "3")):
                continue
            market = "SH" if mkt == "1" else "SZ"
            return code, market
        return None, None

    def _em_basic(self, code: str, market: str):
        secid = f"{'1' if market == 'SH' else '0'}.{code}"
        fields = "f57,f58,f43,f116,f117,f162,f167,f168,f169,f170,f171,f173"
        try:
            d = self._em_quote(secid, fields)
        except Exception:
            return None
        if not d or not d.get("f58"):
            return None
        return {
            "name": d.get("f58"),
            "market_cap": d.get("f116"),
            "pe": self._scale100(d.get("f162")),
            "pb": self._scale100(d.get("f167")),
            "industry": "",
        }

    def _em_financials(self, code: str, market: str):
        secid = f"{'1' if market == 'SH' else '0'}.{code}"
        fields = "f57,f58,f43,f162,f167,f173,f183,f184,f105,f186,f188,f187"
        try:
            d = self._em_quote(secid, fields)
        except Exception:
            d = {}
        fin = FinancialMetrics()

        def num(v):
            if isinstance(v, (int, float)):
                return round(float(v), 2)
            return None

        fin.roe = num(d.get("f173"))
        fin.revenue = d.get("f183") if isinstance(d.get("f183"), (int, float)) else None
        fin.revenue_yoy = num(d.get("f184"))
        fin.net_profit = d.get("f105") if isinstance(d.get("f105"), (int, float)) else None
        fin.gross_margin = num(d.get("f186"))
        fin.debt_ratio = num(d.get("f188"))
        fin.report_period = "实时行情口径"
        return fin
