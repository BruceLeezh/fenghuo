"""规则打分引擎 —— 把供应商数据转成五大维度的风险得分 + 可追溯的明细。

思路：每个维度满分 100，从 100 开始按规则扣分（命中红线扣得狠，
轻微信号扣得少），最终得到五个维度的分数，交给 AHP 加权。

★ 与旧版的区别：每条规则现在必须声明"七要素"，让前端能渲染出
  完整的评分细则（指标名 / 实测值 / 阈值 / 判定 / 扣分 / 条款 / 风险提示）。

规则完全基于《银行保险机构信息科技外包风险监管办法》的监管逻辑，
每个扣分项都标注了对应条款，保证"可追溯、有依据"。
"""
from datetime import date
from typing import Dict, List, Callable, Optional

from core.schemas import SupplierProfile


# ---------------------------------------------------------------------------
# 规则注册
# ---------------------------------------------------------------------------
RULES: Dict[str, List[Dict]] = {}


def _rule(dimension: str, name: str, clause: str, score: int,
          metric: str = "", threshold: str = "", hint: str = "",
          basis: str = ""):
    """规则装饰器工厂：注册一条扣分规则。

    参数：
      dimension  —— 所属维度（legal/operating/financial/reputation/concentration）
      name       —— 规则名（如"高杠杆"）
      clause     —— 监管条款依据
      score      —— 命中的扣分值
      metric     —— 关注的指标名（如"资产负债率"）
      threshold  —— 阈值描述（如"＞70%"）
      hint       —— 风险提示（命中后给业务人员的提示语）
      basis      —— 判定依据/口径说明

    被装饰的函数签名：fn(p) -> (hit: bool, actual: str)
      actual 是实测值的可读文本（未命中时前端也要展示"当前值"与阈值的对比）
    """
    def decorator(fn: Callable):
        RULES.setdefault(dimension, []).append({
            "name": name,
            "clause": clause,
            "score": score,
            "metric": metric,
            "threshold": threshold,
            "hint": hint,
            "basis": basis,
            "check": fn,
        })
        return fn
    return decorator


def _fmt(v, unit: str = "", digits: int = 2, none: str = "无数据") -> str:
    """把数值格式化成可读文本。"""
    if v is None:
        return none
    try:
        f = float(v)
        return f"{f:,.{digits}f}{unit}"
    except (TypeError, ValueError):
        return str(v)


def _fmt_money(v) -> str:
    """金额格式化：自动换算到 万 / 亿。"""
    if v is None:
        return "无数据"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    a = abs(f)
    if a >= 1e8:
        return f"{f / 1e8:,.2f} 亿元"
    if a >= 1e4:
        return f"{f / 1e4:,.2f} 万元"
    return f"{f:,.2f} 元"


# ===========================================================================
# 一、法律风险维度（AHP 权重最高，监管红线信号）
# ===========================================================================

@_rule("legal", "被执行记录", "办法第23条(持续监控)/第30条(风险识别)", 10,
       metric="被执行人记录", threshold="存在即触发",
       hint="公司被法院列为被执行人，反映其债务偿付能力或履约意愿存在问题，可能影响外包服务的持续稳定交付。",
       basis="企查查风控大脑「被执行人」因子扫描")
def _legal_zhixing(p: SupplierProfile):
    evs = [e for e in p.legal_events if "被执行" in e.title]
    return bool(evs), (evs[0].evidence if evs else "未命中")


@_rule("legal", "行政处罚", "办法第17条(尽职调查)/第30条(合规风险)", 15,
       metric="行政处罚记录", threshold="存在即触发",
       hint="受到监管部门行政处罚，说明合规内控存在缺陷，属于《办法》尽职调查重点关注项。",
       basis="企查查风控大脑「行政处罚」因子扫描")
def _legal_penalty(p: SupplierProfile):
    evs = [e for e in p.legal_events if "处罚" in e.title or "罚款" in e.title]
    return bool(evs), (evs[0].evidence if evs else "未命中")


@_rule("legal", "涉诉案件", "办法第30条(风险识别)", 8,
       metric="诉讼/裁判文书", threshold="存在即触发",
       hint="存在诉讼或裁判文书记录，可能牵扯管理层精力与资金，需评估对服务交付的影响。",
       basis="企查查风控大脑「裁判文书」因子扫描")
