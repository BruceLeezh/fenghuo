"""处置策略推演沙盘 —— "如果这样处置，组合会变成什么样？"

定位
----
现有系统能"报警"，但不能"决策"：
    第27条给了约谈/整改/暂停/取消资格四种手段，
    第31条要求业务连续性缓释措施，
    但业务人员真正要回答的问题是：

        "对这家供应商降额或退出后，我的组合还稳不稳？"

本模块就是回答这个问题的 what-if 沙盘。它把一次处置动作沿三条链路推演到底：

  链路一 · 金额与集中度
      退出 → 该供应商金额释放 → 总额下降 → 其余供应商占比被动上升
                                                    ↑ 这可能制造新的集中度风险
  链路二 · 业务连续性
      退出 → 其支撑的业务条线是否还有备份供应商？
              无备份 → 条线断档（第30条业务连续性风险）
  链路三 · 监管义务
      处置后组合是否仍满足第31条缓释要求（备选供应商、退出预案、演练）

监管依据
--------
第27条  督促纠正 → 约谈 → 暂停/取消资格（处置手段的法定顺序）
第30条  业务中断、服务水平下降（业务连续性风险）
第31条  业务连续性五项缓释措施
第33条  集中度风险（退出可能引发二次集中）

设计原则
--------
**不臆测、不补数。** 所有金额来自台账 `annual_amount`，未维护金额的供应商
按 0 处理并在结果中标注；推演只做可以严格推导的加减与占比重算，
不做"预计能省多少钱"这类无法验证的估算。
"""
from typing import Dict, List, Optional

# 处置动作定义（对应《办法》第27条的处置梯度）
ACTIONS = {
    "hold": {
        "key": "hold", "label": "维持现状", "short": "维持",
        "severity": 0, "clause": "第23条",
        "desc": "保持现有合作与额度，仅按常规周期监控。",
        "amount_factor": 1.0,       # 金额保留比例
    },
    "talk": {
        "key": "talk", "label": "约谈高管", "short": "约谈",
        "severity": 1, "clause": "第27条(二)",
        "desc": "就风险事项约谈服务提供商高管，要求限期说明与纠正。",
        "amount_factor": 1.0,       # 约谈不直接改变金额，但会施加整改压力
    },
    "rectify": {
        "key": "rectify", "label": "限期整改", "short": "整改",
        "severity": 2, "clause": "第27条(一)",
        "desc": "要求供应商限期整改并提交整改报告，期间加密监控。",
        "amount_factor": 0.85,      # 实务中通常伴随新增额度暂缓，按保留 85% 估算
    },
    "restrict": {
        "key": "restrict", "label": "限制新增业务", "short": "限制",
        "severity": 3, "clause": "第27条(三)",
        "desc": "停止新增外包需求，存量合同自然到期后不再续约。",
        "amount_factor": 0.40,      # 存量逐步退坡，按保留 40% 估算
    },
    "exit": {
        "key": "exit", "label": "暂停/取消资格", "short": "退出",
        "severity": 4, "clause": "第27条(四)",
        "desc": "暂停或取消其服务提供商资格，启动退出流程。",
        "amount_factor": 0.0,       # 完全退出
    },
}

ACTION_ORDER = ["hold", "talk", "rectify", "restrict", "exit"]


def _amt(item: dict) -> float:
    """台账金额（万元）；未维护或非法（负数/NaN）返回 0 并单独标注。

    负数不能原样参与求和：总额被拉低后其余供应商占比虚高（可 >100%），
    单家/集团阈值会被误触发。口径与 concentration.py 保持一致。
    """
    try:
        v = float(item.get("annual_amount") or 0)
    except (TypeError, ValueError):
        return 0.0
    if v != v or v in (float("inf"), float("-inf")):
        return 0.0
    return v if v > 0 else 0.0


def _group_key(item: dict) -> str:
    """集团归集键（与 concentration.py 口径保持一致）。"""
    g = (item.get("group") or "").strip()
    if g:
        return g
    nm = str(item.get("name") or "")
    for suf in ("集团股份有限公司", "集团有限责任公司", "股份有限公司", "有限责任公司", "有限公司"):
        if nm.endswith(suf):
            nm = nm[: -len(suf)]
            break
    return nm or str(item.get("name") or "未知")


def _hhi(shares: List[float]) -> float:
    """HHI = Σ(份额%)²，份额以百分数计。"""
    return round(sum((s * 100) ** 2 for s in shares if s > 0), 1)


