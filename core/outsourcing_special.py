"""外包专项 —— 服务提供商的外包过往记录与专项风险分析。

定位
----
工商/司法数据回答"这家公司在不在、正不正规"；
本模块回答一个更贴近外包实务的问题：

    "这家供应商给金融机构做过哪些外包？做得怎么样？有没有隐患？"

数据来源：企查查「标讯」数据域（真实招投标与中标公告），是公开可核验的
履约履历，不同于供应商自己申报的案例。

监管依据
--------
第18条  服务提供商的选择与评审（招标/竞争性谈判等）
第22条  不得转包或变相转包（分包风险）
第29条  服务评价结果作为后续准入的重要参考依据
第30条(五) 服务水平下降 — 从中标结构可提前感知
第28条  关联方风险 — 客户/项目高度集中是关联风险的信号
第33条  集中度风险 — 客户集中于少数机构同样是集中度问题

专项风险信号（本模块核心产出）
------------------------------
1. 单一来源占比偏高   —— 竞争不充分，议价与替代能力弱（第18条）
2. 客户高度集中       —— 少数机构贡献大部分中标额，经营波动会传导（第28/33条）
3. 中标规模骤降       —— 近期中标金额显著下滑，可能经营收缩或能力下降（第30条）
4. 非金融行业占比高   —— 金融行业经验不足，服务金融外包的适配性存疑（第18条）
5. 中标频次异常       —— 短期内高频小额外包，可能是转包/拆包信号（第22条）
6. 项目名称缺少延续性 —— 与过往合作机构的项目缺乏延续，客户留存存疑（第29条）
"""
from core.quiet_log import log_swallow as _log_swallow
import os
import re
import threading
from collections import Counter
from datetime import datetime

from core.jsonstore import read_json, write_json

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(_BASE, "data", "special_records")
_LOCK = threading.Lock()

# 金融机构关键词（用于识别"客户是否金融行业"）
FIN_KEYWORDS = [
    "银行", "信用社", "农商行", "农信", "村镇银行", "证券", "保险", "基金",
    "信托", "消费金融", "金融租赁", "理财", "资产管理", "券商", "期货",
    "支付", "银联", "金融科技", "财务公司", "小贷",
]

# 外包/信息科技相关项目关键词（用于筛选"哪些标讯与外包相关"）
OUTSOURCE_KEYWORDS = [
    "软件", "系统", "平台", "开发", "运维", "运行维护", "测试", "信息技术",
    "信息科技", "IT", "数字化", "数据", "云", "信创", "改造", "升级",
    "维护服务", "技术支持", "技术服务", "咨询", "集成", "安全", "灾备",
    "架构", "渠道", "核心", "手机银行", "网上银行", "信贷", "支付", "影像",
]

# 招采方式：视为"非竞争性"的方式
NON_COMPETITIVE = ["单一来源", "直接采购", "询价", "竞争性磋商"]


# ---------------------------------------------------------------------------
# 缓存
# ---------------------------------------------------------------------------
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _cache_path(supplier_id: str) -> str:
    """缓存文件名只允许字母/数字/下划线/连字符。

    supplier_id 来自请求（POST 表单字段与 DELETE 路径段），直接拼进文件名会被
    `../` 穿越到 data/ 目录删改任意文件，因此这里统一做白名单校验。
    """
    sid = str(supplier_id or "")
    if not _SAFE_ID.match(sid):
        raise ValueError(f"非法的供应商编号「{sid}」")
    return os.path.join(CACHE_DIR, f"{sid}.json")


def load_cache(supplier_id: str) -> dict:
    """读取某供应商的外包专项缓存。"""
    p = _cache_path(supplier_id)
    if not os.path.exists(p):
        return {}
    data = read_json(p, {})
    return data if isinstance(data, dict) else {}


def save_cache(supplier_id: str, payload: dict) -> dict:
    """写入缓存（带抓取时间戳，原子替换）。"""
    with _LOCK:
        payload = {**payload, "cached_at": datetime.now().isoformat(timespec="seconds")}
        write_json(_cache_path(supplier_id), payload)
        return payload


def clear_cache(supplier_id: str) -> bool:
    p = _cache_path(supplier_id)
    if os.path.exists(p):
        os.remove(p)
        return True
    return False


