"""服务水平指标（效能和质量监控）—— 《办法》第二十五条六类指标体系。

监管依据
--------
第23条：对外包服务过程进行持续监控，及时发现和纠正各类异常。
第24条：建立外包服务目录、服务水平协议（SLA）、服务水平监控评价机制；
        相关数据至少保存到服务结束后三年。
第25条：明确信息科技外包服务效能和质量监控指标，至少包括六类：
    (1) 信息系统和设备及基础设施的可用率
    (2) 故障次数、故障解决率、故障的响应时间、故障的解决时间
    (3) 服务的次数、客户满意度
    (4) 业务需求的及时完成率、程序的缺陷数、需求变更率
    (5) 外包人员工作饱和率、外包人员考核合格率
    (6) 网络和信息安全指标、业务连续性指标
第27条：指标异常 → 督促纠正 → 约谈高管限期整改 → 暂停/取消资格。
第29条：服务到期前评价，评价结果作为后续准入的重要参考依据。

设计要点
--------
本模块只做"指标的定义、录入、达标判定、趋势与结论"，不参与工商/司法
评分（那是 scoring.py 的职责）。二者通过"服务水平结论"衔接：
    五年维度评分  = 供应商"底子"（能不能合作）
    服务水平达标率 = 供应商"表现"（合作得好不好）
两者共同喂给处置工单（第27条）与到期评价（第29条）。

指标方向 direction：
    "up"   —— 数值越高越好（如可用率、满意度）
    "down" —— 数值越低越好（如故障次数、缺陷数、需求变更率）
"""
from core.quiet_log import log_swallow as _log_swallow
import os
import re
import threading
from datetime import datetime

from core.jsonstore import read_json, write_json

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SL_PATH = os.path.join(_BASE, "data", "service_levels.json")
_LOCK = threading.RLock()  # 可重入：upsert/remove 需要把「读-改-写」圈在同一个锁里

# 报告期必须是 YYYY-MM。没有这道校验时，period="" 会写进数据文件并成为
# 「最新一期」，把该供应商的评价区间整体拉空。
PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
# 供应商 ID 只允许台账编号形态（S001…）：它同时被当作文件名/字典键使用，
# 放进来 "../../etc" 之类的串会污染数据文件、也能拼出越界路径。
SID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


# ---------------------------------------------------------------------------
# 第25条六类指标定义（category → 指标项清单）
# ---------------------------------------------------------------------------
# 每一项：key / label / unit / direction / target / warn / weight / clause / hint
CATEGORIES = [
    {
        "key": "availability",
        "label": "可用率",
        "clause": "第25条(一)",
        "desc": "信息系统和设备及基础设施的可用率",
        "metrics": [
            {"key": "sys_availability", "label": "系统可用率", "unit": "%",
             "direction": "up", "target": 99.9, "warn": 99.5, "weight": 2.0,
             "hint": "核心系统可用率低于 99.9% 将直接影响对外服务"},
            {"key": "infra_availability", "label": "基础设施可用率", "unit": "%",
             "direction": "up", "target": 99.9, "warn": 99.5, "weight": 1.0,
             "hint": "机房、网络等基础设施可用性不足会放大业务中断风险"},
        ],
    },
    {
        "key": "fault",
        "label": "故障处理",
        "clause": "第25条(二)",
        "desc": "故障次数、故障解决率、故障响应时间、故障解决时间",
        "metrics": [
            {"key": "fault_count", "label": "故障次数", "unit": "次",
             "direction": "down", "target": 3, "warn": 6, "weight": 1.0,
             "hint": "故障频发说明服务稳定性不足"},
            {"key": "fault_resolve_rate", "label": "故障解决率", "unit": "%",
             "direction": "up", "target": 98, "warn": 95, "weight": 1.5,
             "hint": "解决率偏低意味着存在长期挂起未闭环的故障"},
            {"key": "fault_response_hours", "label": "故障响应时间", "unit": "小时",
             "direction": "down", "target": 0.5, "warn": 1.0, "weight": 1.5,
             "hint": "响应时间超标会延误故障止损时机"},
            {"key": "fault_resolve_hours", "label": "故障解决时间", "unit": "小时",
             "direction": "down", "target": 4, "warn": 8, "weight": 1.5,
             "hint": "解决时间超标直接影响业务恢复时效"},
        ],
    },
    {
        "key": "service",
        "label": "服务量与满意度",
        "clause": "第25条(三)",
        "desc": "服务的次数、客户满意度",
        "metrics": [
            {"key": "service_count", "label": "服务次数", "unit": "次",
             "direction": "up", "target": 40, "warn": 20, "weight": 0.5,
             "hint": "服务次数异常偏低需核实是否存在服务缩水或虚报人力"},
            {"key": "satisfaction", "label": "客户满意度", "unit": "分",
             "direction": "up", "target": 90, "warn": 80, "weight": 2.0,
             "hint": "满意度是使用部门对服务质量最直接的反馈"},
        ],
    },
    {
        "key": "delivery",
        "label": "交付质量",
        "clause": "第25条(四)",
        "desc": "业务需求的及时完成率、程序的缺陷数、需求变更率",
        "metrics": [
            {"key": "on_time_rate", "label": "需求及时完成率", "unit": "%",
             "direction": "up", "target": 95, "warn": 85, "weight": 2.0,
             "hint": "及时完成率低会导致业务排期普遍延期"},
            {"key": "defect_count", "label": "程序缺陷数", "unit": "个",
             "direction": "down", "target": 4, "warn": 10, "weight": 1.5,
             "hint": "缺陷数偏高反映开发测试质量或交付把关不足"},
            {"key": "change_rate", "label": "需求变更率", "unit": "%",
             "direction": "down", "target": 10, "warn": 20, "weight": 1.0,
             "hint": "变更率过高推高成本且易引入未经评审的改动"},
        ],
    },
    {
        "key": "personnel",
        "label": "外包人员",
        "clause": "第25条(五)",
        "desc": "外包人员工作饱和率、外包人员考核合格率",
        "metrics": [
            {"key": "utilization", "label": "工作饱和率", "unit": "%",
             "direction": "up", "target": 80, "warn": 60, "weight": 1.0,
             "hint": "饱和率过低需警惕人力虚报或资源闲置"},
            {"key": "pass_rate", "label": "考核合格率", "unit": "%",
             "direction": "up", "target": 95, "warn": 85, "weight": 1.5,
             "hint": "考核合格率低说明人员能力与岗位要求不匹配"},
        ],
    },
    {
        "key": "security",
        "label": "安全与连续性",
        "clause": "第25条(六)",
        "desc": "网络和信息安全指标、业务连续性指标",
        "metrics": [
            {"key": "security_incidents", "label": "安全事件数", "unit": "起",
             "direction": "down", "target": 0, "warn": 1, "weight": 2.5,
             "hint": "任何安全事件都需按第38条判定是否构成重大事件"},
            {"key": "vuln_fixed_rate", "label": "漏洞修复率", "unit": "%",
             "direction": "up", "target": 95, "warn": 85, "weight": 1.5,
             "hint": "漏洞未及时修复是数据泄露的主要入口"},
            {"key": "drill_pass", "label": "连续性演练通过率", "unit": "%",
             "direction": "up", "target": 100, "warn": 100, "weight": 1.5,
             "hint": "第31条要求定期组织演练，未通过须限期整改"},
            {"key": "rto_compliance", "label": "RTO 达标率", "unit": "%",
             "direction": "up", "target": 95, "warn": 85, "weight": 1.5,
             "hint": "恢复时间目标未达标意味着真实中断时无法按时恢复"},
        ],
    },
]