def _hhi_level(hhi: float) -> tuple:
    if hhi <= 0:
        return "无数据", "normal"
    if hhi < 1500:
        return "竞争充分", "normal"
    if hhi <= 2500:
        return "中度集中", "warn"
    return "高度集中", "high"


def build_baseline(roster_items: List[dict]) -> dict:
    """构造"现状"基线：总额、各供应商占比、HHI、业务条线覆盖。"""
    amts = {it.get("id"): _amt(it) for it in roster_items}
    total = sum(amts.values())
    suppliers = []
    for it in roster_items:
        a = amts.get(it.get("id")) or 0.0
        suppliers.append({
            "id": it.get("id"), "name": it.get("name"),
            "group": _group_key(it),
            "amount": a,
            "share": round(a / total * 100, 2) if total else 0.0,
            "no_amount": a <= 0,
        })
    suppliers.sort(key=lambda s: -s["amount"])

    shares = [s["share"] / 100 for s in suppliers if s["share"] > 0]
    hhi = _hhi(shares)
    label, risk = _hhi_level(hhi)

    # 业务条线覆盖：每条线由哪些供应商支撑
    lines: Dict[str, List[str]] = {}
    for it in roster_items:
        for bl in (it.get("business_lines") or []):
            lines.setdefault(bl, []).append(it.get("id"))

    return {
        "total_amount": round(total, 2),
        "supplier_count": len(roster_items),
        "suppliers": suppliers,
        "hhi": hhi,
        "hhi_label": label,
        "hhi_risk": risk,
        "lines": {k: v for k, v in lines.items()},
        "amount_missing": [s["name"] for s in suppliers if s["no_amount"]],
    }