def _legal_lawsuit(p: SupplierProfile):
    evs = [e for e in p.legal_events
           if any(kw in e.title for kw in ("诉讼", "纠纷", "涉诉", "裁判文书", "立案"))]
    return bool(evs), (evs[0].evidence if evs else "未命中")


@_rule("legal", "失信/严重违法", "办法第16条(准入)/第26条(提供商财务与内控监控)", 30,
       metric="失信被执行人/严重违法名单", threshold="存在即触发",
       hint="被列入失信被执行人名单或严重违法失信名单，属于最高等级法律风险，建议直接启动退出评估。",
       basis="企查查风控大脑「失信信息」「严重违法」因子扫描")
def _legal_dishonest(p: SupplierProfile):
    evs = [e for e in p.legal_events
           if any(kw in e.title for kw in ("失信", "严重违法"))]
    return bool(evs), (evs[0].evidence if evs else "未命中")


@_rule("legal", "股权冻结", "办法第26条(提供商财务与内控监控)", 12,
       metric="股权冻结记录", threshold="存在即触发",
       hint="股权被司法冻结，可能导致公司治理结构不稳定、实际控制权变更风险上升。",
       basis="企查查风控大脑「股权冻结」因子扫描")
def _legal_freeze(p: SupplierProfile):
    evs = [e for e in p.legal_events if "冻结" in e.title]
    return bool(evs), (evs[0].evidence if evs else "未命中")


# ===========================================================================
# 二、经营风险维度
# ===========================================================================

@_rule("operating", "经营异常名录", "办法第16条(准入)/第26条(提供商财务与内控监控)", 30,
       metric="经营异常名录", threshold="存在即触发",
       hint="被列入经营异常名录，通常因未按时公示年报或失联，反映经营管理规范性不足。",
       basis="企查查风控大脑「经营异常」因子扫描")
def _op_abnormal(p: SupplierProfile):
    evs = [e for e in p.legal_events if "经营异常" in e.title]
    return bool(evs), (evs[0].evidence if evs else "未命中")


@_rule("operating", "工商变更频繁", "办法第26条(提供商财务与内控监控)", 12,
       metric="法定代表人 / 股权变更", threshold="存在变更即触发",
       hint="法定代表人、股权或注册地址发生变更，需核查是否影响服务团队稳定性与合同主体连续性。",
       basis="企查查工商变更记录")
def _op_change(p: SupplierProfile):
    evs = [e for e in p.legal_events
           if any(kw in e.title for kw in ("变更", "股权", "出质", "质押"))]
    return bool(evs), (evs[0].evidence if evs else "未命中")


@_rule("operating", "成立时间较短", "办法第16条(准入)/第17条(尽调)", 10,
       metric="公司成立年限", threshold="＜3 年",
       hint="成立时间较短，缺乏长期履约记录与经营数据积累，建议加强尽职调查与履约担保。",
       basis="企查查工商登记「成立日期」")
def _op_young_company(p: SupplierProfile):
    est = (p.registration or {}).get("established")
    if not est:
        return False, "无数据"
    try:
        import re
        m = re.search(r"(\d{4})", str(est))
        if not m:
            return False, str(est)
        years = date.today().year - int(m.group(1))
        return years < 3, f"成立于 {est}（约 {years} 年）"
    except Exception:
        return False, str(est)


@_rule("operating", "人员规模过小", "办法第17条(尽调)/第20条(关联与同业)", 8,
       metric="参保人数", threshold="＜50 人",
       hint="参保人数偏少，服务承载能力可能不足以支撑重要外包项目，需评估交付保障能力。",
       basis="企查查工商登记「参保人数」")
def _op_small_staff(p: SupplierProfile):
    ic = (p.registration or {}).get("insured_count")
    if ic is None:
        return False, "无数据"
    try:
        n = int(str(ic).replace(",", "").strip())
        return n < 50, f"{n} 人"
    except (TypeError, ValueError):
        return False, str(ic)


# ===========================================================================
# 三、财务风险维度（CFA 财务健康度）
# ===========================================================================

