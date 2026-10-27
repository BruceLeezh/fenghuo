"""专属场景风险评价体系 —— 外包分类管理 + 场景化二次加权。

为什么需要这一层
----------------
现有 scoring.py 的 21 条规则是"一把尺子量所有供应商"。但《办法》第12条
要求对外包活动**分类管理**，不同类别的外包，风险来源与关注重点完全不同：

    运行维护类 —— 最怕"单点依赖"：核心系统由一家撑着，一旦中断无处可退
    开发测试类 —— 最怕"交付失控"：项目延期、代码缺陷、数据带出
    业务支持类 —— 最怕"人力波动"：人员流失、虚报人力、服务质量下滑

用同一套权重去评，会让各自最致命的风险被平均掉。本模块的做法是：

    场景化得分 = Σ(基座维度分 × 场景维度权重) ，再叠加场景专属调整项

设计原则（重要）
----------------
1. 不推翻 scoring.py，而是**在其之上做二次加权**（基座分仍可追溯）
2. 每个场景有**专属的维度权重**（同一维度在不同场景下重要性不同）
3. 每个场景有**专属校验项**（场景特有的红线与加分/扣分信号）
4. 场景由台账 category 自动匹配，也允许人工指定（应对边界情况）

这样路演时能讲一个很硬的观点：
    "同一个 80 分，在开发测试场景可能是安全的，在运行维护场景可能是危险的。"
"""
from typing import Dict, List, Optional

# 基座六大维度（与 scoring.py / ahp.py 对齐）
# performance 是第25条服务水平评价结论，2026-09 起纳入场景加权
BASE_DIMS = ["performance", "legal", "operating", "financial", "reputation", "concentration"]
DIM_LABEL = {
    "performance": "履约表现",
    "legal": "法律合规",
    "operating": "经营稳定性",
    "financial": "财务状况",
    "reputation": "舆情声誉",
    "concentration": "依赖与集中度",
}
# 场景专属信号里，与"履约表现"强相关的信号 key（这些信号已由 performance 维度覆盖，
# 场景层不再重复扣分，避免同一事实被扣两次）
PERF_OWNED_SIGNALS = {
    "ops_fault_up", "delivery_slip", "satisfaction_drop",
    "personnel_quality_drop", "security_incident_spike",
}


