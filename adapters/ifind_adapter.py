"""同花顺 iFinD 适配器 —— 真实接入（行情 / 财务 / 资讯）。

定位
----
iFinD 覆盖本项目"行情 / 财务 / 公告 / 宏观"数据需求，是**权威源**（authoritative）。
与企查查（工商+司法）互补：
  - 企查查  → 主体锚定、工商登记、司法风险、股东
  - iFinD  → 实时行情估值、上市公司财务指标、财经资讯

调用链（每次抓取最多 3 次 MCP 调用，且全部走缓存）
--------------------------------------------------
    stock_highfreq_quotes  → MarketData   （最新价/涨跌幅/换手率/市净率/总市值）
    get_stock_financials   → FinancialMetrics（资产负债率/净利率/ROE/经营现金流，Markdown 表格）
    news.search_news       → NewsItem[]  （风险资讯，用于舆情维度）

注意
----
- iFinD 只覆盖**上市主体**；非上市公司会返回空，此时优雅跳过（不阻断聚合）
- 免费版 2 QPS → 顺序调用，不在线程里并发打
- 返回的 `data` 字段常常是"JSON 字符串"或"Markdown 表格"，需二次解析
"""
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.schemas import SupplierProfile, SourceInfo, MarketData, FinancialMetrics, NewsItem
from core.adapter import DataSourceAdapter, register
from integrations.ifind_mcp import IfindClient, IfindError, explain_error, is_configured

IFIND_SOURCE = SourceInfo(
    key="ifind", label="同花顺 iFinD", category="行情 / 财务 / 资讯",
    authoritative=True, url="https://www.51ifind.com",
)

# 舆情负面词（用于把资讯打成 negative + 严重度）
_NEG_HIGH = ("立案", "处罚", "违规", "违法", "被诉", "诉讼", "冻结", "退市", "问询函", "监管函",
             "警示函", "失信", "被执行", "爆雷", "违约", "净亏损", "巨亏")
_NEG_MID = ("下滑", "下降", "亏损", "风险", "裁员", "缩减", "承压", "减持", "质押", "商誉减值",
            "现金流为负", "业绩预减", "解禁", "业绩下滑")
_POS = ("增长", "提升", "中标", "签约", "突破", "领先", "获奖", "回购", "增持", "扭亏",
        "收窄", "改善", "盈利", "新高", "增效")

# 公司名常见后缀/前缀（用于提取"核心名"做新闻相关性校验，防止无关公司新闻污染舆情）
_COMPANY_SUFFIXES = ("股份有限公司", "有限责任公司", "有限公司", "股份公司", "集团公司", "集团")
_REGION_PREFIXES = ("北京", "上海", "天津", "重庆", "广东", "江苏", "浙江", "山东", "四川",
                    "河南", "湖北", "湖南", "福建", "安徽", "江西", "陕西", "河北", "山西",
                    "辽宁", "吉林", "黑龙江", "云南", "贵州", "海南", "甘肃", "青海", "内蒙古",
                    "广西", "宁夏", "新疆", "西藏", "中国", "深圳", "广州", "杭州", "南京",
                    "武汉", "成都", "西安", "苏州", "宁波", "青岛", "厦门", "大连", "无锡")


def _core_name(name: str) -> str:
    """提取公司名的"核心简称"（去地区前缀 + 公司后缀），用于新闻相关性校验。

    例：「北京科蓝软件系统股份有限公司」→「科蓝软件系统」
    ★ 长度不足 2 字时返回空（无法可靠匹配，宁可不校验）。
    """
    core = (name or "").strip()
    for suf in _COMPANY_SUFFIXES:
        if core.endswith(suf):
            core = core[:-len(suf)]
            break
    for pre in _REGION_PREFIXES:
        if core.startswith(pre) and len(core) - len(pre) >= 2:
            core = core[len(pre):]
            break
    return core if len(core) >= 2 else ""


