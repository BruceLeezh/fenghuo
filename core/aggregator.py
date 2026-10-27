"""多源聚合层 —— 统一调度多个数据源，合并结果并做交叉验证。

设计目标（对应"多数据源、更综合、更可信"的诉求）：

  1. **并行调度**：一次查询并发调用多个数据源，谁先返回谁先入库，
     整体耗时取决于最慢的源，而不是各源之和。
  2. **字段级合并**：不同数据源覆盖不同字段（企查查=工商/司法/财务，
     东方财富=行情/估值），按"字段优先 + 非空覆盖"策略合并。
  3. **交叉验证**：同一指标被多个源覆盖时，标注"已交叉验证"；
     仅单一来源时标注"单源待验证"，供前端展示可信度。
  4. **来源可追溯**：每个字段都记录 contributing sources。

可插拔设计：新增数据源只需实现 DataSourceAdapter 并注册，
在 SOURCE_PRIORITY 里声明它的字段优先级即可，聚合层无需改动。
"""
from core.quiet_log import log_swallow as _log_swallow
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Tuple, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.adapter import get_adapter, DATA_SOURCE_REGISTRY
from core.schemas import SupplierProfile, SourceInfo


# ---------------------------------------------------------------------------
# 数据源注册表（展示用元信息）—— 新增源在这里登记即可
# ---------------------------------------------------------------------------
SOURCE_CATALOG: Dict[str, SourceInfo] = {
    "qcc": SourceInfo(
        key="qcc", label="企查查 MCP", category="工商登记 / 年报财务 / 司法风险",
        authoritative=True, url="https://www.qcc.com",
    ),
    "local": SourceInfo(
        key="local", label="本地预采集库", category="工商登记 / 年报财务 / 司法风险",
        authoritative=True, url="",
    ),
    "eastmoney": SourceInfo(
        key="eastmoney", label="东方财富公开接口", category="实时行情 / 估值",
        authoritative=False, url="https://quote.eastmoney.com",
    ),
    # ---- 以下为可插拔槽位：接入后自动出现在"数据源覆盖"面板 ----
    "ifind": SourceInfo(
        key="ifind", label="同花顺 iFinD", category="行情 / 财务 / 公告 / 宏观",
        authoritative=True, url="https://www.51ifind.com",
    ),
    "wind": SourceInfo(
        key="wind", label="万得 Wind", category="专业财务 / 行业对比",
        authoritative=True, url="",
    ),
}

# 字段覆盖优先级：同一字段被多源提供时，排在前面的源优先采用
FIELD_PRIORITY = {
    "registration": ["qcc", "local", "wind"],
    "financials": ["qcc", "wind", "local", "eastmoney", "ifind"],
    "legal_events": ["qcc", "local"],
    "shareholders": ["qcc"],
    "market": ["eastmoney", "ifind", "wind"],
    "news": ["ifind", "qcc"],
}


class MultiSourceResult:
    """多源聚合结果。"""

    def __init__(self, profile: SupplierProfile, used: List[str],
                 failed: Dict[str, str], verified: Dict[str, bool]):
        self.profile = profile
        self.used = used                # 成功参与的数据源 key
        self.failed = failed            # 失败的数据源 -> 原因
        self.verified = verified        # 字段 -> 是否多源交叉验证


def aggregate(name: str, sources: List[str] = None,
              timeout: float = 45.0, refresh: bool = False) -> MultiSourceResult:
    """并发调用多个数据源并合并结果。

    参数：
      name    —— 公司名称
      sources —— 要调用的数据源 key 列表；None 表示自动（按可用性）
      timeout —— 整体超时（秒）
      refresh —— 是否绕过缓存强制重新拉取（默认 False = 优先用缓存，省企查查积分）
    """
    sources = sources or auto_sources()
    results: Dict[str, SupplierProfile] = {}
    failed: Dict[str, str] = {}
    lock = threading.Lock()

    def run(key: str):
        try:
            adapter = _resolve_adapter(key)
            try:
                p = adapter.fetch_supplier(name, refresh=refresh)
            except TypeError:
                # 不支持 refresh 的适配器（本地库/东财等，本身不消耗额度）
                p = adapter.fetch_supplier(name)
            with lock:
                results[key] = p
        except Exception as e:
            with lock:
                failed[key] = str(e)

    # 并发执行（每源独立线程，互不阻塞）
    # 注意：`with ThreadPoolExecutor(...)` 退出时会 shutdown(wait=True)，超时形同虚设，
    # 慢源会把请求一直拖住。故显式持有 ex，超时后放弃剩余线程而不等待。
    ex = ThreadPoolExecutor(max_workers=max(1, len(sources)))
    try:
        futures = {ex.submit(run, k): k for k in sources}
        for fut in as_completed(futures, timeout=timeout + 5):
            try:
                fut.result()
            except Exception as e:
                # run() 内部已把业务异常写进 failed；能走到这里的是 run 自身
                # 抛出的意外错误（如线程启动失败）。不能静默吞掉，否则该源
                # 凭空消失且无从排查。
                key = futures.get(fut, "?")
                with lock:
                    failed.setdefault(key, f"数据源调度异常: {type(e).__name__}: {e}")
    except TimeoutError:
        with lock:
            for fut, key in futures.items():
                if not fut.done() and key not in results:
                    failed.setdefault(key, f"数据源超时（>{timeout + 5:.0f}s 未返回）")
    finally:
        ex.shutdown(wait=False, cancel_futures=True)

    # ★ 多候选优先：权威源（企查查等）报"多候选"时**优先抛出**让用户选择，
    #   即使其它源（如 iFinD 对不存在/非上市主体返回空 profile）看似"有结果"。
    #   否则会静默返回一个空壳主体（实测查"孚日"返回 supplier="孚日" 的满分空壳）。
    for k, msg in failed.items():
        # 匹配多候选的多种消息：qcc 是"命中多个主体"、local 是"名称不完整，命中多个主体"、异常类名含 MultiCandidate
        if "命中多个主体" in msg or "多候选" in msg or "MultiCandidate" in msg:
            e = _rebuild_multicandidate(k, msg)
            raise e

    if not results:
        first = next(iter(failed.values()), "所有数据源均查询失败")
        raise ValueError(first)

    profile, verified = _merge(results)
    return MultiSourceResult(profile, list(results.keys()), failed, verified)