# ---------------------------------------------------------------------------
# 场景定义
# ---------------------------------------------------------------------------
# weights —— 场景内五大维度的权重（各自归一，和为 1）
# focus    —— 该场景最关注的指标（用于前端高亮与话术）
# signals  —— 场景专属校验项：key/label/check_kind/deduct/hint/clause
#            check_kind 是"信号名"，由 evaluate_scenario() 按上下文计算
SCENARIOS: Dict[str, dict] = {
    "ops_critical": {
        "key": "ops_critical",
        "name": "核心系统运行维护场景",
        "short": "运维·核心系统",
        "match_categories": ["运行维护类"],
        "clause": "第12条（运行维护类）· 第30条(二)业务中断 · 第31条业务连续性",
        "desc": "为核心业务系统提供运行维护的外包。风险核心是单点依赖与业务中断，"
                "一旦服务中断或质量下滑，将直接影响对外服务与账务准确性。",
        "weights": {"performance": 0.22, "legal": 0.14, "operating": 0.12,
                    "financial": 0.10, "reputation": 0.06, "concentration": 0.36},
        "focus": ["单点依赖程度", "业务连续性预案与演练", "故障响应与解决时效",
                  "退出与替代方案可行性"],
        "signals": [
            {"key": "sole_supplier_critical", "label": "核心系统单一供应商支撑",
             "deduct": 12, "clause": "第31条(一)",
             "hint": "核心系统仅由一家供应商运行维护，且无可行替代方案，属重大单点依赖"},
            {"key": "no_exit_plan", "label": "缺少退出策略/供应链保障方案",
             "deduct": 10, "clause": "第31条(一)",
             "hint": "重要外包应事先制定退出策略和供应链安全保障方案"},
            {"key": "no_drill", "label": "未按期开展业务连续性演练",
             "deduct": 8, "clause": "第31条",
             "hint": "未定期演练意味着真实中断时恢复能力未经检验"},
            {"key": "ops_fault_up", "label": "运维故障处理时效持续恶化",
             "deduct": 8, "clause": "第25条(二)",
             "hint": "故障响应/解决时间连续两期不达标，可能演化为服务水平下降"},
            {"key": "resident_needed", "label": "重要系统未采用驻场服务",
             "deduct": 5, "clause": "第13条",
             "hint": "核心系统运维若非驻场，现场故障处置与响应时效难以保障"},
        ],
    },
    "dev_delivery": {
        "key": "dev_delivery",
        "name": "业务系统开发测试场景",
        "short": "开发·交付与数据",
        "match_categories": ["开发测试类"],
        "clause": "第12条（开发测试类）· 第20条(八)数据安全 · 第25条(四)交付质量",
        "desc": "承接业务系统开发与测试的外包。风险核心是交付失控、代码质量"
                "与敏感数据带出，且项目制外包天然存在供应商更替与知识流失风险。",
        "weights": {"performance": 0.30, "legal": 0.16, "operating": 0.10,
                    "financial": 0.15, "reputation": 0.07, "concentration": 0.22},
        "focus": ["历史交付履约记录", "招投标与中标合规性", "数据安全与信息隔离",
                  "需求完成率与代码质量"],
        "signals": [
            {"key": "sole_source_high", "label": "中标记录单一来源占比过高",
             "deduct": 8, "clause": "第18条",
             "hint": "大量项目以单一来源方式中标，可能存在竞争不充分或议价能力失衡"},
            {"key": "delivery_declining", "label": "交付质量指标持续下滑",
             "deduct": 10, "clause": "第25条(四)",
             "hint": "需求及时完成率与缺陷数连续恶化，提示交付能力下降"},
            {"key": "high_change_rate", "label": "需求变更率偏高",
             "deduct": 6, "clause": "第25条(四)",
             "hint": "变更率过高推高成本，且易引入未经评审的改动"},
            {"key": "legal_events_high", "label": "存在较多涉诉/被执行记录",
             "deduct": 10, "clause": "第17条",
             "hint": "项目制外包周期长，供应商司法风险可能中途导致服务终止"},
            {"key": "client_concentration", "label": "客户高度集中于少数金融机构",
             "deduct": 5, "clause": "第30条(五)",
             "hint": "客户集中在中标记录中占比过高，供应商经营波动将传导至本行"},
        ],
    },
    "support_service": {
        "key": "support_service",
        "name": "客服与集中作业支持场景",
        "short": "支持·人力与服务",
        "match_categories": ["业务支持类"],
        "clause": "第12条（业务支持类）· 第25条(三)(五) · 第30条(五)服务水平下降",
        "desc": "承接客服中心、集中作业等业务支持职能。风险核心是人力稳定性"
                "与服务质量，且直接面向客户，声誉风险传导速度快。",
        "weights": {"performance": 0.34, "legal": 0.10, "operating": 0.10,
                    "financial": 0.10, "reputation": 0.20, "concentration": 0.16},
        "focus": ["客户满意度趋势", "人员饱和率与考核合格率", "业务量负载",
                  "消费者权益保护"],
        "signals": [
            {"key": "satisfaction_down", "label": "客户满意度持续下滑",
             "deduct": 10, "clause": "第25条(三)",
             "hint": "客服外包直接触达客户，满意度下滑会快速传导为声誉风险"},
            {"key": "utilization_low", "label": "外包人员饱和率偏低",
             "deduct": 8, "clause": "第25条(五)",
             "hint": "饱和率过低需警惕人力虚报或资源闲置，成本效益不匹配"},
            {"key": "pass_rate_low", "label": "人员考核合格率不达标",
             "deduct": 8, "clause": "第25条(五)",
             "hint": "合格率低说明人员能力与岗位要求不匹配，服务质量难以保障"},
            {"key": "consumer_complaint", "label": "涉及消费者权益负面舆情",
             "deduct": 10, "clause": "第20条(八)",
             "hint": "业务支持类外包涉及客户信息，需重点防范消费者权益侵害"},
            {"key": "volume_drop", "label": "服务量异常下降",
             "deduct": 5, "clause": "第25条(三)",
             "hint": "服务量骤降可能是服务缩水的前兆，需核实实际投入"},
        ],
    },
    "security_service": {
        "key": "security_service",
        "name": "安全服务外包场景",
        "short": "安全·攻防与数据",
        "match_categories": ["安全服务类"],
        "clause": "第12条（安全服务类）· 第20条(八)安全保密 · 第38条重大事件",
        "desc": "安全运营、渗透测试、安全评估等安全服务外包。风险核心是"
                "权限过度集中与信息泄露，供应商本身即掌握大量敏感信息。",
        "weights": {"performance": 0.28, "legal": 0.16, "operating": 0.10,
                    "financial": 0.08, "reputation": 0.10, "concentration": 0.28},
        "focus": ["安全事件与漏洞修复", "权限与数据访问边界", "人员背景与保密约束",
                  "应急预案有效性"],
        "signals": [
            {"key": "security_incident", "label": "发生网络与信息安全事件",
             "deduct": 15, "clause": "第38条",
             "hint": "安全服务商自身发生安全事件，其准入与权限范围须立即复核"},
            {"key": "vuln_unfixed", "label": "漏洞修复率不达标",
             "deduct": 10, "clause": "第25条(六)",
             "hint": "漏洞未及时修复是数据泄露的主要入口"},
            {"key": "sole_security", "label": "安全服务由单一供应商整体承接",
             "deduct": 8, "clause": "第11条",
             "hint": "安全运营整体外包且无第二供应商，风险过度集中且缺乏交叉验证"},
            {"key": "rto_fail", "label": "RTO 达标率不足",
             "deduct": 8, "clause": "第25条(六)",
             "hint": "恢复时间目标未达标意味着真实中断时无法按时恢复"},
        ],
    },
    "consult_planning": {
        "key": "consult_planning",
        "name": "规划咨询服务场景",
        "short": "咨询·战略与感知",
        "match_categories": ["咨询规划类"],
        "clause": "第12条（咨询规划类）· 第11条不得外包职能 · 第20条(八)",
        "desc": "信息科技战略规划、架构咨询等。风险核心是战略敏感性、"
                "核心能力外移与咨询成果的落地依赖。",
        "weights": {"performance": 0.26, "legal": 0.20, "operating": 0.16,
                    "financial": 0.10, "reputation": 0.12, "concentration": 0.16},
        "focus": ["是否触及不得外包职能", "战略信息保密", "成果交付质量",
                  "持续依赖程度"],
        "signals": [
            {"key": "core_function", "label": "触及不得外包的核心职能",
             "deduct": 20, "clause": "第11条",
             "hint": "信息科技战略管理、风险管理、内部审计等职能不得外包"},
            {"key": "strategy_exposure", "label": "掌握本行战略级信息",
             "deduct": 10, "clause": "第20条(八)",
             "hint": "咨询类外包易接触战略规划信息，须强化保密与竞业约束"},
            {"key": "delivery_declining", "label": "交付质量持续下滑",
             "deduct": 8, "clause": "第25条(四)", "hint": "咨询成果质量直接影响决策质量"},
            {"key": "sole_advisor", "label": "长期由单一咨询方主导规划",
             "deduct": 6, "clause": "第30条(一)",
             "hint": "长期单一咨询方可能造成战略视角固化与科技能力丧失"},
        ],
    },
}