def simulate(roster_items: List[dict],
             decisions: Dict[str, str],
             service_scores: Optional[Dict[str, float]] = None) -> dict:
    """推演一组处置决策对组合的影响。

    参数
    ----
    roster_items   —— 台账条目
    decisions      —— {supplier_id: action_key}；未出现的按"维持"处理
    service_scores —— 可选 {supplier_id: 服务水平衰减加权分}，用于评估处置优先级

    返回
    ----
    现状 vs 推演后的对比 + 三类风险提示 + 建议
    """
    service_scores = service_scores or {}
    base = build_baseline(roster_items)

    by_id = {it.get("id"): it for it in roster_items}
    amts_base = {it.get("id"): _amt(it) for it in roster_items}

    # ---- 1) 计算推演后的金额与占比 ----
    amts_after = {}
    for it in roster_items:
        sid = it.get("id")
        act = ACTIONS.get(decisions.get(sid) or "hold", ACTIONS["hold"])
        amts_after[sid] = amts_base.get(sid, 0.0) * act["amount_factor"]

    total_after = sum(amts_after.values())
    suppliers_after = []
    for it in roster_items:
        sid = it.get("id")
        act = ACTIONS.get(decisions.get(sid) or "hold", ACTIONS["hold"])
        a0 = amts_base.get(sid, 0.0)
        a1 = amts_after.get(sid, 0.0)
        suppliers_after.append({
            "id": sid, "name": it.get("name"),
            "group": _group_key(it),
            "action": act["key"], "action_label": act["label"],
            "amount_before": round(a0, 2), "amount_after": round(a1, 2),
            "amount_delta": round(a1 - a0, 2),
            "share_before": round(a0 / base["total_amount"] * 100, 2) if base["total_amount"] else 0.0,
            "share_after": round(a1 / total_after * 100, 2) if total_after else 0.0,
            "exited": act["amount_factor"] <= 0,
        })
    suppliers_after.sort(key=lambda s: -s["amount_after"])

    shares_after = [s["share_after"] / 100 for s in suppliers_after if s["share_after"] > 0]
    hhi_after = _hhi(shares_after)
    label_after, risk_after = _hhi_level(hhi_after)

    amount_freed = round(base["total_amount"] - total_after, 2)

    # ---- 2) 业务条线断档检测 ----
    exited_ids = {s["id"] for s in suppliers_after if s["exited"]}
    line_impacts = []
    for line, sids in base["lines"].items():
        survivors = [s for s in sids if s and s not in exited_ids]
        if not survivors and sids:
            line_impacts.append({
                "line": line, "level": "high",
                "vendor_count_before": len(sids), "vendor_count_after": 0,
                "text": f"「{line}」将失去全部供应商支撑，形成业务断档",
            })
        elif len(survivors) == 1 and len(sids) > 1:
            sv = by_id.get(survivors[0], {}).get("name", survivors[0])
            line_impacts.append({
                "line": line, "level": "warn",
                "vendor_count_before": len(sids), "vendor_count_after": 1,
                "text": f"「{line}」将只剩「{sv}」单一供应商，形成新的单点依赖",
            })
    line_impacts.sort(key=lambda x: {"high": 0, "warn": 1}.get(x["level"], 9))

    # ---- 3) 集中度二次风险：退出后是否有供应商占比被动升破阈值 ----
    from core.concentration import SINGLE_HIGH, SINGLE_WARN
    new_concerns = []
    for s in suppliers_after:
        if s["exited"]:
            continue
        # 阈值比较用未取整的原始份额：先 round 到 2 位会把 29.996 变成 30.0，
        # 恰好漏报「升破 30% 高风险线」。
        raw_after = (s["amount_after"] / total_after * 100) if total_after else 0.0
        raw_before = (s["amount_before"] / base["total_amount"] * 100) if base["total_amount"] else 0.0
        if raw_after >= SINGLE_HIGH > raw_before:
            new_concerns.append({
                "level": "high", "id": s["id"], "name": s["name"],
                "before": s["share_before"], "after": s["share_after"],
                "text": f"「{s['name']}」占比将由 {s['share_before']}% 升至 {s['share_after']}%，"
                        f"突破 {SINGLE_HIGH}% 高风险线 —— 处置一家反而推高另一家的集中度",
            })
        elif raw_after >= SINGLE_WARN > raw_before:
            new_concerns.append({
                "level": "warn", "id": s["id"], "name": s["name"],
                "before": s["share_before"], "after": s["share_after"],
                "text": f"「{s['name']}」占比将由 {s['share_before']}% 升至 {s['share_after']}%，"
                        f"接近 {SINGLE_WARN}% 警戒线，需评估是否同步调整",
            })

    # ---- 4) 第31条缓释措施达标判定 ----
    mitigations = []
    # 4.1 备选供应商（是否所有重要条线都有 2 家以上）
    # 存量单点要在文案里说清：原判断只看「处置后」，基线本来就只有 1 家支撑的条线
    # 被漏掉，界面因此显示「达标」，掩盖了既有单点故障。
    single_line = [l for l, sids in base["lines"].items() if len([s for s in sids if s]) <= 1]
    # ⚠️ ok 必须与文案同口径：只判"新增断档"会让"存量还有 N 条单点支撑"的结论
    #    配上绿色的"达标"徽标，自相矛盾。存量单点同样不满足第31条(一)的备选要求。
    ok_1 = (not line_impacts) and (not single_line)
    if line_impacts:
        text_1 = f"{len(line_impacts)} 条业务线出现断档或单点依赖，未达标"
    elif single_line:
        text_1 = (f"处置后无新增断档，但存量仍有 {len(single_line)} 条业务线只有 1 家支撑"
                  f"（{('、'.join(single_line[:3]))}{'…' if len(single_line) > 3 else ''}），"
                  "退出缓冲有限，需先引入备选 —— 该项未达标")
    else:
        text_1 = "处置后无条线断档，且各条线均有备选，缓释措施达标"
    mitigations.append({
        "clause": "第31条(一)", "label": "备选供应商与退出策略",
        "ok": ok_1,
        "text": text_1,
    })
    # 4.2 集中度
    mitigations.append({
        "clause": "第33条", "label": "集中度控制",
        "ok": not new_concerns,
        "text": (f"HHI 由 {base['hhi']} 变为 {hhi_after}（{label_after}），无新增集中风险"
                 if not new_concerns
                 else f"出现 {len(new_concerns)} 项新的集中度风险，未达标"),
    })
    # 4.3 组合规模（退出过多会导致供应商储备不足）
    kept = len([s for s in suppliers_after if not s["exited"]])
    mitigations.append({
        "clause": "第31条(三)", "label": "供应商储备充足性",
        "ok": kept >= 3,
        "text": (f"处置后仍保留 {kept} 家供应商，储备充足"
                 if kept >= 3
                 else f"处置后仅剩 {kept} 家供应商，选择余地与议价能力不足"),
    })

    # ---- 5) 建议 ----
    advice = _recommend(base, suppliers_after, line_impacts, new_concerns,
                        amount_freed, decisions, service_scores, kept)

    return {
        "baseline": base,
        "after": {
            "total_amount": round(total_after, 2),
            "supplier_count": kept,
            "suppliers": suppliers_after,
            "hhi": hhi_after,
            "hhi_label": label_after,
            "hhi_risk": risk_after,
        },
        "amount_freed": amount_freed,
        "line_impacts": line_impacts,
        "new_concentration": new_concerns,
        "mitigations": mitigations,
        "mitigation_pass": all(m["ok"] for m in mitigations),
        "advice": advice,
        "decisions": decisions,
        "actions": ACTIONS,
    }