# ---------------------------------------------------------------------------
# 采集：企查查标讯 → 结构化外包履历
# ---------------------------------------------------------------------------
def _search_terms(name: str) -> list:
    """生成检索关键词：全称 → 去括号/后缀的简称。

    企查查标讯库对全称的匹配较严格（括号、"(集团)"等后缀会漏配），
    因此退化为简称再试一次，能显著提高命中率。
    """
    n = (name or "").strip()
    terms = [n]
    short = re.sub(r"[（(].*?[)）]", "", n)
    short = short.replace("股份有限公司", "").replace("有限责任公司", "")
    short = short.replace("有限公司", "").replace("集团", "").strip()
    if short and short != n:
        terms.append(short)
    # 去重保序
    seen, out = set(), []
    for t in terms:
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _probe_error(tc, tried: list) -> str:
    """显式探测一次以区分"真未匹配"与"鉴权/额度失败"。

    _safe() 设计上把异常吞成 None（避免单个数据域失败打断整体），
    但这会让"Token 失效/额度耗尽"伪装成"查无此企业"。这里故意让异常冒泡，
    只为了拿到可读原因。
    """
    from integrations.qcc_mcp import explain_error
    try:
        tc.call_tool("search_companies", {"keywords": tried[-1:]})
        return ""
    except Exception as e:
        return f"企查查标讯数据源不可用：{explain_error(e)}"


def _is_fin(text: str) -> bool:
    return any(k in (text or "") for k in FIN_KEYWORDS)


def _is_outsource(text: str) -> bool:
    t = (text or "").upper()
    return any(k.upper() in t for k in OUTSOURCE_KEYWORDS)


def _amount(v) -> float:
    """把金额字段转成 float（单位：元）。

    需处理真实文档里的中文单位写法：`1.2亿` / `5000万元` / `3,200.5`。
    原实现只 replace("元")，遇到"亿/万"会 ValueError 静默归零，
    导致金额类集中度被算成 0 且无任何提示。
    """
    if v is None or v == "":
        return 0.0
    s = str(v).strip().replace(",", "").replace("，", "")
    mult = 1.0
    # 顺序要紧：先匹配"亿元/亿"，再"万元/万"，避免"亿"被"万"抢先
    if "亿" in s:
        mult = 1e8
        s = s.replace("亿元", "").replace("亿", "")
    elif "万" in s:
        mult = 1e4
        s = s.replace("万元", "").replace("万", "")
    s = s.replace("元", "").strip()
    try:
        return float(s) * mult
    except (TypeError, ValueError):
        return 0.0


def _first_name(lst) -> str:
    """从 [{企业名称:...}] 取首个名称。"""
    if isinstance(lst, list) and lst:
        it = lst[0]
        if isinstance(it, dict):
            return it.get("企业名称") or it.get("name") or ""
    return ""


def _names(lst) -> list:
    out = []
    if isinstance(lst, list):
        for it in lst:
            if isinstance(it, dict):
                n = it.get("企业名称") or it.get("name")
                if n:
                    out.append(n)
    return out