# 分类 → 默认场景
CATEGORY_TO_SCENARIO = {
    "运行维护类": "ops_critical",
    "开发测试类": "dev_delivery",
    "业务支持类": "support_service",
    "安全服务类": "security_service",
    "咨询规划类": "consult_planning",
}


def list_scenarios() -> list:
    """场景清单（供前端选择）。"""
    return [{
        "key": s["key"],
        "name": s["name"],
        "short": s["short"],
        "desc": s["desc"],
        "clause": s["clause"],
        "match_categories": s["match_categories"],
        "weights": s["weights"],
        "focus": s["focus"],
        "signal_count": len(s["signals"]),
    } for s in SCENARIOS.values()]


def scenario_for_category(category: str) -> Optional[str]:
    """按外包分类自动匹配场景。"""
    return CATEGORY_TO_SCENARIO.get(category or "")


def get_scenario(key: str) -> Optional[dict]:
    """按 key 取场景（严格匹配）。"""
    return SCENARIOS.get(key)


def resolve_scenario(text: str) -> Optional[str]:
    """把用户输入解析为场景 key，容忍四种写法（任意外部调用方都友好）：
      ① 场景 key（ops_critical）
      ② 场景全称（核心系统运行维护场景）
      ③ 场景短名（运维·核心系统）
      ④ 名称包含关系（"核心系统"、"运维"等片段，唯一命中时）
    解析不出或歧义返回 None，由调用方决定如何提示。
    """
    t = (text or "").strip()
    if not t:
        return None
    if t in SCENARIOS:                       # ① key
        return t
    for s in SCENARIOS.values():             # ② 全称 ③ 短名
        if t == s["name"] or t == s["short"]:
            return s["key"]
    hits = [s["key"] for s in SCENARIOS.values()   # ④ 片段包含（须唯一）
            if t in s["name"] or t in s["short"] or t in s["key"]]
    if len(hits) == 1:
        return hits[0]
    hits2 = [s["key"] for s in SCENARIOS.values()
             if s["name"] in t or s["short"] in t]
    return hits2[0] if len(hits2) == 1 else None