# 便捷索引
CATEGORY_MAP = {c["key"]: c for c in CATEGORIES}
METRIC_MAP = {m["key"]: m for c in CATEGORIES for m in c["metrics"]}

# 全部指标 key（用于校验录入完整性）
ALL_METRIC_KEYS = [m["key"] for c in CATEGORIES for m in c["metrics"]]

# 连续多少个周期未达标 → 建议触发处置
CONSECUTIVE_ALERT = 2


# ---------------------------------------------------------------------------
# 默认演示数据：7 家供应商 × 近 6 个月
# ---------------------------------------------------------------------------
# 设计意图（用于路演讲解）：
#   - 恒生电子（运维·核心账务）：可用率高但近期响应时间恶化 → 提示"重要系统"风险
#   - 科蓝软件（开发测试）：交付质量持续走低、缺陷数偏高 → 与司法风险叠加
#   - 京北方（业务支持）：满意度下滑、饱和率偏低 → 人力风险
#   - 其余为正常值，形成对照
def _row(**kw):
    """构造一行月度指标（缺省项用 None，表示未采集）。"""
    return {k: kw.get(k) for k in ALL_METRIC_KEYS}


def _default_data() -> dict:
    """默认服务水平月报：{supplier_id: {"records": [{period, ...metrics}]}}"""
    mk = lambda **kw: _row(**kw)

    # ---- 通用正常值（用于填充对照供应商）----
    def normal(seed: int = 0):
        return mk(
            sys_availability=round(99.95 - seed * 0.01, 3),
            infra_availability=99.98,
            fault_count=1 + seed % 3,
            fault_resolve_rate=100.0,
            fault_response_hours=0.3,
            fault_resolve_hours=2.8,
            service_count=38 + seed,
            satisfaction=93.0 - seed,
            on_time_rate=97.0 - seed,
            defect_count=2 + seed % 3,
            change_rate=6.0 + seed,
            utilization=85.0,
            pass_rate=97.0,
            security_incidents=0,
            vuln_fixed_rate=98.0,
            drill_pass=100.0,
            rto_compliance=97.0,
        )

    periods = ["2026-04", "2026-05", "2026-06", "2026-07", "2026-08", "2026-09"]

    data = {
        "S001": {  # 科蓝软件 · 开发测试类 —— 交付质量持续恶化
            "records": [
                {"period": p, **mk(
                    sys_availability=99.96, infra_availability=99.98,
                    fault_count=[2, 3, 4, 5, 6, 7][i],
                    fault_resolve_rate=[100, 100, 96, 92, 88, 85][i],
                    fault_response_hours=[0.4, 0.5, 0.8, 1.2, 1.5, 2.0][i],
                    fault_resolve_hours=[3, 4, 6, 9, 12, 16][i],
                    service_count=[42, 40, 38, 35, 33, 30][i],
                    satisfaction=[91, 90, 86, 82, 78, 74][i],
                    on_time_rate=[96, 94, 90, 85, 79, 72][i],
                    defect_count=[3, 5, 8, 12, 15, 19][i],
                    change_rate=[8, 10, 14, 18, 23, 27][i],
                    utilization=88, pass_rate=[96, 94, 91, 87, 83, 80][i],
                    security_incidents=0, vuln_fixed_rate=[97, 96, 94, 90, 86, 82][i],
                    drill_pass=100, rto_compliance=[96, 96, 94, 92, 90, 88][i],
                )} for i, p in enumerate(periods)
            ]
        },
        "S002": {  # 恒生电子 · 运行维护类 —— 可用率高但故障响应恶化
            "records": [
                {"period": p, **mk(
                    sys_availability=[99.99, 99.98, 99.97, 99.95, 99.92, 99.90][i],
                    infra_availability=[99.99, 99.99, 99.98, 99.97, 99.95, 99.93][i],
                    fault_count=[1, 1, 2, 3, 4, 5][i],
                    fault_resolve_rate=[100, 100, 100, 96, 93, 90][i],
                    fault_response_hours=[0.3, 0.3, 0.5, 0.8, 1.2, 1.6][i],
                    fault_resolve_hours=[2, 2.5, 3.5, 5, 7, 9][i],
                    service_count=36,
                    satisfaction=[95, 95, 93, 91, 88, 85][i],
                    on_time_rate=[98, 98, 97, 95, 93, 90][i],
                    defect_count=[1, 1, 2, 3, 4, 5][i],
                    change_rate=[5, 5, 6, 8, 10, 12][i],
                    utilization=86, pass_rate=97,
                    security_incidents=0, vuln_fixed_rate=98,
                    drill_pass=100, rto_compliance=[99, 99, 98, 97, 96, 94][i],
                )} for i, p in enumerate(periods)
            ]
        },
        "S003": {"records": [{"period": p, **normal(i % 2)} for i, p in enumerate(periods)]},
        "S004": {"records": [{"period": p, **normal(i % 3)} for i, p in enumerate(periods)]},
        "S005": {  # 京北方 · 业务支持类 —— 满意度与饱和率下滑（人力风险）
            "records": [
                {"period": p, **mk(
                    sys_availability=99.90, infra_availability=99.95,
                    fault_count=[1, 2, 2, 3, 3, 4][i],
                    fault_resolve_rate=100, fault_response_hours=0.6,
                    fault_resolve_hours=[3, 3, 4, 4, 5, 6][i],
                    service_count=[120, 118, 110, 105, 98, 90][i],
                    satisfaction=[92, 90, 86, 83, 79, 75][i],
                    on_time_rate=[96, 95, 93, 91, 89, 87][i],
                    defect_count=[2, 3, 4, 5, 6, 8][i],
                    change_rate=[8, 9, 11, 13, 15, 18][i],
                    utilization=[82, 76, 70, 64, 58, 52][i],
                    pass_rate=[95, 94, 92, 90, 88, 86][i],
                    security_incidents=0, vuln_fixed_rate=97,
                    drill_pass=100, rto_compliance=95,
                )} for i, p in enumerate(periods)
            ]
        },
        "S006": {"records": [{"period": p, **normal(i % 2)} for i, p in enumerate(periods)]},
        "S007": {"records": [{"period": p, **normal(i % 2)} for i, p in enumerate(periods)]},
    }
    return data


