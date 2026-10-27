"""评级引擎 —— 汇总规则打分 + 服务水平 + AHP 定权，输出综合风险分与红黄绿等级。

流水线终点：供应商数据 → 各维度分 → AHP 加权 → 综合分 → 等级 + 预警。

六维说明（2026-09 扩展）
-----------------------
维度分两类：
  base（企业底子，来自外部可查数据） 法律 / 经营 / 财务 / 舆情 / 关联集中度
  service（履约表现，来自第25条）    履约表现 performance

service 维度的引入让评分从"这家公司正不正规"扩展到"这次外包干得好不好"。
若某供应商尚未录入服务水平数据，该维度标记 na，并用 ahp.renormalize
把它的权重按比例分配给其余维度 —— 既不凭空加分，也不凭空扣分。
"""
import os
import sys
# 允许直接运行本文件（把 app 根目录加入路径，供 `from core...` 使用）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from typing import Dict, Optional

from core.schemas import SupplierProfile, RiskScore
from core.scoring import score_all_dimensions, dim_has_data
from core.ahp import get_weights, renormalize, DIMENSION_LABELS, DIM_KIND, DIMENSIONS
from core import performance as perf

# 等级阈值（综合分 0-100）
LEVEL_THRESHOLDS = [
    ("red", 60),      # < 60 高危
    ("yellow", 80),   # 60-80 关注
    ("green", 100),   # > 80 正常
]

# 「源里没有该维度数据」按 na 处理（而不是按 0 扣分=100 分参与加权）。
# 默认开启：公开源没提供的维度不再算满分，权重按比例分摊给其余可用维度。
# 设 RATING_NA_UNKNOWN=0 可回退到 2026-10-17 之前的旧口径（仅用于复现历史快照）。
NA_UNKNOWN = os.environ.get("RATING_NA_UNKNOWN", "1").strip().lower() not in ("0", "false", "no", "off")

# 红线熔断规则：命中任一即直接判"红"，不等加权分稀释。
# 依据银行风控实务：某些信号是"一票否决"级别的，不能被权重稀释。
RED_LINE_RULES = [
    ("审计意见无法表示/否定", lambda p: p.financials and p.financials.audit_opinion in ("无法表示意见", "否定意见")),
    ("被列为被执行人", lambda p: any("被执行" in e.title for e in p.legal_events)),
]


def _check_red_line(p: SupplierProfile):
    """返回命中的红线规则列表。"""
    return [name for name, check in RED_LINE_RULES if check(p)]


# ---------------------------------------------------------------------------
# 加权之外的修正机制：短板约束（限上限）+ 信号升级（抬下限）
#
# 为什么需要
# ----------
# 加权平均是"补偿性"的：一个维度差，会被其他维度好所抵消。
# 实测案例（恒生电子）：履约表现 69.4（权重最高 37.9%、7 项指标连续未达标、
# 预测下期跌至 60.6），但因财务/舆情均为 100，综合分仍 84.03 → 判"绿·常规监控"，
# 单点系统性风险被权重稀释。
#
# 依据《办法》第27条（督促纠正）、第30条(五)（服务水平下降），
# 某些风险不该被平均掉。故在加权 + 红线之外再施加两道约束：
#   短板约束（限上限）：木桶效应 —— 最低维度过低时，等级不得偏高
#   信号升级（抬下限）：恶化信号 —— 履约维度出现连续未达标/悬崖/前瞻预警时，等级不得偏低
# 分数保持加权结果不变，只修正"该怎么处置"，做到分数（客观）与等级（风控判断）分离。
# ---------------------------------------------------------------------------

# 等级序（数值越大越"安全"），用于"限上限 / 抬下限"
LEVEL_ORDER = {"red": 0, "yellow": 1, "green": 2}

# 短板约束：(最低维度分阈值, 等级上限, 说明)。按阈值从低到高匹配，命中即用。
FLOOR_CONSTRAINTS = [
    (35.0, "red", "存在严重短板维度（最低维度分 < 35）"),
    (50.0, "yellow", "存在明显短板维度（最低维度分 < 50）"),
]

# 信号升级：命中任一恶化信号，等级至少抬到 yellow
SIGNAL_ESCALATION_LEVEL = "yellow"
CONSECUTIVE_THRESHOLD = 3      # 连续未达标指标数阈值（项）
FORECAST_ALERT = 70.0          # 前瞻预警线（与 performance.FORECAST_ALERT 一致）


def _check_floor_constraint(dim_scores: dict):
    """短板约束：只看参与加权的维度（score 非 None），取最低分。
    返回 (等级上限, 原因) 或 None。"""
    avail = [(k, d) for k, d in dim_scores.items() if d.get("score") is not None]
    if not avail:
        return None
    dim_key, lowest = min(avail, key=lambda kv: kv[1]["score"])
    score = lowest["score"]
    label = lowest.get("label") or DIMENSION_LABELS.get(dim_key, dim_key)
    for th, cap, reason in FLOOR_CONSTRAINTS:
        if score < th:
            return cap, f"{reason}：「{label}」仅 {score} 分"
    return None