# ---------------------------------------------------------------------------
# 场景专属信号判定
# ---------------------------------------------------------------------------
def _ctx(profile, service_eval: dict = None) -> dict:
    """构造信号判定所需的上下文（避免在 check 里反复取值）。"""
    fin = getattr(profile, "financials", None)
    latest_sl = (service_eval or {}).get("latest") or {}

    def sl_value(key):
        for c in latest_sl.get("categories", []) or []:
            for m in c.get("metrics", []) or []:
                if m.get("metric") == key:
                    return m.get("value"), m.get("status")
        return None, None

    consecutive = (service_eval or {}).get("consecutive") or []
    cons_keys = {c["metric"] for c in consecutive}

    return {
        "profile": profile,
        "fin": fin,
        "sl": sl_value,
        "cons_keys": cons_keys,
        "latest_sl": latest_sl,
        "consecutive": consecutive,
        "outsourcing": getattr(profile, "outsourcing_special", None),
    }


def _check_signal(key: str, c: dict, roster_item: dict) -> bool:
    """判定某个场景信号是否命中。roster_item 提供台账上下文（驻场/业务条线等）。"""
    p = c["profile"]
    roster_item = roster_item or {}

    if key == "sole_supplier_critical":
        # 台账标记为唯一支撑核心系统（业务条线含"核心"）且无备份。
        # ⚠️ 不能只看 `not roster_item.get("has_alternative")`：台账根本没有这个字段，
        #    缺失被当作"无备份"→ 对所有含"核心"条线的供应商恒命中。改为用集中度模块
        #    的真实"单点支撑"结论作为证据；无证据不命中（未知 ≠ 有风险）。
        lines = [str(x) for x in (roster_item.get("business_lines") or [])]
        core = [x for x in lines if "核心" in x]
        if not core or roster_item.get("has_alternative") is True:
            return False
        try:
            from core import concentration as _conc_mod
            solo = {l.get("line") for l in
                    (_conc_mod.build_concentration().get("lines") or []) if l.get("solo")}
        except Exception:
            return False
        return any(x in solo for x in core)

    if key == "no_exit_plan":
        # 退出策略的证据来自合同条款「退出与过渡期条款」：
        #   已约定 → 不算缺口；明确缺失 → 命中；未登记 → 无法判定，不扣分。
        if not roster_item.get("important"):
            return False
        return _contract_clause_state(roster_item.get("id"), "exit_transition") is False

    if key == "no_drill":
        v, _ = c["sl"]("drill_pass")
        return v is not None and float(v) < 100

    if key == "ops_fault_up":
        return ("fault_response_hours" in c["cons_keys"]
                or "fault_resolve_hours" in c["cons_keys"])

    if key == "resident_needed":
        return bool(roster_item.get("important")) and bool(roster_item.get("non_resident"))

    if key == "sole_source_high":
        # 「中标记录单一来源占比过高」：用集中度模块的独家支撑条线作证据
        # （原来读 `_sole_source_high` 这个从未被写入的私有字段，信号恒不命中）。
        return _has_solo_line(roster_item)

    if key == "delivery_declining":
        return ("on_time_rate" in c["cons_keys"] or "defect_count" in c["cons_keys"])

    if key == "high_change_rate":
        v, st = c["sl"]("change_rate")
        return v is not None and st in ("warn", "fail")

    if key == "legal_events_high":
        return len(getattr(p, "legal_events", []) or []) >= 5

    if key == "client_concentration":
        # 「客户高度集中于少数金融机构」：用股东结构里第一大股东持股比例作证据。
        # 原实现读 `_client_concentration`（从未写入）→ 信号恒不命中。
        pct = _top_shareholder_ratio(p)
        return pct is not None and pct >= 50.0

    if key == "satisfaction_down":
        return "satisfaction" in c["cons_keys"]

    if key == "utilization_low":
        v, st = c["sl"]("utilization")
        return v is not None and st in ("warn", "fail")

    if key == "pass_rate_low":
        return "pass_rate" in c["cons_keys"]

    if key == "consumer_complaint":
        for n in getattr(p, "news", []) or []:
            t = "".join([str(getattr(n, "title", "")), str(getattr(n, "summary", ""))])
            if any(w in t for w in ("消费者", "客户投诉", "个人信息", "泄露")):
                return True
        return False

    if key == "volume_drop":
        v, st = c["sl"]("service_count")
        return v is not None and st in ("warn", "fail")

    if key == "security_incident":
        v, _ = c["sl"]("security_incidents")
        return v is not None and float(v) > 0

    if key == "vuln_unfixed":
        v, st = c["sl"]("vuln_fixed_rate")
        return v is not None and st in ("warn", "fail")

    if key == "sole_security":
        lines = [str(x) for x in (roster_item.get("business_lines") or [])]
        return bool(roster_item.get("important")) and len(lines) <= 2

    if key == "rto_fail":
        v, st = c["sl"]("rto_compliance")
        return v is not None and st in ("warn", "fail")

    if key == "core_function":
        # 《办法》第12条：不得外包的核心职能。台账里以业务条线或分类中的"核心"标记作证据。
        lines = [str(x) for x in (roster_item.get("business_lines") or [])]
        return (any("核心" in x for x in lines)
                or "核心" in str(roster_item.get("category") or "")
                or bool(roster_item.get("_core_function")))

    if key == "strategy_exposure":
        # 战略级信息暴露：战略规划/咨询类外包（与第37条(四)同一判定口径）。
        return ("战略" in str(roster_item.get("name") or "")
                or str(roster_item.get("category") or "") == "咨询规划类"
                or bool(roster_item.get("_strategy_info")))

    if key == "sole_advisor":
        # 「长期由单一咨询方主导规划」：咨询规划类 + 该供应商存在独家支撑条线。
        is_advisor = (str(roster_item.get("category") or "") == "咨询规划类"
                      or "战略" in str(roster_item.get("name") or ""))
        return is_advisor and _has_solo_line(roster_item)

    return False