# ---------------------------------------------------------------------------
# 持久化
# ---------------------------------------------------------------------------
def _ensure_file():
    if not os.path.exists(SL_PATH):
        write_json(SL_PATH, _default_data())


def load_all() -> dict:
    """读取全部服务水平月报。坏文件会被隔离留存，不会被空字典静默覆盖。"""
    with _LOCK:
        _ensure_file()
        data = read_json(SL_PATH, {})
        return data if isinstance(data, dict) else {}


def save_all(data: dict) -> dict:
    """覆盖保存（原子替换）。"""
    with _LOCK:
        write_json(SL_PATH, data)
        return data


def upsert_record(supplier_id: str, record: dict) -> dict:
    """新增或覆盖某供应商某个报告期的指标记录（同 period 覆盖）。

    读-改-写必须在同一把锁内完成，否则两个并发上报会互相覆盖（丢记录）。
    """
    sid = (supplier_id or "").strip()
    period = str(record.get("period") or "").strip()
    if not SID_RE.match(sid):
        raise ValueError(f"供应商 ID 不合法「{sid}」（应为 S001 这类编号）")
    if not PERIOD_RE.match(period):
        raise ValueError(f"报告期格式应为 YYYY-MM，收到「{period}」")
    record = {**record, "period": period}
    with _LOCK:
        data = load_all()
        entry = data.setdefault(sid, {"records": []})
        recs = entry.setdefault("records", [])
        for i, r in enumerate(recs):
            if r.get("period") == period:
                recs[i] = {**r, **record}
                break
        else:
            recs.append(record)
        recs.sort(key=lambda r: str(r.get("period") or ""))
        save_all(data)
        return entry