@_rule("financial", "审计意见非标准", "办法第17条(尽调)/第26条(提供商财务与内控监控)", 25,
       metric="审计意见", threshold="非「标准无保留」",
       hint="审计意见非标准无保留，说明财务报表存在重大不确定性，需重点关注财务真实性。",
       basis="年报审计报告意见类型")
def _fin_audit(p: SupplierProfile):
    if not p.financials or not p.financials.audit_opinion:
        return False, "无数据"
    op = p.financials.audit_opinion
    return ("标准无保留" not in op), op


@_rule("financial", "经营现金流为负", "办法第26条(提供商财务与内控监控)", 15,
       metric="经营活动现金流", threshold="＜0",
       hint="经营活动现金流为负，自身造血能力不足，长期依赖外部融资，偿债风险上升。",
       basis="现金流量表「经营活动产生的现金流量净额」")
def _fin_cashflow(p: SupplierProfile):
    if not p.financials:
        return False, "无数据"
    v = p.financials.operating_cashflow
    return (v is not None and v < 0), _fmt_money(v)


@_rule("financial", "持续亏损", "办法第17条(尽调)/第26条(提供商财务与内控监控)", 20,
       metric="销售净利率", threshold="＜0（亏损）",
       hint="净利率为负说明主营业务亏损，持续亏损将侵蚀净资产，影响长期服务能力。",
       basis="利润表口径销售净利率")
def _fin_negative_margin(p: SupplierProfile):
    if not p.financials:
        return False, "无数据"
    v = p.financials.net_margin
    return (v is not None and v < 0), _fmt(v, "%")


@_rule("financial", "净资产收益率为负", "办法第26条(提供商财务与内控监控)", 15,
       metric="加权净资产收益率(ROE)", threshold="＜0",
       hint="ROE 为负，股东权益回报为负，说明公司为股东持续创造亏损，财务健康度差。",
       basis="年报加权净资产收益率")
def _fin_negative_roe(p: SupplierProfile):
    if not p.financials:
        return False, "无数据"
    v = p.financials.roe
    return (v is not None and v < 0), _fmt(v, "%")


@_rule("financial", "高杠杆", "办法第26条(提供商财务与内控监控)", 10,
       metric="资产负债率", threshold="＞70%",
       hint="资产负债率偏高，财务杠杆较大，一旦融资环境收紧将面临较大偿债压力。",
       basis="负债总额 / 资产总额")
def _fin_leverage(p: SupplierProfile):
    if not p.financials:
        return False, "无数据"
    v = p.financials.debt_ratio
    return (v is not None and v > 70), _fmt(v, "%")


@_rule("financial", "营收大幅下滑", "办法第26条(提供商财务与内控监控)", 10,
       metric="营业收入同比增长", threshold="＜-20%",
       hint="营业收入大幅下滑，可能反映订单流失或行业景气下行，需关注业务可持续性。",
       basis="本期 vs 上期营业收入")
def _fin_revenue_decline(p: SupplierProfile):
    if not p.financials:
        return False, "无数据"
    v = p.financials.revenue_yoy
    return (v is not None and v < -20), _fmt(v, "%")


@_rule("financial", "净利同比大幅下滑", "办法第26条(提供商财务与内控监控)", 10,
       metric="净利润同比增长", threshold="＜-50%",
       hint="净利润同比大幅下滑，盈利能力显著恶化，需排查是否存在经营重大不利变化。",
       basis="本期 vs 上期净利润")
def _fin_profit_decline(p: SupplierProfile):
    if not p.financials:
        return False, "无数据"
    v = p.financials.profit_yoy
    return (v is not None and v < -50), _fmt(v, "%")


@_rule("financial", "流动性紧张", "办法第26条(提供商财务与内控监控)", 8,
       metric="流动比率", threshold="＜1.0",
       hint="流动比率低于 1，短期偿债能力偏弱，可能面临流动性压力，影响付款与工资发放。",
       basis="流动资产 / 流动负债")
def _fin_liquidity(p: SupplierProfile):
    if not p.financials:
        return False, "无数据"
    v = p.financials.current_ratio
    # 用 v < 1 而非 0 < v < 1：比率为 0（流动资产为零/缺失填 0）或异常为负时，
    # 恰恰是最坏情况，原写法会漏判，让最差的公司显得比 0.9 的还「健康」。
    return (v is not None and v < 1), _fmt(v, "", 2)