def _contract_clause_state(supplier_id: str, clause_key: str):
    """读合同条款登记状态：True=已约定 / False=明确缺失 / None=未登记（无法判定）。

    直接调 contract.load_contracts()：一次场景评价只查一家，多读一次文件的代价可忽略，
    换来的是"没登记就不能当成缺失"的三态语义。
    """
    if not supplier_id:
        return None
    try:
        from core import contract as _contract_mod
        rec = (_contract_mod.load_contracts() or {}).get(supplier_id) or {}
        return _contract_mod._norm_present((rec.get("clauses") or {}).get(clause_key))
    except Exception:
        return None


def _has_solo_line(roster_item: dict) -> bool:
    """该供应商是否独家支撑至少一条业务条线（集中度模块的真实结论）。"""
    lines = {str(x) for x in (roster_item.get("business_lines") or [])}
    if not lines:
        return False
    try:
        from core import concentration as _conc_mod
        solo = {l.get("line") for l in
                (_conc_mod.build_concentration().get("lines") or []) if l.get("solo")}
    except Exception:
        return False
    return bool(lines & solo)


def _top_shareholder_ratio(profile) -> Optional[float]:
    """第一大股东持股比例（%）。无股东数据返回 None（未知，不当作"不集中"）。"""
    best = None
    for s in getattr(profile, "shareholders", None) or []:
        r = getattr(s, "ratio", None)
        if r is None:
            continue
        try:
            r = float(r)
        except (TypeError, ValueError):
            continue
        if best is None or r > best:
            best = r
    return best