def remove_period(supplier_id: str, period: str) -> dict:
    """删除某供应商某报告期记录。读-改-写同样圈在一把锁内。"""
    if not SID_RE.match(str(supplier_id or "")):
        raise ValueError(f"供应商 ID 不合法「{supplier_id}」")
    if not PERIOD_RE.match(str(period or "")):
        raise ValueError(f"报告期格式应为 YYYY-MM，收到「{period}」")
    with _LOCK:
        data = load_all()
        entry = data.get(supplier_id) or {"records": []}
        entry["records"] = [r for r in entry.get("records", []) if r.get("period") != period]
        data[supplier_id] = entry
        save_all(data)
    return entry


# ---------------------------------------------------------------------------
# 达标判定与评分
# ---------------------------------------------------------------------------
def _judge(metric: dict, value) -> dict:
    """判定单项指标达标情况。

    返回 {status: pass|warn|fail|na, ...}
      pass —— 达到目标值
      warn —— 未达目标但仍在警戒线内
      fail —— 跌破警戒线
      na   —— 未采集
    """
    if value is None or value == "":
        return {"status": "na", "value": None}
    try:
        v = float(value)
    except (TypeError, ValueError):
        return {"status": "na", "value": value}

    direction = metric["direction"]
    target = float(metric["target"])
    warn = float(metric["warn"])

    if direction == "up":
        if v >= target:
            st = "pass"
        elif v >= warn:
            st = "warn"
        else:
            st = "fail"
    else:
        if v <= target:
            st = "pass"
        elif v <= warn:
            st = "warn"
        else:
            st = "fail"

    return {
        "status": st,
        "value": v,
        "target": target,
        "warn": warn,
        "gap": round(v - target, 4),
        "metric": metric["key"],
        "label": metric["label"],
        "unit": metric["unit"],
        "direction": direction,
        "clause": None,   # 由上层填
        "hint": metric.get("hint", ""),
    }


def evaluate_period(record: dict) -> dict:
    """评价单个报告期：逐项判定 + 分类达标率 + 加权得分。"""
    if not record:
        return {"period": None, "categories": [], "score": None, "pass_rate": None}

    cats_out = []
    total_w = 0.0
    got = 0.0
    n_pass = n_total = 0
    sim_fill = set(record.get("_sim_fill") or [])

    for cat in CATEGORIES:
        items = []
        cw = cgot = 0.0
        cpass = ctotal = 0
        for m in cat["metrics"]:
            j = _judge(m, record.get(m["key"]))
            j["clause"] = cat["clause"]
            if m["key"] in sim_fill:
                j["sim"] = True
            items.append(j)
            if j["status"] == "na":
                continue
            ctotal += 1
            w = float(m["weight"])
            cw += w
            if j["status"] == "pass":
                cpass += 1
                cgot += w
            elif j["status"] == "warn":
                cgot += w * 0.6      # 警戒区内给 60% 权重分
            # fail 不给分
        rate = round(cpass / ctotal * 100, 1) if ctotal else None
        cats_out.append({
            "key": cat["key"],
            "label": cat["label"],
            "clause": cat["clause"],
            "desc": cat["desc"],
            "metrics": items,
            "pass_count": cpass,
            "metric_count": ctotal,
            "pass_rate": rate,
            "score": round(cgot / cw * 100, 1) if cw else None,
        })
        total_w += cw
        got += cgot
        n_pass += cpass
        n_total += ctotal

    return {
        "period": record.get("period"),
        "categories": cats_out,
        "score": round(got / total_w * 100, 1) if total_w else None,
        "pass_rate": round(n_pass / n_total * 100, 1) if n_total else None,
        "pass_count": n_pass,
        "metric_count": n_total,
    }


# ---------------------------------------------------------------------------
# 时效衰减加权 + 趋势外推（让"最近发生了什么"真正影响结论）
# ---------------------------------------------------------------------------
# 设计动机：跨期取算术平均会让一次严重宕机被多次达标稀释。
# 风险是有时效的 —— 上个月的服务中断，比半年前的那次更值得关注。
#
# 近期权重（越近越高，超出窗口的历史不再计权）
# ⚠️ 排列顺序 = 「由新到旧」：DECAY_WEIGHTS[0] 给最近一期。decayed_score 内部会
#    按此约定把它反转成「由旧到新」再与时间升序的期次配对，勿在别处直接 zip。
DECAY_WEIGHTS = [0.5, 0.3, 0.2]
DECAY_WINDOW = len(DECAY_WEIGHTS)

# 悬崖式下滑：单期跌幅达到该阈值即视为"服务悬崖"，不等连续 2 期
CLIFF_DROP = 12.0

# 预测区间的 t 分位（90% 双侧，df = n-2）。n 只有 3–5 期，df 1–3 时 t 值必须
# 明显大于正态近似的 1.96，否则区间会窄得给出"精确"的假象。
_T90 = {1: 6.314, 2: 2.920, 3: 2.353, 4: 2.132, 5: 2.015}

# 告警阈值：衰减加权分低于此值视为服务水平风险
DECAY_ALERT = 75.0