def fetch_special(supplier_name: str, supplier_id: str = "",
                  max_records: int = 40) -> dict:
    """抓取某供应商的外包过往记录（真实标讯）。

    返回结构化档案；失败时返回 {"ok": False, "error": ...} 而非抛异常，
    以便界面明确提示"专项数据未取到"。
    """
    from integrations.qcc_mcp import QCCTenderClient, explain_error

    out = {
        "ok": False,
        "supplier": supplier_name,
        "supplier_id": supplier_id,
        "enterprise": {},
        "records": [],
        "error": "",
    }

    try:
        tc = QCCTenderClient()
        # 1) 定位企业主体（用简称更易命中，全称常因括号/后缀差异漏配）
        comps = []
        tried = []
        for kw in _search_terms(supplier_name):
            tried.append(kw)
            sr = tc.search_company(kw) or {}
            comps = sr.get("企业列表") or []
            if comps:
                break
        if not comps:
            # _safe() 会把网络/鉴权异常吞成 None，这里补一次显式探测，避免误报"未匹配"
            probe = _probe_error(tc, tried)
            out["error"] = probe or f"企查查标讯库未匹配到该企业主体（已尝试关键词：{'、'.join(tried)}）"
            return out
        comp = comps[0]
        eid = comp.get("企业ID")
        if not eid:
            out["error"] = "未取得企业ID，无法检索标讯"
            return out

        out["enterprise"] = {
            "name": comp.get("企业名称"),
            "credit_code": comp.get("统一社会信用代码"),
            "industry": (comp.get("行业分类") or [""])[0] if comp.get("行业分类") else "",
            "region": comp.get("省市区"),
            "status": comp.get("登记状态"),
            "roles": comp.get("标讯角色") or [],
            "related": int(comp.get("相关标讯数") or 0),
            "as_buyer": int(comp.get("招采数") or 0),
            "as_bidder": int(comp.get("投标数") or 0),
            "as_winner": int(comp.get("中标数") or 0),
            "as_agent": int(comp.get("代理数") or 0),
        }

        # 2) 拉取中标记录（作为履约履历的主证据）
        wr = tc.company_tenders(eid, role="3", page_size=max_records) or {}
        raw = wr.get("标讯列表") or []
        out["query_summary"] = wr.get("查询摘要") or {}
        out["total_hits"] = int((wr.get("查询摘要") or {}).get("命中总数") or 0)

        records = []
        for r in raw:
            buyer = _first_name(r.get("招采单位"))
            title = r.get("标题") or ""
            prods = r.get("相关产品") or []
            prod_txt = " ".join([str(x) for x in prods])
            amt = _amount(r.get("中标金额（元）")) or _amount(r.get("预算金额（元）"))
            records.append({
                "tender_id": r.get("标讯ID") or "",
                "title": title,
                "info_type": r.get("信息类型") or "",
                "status": r.get("公告子状态") or "",
                "region": r.get("省市区") or "",
                "buyer": buyer,
                "buyer_is_fin": _is_fin(buyer),
                "winner": _first_name(r.get("中标单位")),
                "bid_type": r.get("招采类型") or "",
                "method": r.get("招采方式") or "",
                "project_no": r.get("项目编号") or "",
                "amount": amt,
                "publish_date": (r.get("发布时间") or "")[:10],
                "products": prods,
                "industry": (r.get("标讯行业分类") or [""])[0] if r.get("标讯行业分类") else "",
                "is_outsource": _is_outsource(f"{title} {prod_txt}"),
                "non_competitive": (r.get("招采方式") or "") in NON_COMPETITIVE,
                "role": "中标单位",
            })

        out["records"] = records
        out["ok"] = True
        return out

    except Exception as e:
        out["error"] = f"外包过往记录获取失败：{explain_error(e)}"
        return out