# ---------------------------------------------------------------------------
# 场景化评价主函数
# ---------------------------------------------------------------------------
def evaluate_scenario(profile, base_result: dict, scenario_key: str,
                      roster_item: dict = None, service_eval: dict = None) -> dict:
    """在基座评分之上做场景化二次加权。

    参数
    ----
    profile       —— SupplierProfile（供场景信号判定）
    base_result   —— 基座评级结果（rating.rate_supplier 的维度分）
    scenario_key  —— 场景 key
    roster_item   —— 台账条目（提供驻场/条线等业务上下文）
    service_eval  —— servicelevel.evaluate_supplier 的结果（第25条指标）

    返回
    ----
    {scenario, weights, base_score, scenario_score, delta, adjustments, signals, verdict}
    """
    sc = SCENARIOS.get(scenario_key)
    if not sc:
        return {"error": f"未知场景：{scenario_key}"}

    dim_scores = {d: (base_result.get("dimensions", {}).get(d) or {}).get("score", 100)
                  for d in BASE_DIMS}
    base_score = base_result.get("total_score", 0)

    # 0) 剔除无数据（na）的维度，并在场景权重内重新归一化。
    #    否则 performance=na 会被当作 0 分，凭空拉低场景分（失真）。
    na_dims = [d for d in BASE_DIMS
               if (base_result.get("dimensions", {}).get(d) or {}).get("na")
               or dim_scores.get(d) is None]
    avail = [d for d in BASE_DIMS if d not in na_dims]
    raw_w = sc["weights"]
    wsum = sum(raw_w.get(d, 0) for d in avail) or 1.0
    weights = {d: round(raw_w.get(d, 0) / wsum, 6) for d in avail}

    # 1) 场景加权（仅对可用维度）
    weighted = sum(dim_scores[d] * weights.get(d, 0) for d in avail)
    weighted = round(min(weighted, 100), 2)

    # 2) 场景专属信号
    #    与履约表现维度重复的信号不再扣分（同一事实不扣两次）——
    #    这些信号的"长期恶化"已被 performance 维度的连续未达标/悬崖检测覆盖；
    #    但若该供应商没有服务水平数据（performance 为 na），则仍需在场景层体现。
    perf_available = "performance" not in na_dims
    c = _ctx(profile, service_eval)
    signals = []
    skipped = []
    total_deduct = 0
    for sig in sc["signals"]:
        if not _check_signal(sig["key"], c, roster_item):
            continue
        if perf_available and sig["key"] in PERF_OWNED_SIGNALS:
            skipped.append({
                "key": sig["key"], "label": sig["label"],
                "reason": "已由履约表现维度扣分，此处不重复计算",
            })
            continue
        signals.append({
            "key": sig["key"], "label": sig["label"],
            "deduct": sig["deduct"], "clause": sig["clause"],
            "hint": sig["hint"],
        })
        total_deduct += sig["deduct"]

    # 3) 场景得分
    scenario_score = round(max(weighted - total_deduct, 0), 2)

    # 4) 维度“场景重要性”对比（基座权重 vs 场景权重）
    #    基座权重用再归一化前的原始值，才能真实反映"权重迁移"幅度
    base_weights = base_result.get("raw_weights") or base_result.get("weights") or {}
    dim_compare = []
    for d in BASE_DIMS:
        bw = float(base_weights.get(d, 0) or 0)
        sw = float(raw_w.get(d, 0) or 0)
        dim_compare.append({
            "dim": d, "label": DIM_LABEL[d],
            "score": dim_scores[d],
            "base_weight": round(bw, 4),
            "scenario_weight": round(sw, 4),
            "eff_weight": round(weights.get(d, 0), 4),   # 本次实际生效权重
            "shift": round(sw - bw, 4),
            "contribution": round((dim_scores[d] or 0) * weights.get(d, 0), 2),
            "na": d in na_dims,
        })
    dim_compare.sort(key=lambda x: -x["scenario_weight"])

    # 5) 结论
    level = _level_of(scenario_score, bool(signals and any(s["deduct"] >= 12 for s in signals)))
    verdict = _verdict(sc, scenario_score, signals, delta=round(scenario_score - base_score, 2))
    if na_dims:
        miss = "、".join(DIM_LABEL.get(d, d) for d in na_dims)
        verdict = f"{verdict}（注：{miss}暂无数据，权重已在剩余维度间重新归一化）"

    return {
        "scenario": {
            "key": sc["key"], "name": sc["name"], "short": sc["short"],
            "desc": sc["desc"], "clause": sc["clause"], "focus": sc["focus"],
        },
        "weights": weights,
        "raw_weights": raw_w,
        "na_dimensions": [DIM_LABEL.get(d, d) for d in na_dims],
        "skipped_signals": skipped,
        "base_score": base_score,
        "scenario_score": scenario_score,
        "delta": round(scenario_score - base_score, 2),
        "level": level,
        "dimensions": dim_compare,
        "signals": signals,
        "signal_deduct": total_deduct,
        "verdict": verdict,
    }