def decayed_score(periods: list) -> dict:
    """按"越近越重要"计算时效衰减加权分。

    periods 需按时间升序传入（最后一期为最近一期）。

    返回：
      score       —— 衰减加权分（近 3 期 0.5/0.3/0.2）
      avg         —— 全部期的算术平均（对照组，用于说明差异）
      latest      —— 最近一期得分
      delta       —— 衰减分相对算术平均的偏移（负数说明近期在恶化）
      cliff       —— 悬崖式下滑检测结果（有则是 dict，无则 None）
      forecast    —— 下一期得分预测（线性外推）
      trend_line  —— 近 5 期得分序列（供前端画趋势）
    """
    valid = [p for p in (periods or []) if p.get("score") is not None]
    if not valid:
        return {"score": None, "avg": None, "latest": None, "delta": None,
                "cliff": None, "forecast": None, "trend_line": [],
                "window": 0, "has_data": False}

    scores = [p["score"] for p in valid]
    latest = scores[-1]
    avg = round(sum(scores) / len(scores), 1)

    # 衰减加权：只取最近 DECAY_WINDOW 期
    # ⚠️ 方向必须对齐：`recent` 由 `valid[-N:]` 得到，仍是**时间升序**（末位是最近一期），
    #    而 DECAY_WEIGHTS 是「由新到旧」排列。直接把两者 zip 会把 0.5 配到最旧一期上，
    #    正好与"近重远轻"相反：恶化企业反而拿到偏高的衰减分（实测 100→80→60 报 86，
    #    而按设计应为 74）。故先把权重反转为「由旧到新」再配对。
    recent = valid[-DECAY_WINDOW:]
    weights = list(reversed(DECAY_WEIGHTS[:len(recent)]))
    wsum = sum(weights)
    dec = round(sum(p["score"] * w for p, w in zip(recent, weights)) / wsum, 1)

    # 悬崖式下滑：看最近两期的落差
    cliff = None
    if len(scores) >= 2:
        drop = round(scores[-2] - scores[-1], 1)
        if drop >= CLIFF_DROP:
            cliff = {
                "from": scores[-2],
                "to": scores[-1],
                "drop": drop,
                "from_period": valid[-2].get("period"),
                "to_period": valid[-1].get("period"),
                "severity": "high" if drop >= 20 else "warn",
            }

    # 趋势外推：对最近几期做最小二乘线性拟合，预测下一期
    # 点预测不是结论而是估计：必须同时给出区间，否则"下期 68 分"会被当成已经发生的事实。
    # 这里给 90% 双侧**单点预测**区间（含残差项 1 + 1/n + (x0-x̄)²/Sxx），自由度 n-2。
    forecast = None
    if len(scores) >= 3:
        tail = scores[-min(5, len(scores)):]
        n = len(tail)
        xs = list(range(n))
        mx = sum(xs) / n
        my = sum(tail) / n
        den = sum((x - mx) ** 2 for x in xs)
        if den:
            slope = sum((x - mx) * (y - my) for x, y in zip(xs, tail)) / den
            intercept = my - slope * mx
            pred = my + slope * (n - mx)          # 外推一格
            forecast = {
                "value": round(max(0.0, min(100.0, pred)), 1),
                "slope": round(slope, 2),
                "direction": "down" if slope <= -1 else ("up" if slope >= 1 else "flat"),
                "n_obs": n,
            }
            if n > 2:
                resid = sum((y - (intercept + slope * x)) ** 2 for x, y in zip(xs, tail))
                s = (resid / (n - 2)) ** 0.5
                se = s * (1 + 1.0 / n + (n - mx) ** 2 / den) ** 0.5
                t = _T90.get(n - 2, 1.96)
                forecast["p_lower"] = round(max(0.0, pred - t * se), 1)
                forecast["p_upper"] = round(min(100.0, pred + t * se), 1)
                forecast["resid_se"] = round(s, 2)

    return {
        "score": dec,
        "avg": avg,
        "latest": latest,
        "delta": round(dec - avg, 1),
        "cliff": cliff,
        "forecast": forecast,
        "trend_line": [{"period": p.get("period"), "score": p["score"]} for p in valid[-6:]],
        "window": len(recent),
        "has_data": True,
    }


