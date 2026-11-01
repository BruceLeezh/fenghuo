"""外部数据源结果缓存 —— 少调一次是一次（企查查积分 / iFinD 取数额度都很贵）。

为什么需要
----------
一次完整的供应商抓取要调多个外部工具（企查查 5 个 + iFinD 3 个）。
组合看板扫描 7 家就是几十次调用，重复点两次翻倍 —— 额度很快见底。
但工商登记、年报财务这类数据本来就是**低频变化**，短时间内重复查询纯属浪费。

本模块同时服务 **企查查**（`server` = company/risk/...）与
**同花顺 iFinD**（`server` = `ifind-stock` / `ifind-news` / ...），
key 带服务前缀，天然隔离。

设计
----
- 两层缓存：进程内存（dict）+ 磁盘 JSON（重启后依然有效）
- key = "<server>|<tool>|<规范化参数 sha1>"
- TTL 按"数据变化速度"分类：工商类长、风险类短
- **只缓存成功结果**：None / 异常不写入，避免把一次性失败固化成长期结论
- 环境变量 `QCC_CACHE=0` 可整体关闭（调试用）

统计
----
`stats()` 返回 hits / misses / saved，供界面展示"本次节省了多少次调用"。
"""
from core.quiet_log import log_swallow as _log_swallow
import hashlib
import json
import os
import threading
import time

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_PATH = os.path.join(_BASE, "data", "qcc_cache.json")
_LOCK = threading.Lock()

# 磁盘缓存上限（超过则按最久未访问淘汰），防止无限膨胀
_MAX_ENTRIES = 400
_MAX_BYTES = 6 * 1024 * 1024

HOUR = 3600
DAY = 24 * HOUR

TTL_LONG = 30 * DAY     # 实体锚定：企业名 → 信用代码，基本不变
TTL_MID = 7 * DAY       # 工商登记 / 年报财务 / 股东：低频变化
TTL_SHORT = 12 * HOUR   # 风险扫描 / 司法风险：变化相对快
TTL_DEFAULT = 24 * HOUR

# 工具 → TTL（按数据变化速度，不是"越短越安全"，而是按实际更新频率）
_TOOL_TTL = {
    # 实体锚定（名称/代码 → 主体）
    "get_company_by_query": TTL_LONG,
    "get_company_profile": TTL_MID,
    "verify_company_accuracy": TTL_LONG,
    # 工商登记类
    "get_company_registration_info": TTL_MID,
    "get_change_records": TTL_MID,
    "get_branches": TTL_MID,
    "get_contact_info": TTL_MID,
    # 财务类（年报，按年更新）
    "get_financial_data": TTL_MID,
    "get_annual_reports": TTL_MID,
    # 股权 / 关联方
    "get_shareholder_info": TTL_MID,
    "get_actual_controller": TTL_MID,
    "get_beneficial_owners": TTL_MID,
    "get_external_investments": TTL_MID,
    "get_key_personnel": TTL_MID,
    # 风险类（司法/处罚，变化较快）
    "get_company_risk_scan": TTL_SHORT,
    "get_company_related_risk_scan": TTL_SHORT,
    "get_dishonest_info": TTL_SHORT,
    "get_judgment_debtor_info": TTL_SHORT,
    "get_administrative_penalty": TTL_SHORT,
    "get_business_exception": TTL_SHORT,
    "get_serious_violation": TTL_SHORT,
    "get_judicial_documents": TTL_SHORT,
    "get_equity_freeze": TTL_SHORT,
    "get_tax_arrears_notice": TTL_SHORT,
    "get_case_filing_info": TTL_SHORT,
    # 招投标（外包专项，变化中等）
    "search_companies": TTL_MID,
    "search_company_tenders": TTL_DEFAULT,
    "get_tender_detail": TTL_LONG,
    # ---- 同花顺 iFinD ----
    "stock_highfreq_quotes": int(0.5 * HOUR),   # 实时行情：短缓存，既要新鲜又要防重复打
    "get_stock_financials": TTL_MID,            # 财报（季度更新）
    "get_stock_shareholders": TTL_MID,
    "get_stock_info": TTL_MID,
    "get_stock_summary": TTL_MID,
    "get_stock_performance": TTL_SHORT,         # 日频行情/技术指标
    "get_risk_indicators": TTL_SHORT,
    "get_stock_events": TTL_SHORT,
    "get_esg_data": TTL_MID,
    "search_stocks": TTL_SHORT,
    "search_news": TTL_DEFAULT,                 # 资讯
    "search_notice": TTL_DEFAULT,
    "search_trending_news": TTL_SHORT,
    "search_edb": TTL_MID,
    "get_edb_data": TTL_MID,
    # ---- 万得 Wind ----
    "get_stock_fundamentals": TTL_MID,            # 财务基本面（季度更新）
    "get_stock_basicinfo": TTL_LONG,              # 静态档案（基本不变）
    "get_stock_price_indicators": int(0.5 * HOUR),  # 行情快照：短缓存，防重复打
    "get_stock_quote": int(0.5 * HOUR),
    "get_stock_kline": TTL_DEFAULT,
    "get_stock_technicals": TTL_SHORT,
    "get_risk_metrics": TTL_SHORT,
    "get_stock_equity_holders": TTL_MID,
    # search_stocks / get_stock_events 与 iFinD 同名，复用上方 TTL
}

