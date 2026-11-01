"""为演示补齐缺失数据（可重复执行，全部标注 _simulated）。

覆盖三处空白：
  1. data/service_levels.json —— 30 家在管供应商没有服务水平记录
  2. data/workorders.json     —— 只有 2 条工单，状态机流转看不到
  3. data/alert_states.json   —— 146 条预警全是「待处理」，处置中/已闭环全 0

每一条写入的数据都带 `_simulated: true` 与 `_source: "模拟演示"`，
据此可与公开数据严格区分；`--reset` 先删掉本脚本写入的部分再重建。

用法：
    python -X utf8 scripts/make_demo_fill.py            # 干跑，打印将要补多少条
    python -X utf8 scripts/make_demo_fill.py --apply    # 落盘
    python -X utf8 scripts/make_demo_fill.py --apply --only service_levels
"""
import argparse
import hashlib
import json
import os
import random
import shutil
from datetime import datetime, timedelta

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(_BASE, "data")
SIM_TAG = {"_simulated": True, "_source": "模拟演示"}

PERIODS = ["2026-04", "2026-05", "2026-06", "2026-07", "2026-08", "2026-09"]


def _seed(key: str) -> random.Random:
    h = int(hashlib.md5(key.encode("utf-8")).hexdigest()[:12], 16)
    return random.Random(h)


def _load(name):
    with open(os.path.join(DATA, name), encoding="utf-8") as f:
        return json.load(f)


def _save(name, obj, apply):
    path = os.path.join(DATA, name)
    if not apply:
        return
    shutil.copy2(path, path + ".bak")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def _roster():
    return _load("roster.json")


# ---------------------------------------------------------------- 服务水平
TIER = {
    "优秀": dict(avail=99.97, sat=93, ontime=97, defect=3, resp=0.4, fix=4),
    "良好": dict(avail=99.94, sat=90, ontime=93, defect=5, resp=0.7, fix=6),
    "关注": dict(avail=99.86, sat=84, ontime=88, defect=9, resp=1.4, fix=12),
    "预警": dict(avail=99.72, sat=79, ontime=82, defect=13, resp=2.2, fix=20),
}


def _records_for(sid: str, tier: str, blend: float = None):
    """6 个月的 18 项指标。tier 决定质量档，cliff 档最后一个月明显掉。

    blend 非空时按 blend 在「良好」与「关注」之间线性插值，用来造出落在
    关注档（60–75 分）的样本。
    """
    rng = _seed("sl:" + sid)
    if blend is not None:
        a, b = TIER["良好"], TIER["关注"]
        base = {k: a[k] + (b[k] - a[k]) * blend for k in a}
        tier = "混合"
    else:
        base = TIER[tier]
    out = []
    for i, period in enumerate(PERIODS):
        decay = 0.0
        if tier == "预警" and i >= 4:          # 最后两个月悬崖式下滑
            decay = (i - 3) * 1.9
        elif tier == "关注" and i == 5:
            decay = 1.2
        out.append({
            "period": period,
            "sys_availability": round(min(99.99, base["avail"] + rng.uniform(-0.03, 0.03) - decay * 0.25), 2),
            "infra_availability": round(min(99.99, base["avail"] + 0.02 + rng.uniform(-0.03, 0.03) - decay * 0.2), 2),
            "fault_count": max(0, int(base["defect"] * 0.9 + rng.randint(-1, 2) + decay * 2)),
            "fault_resolve_rate": round(max(80.0, 100 - decay * 2.5 - rng.uniform(0, 3)), 1),
            "fault_response_hours": round(max(0.2, base["resp"] + rng.uniform(-0.15, 0.3) + decay * 0.5), 1),
            "fault_resolve_hours": round(max(1.0, base["fix"] + rng.uniform(-1.5, 2.5) + decay * 3), 1),
            "service_count": int(base["sat"] * 0.45 + rng.randint(-4, 6)),
            "satisfaction": round(max(60.0, base["sat"] + rng.uniform(-2.5, 2.5) - decay * 3), 0),
            "on_time_rate": round(max(60.0, base["ontime"] + rng.uniform(-2.5, 2.0) - decay * 2.5), 0),
            "defect_count": max(0, int(base["defect"] + rng.randint(-2, 3) + decay * 2)),
            "change_rate": max(0, int(8 + rng.randint(-3, 5) + decay * 2)),
            "utilization": max(50, min(99, int(86 + rng.randint(-6, 8) - decay))),
            "pass_rate": round(max(70.0, base["sat"] + 4 + rng.uniform(-3, 3) - decay * 3), 0),
            "security_incidents": 1 if (tier == "预警" and i == 5) else 0,
            "vuln_fixed_rate": round(max(70.0, 96 - decay * 2 + rng.uniform(-3, 2)), 0),
            "drill_pass": 100 if tier in ("优秀", "良好") else round(max(70.0, 100 - decay * 3 - rng.uniform(0, 5)), 0),
            "rto_compliance": round(max(70.0, 96 - decay * 2.5 - rng.uniform(0, 3)), 0),
        })
    return out