def _level_of(score: float, force_red: bool = False) -> str:
    if force_red or score < 60:
        return "red"
    if score < 80:
        return "yellow"
    return "green"


def _verdict(sc: dict, score: float, signals: list, delta: float) -> dict:
    """生成场景化结论话术（路演用，强调"场景改变结论"）。"""
    hit = len(signals)
    key_hits = [s["label"] for s in signals if s["deduct"] >= 10]

    if score >= 80 and not key_hits:
        level_txt = "在本场景下属可接受风险"
        advice = "按常规周期监控，保持服务水平指标跟踪"
    elif score >= 60:
        level_txt = "在本场景下属需关注风险"
        advice = "针对已命中的场景专项信号，督促限期纠正并加密监控"
    else:
        level_txt = "在本场景下属高风险"
        advice = "建议按第27条启动约谈，并评估退出与替代方案可行性"

    # 突出"场景视角差异"
    if delta <= -10:
        shift_txt = f"换到本场景评价后得分下降 {abs(delta)} 分，该供应商在本场景下风险显著高于通用视角"
    elif delta >= 10:
        shift_txt = f"换到本场景评价后得分上升 {delta} 分，通用视角高估了其在本场景下的风险"
    else:
        shift_txt = "本场景评价与通用视角结论接近"

    return {
        "level_text": level_txt,
        "advice": advice,
        "shift": shift_txt,
        "signal_count": hit,
        "key_signals": key_hits,
        "focus": sc["focus"],
    }


def apply_scenario_to_scan_row(row: dict, roster_item: dict, service_eval: dict) -> dict:
    """给批量扫描结果补场景标签（轻量版，不重复算分）。"""
    cat = (roster_item or {}).get("category", "")
    sk = scenario_for_category(cat)
    sc = SCENARIOS.get(sk)
    if not sc:
        return {**row, "scenario": None}
    return {
        **row,
        "scenario": {
            "key": sc["key"], "name": sc["name"], "short": sc["short"],
            "top_dim": max(sc["weights"].items(), key=lambda kv: kv[1])[0],
            "top_dim_label": DIM_LABEL[max(sc["weights"].items(), key=lambda kv: kv[1])[0]],
        },
    }