def evaluate_supplier(supplier_id: str) -> dict:
    """评价某供应商全部历史：逐期评价 + 趋势 + 连续未达标预警 + 结论。"""
    data = load_all()
    entry = data.get(supplier_id) or {"records": []}
    records = sorted(entry.get("records", []), key=lambda r: str(r.get("period") or ""))

    periods = [evaluate_period(r) for r in records]
    latest = periods[-1] if periods else None
    prev = periods[-2] if len(periods) >= 2 else None

    # 趋势：最新一期相对上一期的得分变化
    delta = None
    trend = "flat"
    if latest and prev and latest.get("score") is not None and prev.get("score") is not None:
        delta = round(latest["score"] - prev["score"], 1)
        trend = "down" if delta <= -2 else ("up" if delta >= 2 else "flat")

    # 连续未达标指标：取最近 N 期都非 pass 的指标
    consecutive = _consecutive_failures(periods)

    # 时效衰减 + 趋势外推（近期表现比远期更重要）
    decay = decayed_score(periods)

    # 亮点与问题
    highlights, issues = [], []
    if latest:
        for c in latest["categories"]:
            if c["pass_rate"] is None:
                continue
            if c["pass_rate"] == 100:
                highlights.append(f"{c['label']}全部达标（{c['metric_count']}项）")
            elif c["pass_rate"] < 60:
                issues.append(f"{c['label']}达标率仅 {c['pass_rate']}%")
        for it in latest["categories"]:
            for m in it["metrics"]:
                if m["status"] == "fail":
                    # 方向不同，表述不同：up=低于警戒值，down=高于警戒值
                    word = "低于" if m.get("direction") == "up" else "高于"
                    issues.append(
                        f"{m['label']} {m['value']}{m['unit']}，{word}警戒值 {m['warn']}{m['unit']}"
                    )

    # 结论（供第29条到期评价参考）
    # 注意：结论以"时效衰减加权分"为准，而不是最近一期或算术平均。
    # 理由：一次近期严重事故不应被久远的达标记录稀释，也不应因单期波动而过度反应。
    score = decay.get("score") if decay.get("has_data") else None
    if score is None:
        grade, advice = "无数据", "尚未录入服务水平数据"
    elif score >= 90:
        grade, advice = "优秀", "服务水平达标，可按期续约，保持常规监控"
    elif score >= 75:
        grade, advice = "良好", "个别指标未达标，建议书面提醒并跟踪改善"
    elif score >= 60:
        grade, advice = "关注", "多项指标未达标，建议按第27条督促限期整改"
    else:
        grade, advice = "预警", "服务水平严重不达标，建议启动约谈并按第27条处置"

    # 悬崖式下滑优先级最高：单期大幅下跌直接升级建议
    if decay.get("cliff"):
        c = decay["cliff"]
        advice = (f"服务出现悬崖式下滑（{c['from_period']} {c['from']} 分 → "
                  f"{c['to_period']} {c['to']} 分，单期跌 {c['drop']} 分），"
                  f"建议立即约谈并要求书面说明原因")

    return {
        "supplier_id": supplier_id,
        "periods": periods,
        "latest": latest,
        "trend": trend,
        "delta": delta,
        "decay": decay,
        "consecutive": consecutive,
        "highlights": highlights[:6],
        "issues": issues[:8],
        "grade": grade,
        "advice": advice,
        "has_data": bool(records),
        "record_count": len(records),
    }


def _consecutive_failures(periods: list, lookback: int = None) -> list:
    """找出最近连续若干个报告期都未达标（非 pass）的指标。

    这是第27条"及时发现异常"的抓手：单月波动不报警，
    连续未达标才升级为需要督促整改的信号。
    """
    if not periods:
        return []
    n = lookback or CONSECUTIVE_ALERT
    if len(periods) < n:
        n = len(periods)
    window = periods[-n:]

    # 收集每个指标在各期的状态
    status_by_metric = {}
    for p in window:
        for c in p.get("categories", []):
            for m in c["metrics"]:
                if m["status"] == "na":
                    continue
                status_by_metric.setdefault(m["metric"], {"label": m["label"],
                                                          "clause": m["clause"],
                                                          "unit": m["unit"],
                                                          "statuses": [],
                                                          "values": [],
                                                          "hint": m.get("hint", "")})
                status_by_metric[m["metric"]]["statuses"].append(m["status"])
                status_by_metric[m["metric"]]["values"].append(m["value"])

    out = []
    for key, info in status_by_metric.items():
        sts = info["statuses"]
        if len(sts) < n:
            continue
        if all(s != "pass" for s in sts):
            out.append({
                "metric": key,
                "label": info["label"],
                "clause": info["clause"],
                "unit": info["unit"],
                "periods": n,
                "values": info["values"],
                "worst": "fail" if any(s == "fail" for s in sts) else "warn",
                "hint": info["hint"],
            })
    # 严重度排序：fail 优先，其次数值恶化幅度
    out.sort(key=lambda x: (x["worst"] != "fail",))
    return out


def find_supplier_id_by_name(name: str) -> str:
    """按供应商名称反查台账 ID（供评级引擎关联服务水平数据）。

    匹配策略：先全称精确匹配，再退化为"去括号/去地域后缀"的包含匹配。
    """
    if not name:
        return ""
    try:
        from core import roster
        items = roster.load_roster()
        for it in items or []:
            if it.get("name") == name:
                return it.get("id") or ""
        # 退化：去括号后包含匹配
        import re as _re
        norm = _re.sub(r"[（(].*?[)）]", "", str(name)).strip()
        if len(norm) >= 4:
            for it in items or []:
                inm = str(it.get("name") or "")
                inm_norm = _re.sub(r"[（(].*?[)）]", "", inm).strip()
                if norm and (norm in inm_norm or inm_norm in norm):
                    return it.get("id") or ""
    except Exception:
        _log_swallow("core/servicelevel.py:716")
    return ""