# 缺失指标补全：整数型指标 vs 小数型指标，以及各自的合理区间
INT_KEYS = {"fault_count", "service_count", "satisfaction", "on_time_rate", "defect_count",
            "change_rate", "utilization", "pass_rate", "security_incidents",
            "vuln_fixed_rate", "drill_pass", "rto_compliance", "fault_resolve_rate"}
RANGE = {
    "sys_availability": (95.0, 99.99), "infra_availability": (95.0, 99.99),
    "fault_count": (0, 30), "fault_resolve_rate": (60.0, 100.0),
    "fault_response_hours": (0.2, 12.0), "fault_resolve_hours": (1.0, 72.0),
    "service_count": (5, 200), "satisfaction": (60.0, 100.0),
    "on_time_rate": (60.0, 100.0), "defect_count": (0, 60),
    "change_rate": (0, 60), "utilization": (50, 99),
    "pass_rate": (60.0, 100.0), "security_incidents": (0, 5),
    "vuln_fixed_rate": (60.0, 100.0), "drill_pass": (60.0, 100.0),
    "rto_compliance": (60.0, 100.0),
}


def _median(xs):
    xs = sorted(xs)
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def fill_metric_gaps(apply: bool):
    """把服务水平里「某期缺项」的指标也补成模拟值。

    真实记录的缺项目前只有恒生电子 2026-10 / 2026-11 两期（6 项指标空），
    以及这两期 satisfaction 用了 5 分制（4.5）与其余期 0-100 口径不一致。
    补出来的值取自该供应商自身历史中位数 ± 抖动，并写入 _sim_fill 标记，
    界面上这些格子会带「模拟」角标。
    """
    import sys
    sys.path.insert(0, _BASE)
    from core.servicelevel import ALL_METRIC_KEYS

    sl = _load("service_levels.json")
    touched = []
    for sid, blob in sl.items():
        recs = blob.get("records") or []
        hist = {k: [r[k] for r in recs if isinstance(r.get(k), (int, float))] for k in ALL_METRIC_KEYS}
        for r in recs:
            filled, normalized = [], []
            for k in ALL_METRIC_KEYS:
                v = r.get(k)
                if isinstance(v, (int, float)):
                    if k == "satisfaction" and v <= 5:      # 5 分制口径 → 折成百分制
                        r[k] = round(v * 20, 1)
                        normalized.append(k)
                    continue
                rng = _seed(f"gap:{sid}:{r.get('period')}:{k}")
                pool = [x for x in hist[k] if x is not None]
                if k == "satisfaction":
                    pool = [x * 20 if x <= 5 else x for x in pool]
                base = _median(pool) if pool else 90.0
                lo, hi = RANGE[k]
                if k == "security_incidents":   # 不替任何真实供应商编排安全事故，取历史中位数
                    val = int(min(hi, max(0, base)))
                elif k in INT_KEYS:
                    val = int(round(min(hi, max(lo, base + rng.randint(-2, 2)))))
                else:
                    val = round(min(hi, max(lo, base * (1 + rng.uniform(-0.01, 0.01)))), 2)
                r[k] = val
                filled.append(k)
            if filled or normalized:
                r["_sim_fill"] = sorted(set(r.get("_sim_fill") or []) | set(filled))
                r.setdefault("_source", "模拟演示")
                touched.append((sid, r.get("period"), filled, normalized))
    _save("service_levels.json", sl, apply)
    print(f"服务水平缺项补全：{len(touched)} 期被补，明细 {touched}")
    return touched


