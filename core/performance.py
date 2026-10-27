"""履约表现维度 —— 把第25条服务水平结果折算成 AHP 六维中的一维。

为什么需要这个模块
------------------
在此之前系统存在一个结构性缺陷：
    五个维度（法律/经营/财务/舆情/集中度）全部来自"外部可查数据"，
    回答的是"这家公司正不正规"；
而真正回答"这次外包干得好不好"的第25条服务水平，
    只挂在监控中心的独立页签上，不参与任何评分。

结果是评分体系与外包实务脱节 —— 一家服务天天宕机但工商司法干净的公司，
综合分依然很高。本模块把服务水平结论折成"履约表现"维度，接进 AHP 加权，
让"干得好不好"真正影响最终结论。

设计要点
--------
1. 维度满分 100，由服务水平评价的【时效衰减加权分】直接映射
   （用衰减分而非算术平均：近期表现比远期更重要）。
2. 无服务水平数据时不臆造分数，而是返回 na，
   由 ahp 层做权重再归一化 —— 既不加分也不扣分。
3. 悬崖式下滑额外扣分（单期跌幅 ≥ 12 分），并在提示语中明示，
   因为"服务悬崖"是第30条(五)"服务水平下降"的典型形态。
4. 预测下期得分低于警戒线时给出前瞻性提示，
   这是从"事后统计"转向"事前预警"的关键一步。
"""
import os
from typing import Optional

DIM_KEY = "performance"
DIM_LABEL = "履约表现"

# 本维度数据出处。本演示没有行内真实服务水平台账，用的是
# data/service_levels.json 里的确定性模拟填充（40 家 / 226 条 / 0–8 期）。
# 这个标签会随评分结果透出到接口与前端横幅，避免把模拟分当成实测结论；
# 接上行内真实系统后设 FENGHUO_PERF_DATA_ORIGIN=row-data 即可。
DATA_ORIGIN = os.environ.get("FENGHUO_PERF_DATA_ORIGIN", "simulated").strip() or "simulated"

# 悬崖式下滑的额外扣分（在服务水平得分基础上再扣，体现"趋势恶化"的独立风险）
CLIFF_PENALTY = {"high": 12, "warn": 6}

# 预测下期得分低于此值时触发前瞻提示
FORECAST_ALERT = 70.0


def _grade_of(score: Optional[float]) -> str:
    if score is None:
        return "na"
    if score >= 90:
        return "优秀"
    if score >= 75:
        return "良好"
    if score >= 60:
        return "关注"
    return "预警"


def build_dimension(service_eval: Optional[dict]) -> dict:
    """由服务水平评价结果构造「履约表现」维度，结构对齐 scoring.py 的维度返回。

    返回结构与 score_dimension() 保持一致（score / deducted / triggered / checks），
    这样 rating.py 可以像处理其它五维一样直接加权，无需特判。
    """
    if not service_eval or not service_eval.get("has_data"):
        return {
            "score": None,
            "deducted": 0,
            "triggered": [],
            "checks": [],
            "na": True,
            "reason": "尚未录入服务水平数据，本维度不参与加权（权重将再归一化）",
        }

    decay = service_eval.get("decay") or {}
    dec = decay.get("score")
    if dec is None:
        return {
            "score": None, "deducted": 0, "triggered": [], "checks": [],
            "na": True, "reason": "服务水平数据不完整，无法计算履约表现分",
        }

    checks = []
    triggered = []
    deducted = 0.0

    # ---- 检查 1：时效衰减加权分（基础映射）----
    base_deduct = round(max(0.0, 100.0 - float(dec)), 1)
    checks.append({
        "name": "服务水平时效衰减加权分",
        "clause": "第25条(效能和质量监控指标)",
        "metric": "近 3 期衰减加权得分",
        "threshold": "≥ 90 满分档 / ≥ 75 良好 / ≥ 60 关注 / < 60 预警",
        "hint": "按「越近越重要」加权（近 3 期 0.5/0.3/0.2），近期服务恶化会被放大。",
        "basis": f"近 {decay.get('window', 0)} 期加权；算术平均为 {decay.get('avg')} 分",
        "actual": f"{dec} 分（{_grade_of(dec)}）",
        "deduct": base_deduct,
        "max_deduct": 100,
        "hit": base_deduct > 0,
    })
    if base_deduct > 0:
        triggered.append(checks[-1])
        deducted += base_deduct

    # ---- 检查 2：悬崖式下滑（独立于分数高低的趋势风险）----
    cliff = decay.get("cliff")
    if cliff:
        pen = CLIFF_PENALTY.get(cliff.get("severity"), 6)
        item = {
            "name": "服务水平悬崖式下滑",
            "clause": "第30条(五) 服务水平下降",
            "metric": "单期得分跌幅",
            "threshold": "≥ 12 分即触发",
            "hint": ("服务表现出现断崖式下跌，往往先于实质事故暴露，"
                     "建议立即约谈并要求书面说明。"),
            "basis": f"{cliff.get('from_period')} → {cliff.get('to_period')}",
            "actual": f"{cliff.get('from')} 分 → {cliff.get('to')} 分（跌 {cliff.get('drop')} 分）",
            "deduct": pen,
            "max_deduct": CLIFF_PENALTY["high"],
            "hit": True,
        }
        checks.append(item)
        triggered.append(item)
        deducted += pen

    # ---- 检查 3：连续未达标指标（第27条抓手）----
    consec = service_eval.get("consecutive") or []
    if consec:
        pen = min(8, 2 * len(consec))     # 每项 2 分，上限 8 分
        labels = "、".join(str(c.get("label") or "") for c in consec[:4])
        item = {
            "name": "指标连续未达标",
            "clause": "第27条(督促纠正)",
            "metric": "连续 2 期以上未达标的指标数",
            "threshold": "0 项",
            "hint": "连续未达标说明是系统性问题而非偶发波动，应督促限期整改。",
            "basis": "最近连续若干个报告期均非达标",
            "actual": f"{len(consec)} 项（{labels}）",
            "deduct": pen,
            "max_deduct": 8,
            "hit": True,
        }
        checks.append(item)
        triggered.append(item)
        deducted += pen

    # ---- 检查 4：前瞻预警（预测下期跌破警戒线）----
    fc = decay.get("forecast")
    if fc and fc.get("value") is not None and fc["value"] < FORECAST_ALERT:
        pen = 5
        item = {
            "name": "服务水平前瞻预警",
            "clause": "第23条(持续监控)/第30条(五)",
            "metric": "下期得分预测（线性外推）",
            "threshold": f"≥ {FORECAST_ALERT} 分",
            "hint": ("按当前趋势外推，下一报告期服务水平可能跌破警戒线，"
                     "建议提前介入而非等出事后处置。"),
            "basis": f"近 5 期最小二乘拟合，斜率 {fc.get('slope')}",
            "actual": f"预测下期 {fc['value']} 分（趋势{'下行' if fc['direction']=='down' else ('上行' if fc['direction']=='up' else '持平')}）",
            "deduct": pen,
            "max_deduct": 5,
            "hit": True,
        }
        checks.append(item)
        triggered.append(item)
        deducted += pen

    score = round(max(0.0, 100.0 - deducted), 1)

    return {
        "score": score,
        "deducted": round(deducted, 1),
        "triggered": triggered,
        "checks": checks,
        "na": False,
        "grade": _grade_of(dec),
        "decay": dec,
        "avg": decay.get("avg"),
        "latest": decay.get("latest"),
        "forecast": fc,
        "cliff": cliff,
        "consecutive_count": len(consec),
        "advice": service_eval.get("advice") or "",
        "summary": _summary(dec, score, decay, cliff, fc),
    }