def _recommend(base, after_suppliers, line_impacts, new_concerns,
               amount_freed, decisions, service_scores, kept) -> dict:
    """给出一句话结论 + 分条建议。"""
    changed = {k: v for k, v in decisions.items() if v and v != "hold"}
    if not changed:
        return {
            "headline": "尚未选择任何处置动作",
            "points": ["请对需要处置的供应商选择动作（约谈/整改/限制/退出），"
                       "系统将实时推演对组合的影响。"],
            "verdict": "idle",
        }

    high_line = [l for l in line_impacts if l["level"] == "high"]
    worst = [c for c in new_concerns if c["level"] == "high"]

    points = []
    # 组合金额变化
    if amount_freed > 0:
        pct = round(amount_freed / base["total_amount"] * 100, 1) if base["total_amount"] else 0
        points.append(f"本次处置释放外包额度 {amount_freed:,.0f} 万元"
                      f"（占总盘子 {pct}%），需提前规划承接方案")
    # 断档
    for l in line_impacts[:3]:
        points.append(f"⚠ {l['text']}")
    # 集中度
    for c in new_concerns[:3]:
        points.append(f"⚠ {c['text']}")
    # 供应商储备
    if kept < 3:
        points.append(f"处置后仅剩 {kept} 家供应商，低于第31条建议的储备水平，"
                      f"建议先引入备选再执行退出")

    if high_line or worst:
        verdict, headline = "unsafe", "该处置组合会引入新的重大风险，建议调整"
    elif line_impacts or new_concerns:
        verdict, headline = "caution", "处置方向合理，但需同步落实缓释措施"
    else:
        verdict, headline = "safe", "该处置组合未引入新的组合级风险"

    if not points:
        points.append("无新增断档或集中度风险；建议在工单中记录处置依据并留痕。")

    return {"headline": headline, "points": points, "verdict": verdict}


def suggest_actions(roster_items: List[dict],
                    levels: Dict[str, str],
                    service_scores: Optional[Dict[str, float]] = None) -> dict:
    """按风险等级给出"建议的初始处置组合"（供沙盘一键载入）。

    规则（对齐第27条的处置梯度）：
      red    → 退出（若为唯一支撑则降为限制，避免直接断档）
      yellow → 限制新增或限期整改
      green  → 维持
    服务水平衰减分 < 60 的，等级至少提到 yellow（表现拖累）
    """
    service_scores = service_scores or {}
    base = build_baseline(roster_items)
    # 统计每条线的供应商数量，判断能否直接退出
    solo_lines = {}
    for line, sids in base["lines"].items():
        live = [s for s in sids if s]
        if len(live) == 1:
            solo_lines.setdefault(live[0], []).append(line)

    out, reasons = {}, {}
    for it in roster_items:
        sid = it.get("id")
        # 缺等级（扫描失败/未覆盖）不能当成 green：那等于把「没评估」判成「最安全」，
        # 还会让服务水平升级逻辑（要求 lv == "green"）永远进不去。按 yellow 保守处理。
        lv = levels.get(sid) or "yellow"
        svc = service_scores.get(sid)
        eff = lv
        if svc is not None and svc < 60 and lv == "green":
            eff = "yellow"          # 服务水平拖累，至少升级为关注

        if eff == "red":
            if sid in solo_lines:
                out[sid] = "restrict"
                reasons[sid] = (f"风险等级为高危，但该供应商独家支撑"
                                f"「{'、'.join(solo_lines[sid])}」，"
                                f"直接退出会造成业务断档，建议先限制新增并引入备选")
            else:
                out[sid] = "exit"
                reasons[sid] = "风险等级为高危，且其业务条线有备份供应商，建议启动退出"
        elif eff == "yellow":
            if svc is not None and svc < 75:
                out[sid] = "rectify"
                reasons[sid] = f"服务水平衰减加权分 {svc} 偏低，建议限期整改并加密监控"
            else:
                out[sid] = "restrict"
                reasons[sid] = "处于关注等级，建议限制新增业务并观察改善情况"
        else:
            out[sid] = "hold"
            reasons[sid] = "风险可控，维持现状并按常规周期监控"
    return {"decisions": out, "reasons": reasons}