def ensure_grade_coverage(apply: bool, want: int = 2):
    """四个等级档位都要有人，避免「关注」一栏恒为 0。

    只动本脚本生成过的模拟供应商，把分数最低的 want 家改档为「关注」重建记录。
    """
    import sys
    sys.path.insert(0, _BASE)
    from core import servicelevel as sl_mod

    sl = _load("service_levels.json")
    ov = sl_mod.build_overview(_roster())
    rows = [r for r in ov["rows"] if r.get("simulated")]
    if (ov.get("grades") or {}).get("关注", 0) >= want:
        print("服务水平档位覆盖：已满足，无需调整")
        return 0
    cands = sorted([r for r in rows if r.get("grade") == "良好"], key=lambda r: r.get("score") or 0)
    picked = [r["id"] for r in cands[:want]]
    tuned = []
    for sid in picked:
        hit = None
        for blend in (0.3, 0.4, 0.5, 0.6):
            sl[sid]["records"] = _records_for(sid, "关注", blend=blend)
            _save("service_levels.json", sl, apply)   # 判定要读盘，先落再评
            g = sl_mod.evaluate_supplier(sid).get("grade") if apply else "关注"
            if g == "关注":
                hit = blend
                break
        tuned.append(f"{sid}@{hit}")
    _save("service_levels.json", sl, apply)
    print(f"服务水平档位覆盖：{len(picked)} 家改档为「关注」 → {tuned}")
    return len(picked)


def fill_service_levels(apply: bool):
    roster = _roster()
    sl = _load("service_levels.json")
    missing = [r for r in roster if r["id"] not in sl]
    tiers = {}
    for r in missing:
        rng = _seed("tier:" + r["id"])
        if r.get("important"):
            tier = rng.choices(["优秀", "良好", "关注"], [0.45, 0.4, 0.15])[0]
        else:
            tier = rng.choices(["优秀", "良好", "关注", "预警"], [0.55, 0.3, 0.1, 0.05])[0]
        tiers[r["id"]] = tier
        sl[r["id"]] = {"records": _records_for(r["id"], tier), **SIM_TAG}
    _save("service_levels.json", sl, apply)
    from collections import Counter
    print(f"服务水平：新增 {len(missing)} 家，覆盖率 {len(sl)}/{len(roster)}，档位分布 {dict(Counter(tiers.values()))}")
    return missing


# ---------------------------------------------------------------- 处置工单
WO_PLAN = [
    # (处置类型, 状态, 触发来源, 条款, 措施摘要)
    ("约谈", "待处理", "集中度预警：核心账务系统单点依赖", "办法第33条",
     "核减份额或引入备选供应商；核心系统须同步准备切换预案。"),
    ("整改", "处置中", "履约表现未达标：连续 3 项指标低于基线", "办法第25条",
     "要求提交月度改进计划，下个周期复评。"),
    ("整改", "待验证", "合同条款缺口：源代码托管与退出过渡条款缺失", "办法第16条、第27条",
     "补充源代码托管协议与退出过渡期条款，法务复核后归档。"),
    ("限制", "已升级", "触发红线熔断：审计意见无法表示意见（兼营风险）", "办法第27条、第29条",
     "限制新增外包业务，要求书面说明并核查业务连续性预案。"),
    ("退出", "已闭环", "业务连续性风险：单一供应商独家支撑 9 条业务线之一", "办法第29条、第31条",
     "已完成切换演练，备选供应商接管；独家依赖解除。"),
    ("约谈", "已闭环", "合规时限：尽调有效期届满未续做", "办法第17条、第34条",
     "已补做尽调并更新台账，闭环归档。"),
]