def build_overview(roster_items: list) -> dict:
    """组合视图：全部供应商的服务水平横向对比（供前端总览）。"""
    name2item = {it.get("id"): it for it in roster_items}
    try:
        _raw = load_all() or {}
    except Exception:
        _raw = {}
    rows = []
    for it in roster_items:
        sid = it.get("id")
        ev = evaluate_supplier(sid)
        latest = ev.get("latest") or {}
        decay = ev.get("decay") or {}
        rows.append({
            "id": sid,
            "name": it.get("name"),
            "category": it.get("category"),
            "important": bool(it.get("important")),
            "simulated": bool((_raw.get(sid) or {}).get("_simulated")),
            "score": decay.get("score"),          # 时效衰减加权分（结论口径）
            "latest_score": latest.get("score"),   # 最近一期（对照展示）
            "avg_score": decay.get("avg"),         # 算术平均（对照展示）
            "pass_rate": latest.get("pass_rate"),
            "trend": ev.get("trend"),
            "delta": ev.get("delta"),
            "forecast": (decay.get("forecast") or {}).get("value"),
            "cliff": bool(decay.get("cliff")),
            "grade": ev.get("grade"),
            "advice": ev.get("advice"),
            "issues": len(ev.get("issues") or []),
            "consecutive": len(ev.get("consecutive") or []),
            "has_data": ev.get("has_data"),
            "period": latest.get("period"),
        })
    rows.sort(key=lambda r: (r["score"] is None, r["score"] if r["score"] is not None else 999))

    scored = [r for r in rows if r["score"] is not None]
    return {
        "rows": rows,
        "total": len(rows),
        "has_data": len(scored),
        "avg_score": round(sum(r["score"] for r in scored) / len(scored), 1) if scored else None,
        "below_target": [r["name"] for r in scored if r["score"] < 75],
        "grades": {g: sum(1 for r in scored if r["grade"] == g)
                   for g in ["优秀", "良好", "关注", "预警"]},
        "category_defs": [
            {"key": c["key"], "label": c["label"], "clause": c["clause"],
             "desc": c["desc"],
             "metrics": [{"key": m["key"], "label": m["label"], "unit": m["unit"],
                          "direction": m["direction"], "target": m["target"],
                          "warn": m["warn"], "hint": m.get("hint", "")}
                         for m in c["metrics"]]}
            for c in CATEGORIES
        ],
        "alert_threshold": CONSECUTIVE_ALERT,
    }


def suggest_workorder(ev: dict) -> dict:
    """由服务水平结论推导建议处置（衔接第27条工单）。

    返回 {needed, disposal_type, trigger, measures}
    """
    score = (ev.get("latest") or {}).get("score")
    consec = ev.get("consecutive") or []
    if score is None:
        return {"needed": False, "reason": "无服务水平数据"}

    fails = [c for c in consec if c.get("worst") == "fail"]
    if score < 60 or len(fails) >= 2:
        return {
            "needed": True,
            "disposal_type": "约谈",
            "trigger": f"服务水平评分 {score}，连续未达标指标 {len(consec)} 项"
                       + (f"（其中跌破警戒值 {len(fails)} 项）" if fails else ""),
            "measures": "1) 按第27条约谈服务提供商高管人员并限期整改；"
                        "2) 要求提交服务水平改善方案并约定复评时点；"
                        "3) 复核业务连续性预案与备份供应商可用性。",
        }
    if score < 75 or consec:
        return {
            "needed": True,
            "disposal_type": "整改",
            "trigger": f"服务水平评分 {score}，存在连续未达标指标 {len(consec)} 项",
            "measures": "1) 书面督促限期纠正未达标指标；"
                        "2) 将未达标项纳入下期评价重点；"
                        "3) 关注是否影响重要外包服务的连续性。",
        }
    return {"needed": False, "reason": "服务水平达标，保持常规监控"}


# ---------------------------------------------------------------------------
# 批量录入：粘贴 Excel / CSV 文本 → 解析 → 校验 → 预览
# ---------------------------------------------------------------------------
# 设计动机：真实落地时，服务水平数据来自运维部门的台账或工单系统导出。
# 让业务人员逐项手敲 17 个指标 × 6 期是不现实的 —— 必须支持"从 Excel 直接粘"。
#
# 列名容错：优先匹配指标 key，其次匹配指标中文名，最后匹配常见别名。
_METRIC_ALIASES = {
    "期间": "period", "报告期": "period", "月份": "period", "period": "period", "日期": "period",
    "系统可用率": "sys_availability", "可用率": "sys_availability",
    "基础设施可用率": "infra_availability", "基础设施可用性": "infra_availability",
    "故障次数": "fault_count",
    "故障解决率": "fault_resolve_rate",
    "故障响应时间": "fault_response_hours", "响应时间": "fault_response_hours",
    "故障解决时间": "fault_resolve_hours", "解决时间": "fault_resolve_hours",
    "服务次数": "service_count", "服务量": "service_count",
    "客户满意度": "satisfaction", "满意度": "satisfaction",
    "需求及时完成率": "on_time_rate", "及时完成率": "on_time_rate",
    "程序缺陷数": "defect_count", "缺陷数": "defect_count",
    "需求变更率": "change_rate", "变更率": "change_rate",
    "工作饱和率": "utilization", "饱和率": "utilization",
    "人员饱和率": "utilization", "人员工作饱和率": "utilization",
    "考核合格率": "pass_rate", "合格率": "pass_rate", "人员考核合格率": "pass_rate",
    "网络安全事件数": "security_incidents", "安全事件数": "security_incidents",
    "漏洞修复率": "vuln_fixed_rate",
    "演练达标率": "drill_pass", "演练通过率": "drill_pass", "演练完成率": "drill_pass",
    "连续性演练通过率": "drill_pass", "连续性演练达标率": "drill_pass",
    "RTO达标率": "rto_compliance", "RTO合规率": "rto_compliance",
}