_STATS_LOCK = threading.Lock()


def _bump(name: str, n: int = 1) -> None:
    """线程安全地累加统计计数（无锁自增会丢数、低报缓存命中）。"""
    with _STATS_LOCK:
        _STATS[name] = _STATS.get(name, 0) + n


_STATS = {"hits": 0, "misses": 0, "writes": 0, "skipped_none": 0}


def cache_enabled() -> bool:
    """缓存总开关（QCC_CACHE=0 关闭）。"""
    return (os.environ.get("QCC_CACHE", "1") or "1").strip() not in ("0", "false", "False")


def ttl_for(tool: str) -> int:
    """取某工具的缓存有效期（秒）。"""
    return _TOOL_TTL.get(tool, TTL_DEFAULT)


def make_key(server: str, tool: str, args: dict) -> str:
    """构造缓存 key：参数规范化后取 sha1，避免顺序/空白差异导致的假性未命中。"""
    norm = json.dumps(args or {}, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=str)
    h = hashlib.sha1(norm.encode("utf-8")).hexdigest()[:16]
    return f"{server}|{tool}|{h}"


# ---------------------------------------------------------------------------
# 磁盘读写
# ---------------------------------------------------------------------------
def _load() -> dict:
    with _LOCK:
        if not os.path.exists(CACHE_PATH):
            return {}
        try:
            with open(CACHE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}


def _save(data: dict):
    """写盘（带容量控制：超限时淘汰最久未访问的条目）。"""
    with _LOCK:
        try:
            if len(data) > _MAX_ENTRIES:
                # 按 last_hit 由旧到新排序，保留最近的
                ordered = sorted(data.items(), key=lambda kv: kv[1].get("hit_at", 0), reverse=True)
                data = dict(ordered[:_MAX_ENTRIES])
            payload = json.dumps(data, ensure_ascii=False)
            if len(payload.encode("utf-8")) > _MAX_BYTES:
                ordered = sorted(data.items(), key=lambda kv: kv[1].get("hit_at", 0), reverse=True)
                while ordered and len(json.dumps(dict(ordered), ensure_ascii=False).encode("utf-8")) > _MAX_BYTES:
                    ordered.pop()
                data = dict(ordered)
                payload = json.dumps(data, ensure_ascii=False)
            os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
            with open(CACHE_PATH, "w", encoding="utf-8") as f:
                f.write(payload)
        except Exception:
            _log_swallow("integrations/qcc_cache.py:175")


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------
def get(server: str, tool: str, args: dict):
    """取缓存（过期或不存在返回 None）。"""
    if not cache_enabled():
        return None
    key = make_key(server, tool, args)
    # 一次读盘拿全量，既能取 entry 又能原地更新 hit_at —— 原实现命中时
    # 要 _load() 两次 + _save() 一次，单次命中即放大三倍 I/O。
    data = _load()
    entry = data.get(key)
    if not entry:
        _bump("misses")
        return None
    ts = entry.get("ts", 0)
    if time.time() - ts > ttl_for(tool):
        _bump("misses")
        return None
    # 命中：更新访问时间（用于容量淘汰）
    entry["hit_at"] = time.time()
    _bump("hits")
    _save(data)
    return entry.get("data")


def put(server: str, tool: str, args: dict, result):
    """写缓存（只写成功结果）。"""
    if not cache_enabled():
        return
    if result is None:
        _bump("skipped_none")  # 失败结果不缓存，避免把一次性失败固化
        return
    key = make_key(server, tool, args)
    data = _load()
    now = time.time()
    data[key] = {"ts": now, "hit_at": now, "server": server, "tool": tool,
                 "args": args, "data": result}
    _bump("writes")
    _save(data)


def stats() -> dict:
    """缓存统计（含磁盘条目数与按工具分布）。"""
    data = _load()
    by_tool = {}
    for e in data.values():
        t = e.get("tool", "?")
        by_tool[t] = by_tool.get(t, 0) + 1
    total_calls = _STATS["hits"] + _STATS["misses"]
    return {
        "enabled": cache_enabled(),
        "entries": len(data),
        "hits": _STATS["hits"],
        "misses": _STATS["misses"],
        "saved_calls": _STATS["hits"],       # 每次命中 = 省下一次企查查调用
        "hit_rate": round(_STATS["hits"] / total_calls * 100, 1) if total_calls else 0.0,
        "by_tool": by_tool,
        "path": CACHE_PATH,
    }


def clear() -> int:
    """清空缓存（强制下次全量拉取），返回清除条目数。"""
    data = _load()
    n = len(data)
    _save({})
    try:
        if os.path.exists(CACHE_PATH):
            os.remove(CACHE_PATH)
    except OSError:
        _log_swallow("integrations/qcc_cache.py:249")
    return n


def purge_expired() -> int:
    """清理已过期条目，返回清理数。"""
    data = _load()
    now = time.time()
    keep = {k: v for k, v in data.items()
            if now - v.get("ts", 0) <= ttl_for(v.get("tool", ""))}
    removed = len(data) - len(keep)
    if removed:
        _save(keep)
    return removed