def fill_workorders(apply: bool):
    roster = _roster()
    wos = _load("workorders.json")
    existing = {w["id"] for w in wos}
    today = datetime(2026, 10, 17, 9, 30)
    added = []
    seq = 0
    for (dtype, status, trigger, clause, measures) in WO_PLAN:
        seq += 1
        day = today - timedelta(days=(len(WO_PLAN) - seq) * 6 + 2)
        mmdd = day.strftime("%Y%m%d")
        wo_id = f"WO-{mmdd}-90{seq}"
        while wo_id in existing:
            seq += 1
            wo_id = f"WO-{mmdd}-90{seq}"
        rng = _seed("wo:" + wo_id)
        cands = [r for r in roster if r.get("important")] or roster
        sup = rng.choice(cands)
        path = {"待处理": ["待处理"], "处置中": ["待处理", "处置中"],
                "待验证": ["待处理", "处置中", "待验证"],
                "已升级": ["待处理", "处置中", "已升级"],
                "已闭环": ["待处理", "处置中", "待验证", "已闭环"]}[status]
        hist, at = [], day
        for st in path:
            hist.append({
                "at": at.isoformat(timespec="seconds"),
                "status": st,
                "by": "张风控" if st != "已闭环" else "王合规",
                "note": ("工单创建" if st == "待处理" else f"状态流转：{st}"),
            })
            at = at + timedelta(days=rng.randint(2, 6), hours=rng.randint(1, 8))
        due = day + timedelta(days=rng.choice([7, 10, 14, 21, 30]))
        wo = {
            "id": wo_id,
            "supplier": sup["name"],
            "supplier_id": sup["id"],
            "level": rng.choice(["red", "yellow", "green"]),
            "score": round(rng.uniform(60, 92), 2),
            "trigger": trigger,
            "clause": clause,
            "disposal_type": dtype,
            "measures": measures,
            "owner": rng.choice(["张风控", "李合规", "王架构"]),
            "dept": "信息科技部",
            "due_date": due.strftime("%Y-%m-%d"),
            "status": status,
            "created_at": day.isoformat(timespec="seconds"),
            "updated_at": hist[-1]["at"],
            "history": hist,
            **SIM_TAG,
        }
        wos.append(wo)
        existing.add(wo_id)
        added.append(wo)
    _save("workorders.json", wos, apply)
    print(f"处置工单：新增 {len(added)} 条，合计 {len(wos)} 条")
    return added


# ---------------------------------------------------------------- 预警闭环
def fill_alert_states(apply: bool, workorders):
    import sys
    sys.path.insert(0, _BASE)
    from core import alerts as alerts_mod

    states = _load("alert_states.json")
    got = alerts_mod.collect_alerts()
    items = got.get("alerts") or got.get("items") or []
    real = [a for a in items if a.get("key") not in states]
    rng = _seed("alert-state")
    plan = [("处置中", 0.38), ("已闭环", 0.42), ("已忽略", 0.20)]
    open_wo = [w["id"] for w in workorders if w["status"] in ("处置中", "待验证", "已升级")]
    closed_wo = [w["id"] for w in workorders if w["status"] == "已闭环"]
    added = 0
    for a in real:
        if a.get("level") == "info" and rng.random() > 0.55:
            continue  # 提示级不必全处置，留一部分原样
        if rng.random() > 0.42:
            continue
        state = rng.choices([p[0] for p in plan], [p[1] for p in plan])[0]
        entry = {
            "state": state,
            "note": "［模拟演示］" + {"处置中": "已转处置工单，跟进中",
                     "已闭环": "整改完成并归档，闭环留痕",
                     "已忽略": "经评估为可接受风险，留痕忽略"}[state],
            "updated_at": (datetime(2026, 10, 17, 8, 0) - timedelta(days=rng.randint(1, 30))).isoformat(timespec="seconds"),
            **SIM_TAG,
        }
        if state == "处置中" and open_wo:
            entry["workorder_id"] = rng.choice(open_wo)
        elif state == "已闭环" and closed_wo:
            entry["workorder_id"] = rng.choice(closed_wo)
        states[a["key"]] = entry
        added += 1
    _save("alert_states.json", states, apply)
    print(f"预警闭环：新增 {added} 条状态（总预警 {len(items)} 条，已有状态 {len(states) - added} 条）")
    return added


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--only", default="", choices=["", "service_levels", "workorders", "alert_states"])
    args = ap.parse_args()
    apply = args.apply
    print("模式：", "落盘" if apply else "干跑（加 --apply 生效）")
    if args.only in ("", "service_levels"):
        fill_service_levels(apply)
        ensure_grade_coverage(apply)
        fill_metric_gaps(apply)
    wos = _load("workorders.json")
    if args.only in ("", "workorders"):
        wos = fill_workorders(apply)
    if args.only in ("", "alert_states"):
        fill_alert_states(apply, wos)


if __name__ == "__main__":
    main()