@_rule("financial", "速动比率偏低", "办法第26条(提供商财务与内控监控)", 6,
       metric="速动比率", threshold="＜0.5",
       hint="速动比率偏低，扣除存货后的快速变现能力不足，短期资金链压力较大。",
       basis="(流动资产-存货) / 流动负债")
def _fin_quick(p: SupplierProfile):
    if not p.financials:
        return False, "无数据"
    v = p.financials.quick_ratio
    # 同上：0 或负值是比 0.4 更糟的情形，不能被 0 < v 挡掉
    return (v is not None and v < 0.5), _fmt(v, "", 2)


# ===========================================================================
# 四、舆情风险维度
# ===========================================================================

@_rule("reputation", "高严重度负面舆情", "办法第30条(声誉等风险)", 20,
       metric="负面舆情（高严重度）", threshold="存在即触发",
       hint="存在高严重度负面舆情，可能影响银行声誉与客户信任，需评估事件真实性与影响面。",
       basis="新闻源 + LLM 情感分析")
def _rep_high_neg(p: SupplierProfile):
    hits = [n for n in p.news if n.sentiment == "negative" and n.severity == "high"]
    return bool(hits), (hits[0].title if hits else "未命中")


@_rule("reputation", "一般负面舆情", "办法第30条(声誉等风险)", 8,
       metric="负面舆情（一般）", threshold="存在即触发",
       hint="存在一般负面舆情，建议持续观察，判断是否演化为系统性声誉风险。",
       basis="新闻源 + LLM 情感分析")
def _rep_neg(p: SupplierProfile):
    hits = [n for n in p.news if n.sentiment == "negative" and n.severity != "high"]
    return bool(hits), (hits[0].title if hits else "未命中")


# ===========================================================================
# 五、关联与集中度维度
# ===========================================================================

@_rule("concentration", "重要外包依赖度过高", "办法第33条(集中度风险)", 10,
       metric="外包重要程度", threshold="判定为「重要外包」",
       hint="属于重要外包，一旦服务中断将直接影响银行关键业务，需建立退出预案与备选供应商。",
       basis="《办法》第十二、十三条「重要外包」判定")
def _con_important(p: SupplierProfile):
    return p.important_outsourcing, ("判定为重要外包" if p.important_outsourcing else "非重要外包")


@_rule("concentration", "跨境外包", "办法第19条(跨境外包)", 8,
       metric="是否跨境外包", threshold="判定为跨境",
       hint="涉及跨境外包，需额外关注数据出境合规、法律管辖与服务连续性风险。",
       basis="《办法》第十九条跨境外包评估")
def _con_cross_border(p: SupplierProfile):
    return p.cross_border, ("涉及跨境外包" if p.cross_border else "非跨境外包")


@_rule("concentration", "非驻场外包", "办法第18条(非驻场附加尽调)", 5,
       metric="是否非驻场", threshold="判定为非驻场",
       hint="非驻场外包对现场管控较弱，需加强远程监控与数据安全管理。",
       basis="《办法》第十八条非驻场外包认定")
def _con_non_resident(p: SupplierProfile):
    return p.non_resident, ("非驻场外包" if p.non_resident else "驻场外包")


# ---------------------------------------------------------------------------
# 打分：单维度 / 全维度
# ---------------------------------------------------------------------------