def _check_signal_escalation(dim_scores: dict):
    """信号升级：检查履约表现维度的恶化信号。
    返回原因字符串（多个用；连接）或 None。"""
    p = dim_scores.get(perf.DIM_KEY) or {}
    reasons = []
    consec = p.get("consecutive_count") or 0
    if consec >= CONSECUTIVE_THRESHOLD:
        reasons.append(f"履约表现有 {consec} 项指标连续未达标")
    cliff = p.get("cliff")
    if cliff and cliff.get("severity") == "high":
        reasons.append("履约表现出现悬崖式下滑")
    fc = p.get("forecast")
    if (fc and fc.get("value") is not None
            and fc["value"] < FORECAST_ALERT and fc.get("direction") == "down"):
        # 点预测跌破预警线只是"值得关注"，不足以单独抬等级：3–5 期拟合的外推
        # 误差常有 ±8 分，把 68 分当成事实会让等级被噪声推着走。
        # 因此要求整个 90% 预测区间的上界都在预警线以下（即"即便按乐观情形也跌破"）。
        upper = fc.get("p_upper")
        if upper is None or upper < FORECAST_ALERT:
            band = (f"，90% 区间 {fc['p_lower']}–{upper} 分" if upper is not None else "")
            reasons.append(f"履约表现预测下期跌破 {FORECAST_ALERT:.0f} 分"
                           f"（点预测 {fc['value']} 分{band}，趋势下行）")
    return "；".join(reasons) if reasons else None


def _service_eval_for(supplier_id: str = "", supplier_name: str = "") -> Optional[dict]:
    """尽力而为地取该供应商的服务水平评价；任何异常都退化为"无数据"。

    用 try/except 包住是因为：服务水平只是六维之一，
    它取不到不应该让整个评分流程失败（评级要尽可能降级而不是崩溃）。
    """
    try:
        from core import servicelevel as sl
        sid = supplier_id
        if not sid and supplier_name:
            sid = sl.find_supplier_id_by_name(supplier_name)
        if not sid:
            return None
        return sl.evaluate_supplier(sid)
    except Exception:
        return None