def _rebuild_multicandidate(key: str, msg: str):
    """把子线程里的 MultiCandidateError 还原成带候选列表的异常。"""
    from adapters.qcc_adapter import MultiCandidateError
    # 只有 qcc 的"命中多个主体「XX」"格式才带得出主体名；其它多候选文案
    # （如"名称不完整，命中多个主体"）不含书名号，直接退回原 ValueError，
    # 不能假定「」必然存在，否则 IndexError 会把正常的多候选分支变成 500。
    if "「" not in msg or "」" not in msg:
        return ValueError(msg)
    try:
        subj = msg.split("「")[1].split("」")[0]
    except IndexError:
        return ValueError(msg)
    # 从注册表重新跑一次以拿到候选（多候选很轻量，可接受）
    try:
        get_adapter(key).fetch_supplier(subj)
    except MultiCandidateError as e:
        return e
    except Exception:
        _log_swallow("core/aggregator.py:165")
    return ValueError(msg)


def auto_sources() -> List[str]:
    """自动选择可用数据源（按配置中心的启用状态 + 凭据 + 适配器可用性）。"""
    from core import datasource_config as dsc
    keys = dsc.available_source_keys()
    # eastmoney 与 local 的行情补充重叠：local 启用时已用内置股票代码直查东财行情，
    # 为避免 eastmoney 按全称搜失败产生噪音，此时不单独进聚合。
    if "local" in keys and "eastmoney" in keys:
        keys.remove("eastmoney")
    return keys


def _resolve_adapter(key: str):
    """按 key 解析适配器实例：内置源走注册表，自定义源走通用适配器。"""
    from core import datasource_config as dsc
    if key in dsc.load_config().get("custom", {}):
        from adapters.generic_adapter import GenericAdapter
        return GenericAdapter(custom_key=key)
    return get_adapter(key)