def build_dimension_from_snapshot(snap: Optional[dict]) -> dict:
    """用**上传文件自带的履约指标**构造「履约表现」维度（无台账服务水平数据时的兜底）。

    尽调报告 / 履约报告常自带第25条效能质量指标的直接证据
    （合同履约率、订单准时交付率、一次验收合格率、SLA 达成率、履约评价得分…）。
    此前因为没有接入而被标成 na —— "文件里有履约数据，履约维度却显示未录入"。

    ⚠️ 诚实性：这是**单期快照**，没有多期序列，因此不做趋势外推与悬崖判定；
    返回里用 source="upload" 标明来源，前端据此提示"未接入台账多期记录"。
    """
    if not snap or snap.get("score") is None:
        return build_dimension(None)

    score = float(snap["score"])
    items = snap.get("items") or {}
    extra = "、".join(f"{k} {v}" for k, v in list(items.items())[:6])

    eval_ = {
        "has_data": True,
        # 单期：window=1，加权=算术平均，无悬崖、无预测
        "decay": {"score": score, "avg": score, "latest": score,
                  "window": 1, "cliff": None, "forecast": None},
        "consecutive": [],
        "advice": "履约指标取自上传文件（单期），未接入台账多期记录，暂不做趋势外推与悬崖判定。",
    }
    dim = build_dimension(eval_)

    # 改写第一条 check 的表述，标明数据来源，避免被误读成"多期台账数据"
    if dim.get("checks"):
        c = dim["checks"][0]
        c["name"] = "文件履约指标综合得分"
        c["metric"] = "上传文件中的履约指标"
        c["basis"] = (extra or "上传文件") + f"；综合得分 {score} 分"
        c["hint"] = ("综合合同履约率、准时交付率、一次验收合格率、SLA 达成率与"
                     "履约评价维度得分得出；数据来自上传文件，未接入台账多期记录。")
    dim["source"] = "upload"
    dim["snapshot_score"] = score
    dim["snapshot_items"] = items
    return dim


def _summary(dec, score, decay, cliff, fc) -> str:
    """一句话说明本维度分数的由来（供前端直接展示）。"""
    parts = [f"时效衰减加权 {dec} 分"]
    if decay.get("avg") is not None and abs(decay["avg"] - dec) >= 1:
        parts.append(f"算术平均 {decay['avg']} 分")
    if cliff:
        parts.append(f"含悬崖式下滑扣 {CLIFF_PENALTY.get(cliff.get('severity'), 6)} 分")
    if fc and fc.get("direction") == "down":
        parts.append(f"预测下期 {fc['value']} 分")
    return "，".join(parts) + f"；履约表现维度最终 {score} 分"


def dimension_note(score: Optional[float], grade: str = "") -> str:
    """履约表现维度的风险提示语（与 scoring.dimension_risk_note 风格一致）。"""
    if score is None:
        return ("无数据。尚未录入该供应商的服务水平（第25条）数据，"
                "本维度暂不参与加权 —— 建议尽快补录以形成完整评价。")
    base = "履约表现反映服务提供商在合同期内的实际服务水平，是《办法》第25条六类指标的直接结论。"
    if score >= 90:
        return f"优异。{base}当前服务持续达标，可按期续约并保持常规监控。"
    if score >= 75:
        return f"良好。{base}存在个别未达标项，建议书面提醒并跟踪改善。"
    if score >= 60:
        return (f"偏弱。{base}多项指标未达标，建议按第27条督促限期整改，"
                f"并在到期评价（第29条）中下调其准入优先级。")
    return (f"严峻。{base}服务已严重不达标，建议立即启动约谈，"
            f"并评估是否暂停新增业务或启动退出流程。")
