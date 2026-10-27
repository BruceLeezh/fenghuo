"""集中度风险量化 —— 《办法》第28条（关联外包）、第33条（集中度风险）。

监管要求（第33条）：
  "银行保险机构应当关注信息科技外包集中度风险，防范因个别服务提供商
   无法履约而对本机构信息科技活动产生重大影响。"

监管要求（第28条）：
  "银行保险机构应当推动母公司或集团将外包服务质量纳入对提供商的业绩评价范围，
   并建立外包服务重大事件问责机制。"

本模块解决三个量化问题：
  1. **单供应商集中度**：某供应商占本行信息科技外包总金额的比重
  2. **集团/关联方合并集中度**：同一集团或同一实际控制人下多家供应商合并占比
     （关键：只看单个法人会严重低估——三家子公司各占 8% 看似分散，
      实为同一集团 24%，已构成重大集中）
  3. **业务条线集中度**：单一供应商对某条业务线的独占程度
     （一家供应商支撑全部手机银行，即使金额占比不高，业务连续性风险也极高）

另附行业通用的集中度指标 HHI（赫芬达尔指数）：
  HHI = Σ(份额²)，分值 0~10000。＜1500 竞争充分，1500-2500 中度集中，＞2500 高度集中。

本模块的全部"金额"来自供应商台账的 `annual_amount` 字段（年度外包金额，万元），
由业务人员在台账中维护；未维护金额的供应商按 0 处理并在结果中明确标注，
不臆测、不补数。
"""
from typing import Dict, List, Optional

# 集中度阈值（参考监管惯例与银行业外包管理实务）
SINGLE_WARN = 20.0        # 单一供应商占比警戒线（%）
SINGLE_HIGH = 30.0        # 单一供应商占比高风险线（%）
GROUP_WARN = 20.0         # 同一集团合并占比警戒线（%）
GROUP_HIGH = 30.0         # 同一集团合并占比高风险线（%）
LINE_DOMINANT = 60.0      # 单一供应商对某业务条线的支配线（%）

# 业务条线重要性（决定"独家支撑"是否构成实质风险）
# 依据《办法》第13条对重要信息系统的口径：直接影响实时服务、账务准确性、
# 集中处理重要数据的条线，单点故障后果最严重。
LINE_CRITICALITY = {
    "核心账务系统": "high", "核心系统": "high", "支付系统": "high",
    "手机银行": "high", "网上银行": "high", "数据中心运维": "high",
    "信贷系统": "medium", "理财系统": "medium", "网络运维": "medium",
    "客服中心": "medium", "集中作业": "medium",
    "影像平台": "low",
}


def _line_criticality(line: str) -> str:
    """业务条线重要性；未知条线按 medium 保守处理。"""
    return LINE_CRITICALITY.get(line, "medium")

# HHI 分档
def _hhi_level(hhi: float) -> tuple:
    if hhi <= 0:
        return "无数据", "normal"
    if hhi < 1500:
        return "竞争充分", "normal"
    if hhi <= 2500:
        return "中度集中", "warn"
    return "高度集中", "high"


def _group_key(item: dict) -> str:
    """归集到集团/关联方。

    优先级：显式声明的 group > 名称推断的集团前缀 > 自身名称（视为独立主体）。
    名称推断仅做保守合并：取"XX集团""XX控股"等明显标志，
    以及对同名主体去掉括号后缀后的主干名，避免误合并。
    """
    g = (item.get("group") or "").strip()
    if g:
        return g
    name = (item.get("name") or "").strip()
    if not name:
        return "未知主体"
    # 保守推断：名称中若含"集团/控股"，以它为主体名
    for kw in ("集团", "控股"):
        idx = name.find(kw)
        if idx > 0:
            return name[: idx + len(kw)]
    # 去掉括号后缀（如"XX（武汉）有限公司"→"XX"）后再判断是否同名主干
    base = name.split("（")[0].split("(")[0].strip()
    return base or name