def _merge(results: Dict[str, SupplierProfile]) -> Tuple[SupplierProfile, Dict[str, bool]]:
    """按字段优先级合并多源结果，并计算交叉验证状态。"""
    # 用"主源"（字段最全的那个）作为基底
    primary_key = _pick_primary(results)
    base = results[primary_key]

    verified: Dict[str, bool] = {}

    # ---- registration ----
    reg_sources = [k for k, p in results.items() if p.registration]
    if reg_sources:
        # 按优先级选主，再补齐空字段
        ordered = _order_by(FIELD_PRIORITY["registration"], reg_sources)
        merged_reg = {}
        for k in reversed(ordered):   # 反向填，优先级高的最后覆盖
            for fk, fv in (results[k].registration or {}).items():
                if fv not in (None, "", []):
                    merged_reg[fk] = fv
        # 补上主源的缺失字段
        for fk, fv in (results[ordered[0]].registration or {}).items():
            if fk not in merged_reg and fv not in (None, ""):
                merged_reg[fk] = fv
        base.registration = merged_reg
        verified["registration"] = len(reg_sources) >= 2

    # ---- financials ----
    fin_sources = [k for k, p in results.items() if p.financials]
    if fin_sources:
        ordered = _order_by(FIELD_PRIORITY["financials"], fin_sources)
        merged_fin = {}
        for k in reversed(ordered):
            for fk, fv in results[k].financials.__dict__.items():
                if fv not in (None, "", []):
                    merged_fin[fk] = fv
        from core.schemas import FinancialMetrics
        base.financials = FinancialMetrics(**{
            k: v for k, v in merged_fin.items()
            if k in FinancialMetrics.__dataclass_fields__
        })
        verified["financials"] = len(fin_sources) >= 2

    # ---- market（行情：优先 eastmoney）----
    mkt_sources = [k for k, p in results.items() if p.market]
    if mkt_sources:
        ordered = _order_by(FIELD_PRIORITY["market"], mkt_sources)
        base.market = results[ordered[0]].market
        verified["market"] = len(mkt_sources) >= 2

    # ---- legal_events（合并去重）----
    all_events = []
    seen = set()
    ev_sources = [k for k, p in results.items() if p.legal_events]
    for k in _order_by(FIELD_PRIORITY["legal_events"], ev_sources):
        for e in results[k].legal_events:
            sig = (e.title, e.source)
            if sig in seen:
                continue
            seen.add(sig)
            all_events.append(e)
    if all_events:
        order = {"high": 0, "medium": 1, "low": 2}
        all_events.sort(key=lambda e: order.get(e.severity, 3))
    base.legal_events = all_events
    verified["legal_events"] = len(ev_sources) >= 2

    # ---- shareholders ----
    sh_sources = [k for k, p in results.items() if p.shareholders]
    if sh_sources:
        ordered = _order_by(FIELD_PRIORITY["shareholders"], sh_sources)
        base.shareholders = results[ordered[0]].shareholders
        verified["shareholders"] = len(sh_sources) >= 2

    # ---- news（合并去重）----
    all_news = []
    seen_n = set()
    nw_sources = [k for k, p in results.items() if p.news]
    for k in _order_by(FIELD_PRIORITY["news"], nw_sources):
        for n in results[k].news:
            if n.title in seen_n:
                continue
            seen_n.add(n.title)
            all_news.append(n)
    base.news = all_news
    verified["news"] = len(nw_sources) >= 2

    # ---- 合并来源元信息（含适配器内部子源，如 local 内部补的 eastmoney 行情）----
    used = list(results.keys())
    sub_sources = set(used)
    meta = {}
    for k in used:
        meta[k] = SOURCE_CATALOG.get(k, SourceInfo(k, k))
        for sk, si in (results[k].source_meta or {}).items():
            sub_sources.add(sk)
            if sk not in meta:
                meta[sk] = si
    base.sources = list(sub_sources)
    base.source_meta = meta

    # ---- 合并风险提示（去重）----
    alerts = []
    seen_a = set()
    for k in used:
        for a in results[k].risk_alerts:
            if a["title"] in seen_a:
                continue
            seen_a.add(a["title"])
            alerts.append(a)
    order = {"high": 0, "medium": 1, "low": 2}
    alerts.sort(key=lambda a: order.get(a.get("level", "low"), 3))
    base.risk_alerts = alerts[:10]

    # ---- 供应商主体名（用企查查/权威源的名字）----
    for k in _order_by(["qcc", "local", "wind", "eastmoney"], used):
        nm = results[k].name
        if nm:
            base.name = nm
            break

    # ---- 取数失败登记的维度（区分「调用失败」与「确实为空」）----
    # 各源失败登记取并集，但**合并结果里已经有数据、或换源取到了**的维度必须剔除：
    # 否则 qcc 股东接口失败会把 local 已取到 shareholders 的维度也标成 na，
    # 评分层剔掉该维、权重转给其余维度 → 总分被系统性抬高。
    unavail = []
    for k in used:
        for d in (getattr(results[k], "unavailable_dims", None) or []):
            if d not in unavail:
                unavail.append(d)
    if unavail:
        from core.scoring import dim_has_data
        unavail = [d for d in unavail if not dim_has_data(base, d)]
    base.unavailable_dims = unavail

    # 记录交叉验证结果供上层使用
    base.registration = base.registration or {}
    return base, verified


def _order_by(priority: List[str], keys: List[str]) -> List[str]:
    """按优先级列表排序 keys，不在优先级里的排最后。"""
    def rank(k):
        try:
            return priority.index(k)
        except ValueError:
            return len(priority)
    return sorted(keys, key=rank)


def _pick_primary(results: Dict[str, SupplierProfile]) -> str:
    """选字段最全的源作为基底。"""
    def score(item):
        p = item[1]
        s = 0
        s += 3 if p.registration else 0
        s += 3 if p.financials else 0
        s += 2 if p.legal_events else 0
        s += 1 if p.market else 0
        s += 1 if p.shareholders else 0
        # 权威源加权
        if SOURCE_CATALOG.get(item[0], SourceInfo(item[0], "")).authoritative:
            s += 1
        return s
    return max(results.items(), key=score)[0]


def catalog() -> List[dict]:
    """返回数据源目录（供前端"数据源覆盖"面板展示）。

    可用性由配置中心判定：启用 + 凭据就绪 + 适配器可用的源 available=True。
    """
    from core import datasource_config as dsc
    active = set(dsc.available_source_keys())
    cfg = dsc.load_config()
    out = []
    for key, info in SOURCE_CATALOG.items():
        st = dsc.get_source_state(key)
        out.append({
            "key": key,
            "label": info.label,
            "category": info.category,
            "authoritative": info.authoritative,
            "available": key in active,
            "enabled": st["enabled"],
        })
    # 自定义数据源
    for ck, c in cfg.get("custom", {}).items():
        out.append({
            "key": ck, "label": c.get("label", ck), "category": "自定义数据源",
            "authoritative": False, "available": ck in active,
            "enabled": bool(c.get("enabled", True)), "custom": True,
        })
    return out