def _f(v):
    """把 iFinD 返回值转 float。

    ★ iFinD 的数值**常带中文单位后缀**且不固定：
      `-982280538.55`（纯元） / `369.4799万` / `1.8001亿` —— 必须换算，否则差 1万~1亿倍。
    """
    if v is None:
        return None
    s = str(v).strip().replace(",", "").replace("%", "")
    if not s:
        return None
    mult = 1.0
    if "亿" in s:
        mult = 1e8
    elif "万" in s:
        mult = 1e4
    s = (s.replace("亿元", "").replace("万元", "")
          .replace("亿", "").replace("万", "").replace("元", ""))
    m = re.search(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?", s)
    if not m:
        return None
    try:
        return float(m.group(0)) * mult
    except ValueError:
        return None


def _unwrap(resp):
    """统一解包：{code,msg,data:'...'} → dict/list；data 可能是 JSON 字符串。"""
    if not isinstance(resp, dict):
        return resp
    if resp.get("code") not in (None, 1) and resp.get("msg") not in (None, "success"):
        raise IfindError(f"iFinD 返回异常: {resp.get('msg')}")
    data = resp.get("data")
    if isinstance(data, str):
        try:
            return json.loads(data)
        except (json.JSONDecodeError, TypeError):
            return data
    return data


def _parse_md_table(md: str) -> tuple:
    """解析 Markdown 表格 → (表头 list, 数据行 list)。"""
    rows = [ln.strip() for ln in str(md).splitlines()
            if ln.strip().startswith("|")]
    if len(rows) < 3:
        return [], []
    def cells(line):
        return [c.strip() for c in line.strip("|").split("|")]
    header = cells(rows[0])
    # 跳过 |---|---| 分隔行
    body = rows[2] if set(rows[1].replace("|", "").replace("-", "").strip()) <= {""} else rows[1]
    return header, cells(body)


def _col(header: list, *kws: str, exclude: tuple = ()):
    """按关键词（全部命中）模糊定位列下标；exclude 里的词命中则跳过该列。

    iFinD 多指标查询会同时返回「营业收入（单位：元）」「单季度.营业收入」
    「营业收入(同比增长率)」等相似列，必须靠 exclude 排除干扰列。
    """
    for i, h in enumerate(header):
        if all(k in h for k in kws) and not any(x in h for x in exclude):
            return i
    return None


@register("ifind")
class IfindAdapter(DataSourceAdapter):
    """同花顺 iFinD 数据源（实时行情 + 上市公司财务 + 风险资讯）。"""

    def __init__(self, with_news: bool = True):
        self.with_news = with_news

    def is_available(self) -> bool:
        return is_configured()

    def source_info(self) -> dict:
        return {"label": "同花顺 iFinD", "category": "行情 / 财务 / 资讯",
                "authoritative": True}

    # ---------- 统一入口 ----------
    def fetch_supplier(self, name: str, refresh: bool = False) -> SupplierProfile:
        """抓取上市主体的行情/财务/资讯。

        非上市主体 → iFinD 无数据 → 返回空档案（sources 标记为 ifind 但无字段），
        由聚合层按"该源无贡献"处理，不影响其他源。
        """
        if not is_configured():
            raise ValueError("未配置 iFinD MCP 密钥（IFIND_MCP_KEY）。请在「数据源管理」页填写。")

        market, financials = None, None
        try:
            client = IfindClient("stock")
            market = self._fetch_market(client, name, refresh=refresh)
            time.sleep(0.4)   # 免费版 2 QPS，避免触发限流
            financials = self._fetch_financials(client, name, refresh=refresh)
        except IfindError as e:
            raise ValueError(f"iFinD 数据源不可用：{explain_error(e)}") from e

        news = []
        if self.with_news:
            try:
                news = self._fetch_news(name, refresh=refresh)
            except Exception:
                news = []   # 资讯是增强项，失败不影响主流程

        profile = SupplierProfile(
            name=name, market=market, financials=financials, news=news,
            sources=["ifind"], source_meta={"ifind": IFIND_SOURCE},
        )
        if market is None and financials is None and not news:
            # 明确标记无贡献（避免上层误判为"iFinD 已覆盖且无风险"）
            profile.source_meta["ifind"] = SourceInfo(
                key="ifind", label="同花顺 iFinD（未覆盖该主体）",
                category="行情 / 财务 / 资讯", authoritative=False, url=IFIND_SOURCE.url)
        return profile

    # ---------- 行情 ----------
    def _fetch_market(self, client: IfindClient, name: str, refresh: bool = False):
        raw = client.call_tool("stock_highfreq_quotes", {
            "symbols": name,
            "indicators": "最新价,涨跌幅,换手率,市净率,总市值",
            "data_mode": "real_time",
        }, refresh=refresh)
        d = _unwrap(raw)
        if not isinstance(d, dict):
            return None
        tables = d.get("tables") or []
        if len(tables) < 2:
            return None
        header, row = tables[0], tables[1]
        if not row or len(row) < len(header):
            return None

        def g(*kws):
            i = _col(header, *kws)
            return row[i] if i is not None and i < len(row) else None

        code = g("证券代码")
        m = MarketData(
            stock_code=str(code) if code else None,
            stock_name=g("证券简称") or name,
            price=_f(g("最新价")),
            change_pct=_f(g("涨跌幅")),
            pb=_f(g("市净率")),
            total_market_cap=_f(g("总市值")),
            turnover_rate=_f(g("换手率")),
            source="同花顺 iFinD（实时）",
        )
        if m.price is None and m.total_market_cap is None:
            return None
        return m

    # ---------- 财务 ----------
    def _fetch_financials(self, client: IfindClient, name: str, refresh: bool = False):
        raw = client.call_tool("get_stock_financials", {
            "query": (f"{name}最新一期的资产负债率、销售净利率、加权净资产收益率ROE、"
                      f"经营活动产生的现金流量净额、营业收入、净利润"),
        }, refresh=refresh)
        d = _unwrap(raw)
        answer = d.get("answer") if isinstance(d, dict) else (d if isinstance(d, str) else None)
        if not answer:
            return None
        header, row = _parse_md_table(answer)
        if not header or not row:
            return None

        def val(*kws, exclude=()):
            i = _col(header, *kws, exclude=exclude)
            if i is None or i >= len(row):
                return None
            return _f(row[i])

        # 报告期：iFinD 表格本身常无日期列，改从 indicators_params 读
        period = None
        params = d.get("indicators_params") if isinstance(d, dict) else None
        if isinstance(params, dict):
            for v in params.values():
                if isinstance(v, dict) and v.get("报告期"):
                    period = str(v["报告期"])
                    break
        pi = _col(header, "日期") or _col(header, "报告期")
        if pi is not None and pi < len(row):
            raw_p = str(row[pi])
            if len(raw_p) == 8 and raw_p.isdigit():
                period = f"{raw_p[:4]}年{raw_p[4:6]}月末报告期"
            elif raw_p.strip() not in ("", "—"):
                period = raw_p

        fin = FinancialMetrics(
            debt_ratio=val("资产负债率"),
            net_margin=val("净利率", exclude=("同比",)),
            roe=(val("ROE", "加权", exclude=("摊薄",))
                 or val("净资产收益率", "加权", exclude=("摊薄",)) or val("ROE")),
            operating_cashflow=val("现金流", exclude=("同比",)),
            revenue=val("营业收入", exclude=("同比", "单季度", "单季")),
            net_profit=val("净利润", exclude=("同比", "单季度", "单季", "归属", "母公司")),
            report_period=period,
        )
        if all(v is None for v in (fin.debt_ratio, fin.net_margin, fin.roe, fin.operating_cashflow)):
            return None
        return fin

    # ---------- 资讯 ----------
    def _fetch_news(self, name: str, refresh: bool = False):
        """风险资讯检索（近一年），转成 NewsItem。"""
        from datetime import date, timedelta
        client = IfindClient("news")
        end = date.today().isoformat()
        start = (date.today() - timedelta(days=365)).isoformat()
        raw = client.call_tool("search_news", {
            "query": f"{name} 经营情况 风险 业绩",
            "time_start": start, "time_end": end, "size": 5,
        }, refresh=refresh)
        d = _unwrap(raw)
        items = d.get("data") if isinstance(d, dict) else d
        if isinstance(items, str):
            try:
                items = json.loads(items)
            except (json.JSONDecodeError, TypeError):
                items = []
        if not isinstance(items, list):
            return []

        out = []
        core = _core_name(name)          # 公司核心名，用于过滤无关新闻
        keyword = core[:4] if len(core) >= 4 else core   # 取前 4 字做关键词（兼容简称）
        for it in items[:5]:
            if not isinstance(it, dict):
                continue
            title = (it.get("资讯标题") or "").strip()
            if not title:
                continue
            body = it.get("资讯内容") or ""
            # ★ 相关性校验：标题/正文须含目标公司核心名，否则丢弃（iFinD 对不存在的名字
            #   会返回其它公司的新闻，例如查"不存在的公司XYZ123"返回"华天酒店"公告）
            if keyword and keyword not in title and keyword not in body[:400]:
                continue
            # 以**标题**为主判情绪（正文常含无关词导致误判）；
            # 先识别"改善性"表述，避免"亏损收窄/扭亏"被当成负面舆情
            if any(k in title for k in ("亏损收窄", "亏损大幅收窄", "扭亏", "减亏", "业绩改善")):
                sentiment, severity = "positive", "low"
            elif any(k in title for k in _NEG_HIGH):
                sentiment, severity = "negative", "high"
            elif any(k in title for k in _NEG_MID):
                sentiment, severity = "negative", "medium"
            elif any(k in title for k in _POS):
                sentiment, severity = "positive", "low"
            elif any(k in body[:300] for k in _NEG_HIGH):
                sentiment, severity = "negative", "medium"
            else:
                sentiment, severity = "neutral", "low"
            out.append(NewsItem(
                title=title[:120],
                sentiment=sentiment, severity=severity,
                published_at=it.get("日期"),
                source="同花顺 iFinD 资讯",
                url=it.get("URL") or "",
                evidence=body[:200],
            ))
        return out