def _norm_col(name: str) -> str:
    """把列名规范化为指标 key。"""
    if not name:
        return ""
    n = str(name).strip().replace(" ", "").replace("　", "")
    if n in METRIC_MAP:
        return n
    if n in _METRIC_ALIASES:
        return _METRIC_ALIASES[n]
    # 去掉单位与括号后再试
    import re as _re
    n2 = _re.sub(r"[（(].*?[)）]", "", n)
    n2 = _re.sub(r"[/／].*$", "", n2)
    n2 = n2.strip()
    if n2 in _METRIC_ALIASES:
        return _METRIC_ALIASES[n2]
    for k, m in METRIC_MAP.items():
        if m["label"] == n2 or m["label"] in n or n in m["label"]:
            return k
    return ""


def _to_float(v):
    """把单元格文本转数字；'98%'、'1,234'、'—' 等都能处理。"""
    if v is None:
        return None
    s = str(v).strip()
    if s in ("", "-", "—", "N/A", "n/a", "null", "None", "/"):
        return None
    s = s.replace(",", "").replace("，", "")
    pct = s.endswith("%")
    if pct:
        s = s[:-1]
    if s.endswith("小时"):
        s = s[:-2]
    if s.endswith("次"):
        s = s[:-1]
    if s.endswith("个"):
        s = s[:-1]
    try:
        f = float(s)
    except (TypeError, ValueError):
        return None
    # 带 % 且数值在 0~1 之间，按比例放大（如 0.98 → 98）
    if pct and 0 <= abs(f) <= 1:
        f *= 100
    return f


def parse_pasted(text: str, default_period: str = "") -> dict:
    """解析粘贴的表格文本。

    返回：
      ok       —— 是否解析成功
      columns  —— 识别到的列（key → 表头原文）
      rows     —— [{period, values, valid, issues:[{level,key,msg}]}]
      summary  —— 统计（行数/可录入/告警/错误）
      unknown  —— 未识别的列名
    """
    if not text or not text.strip():
        return {"ok": False, "error": "粘贴内容为空", "rows": [], "summary": {}}

    lines = [ln for ln in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    lines = [ln for ln in lines if ln.strip()]
    if len(lines) < 2:
        return {"ok": False, "error": "至少需要表头 + 1 行数据", "rows": [], "summary": {}}

    # 分隔符识别：优先 Tab（Excel 粘贴），其次逗号
    def split(line):
        if "\t" in line:
            return [c.strip() for c in line.split("\t")]
        if "," in line:
            return [c.strip() for c in line.split(",")]
        return [c.strip() for c in line.split()]

    header = split(lines[0])
    col_map, unknown = {}, []
    for i, h in enumerate(header):
        k = _norm_col(h)
        if k:
            col_map[i] = k
        elif h and h not in ("", "序号", "备注"):
            unknown.append(h)

    if not col_map:
        return {"ok": False, "rows": [], "summary": {},
                "error": "未能识别任何指标列，请检查表头（可用指标中文名或英文 key）",
                "unknown": unknown}

    has_period_col = any(k == "period" for k in col_map.values())

    rows, n_valid, n_warn, n_err = [], 0, 0, 0
    for ln in lines[1:]:
        cells = split(ln)
        if not any(c.strip() for c in cells):
            continue
        values, issues = {}, []
        period = ""
        # col_map 的键是**表头下标**（见上面 `col_map[i] = k`），不是识别出的第 n 列：
        # 原写法 `enumerate(col_map.values())` 用识别序号去取单元格，表头里只要有一列
        # 未识别（如「序号」「备注」）就整体左移，指标被错录且仍标 valid=True。
        for i, k in col_map.items():
            raw = cells[i] if i < len(cells) else ""
            if k == "period":
                period = str(raw).strip()
                continue
            val = _to_float(raw)
            if raw.strip() != "" and val is None and str(raw).strip() not in ("", "-", "—"):
                issues.append({"level": "warn", "key": k,
                               "msg": f"{METRIC_MAP[k]['label']}「{raw}」不是有效数字，已忽略"})
            if val is not None:
                values[k] = val

        if not period:
            period = default_period
        if not period:
            issues.append({"level": "error", "key": "period", "msg": "缺少报告期"})

        # 范围校验：百分比类指标应在合理区间
        for k, v in list(values.items()):
            m = METRIC_MAP.get(k) or {}
            unit = m.get("unit")
            if unit == "%" and not (-100 <= v <= 100):
                issues.append({"level": "error", "key": k,
                               "msg": f"{m['label']} {v}% 超出合理范围（-100~100）"})
            if v < 0 and unit not in ("%",):
                issues.append({"level": "warn", "key": k,
                               "msg": f"{m['label']} 为负值（{v}），请确认是否录入有误"})

        valid = not any(it["level"] == "error" for it in issues)
        if valid and values:
            n_valid += 1
        elif not values:
            valid = False
            issues.append({"level": "error", "key": "", "msg": "该行没有可用数值"})
        if any(it["level"] == "warn" for it in issues):
            n_warn += 1
        if any(it["level"] == "error" for it in issues):
            n_err += 1

        rows.append({
            "period": period, "values": values,
            "valid": valid and bool(values),
            "issues": issues,
            "filled": len(values),
        })

    return {
        "ok": True,
        "columns": {k: header[i] for i, k in col_map.items() if i < len(header)},
        "metric_columns": len([k for k in col_map.values() if k != "period"]),
        "has_period_col": has_period_col,
        "rows": rows,
        "unknown": unknown,
        "summary": {
            "total": len(rows), "valid": n_valid,
            "with_warn": n_warn, "with_error": n_err,
        },
    }