def rate_supplier(p: SupplierProfile, supplier_id: str = "",
                  strict_na: bool = False) -> RiskScore:
    """对单个供应商评级：返回综合分 + 等级 + 各维度明细。

    supplier_id 用于关联服务水平（第25条）数据；留空时按名称反查。
    strict_na=True（涉密文件上传通道）：文件中未提供某维度任何内容的，
    该维度标 na（无信息），不参与加权 —— 没有 ≠ 健康，绝不臆造分数。
    """
    dim_scores = score_all_dimensions(p)

    # ---- strict_na：无数据的维度置为 na（score=None）----
    if strict_na:
        for dim, detail in dim_scores.items():
            if dim == perf.DIM_KEY:
                continue  # 履约表现维度有自己的 na 逻辑
            if not dim_has_data(p, dim):
                detail["score"] = None
                detail["na"] = True
                detail["reason"] = (
                    "上传文件中未提供该维度的评价内容，系统不臆造分数："
                    "该维度不参与本次评分，其权重已按比例分摊给其余可用维度。"
                )

    # ---- 从未取数的维度同样标 na（「源里没有该字段」≠「零风险」）----
    # 与上面的「取数失败」同源不同因：某些维度根本没有数据来源，按 0 扣分=100 分
    # 参与加权会把总分系统性抬高（资料最少的最健康）。统一按 na 处理，权重按比例
    # 分摊给有数据的维度；可用维度不足 3 个时由下面的 insufficient 封顶黄。
    #
    # ponytail: 口径开关，**默认开启**（交付快照与方案书口径即为"缺失维度按 na、
    # 权重再归一化"，22 家分布 green 11 / yellow 11 / red 0 就是在这个口径下得出的）。
    # 打开后 22 家可评级分数全部下降、7 家降档、分布从 green18/yellow3/red1 变成
    # green11/yellow11/red0（恒生的 red 来自真实取数时的被执行人熔断，离线缓存里没有
    # 司法数据，复现不出来）。设 RATING_NA_UNKNOWN=0 可回退到 2026-10-17 之前的旧口径，
    # 仅用于复现历史快照，避免新旧数字被无声混用。
    if not strict_na and NA_UNKNOWN:
        for dim in list(dim_scores):
            if dim == perf.DIM_KEY:
                continue
            detail = dim_scores.get(dim)
            if detail is None or detail.get("score") is None:
                continue
            if dim_has_data(p, dim):
                continue
            if not [d for d in dim_scores
                    if d != dim and dim_scores[d].get("score") is not None]:
                continue  # 保险：别把唯一有数据的维度也标 na
            detail["score"] = None
            detail["na"] = True
            detail["reason"] = (
                "各公开数据源均未提供该维度的数据（不是取数失败，是本来就没有），"
                "系统不臆造分数：该维度不参与本次评分，其权重已按比例分摊给其余可用维度。"
            )

    failed_dims = list(getattr(p, "unavailable_dims", None) or [])
    if failed_dims:
        # 保险：不要把可用维度全 na 光。至少留一个维度参与加权，否则总分恒为 0。
        remaining = [d for d in dim_scores
                     if d not in failed_dims and dim_scores[d].get("score") is not None]
        if remaining:
            for dim in failed_dims:
                detail = dim_scores.get(dim)
                if detail is None or detail.get("score") is None:
                    continue
                detail["score"] = None
                detail["na"] = True
                detail["reason"] = (
                    "该维度本次**取数失败**（网络/额度/超时等），与「确实无风险」不同："
                    "系统不臆造分数，该维度不参与本次评分，其权重已按比例分摊给其余可用维度。"
                )

    # ---- 第六维：履约表现（第25条服务水平结论）----
    # 优先用台账服务水平数据（多期，可算趋势/悬崖/外推）；
    # 台账没有时，退回**上传文件自带的履约指标快照**（单期，不做趋势外推），
    # 避免"文件里明明有履约数据，履约维度却显示未录入"。
    service_eval = _service_eval_for(supplier_id, p.name)
    if service_eval and service_eval.get("has_data"):
        perf_dim = perf.build_dimension(service_eval)
    else:
        perf_dim = perf.build_dimension_from_snapshot(
            getattr(p, "perf_snapshot", None))
    dim_scores[perf.DIM_KEY] = perf_dim

    # ---- AHP 加权（na 维度不做分母）----
    weights, cr = get_weights()
    available = [d for d in weights if dim_scores.get(d, {}).get("score") is not None]
    na_dims = [d for d in weights if d not in available]

    if strict_na and not available:
        # 文件中没有任何可评估内容：明确拒绝评分，避免给出误导性结论
        raise ValueError("文件中未识别到任何可评估内容（无财务指标、事件或外包标记），无法评分。请确认文件内容或格式。")

    # 可用维度太少时，重归一化会把剩余维度的权重抬到 1.0 —— 单维一个好指标
    # 就能把总分顶到 100 分（实测：只上传工商信息的文件，operating 无扣分即 100 分、
    # 判 green）。这类分数不可用于决策，统一封顶 yellow 并标注数据不足。
    insufficient = len(available) < 3

    eff_weights = renormalize(weights, available)

    weighted = sum(
        dim_scores[dim]["score"] * eff_weights[dim]
        for dim in eff_weights
    )
    weighted_raw = min(weighted, 100)      # 阈值比较用未取整值，避免 59.996→60 跨档
    total = round(weighted_raw, 2)         # 钳制上限 100

    # ---- 覆盖度与"公开数据对照分" ----
    # 缺哪个维度，是每家不相同的，重归一化之后的权重分布也随之不同：
    # 3 维企业里单个维度的有效权重可能是 6 维企业的两倍，分数之间**不能直接横比**。
    # 因此把"用了几个维度、缺了哪几个、属于哪个覆盖组"显式输出，让比较只在同组内进行。
    unavailable = [d for d in DIM_KIND if d not in eff_weights]
    base_avail = [d for d in DIM_KIND if DIM_KIND[d] == "base" and d in eff_weights]
    baseline_score = None
    if base_avail:
        bw = {d: weights.get(d, 0) for d in base_avail}
        bsum = sum(bw.values())
        if bsum > 0:
            baseline_score = round(min(
                sum(dim_scores[d]["score"] * bw[d] for d in base_avail) / bsum, 100), 2)
    coverage = {
        "used": len(available),
        "available": sorted(available),
        "missing": sorted(unavailable),
        "base_available": len(base_avail),
        "group": "+".join(sorted(unavailable)) or "full",
        "comparable": "同覆盖组内可比（缺少的维度相同才可直接比较分数）",
    }

    # 红线熔断：命中即红
    red_lines = _check_red_line(p)
    constraints = []          # 记录本次等级被哪些约束调整过（供前端"为何升级/降级"展示）
    if red_lines:
        level = "red"
    else:
        # 等级映射
        level = "green"
        for lv, th in LEVEL_THRESHOLDS:
            if weighted_raw < th:
                level = lv
                break

        # ---- 短板约束（限上限）：防"一俊遮百丑" ----
        floor = _check_floor_constraint(dim_scores)
        if floor:
            cap, reason = floor
            if LEVEL_ORDER[level] > LEVEL_ORDER[cap]:
                level = cap
                constraints.append({"type": "floor", "level": cap, "reason": reason})

        # ---- 信号升级（恶化信号 → 等级不得判为"绿·正常"）----
        # 注意：LEVEL_ORDER 越大越"安全"，故此处用 >（当前等级过于宽松时才压低）
        sig = _check_signal_escalation(dim_scores)
        if sig and LEVEL_ORDER[level] > LEVEL_ORDER[SIGNAL_ESCALATION_LEVEL]:
            level = SIGNAL_ESCALATION_LEVEL
            constraints.append({"type": "signal", "level": SIGNAL_ESCALATION_LEVEL, "reason": sig})

        # ---- 可用维度不足（<3）：单维高分不代表整体健康，等级封顶黄 ----
        if insufficient and LEVEL_ORDER[level] > LEVEL_ORDER["yellow"]:
            level = "yellow"
            constraints.append({
                "type": "insufficient", "level": "yellow",
                "reason": (f"仅 {len(available)} 个维度有数据（少于 3 个），"
                           "综合评价依据不足，等级上限为黄、仅供参考"),
            })

    return RiskScore(
        supplier=p.name,
        total_score=total,
        level=level,
        dimension_scores={
            dim: {
                "score": dim_scores[dim]["score"],
                "deducted": dim_scores[dim]["deducted"],
                "triggered": dim_scores[dim]["triggered"],
                "checks": dim_scores[dim].get("checks", []),
                "weight": eff_weights.get(dim, 0),
                "raw_weight": weights.get(dim, 0),
                "na": bool(dim_scores[dim].get("na")),
                "label": DIMENSION_LABELS.get(dim, dim),
                "kind": DIM_KIND.get(dim, "base"),
                "note": dim_scores[dim].get("reason", "") or _dim_note(dim, dim_scores[dim]),
            }
            for dim in dim_scores
        },
        # 附加红线命中信息（供前端展示"为何熔断"）
        red_lines=red_lines,
        weights=eff_weights,
        cr=cr,
        dimension_labels=dict(DIMENSION_LABELS),
        dim_kind=dict(DIM_KIND),
        na_dimensions=[DIMENSION_LABELS.get(d, d) for d in na_dims],
        raw_weights=weights,
        performance={
            k: v for k, v in perf_dim.items()
            if k in ("score", "deducted", "grade", "decay", "avg", "latest",
                     "forecast", "cliff", "consecutive_count", "advice", "summary", "na")
        },
        constraints=constraints,
        # 覆盖度：谁缺维度、缺哪几个、能不能横比（缺维度的组不能比）
        coverage=coverage,
        # 不含履约维度的对照分：履约数据在本演示中是模拟填充的，主分数会受其影响；
        # 对照分只用公开数据可得的基础维度，用于让"分数里有多少来自模拟数据"可被检验。
        baseline_score=baseline_score,
        data_origin={"performance": getattr(perf, "DATA_ORIGIN", "simulated")},
    )