def score_dimension(p: SupplierProfile, dimension: str) -> Dict:
    """对单个维度打分：100 分起扣。

    返回结构包含：
      score      —— 维度得分
      deducted   —— 总扣分
      triggered  —— 命中的规则明细（含七要素）
      checks     —— 全部规则的检查结果（含未命中项），供前端展示"完整评分细则"
    """
    triggered = []
    checks = []
    deducted = 0
    errors = []   # 规则内部异常：必须显式记录，不能被当成"未命中"

    for rule in RULES.get(dimension, []):
        rule_error = None
        try:
            hit, actual = rule["check"](p)
        except Exception as e:
            # ⚠️ 关键：规则抛异常时若默认为 hit=False，表现为"该检查通过、不扣分"，
            #    等于**把风险静默放过**（风控系统最坏的失败方向）。
            #    这里保留温和的默认值以免单条规则拖垮整体评分，但必须把异常
            #    记录进 errors 并在返回结构中暴露，使问题可见、可追。
            hit, actual = False, f"检查异常: {e}"
            rule_error = f"{rule['name']}: {type(e).__name__}: {e}"
            errors.append(rule_error)

        item = {
            "name": rule["name"],
            "clause": rule["clause"],
            "metric": rule["metric"],
            "threshold": rule["threshold"],
            "hint": rule["hint"],
            "basis": rule["basis"],
            "actual": actual if actual is not None else "无数据",
            "deduct": rule["score"] if hit else 0,
            "max_deduct": rule["score"],
            "hit": bool(hit),
            "error": rule_error,   # 非 None 表示该规则执行失败
        }
        checks.append(item)
        if hit:
            triggered.append(item)
            deducted += rule["score"]

    score = max(0, 100 - deducted)
    return {
        "score": score,
        "deducted": deducted,
        "triggered": triggered,
        "checks": checks,
        "errors": errors,
    }


def score_all_dimensions(p: SupplierProfile) -> Dict[str, Dict]:
    """对全部五个维度打分，返回维度名 -> 得分详情。"""
    return {dim: score_dimension(p, dim) for dim in RULES}


def dim_has_data(p: SupplierProfile, dimension: str) -> bool:
    """判断某维度是否有可评估的数据（涉密文件 strict_na 模式用）。

    原则：没有数据 ≠ 健康。若文件中未提供某维度的任何内容，
    该维度应显示"无信息"（na），而不是默认 100 分起扣后拿满分。
    """
    if dimension == "legal":
        return bool(p.legal_events)
    if dimension == "operating":
        return bool(p.registration)  # 成立年限 / 参保人数等工商信息
    if dimension == "financial":
        f = p.financials
        if not f:
            return False
        keys = ("debt_ratio", "current_ratio", "quick_ratio", "net_margin",
                "gross_margin", "roe", "roa", "operating_cashflow",
                "net_profit", "revenue", "total_assets", "revenue_yoy",
                "profit_yoy", "audit_opinion")
        return any(getattr(f, k, None) is not None for k in keys)
    if dimension == "reputation":
        return bool(p.news)
    if dimension == "concentration":
        # 集中度的 3 条规则（重要/非驻场/跨境）**只消费外包标记**；股东结构当前
        # 没有任何规则消费，不能作为"维度可评"的依据——否则仅有股东结构时（如企查查
        # 实时路径：返回股东、不返回本行外包标记），维度会以零扣分拿 100 分，
        # 把"标记未知"包装成"无依赖风险"。
        # 三个 bool 标记默认 False 无法区分"未提供"与"明确为否"，
        # 故由数据方（本地库/文件解析器）在确实提供这些字段时打 conc_flags_provided。
        return bool(getattr(p, "conc_flags_provided", False))
    return True


def dimension_risk_note(dim: str, score: int) -> str:
    """按维度得分给出整体风险提示语（供前端卡片展示）。"""
    if score >= 90:
        lvl = "优异"
        tail = "该维度未发现显著风险信号，保持常规监控即可。"
    elif score >= 75:
        lvl = "良好"
        tail = "该维度存在个别关注项，建议纳入常态化监控。"
    elif score >= 60:
        lvl = "偏弱"
        tail = "该维度存在多项风险信号，建议加强监控并要求供应商说明。"
    else:
        lvl = "严峻"
        tail = "该维度风险信号集中，建议立即约谈并评估合作可持续性。"

    notes = {
        "legal": "法律风险反映供应商的合规状况与涉诉情况，是《办法》中最明确的红线领域。",
        "operating": "经营风险反映供应商的组织稳定性与履约能力，变更频繁可能影响服务连续性。",
        "financial": "财务风险反映供应商的偿债能力、盈利能力与现金流健康度。",
        "reputation": "舆情风险反映供应商的社会声誉与客户评价，负面舆情可能波及银行声誉。",
        "concentration": "关联与集中度风险反映外包对银行的依赖程度与跨境合规风险。",
    }
    return f"{lvl}。{notes.get(dim, '')}{tail}"