# ---------------------------------------------------------------------------
# 分析：从记录中提取专项风险信号
# ---------------------------------------------------------------------------
def analyze_special(payload: dict) -> dict:
    """对抓取到的外包过往记录做专项分析。"""
    recs = payload.get("records") or []
    ent = payload.get("enterprise") or {}

    if not recs:
        return {
            "has_data": False,
            "record_count": 0,
            "signals": [],
            "summary": {},
            "insight": "尚未获取到外包过往记录，请点击「刷新外包记录」同步企查查标讯数据。",
        }

    total = len(recs)
    amounts = [r["amount"] for r in recs if r.get("amount")]
    total_amount = sum(amounts)

    # ---- 结构化统计 ----
    buyers = Counter(r["buyer"] for r in recs if r.get("buyer"))
    fin_recs = [r for r in recs if r.get("buyer_is_fin")]
    out_recs = [r for r in recs if r.get("is_outsource")]
    nc_recs = [r for r in recs if r.get("non_competitive")]

    fin_amount = sum(r["amount"] for r in fin_recs if r.get("amount"))
    nc_amount = sum(r["amount"] for r in nc_recs if r.get("amount"))

    # 客户集中度：前 3 大客户占比
    buyer_amount = Counter()
    for r in recs:
        if r.get("buyer") and r.get("amount"):
            buyer_amount[r["buyer"]] += r["amount"]
    top3 = buyer_amount.most_common(3)
    top3_amount = sum(a for _, a in top3)
    top3_ratio = round(top3_amount / total_amount * 100, 1) if total_amount else 0.0
    buyer_count = len(buyers)

    # 金额波动：**等窗口**比较最近一段 vs 紧邻的上一段。
    # ⚠️ 原实现用「最近 6 条」对比「此前全部记录」：两段样本量不对等，记录数一多，
    #    金额完全稳定也会被算成大幅下滑（实测 40 条等额记录得出 -82.4% 的 high 警报）。
    dated = sorted([r for r in recs if r.get("publish_date")], key=lambda r: r["publish_date"])
    recent_amt = earlier_amt = 0
    amt_trend = None
    win = min(6, len(dated) // 2)
    if win >= 2:
        recent_amt = sum(r["amount"] for r in dated[-win:])
        earlier_amt = sum(r["amount"] for r in dated[-2 * win:-win])
        if earlier_amt > 0:
            amt_trend = round((recent_amt - earlier_amt) / earlier_amt * 100, 1)

    # ---- 专项风险信号 ----
    signals = []

    # 1) 单一来源占比
    nc_ratio = round(len(nc_recs) / total * 100, 1) if total else 0
    if nc_ratio >= 50:
        signals.append({
            "key": "non_competitive_high", "level": "high",
            "label": f"非竞争性采购占比 {nc_ratio}%",
            "detail": f"{len(nc_recs)}/{total} 条记录为单一来源/直接采购等非竞争性方式",
            "clause": "第18条", "deduct": 8,
            "hint": "大量项目以非竞争性方式中标，可能存在议价能力失衡或竞争不充分",
        })
    elif nc_ratio >= 30:
        signals.append({
            "key": "non_competitive_mid", "level": "warn",
            "label": f"非竞争性采购占比 {nc_ratio}%",
            "detail": f"{len(nc_recs)}/{total} 条记录为非竞争性方式",
            "clause": "第18条", "deduct": 4,
            "hint": "非竞争性采购比例偏高，建议关注采购方式的合规性",
        })

    # 2) 客户集中度
    if top3_ratio >= 70 and buyer_count >= 2:
        signals.append({
            "key": "client_concentration", "level": "high",
            "label": f"前 3 大客户占中标额 {top3_ratio}%",
            "detail": "；".join(f"{n}（{a/10000:.0f}万）" for n, a in top3),
            "clause": "第28条(关联方) / 第33条(集中度)", "deduct": 6,
            "hint": "客户高度集中在少数机构，供应商经营波动会直接传导至本行服务",
        })
    elif top3_ratio >= 50 and buyer_count >= 3:
        signals.append({
            "key": "client_concentration_mid", "level": "warn",
            "label": f"前 3 大客户占中标额 {top3_ratio}%",
            "detail": "；".join(f"{n}（{a/10000:.0f}万）" for n, a in top3),
            "clause": "第28条(关联方) / 第33条(集中度)", "deduct": 3,
            "hint": "客户集中度中等偏高，建议关注客户结构稳定性",
        })

    # 3) 中标金额骤降（等窗口环比：最近 win 条 vs 紧邻的前 win 条）
    if amt_trend is not None and amt_trend <= -50:
        signals.append({
            "key": "amount_drop", "level": "high",
            "label": f"近期中标金额环比下滑 {abs(amt_trend)}%",
            "detail": f"最近 {win} 条合计 {recent_amt/10000:.0f} 万元，"
                      f"前 {win} 条合计 {earlier_amt/10000:.0f} 万元",
            "clause": "第30条(五)", "deduct": 8,
            "hint": "中标规模大幅收缩可能反映经营收缩或市场竞争力下降",
        })

    # 4) 金融行业经验不足
    fin_ratio = round(len(fin_recs) / total * 100, 1) if total else 0
    if fin_ratio < 30 and total >= 5:
        signals.append({
            "key": "low_finance_exp", "level": "warn",
            "label": f"金融机构客户占比仅 {fin_ratio}%",
            "detail": f"{len(fin_recs)}/{total} 条记录的采购方为金融机构",
            "clause": "第18条", "deduct": 5,
            "hint": "金融行业服务经验相对不足，需重点评估其对监管要求与业务连续性的理解",
        })

    # 5) 中标频次异常（短周期高频小额）
    if total >= 10:
        small = [r for r in recs if 0 < r.get("amount", 0) < 500000]
        if len(small) / total >= 0.6:
            signals.append({
                "key": "freq_small", "level": "warn",
                "label": f"小额项目占比 {round(len(small)/total*100)}%",
                "detail": f"{len(small)}/{total} 条记录中标金额低于 50 万元",
                "clause": "第22条", "deduct": 4,
                "hint": "高频小额项目偏多，需关注是否存在转包或合同拆包情形",
            })

    # 6) 外包相关性
    out_ratio = round(len(out_recs) / total * 100, 1) if total else 0
    if out_ratio < 50 and total >= 5:
        signals.append({
            "key": "low_outsource_relevance", "level": "info",
            "label": f"信息科技类项目占比仅 {out_ratio}%",
            "detail": f"{len(out_recs)}/{total} 条记录属信息科技/软件类外包项目",
            "clause": "第18条", "deduct": 0,
            "hint": "中标记录中信息科技类占比较低，需核实其主营业务与外包服务能力的匹配度",
        })

    signals.sort(key=lambda s: {"high": 0, "warn": 1, "info": 2}.get(s["level"], 3))

    # ---- 总体画像 ----
    levels = [r["industry"] for r in recs if r.get("industry")]
    ind_top = Counter(levels).most_common(2)

    summary = {
        "record_count": total,
        "total_hits": payload.get("total_hits") or ent.get("as_winner") or total,
        "analyzed": total,
        "total_amount": total_amount,
        "total_amount_display": f"{total_amount/10000:,.0f} 万元" if total_amount else "未披露",
        "avg_amount": round(total_amount / len(amounts)) if amounts else 0,
        "buyer_count": buyer_count,
        "top_buyers": [{"name": n, "count": c, "amount": buyer_amount.get(n, 0)}
                       for n, c in buyers.most_common(5)],
        "fin_ratio": fin_ratio,
        "fin_count": len(fin_recs),
        "outsource_ratio": out_ratio,
        "outsource_count": len(out_recs),
        "non_competitive_ratio": nc_ratio,
        "non_competitive_count": len(nc_recs),
        "top3_ratio": top3_ratio,
        "amount_trend": amt_trend,
        "industries": [{"name": n, "count": c} for n, c in ind_top],
        "date_range": [dated[0]["publish_date"], dated[-1]["publish_date"]] if dated else [],
    }

    # ---- 一句话洞察 ----
    high = [s for s in signals if s["level"] == "high"]
    if high:
        insight = f"专项分析发现 {len(high)} 项高风险信号：" + "；".join(s["label"] for s in high)
    elif signals:
        insight = f"专项分析发现 {len(signals)} 项需关注信号，未发现高风险项"
    else:
        insight = "外包过往记录结构健康，未发现专项风险信号"

    return {
        "has_data": True,
        "record_count": total,
        "summary": summary,
        "signals": signals,
        "deduct": sum(s["deduct"] for s in signals),
        "insight": insight,
        # ⚠️ 时间线是"可选展示层"，不能因为某条记录缺 title/method 等字段就 500
        #    （抓取源字段并不齐全）。一律 .get，缺就留空。
        "timeline": [{
            "date": r.get("publish_date") or "", "title": r.get("title") or "",
            "buyer": r.get("buyer") or "",
            "amount": r.get("amount") or 0, "method": r.get("method") or "",
            "is_fin": bool(r.get("buyer_is_fin")),
            "is_outsource": bool(r.get("is_outsource")),
        } for r in sorted(recs, key=lambda x: x.get("publish_date") or "", reverse=True)[:20]],
    }


def _snapshot_payload(supplier_name: str, supplier_id: str, reason: str = "") -> dict:
    """实时抓取不可用时的离线兜底：返回打包样本并标注 source。

    明确标注 offline=True / source="snapshot"，前端据此显示「离线样本」徽章，
    避免把演示样本误认为实时抓取结果（合规演示的关键诚实性）。
    """
    try:
        from core import special_snapshot
        if special_snapshot.has_snapshot(supplier_id):
            snap = special_snapshot.normalize_snapshot(supplier_id)
            if snap:
                snap["source"] = "snapshot"
                if reason:
                    snap["offline_reason"] = reason
                return snap
    except Exception:
        _log_swallow("core/outsourcing_special.py:496")
    return {}


def get_special(supplier_name: str, supplier_id: str, refresh: bool = False) -> dict:
    """对外主入口：取缓存或刷新抓取，失败时回退到离线样本。

    优先级：实时/历史缓存 → 实时抓取 → 离线样本兜底 → 空结果（附错误原因）
    """
    if not refresh and supplier_id:
        cached = load_cache(supplier_id)
        if cached and cached.get("ok"):
            cached.setdefault("source", "snapshot" if cached.get("offline") else "live")
            return {"raw": cached, "analysis": analyze_special(cached),
                    "cached": True, "source": cached.get("source", "live"),
                    "offline": bool(cached.get("offline"))}

    payload = fetch_special(supplier_name, supplier_id)
    if payload.get("ok"):
        payload["source"] = "live"
        if supplier_id:
            save_cache(supplier_id, payload)
        return {"raw": payload, "analysis": analyze_special(payload),
                "cached": False, "source": "live", "offline": False}

    # 实时失败 —— 尝试离线样本兜底
    reason = payload.get("error", "")
    snap = _snapshot_payload(supplier_name, supplier_id, reason)
    if snap:
        return {"raw": snap, "analysis": analyze_special(snap),
                "cached": False, "source": "snapshot", "offline": True,
                "live_error": reason}

    return {"raw": payload, "analysis": analyze_special(payload),
            "cached": False, "source": "none", "offline": False}