def _dim_note(dim: str, detail: dict) -> str:
    """维度风险提示语：履约表现走 performance 专属文案，na 维度给"无信息"说明，其余走 scoring 通用文案。"""
    if dim == perf.DIM_KEY:
        return perf.dimension_note(detail.get("score"), detail.get("grade", ""))
    from core.scoring import dimension_risk_note
    s = detail.get("score")
    if s is None:
        return ("无信息：未获取到该维度的评价内容，系统不臆造分数。"
                "该维度未参与本次综合安全度计算，其权重已按比例分摊给其余可用维度。")
    return dimension_risk_note(dim, s)


def level_to_action(level: str) -> str:
    """等级 → 处置动作（对应《办法》的监管要求）。"""
    actions = {
        "red": "立即约谈 + 启动退出预案评估 + 24h 报告",
        "yellow": "发送整改提醒 + 加密监控频率",
        "green": "常规周期监控",
    }
    return actions.get(level, "常规周期监控")


if __name__ == "__main__":
    from adapters.mock_adapter import MockAdapter

    p = MockAdapter().fetch_supplier("XX科技有限公司")
    r = rate_supplier(p, supplier_id="S001")
    print(f"供应商: {r.supplier}")
    print(f"综合分: {r.total_score} | 等级: {r.level} | 处置: {level_to_action(r.level)}")
    print(f"一致性比率 CR = {r.cr}")
    print("各维度：")
    for dim, d in r.dimension_scores.items():
        tag = "履约" if d.get("kind") == "service" else "底子"
        na = " [未录入·不参与加权]" if d.get("na") else ""
        print(f"  [{tag}] {d.get('label', dim)}: {d['score']} 分 "
              f"(权重 {d.get('weight')}) 触发 {len(d['triggered'])} 条{na}")