def build_concentration() -> Dict:
    """扫描台账，输出集中度风险量化视图。"""
    from core import roster as roster_mod

    items = roster_mod.load_roster()
    total_amt = 0.0
    missing_amount = []

    rows: List[dict] = []
    for it in items:
        amt = it.get("annual_amount")
        try:
            amt = float(amt) if amt not in (None, "") else 0.0
        except (TypeError, ValueError):
            amt = 0.0
        if amt <= 0:
            # 负数/缺失金额不进分母：否则总额被拉低，其余供应商 share 虚高
            # （甚至 >100%），单家与集团阈值会被误触发。
            missing_amount.append(it.get("name", ""))
            amt = 0.0
        total_amt += amt
        rows.append({
            "id": it.get("id", ""),
            "name": it.get("name", ""),
            "group": _group_key(it),
            "category": it.get("category", ""),
            "important": bool(it.get("important")),
            "important_outsourcing": bool(it.get("important")),
            "annual_amount": amt,
            "business_lines": it.get("business_lines") or [],
        })

    # ---- 1. 单供应商占比 ----
    suppliers = []
    for r in rows:
        share = (r["annual_amount"] / total_amt * 100.0) if total_amt > 0 else 0.0
        risk = "normal"
        if share >= SINGLE_HIGH:
            risk = "high"
        elif share >= SINGLE_WARN:
            risk = "warn"
        suppliers.append({**r, "share": round(share, 2), "risk": risk})
    suppliers.sort(key=lambda x: -x["share"])

    # ---- 2. 集团/关联方合并占比 ----
    gmap: Dict[str, dict] = {}
    for r in rows:
        g = r["group"]
        d = gmap.setdefault(g, {"group": g, "amount": 0.0, "members": []})
        d["amount"] += r["annual_amount"]
        d["members"].append(r["name"])
    groups = []
    for g, d in gmap.items():
        share = (d["amount"] / total_amt * 100.0) if total_amt > 0 else 0.0
        risk = "normal"
        if share >= GROUP_HIGH:
            risk = "high"
        elif share >= GROUP_WARN:
            risk = "warn"
        # 仅当该集团含 ≥2 个法人时，"合并"才有意义，单独标注
        merged = len(d["members"]) >= 2
        groups.append({
            "group": g,
            "amount": round(d["amount"], 2),
            "share": round(share, 2),
            "risk": risk,
            "members": d["members"],
            "is_merged": merged,
        })
    groups.sort(key=lambda x: -x["share"])

    # ---- 3. 业务条线集中度 ----
    line_map: Dict[str, Dict[str, float]] = {}
    for r in rows:
        for bl in r["business_lines"]:
            if not bl:
                continue
            line_map.setdefault(bl, {})
            line_map[bl][r["name"]] = line_map[bl].get(r["name"], 0.0) + r["annual_amount"]
    lines = []
    for bl, m in line_map.items():
        tot = sum(m.values())
        top_name, top_amt = (max(m.items(), key=lambda kv: kv[1]) if m else ("", 0.0))
        share = (top_amt / tot * 100.0) if tot > 0 else 0.0
        # 业务条线按"有效支撑家数"判断独占：金额 >0 的家数 ≤1 即单点故障
        # （原写法 `<=1 and len(m)==1` 后半句吞掉前半句，登记了两家但其中一家金额
        #  缺失时会被当成"已有备份"，重要条线由 high 掉成 warn）
        effective = [k for k, v in m.items() if v > 0]
        solo = len(effective) <= 1
        crit = _line_criticality(bl)
        # 风险定级：独家支撑 + 条线重要性 共同决定
        risk = "normal"
        if solo and crit == "high":
            risk = "high"        # 重要系统被独家支撑 → 单点故障后果严重
        elif solo and crit == "medium":
            risk = "warn"
        elif solo:
            risk = "low"         # 次要系统独家支撑，风险可控
        elif share >= LINE_DOMINANT:
            risk = "warn"
        lines.append({
            "line": bl,
            "vendors": len(m),
            "top_vendor": top_name,
            "top_share": round(share, 2),
            "total_amount": round(tot, 2),
            "solo": solo,
            "criticality": crit,
            "risk": risk,
        })
    _crit_order = {"high": 0, "medium": 1, "low": 2}
    lines.sort(key=lambda x: (_crit_order.get(x["criticality"], 1), -x["top_share"]))

    # ---- 4. HHI 指数 ----
    hhi = sum((s["share"] ** 2) for s in suppliers) if total_amt > 0 else 0.0
    hhi = round(hhi, 1)
    hhi_label, hhi_risk = _hhi_level(hhi)

    # ---- 4b. CR 指标（更直观的集中度口径）----
    # 1500/2500 两档原本出自**市场竞争结构**分析（反垄断口径），搬到"单一银行
    # 自己的外包盘子里"并不等价：这里的分母是本行外包总额，不是整个行业市场。
    # 因此把 CR1/CR3 与"独家支撑条线数"提到主指标，HHI 降为辅助并标注口径。
    cr1 = round(suppliers[0]["share"], 2) if suppliers else 0.0
    cr3 = round(sum(s["share"] for s in suppliers[:3]), 2) if suppliers else 0.0
    cr5 = round(sum(s["share"] for s in suppliers[:5]), 2) if suppliers else 0.0
    solo_lines = [x for x in lines if x.get("solo")]
    critical_solo = [x for x in solo_lines if x.get("criticality") == "high"]
    hhi_note = ("HHI 的 1500/2500 分档来自市场竞争结构分析（分母为全行业），"
                "本页分母为本行外包总额，属不同口径：仅作横向参考，"
                "判断集中度以 CR1/CR3 与独家支撑条线为主。")

    # ---- 汇总与预警 ----
    alerts = []
    for s in suppliers:
        if s["risk"] == "high":
            alerts.append({
                "level": "high",
                "scope": "单供应商",
                "target": s["name"],
                "value": s["share"],
                "text": f"单一供应商占本行外包总额 {s['share']}%，超过 {SINGLE_HIGH}% 高风险线（第33条集中度风险）",
                "clause": "办法第33条",
            })
        elif s["risk"] == "warn":
            alerts.append({
                "level": "medium",
                "scope": "单供应商",
                "target": s["name"],
                "value": s["share"],
                "text": f"单一供应商占本行外包总额 {s['share']}%，超过 {SINGLE_WARN}% 警戒线（第33条）",
                "clause": "办法第33条",
            })
    for g in groups:
        if g["is_merged"] and g["risk"] in ("high", "warn"):
            lvl = "high" if g["risk"] == "high" else "medium"
            alerts.append({
                "level": lvl,
                "scope": "集团合并",
                "target": g["group"],
                "value": g["share"],
                "text": f"「{g['group']}」旗下 {len(g['members'])} 个法人（{'、'.join(g['members'])}）"
                        f"合并占比 {g['share']}%，{'超过 ' + str(GROUP_HIGH) + '% 高风险线' if lvl == 'high' else '超过 ' + str(GROUP_WARN) + '% 警戒线'}"
                        f"，存在关联方与集中度叠加风险（第28/33条）",
                "clause": "办法第28条、第33条",
            })
    for l in lines:
        if l["risk"] == "high":
            alerts.append({
                "level": "high",
                "scope": "业务条线",
                "target": l["line"],
                "value": 100.0,
                "text": f"重要业务条线「{l['line']}」由单一供应商「{l['top_vendor']}」独家支撑，"
                        f"无备份供应商，一旦无法履约将直接中断该业务（第30条业务连续性）",
                "clause": "办法第30条",
            })
        elif l["risk"] == "warn" and l["solo"]:
            alerts.append({
                "level": "medium",
                "scope": "业务条线",
                "target": l["line"],
                "value": 100.0,
                "text": f"业务条线「{l['line']}」由单一供应商「{l['top_vendor']}」支撑，"
                        f"缺少备份，建议评估备选供应商（第30条）",
                "clause": "办法第30条",
            })
        elif l["risk"] == "warn":
            alerts.append({
                "level": "medium",
                "scope": "业务条线",
                "target": l["line"],
                "value": l["top_share"],
                "text": f"业务条线「{l['line']}」中「{l['top_vendor']}」占比 {l['top_share']}%，"
                        f"超过 {LINE_DOMINANT}% 支配线（第33条）",
                "clause": "办法第33条",
            })

    order = {"high": 0, "medium": 1, "low": 2}
    alerts.sort(key=lambda a: order.get(a["level"], 9))

    return {
        "total_amount": round(total_amt, 2),
        "total_amount_display": f"{total_amt:,.2f} 万元" if total_amt > 0 else "未维护",
        "supplier_count": len(items),
        "suppliers": suppliers,
        "groups": groups,
        "lines": lines,
        # 主指标：CR1/CR3 与独家支撑条线（口径直观、不依赖市场结构假设）
        "cr1": cr1,
        "cr3": cr3,
        "cr5": cr5,
        "solo_line_count": len(solo_lines),
        "critical_solo_count": len(critical_solo),
        "critical_solo_lines": [x["line"] for x in critical_solo],
        # 辅助指标：HHI（附口径说明，避免把竞争结构分档直接套到单一机构盘子上）
        "hhi": hhi,
        "hhi_label": hhi_label,
        "hhi_risk": hhi_risk,
        "hhi_note": hhi_note,
        "alerts": alerts,
        "thresholds": {
            "single_warn": SINGLE_WARN, "single_high": SINGLE_HIGH,
            "group_warn": GROUP_WARN, "group_high": GROUP_HIGH,
            "line_dominant": LINE_DOMINANT,
        },
        "missing_amount": missing_amount,
    }
