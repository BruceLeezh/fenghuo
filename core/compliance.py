"""合规日历 —— 把《办法》里的时限要求自动排期为待办事项。

对应条款：
- 第17条：尽职调查结果原则上一年内有效 → 尽调到期提醒
- 第24条：监控数据至少保存到服务结束后三年 → 数据留存检查
- 第31条：每年至少一次含服务提供商的应急演练
- 第34条：非驻场外包原则上每三年覆盖全部 → 实地检查排期
- 第35条：每年至少开展一次全面风险管理评估，报董事会/高管层
- 第36条：至少每三年覆盖所有重要外包的审计；重大事件后专项审计
- 第37条：合同签订前二十个工作日报告
- 第26/27条：持续监控触发式检查
"""
from core.quiet_log import log_swallow as _log_swallow
from datetime import date, datetime, timedelta
from typing import List, Dict

from core import roster as roster_mod


def _parse(d) -> date:
    if isinstance(d, date):
        return d
    if not d:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"):
        try:
            return datetime.strptime(str(d).strip(), fmt).date()
        except ValueError:
            _log_swallow("core/compliance.py:28")
            continue
    return None


def _days_until(d: date) -> int:
    return (d - date.today()).days


def _urgency(days: int) -> str:
    """按剩余天数定紧急度。"""
    if days < 0:
        return "overdue"     # 已逾期
    if days <= 30:
        return "urgent"      # 30 天内
    if days <= 90:
        return "soon"        # 90 天内
    return "normal"


def _item(rule, clause, title, target, deadline, detail, action):
    days = _days_until(deadline) if deadline else None
    return {
        "rule": rule,
        "clause": clause,
        "title": title,
        "target": target,
        "deadline": deadline.isoformat() if deadline else "",
        "days_left": days,
        "urgency": _urgency(days) if days is not None else "normal",
        "detail": detail,
        "action": action,
    }


def build_calendar() -> Dict:
    """扫描台账，生成全部合规待办（按紧急度排序）。"""
    items = roster_mod.load_roster()
    today = date.today()
    todos: List[dict] = []

    # ---- 机构级年度事项（第31/35/36条）----
    # 年度全面风险评估：每年 12/31 前至少一次
    eval_deadline = date(today.year, 12, 31)
    if eval_deadline < today:
        eval_deadline = date(today.year + 1, 12, 31)
    todos.append(_item(
        "第35条", "办法第35条", "年度外包风险管理评估",
        "全行（机构级）", eval_deadline,
        f"每年至少开展一次全面的信息科技外包风险管理评估，并向董事会或高级管理层提交评估报告。"
        f"本年度评估截止 {eval_deadline.isoformat()}。",
        "生成本年度《信息科技外包风险管理评估报告》并报送董（理）事会/高管层",
    ))

    # 年度应急演练（含服务提供商，第31条）
    drill_deadline = date(today.year, 12, 31)
    if drill_deadline < today:
        drill_deadline = date(today.year + 1, 12, 31)
    todos.append(_item(
        "第31条", "办法第31条(四)", "年度应急演练（须纳入服务提供商）",
        "全行（机构级）", drill_deadline,
        "至少每年在综合性演练或专项演练中纳入一个或多个服务提供商开展一次相关演练。",
        "组织含重要外包服务提供商的应急演练，留存演练记录",
    ))

    # 年度外包审计（第36条：至少每三年覆盖所有重要外包）
    todos.append(_item(
        "第36条", "办法第36条", "外包审计（三年周期覆盖全部重要外包）",
        "全行（机构级）", date(today.year, 12, 31) if date(today.year, 12, 31) >= today else date(today.year + 1, 12, 31),
        "至少每三年覆盖所有重要外包；发生重大外包风险事件后应及时开展专项审计。",
        "排定年度审计计划，确认三年周期内重要外包全覆盖",
    ))

    # ---- 供应商级事项 ----
    for it in items:
        name = it.get("name", "")
        important = bool(it.get("important"))
        non_resident = bool(it.get("non_resident"))
        tag = "重要外包" if important else "一般外包"

        # 第17条：尽调一年有效
        dd = _parse(it.get("last_dd"))
        if dd:
            dd_due = dd + timedelta(days=365)
            todos.append(_item(
                "第17条", "办法第17条", f"尽职调查到期（{tag}）",
                name, dd_due,
                f"最近一次尽调 {dd.isoformat()}，尽调结果原则上一年内有效，"
                f"到期需重新开展尽调（含技术能力、内控、网络安全、持续经营、合规、关联性共 7 项）。",
                "重新开展尽职调查并更新尽调报告",
            ))
        elif important:
            todos.append(_item(
                "第17条", "办法第17条", f"缺失尽调记录（{tag}）",
                name, None,
                "台账中未登记尽调日期，重要外包签约前必须完成尽职调查。",
                "补录尽调日期或立即开展尽调",
            ))

        # 第34条：非驻场外包每三年实地检查（仅重要非驻场）
        if important and non_resident:
            onsite = _parse(it.get("last_onsite"))
            if onsite:
                due = onsite + timedelta(days=365 * 3)
                todos.append(_item(
                    "第34条", "办法第34条", "非驻场外包实地检查（三年周期）",
                    name, due,
                    f"最近一次实地检查 {onsite.isoformat()}；符合重要外包标准的非驻场外包，"
                    f"原则上每三年覆盖所有重要的非驻场外包服务。",
                    "安排实地检查，重点核查数据边界、访问权限、灾备与业务连续性",
                ))
            else:
                todos.append(_item(
                    "第34条", "办法第34条", "未开展非驻场实地检查",
                    name, date(today.year, 12, 31) if date(today.year, 12, 31) >= today else date(today.year + 1, 12, 31),
                    "该供应商为重要外包且非驻场，台账中无实地检查记录，需在本周期内完成检查。",
                    "安排实地检查（重点：设施/系统/数据边界、最高访问权限、灾备）",
                ))

        # 第29条：合同到期前评估是否继续外包
        ce = _parse(it.get("contract_end"))
        if ce:
            # 合同到期前 60 天应启动评估
            todos.append(_item(
                "第29条", "办法第29条", "合同到期前续约评估",
                name, ce - timedelta(days=60),
                f"合同 {ce.isoformat()} 到期。外包服务到期前，应就是否继续外包进行评估决策；"
                f"服务结束时应对服务提供商进行评价，评价结果作为后续准入的重要参考依据。",
                "开展续约/退出评估，形成服务评价结论",
            ))

        # 第24条：数据留存（服务结束后三年）
        if ce and ce < today:
            retain_until = ce + timedelta(days=365 * 3)
            todos.append(_item(
                "第24条", "办法第24条", "监控数据留存期满核对",
                name, retain_until,
                f"服务 {ce.isoformat()} 结束，监控信息与评价结果应至少保存至 {retain_until.isoformat()}。",
                "确认数据留存状态，未到期不得删除",
            ))

    # 排序：逾期 → 30天内 → 90天内 → 其他；同日按目标名称
    order = {"overdue": 0, "urgent": 1, "soon": 2, "normal": 3}
    todos.sort(key=lambda x: (order.get(x["urgency"], 9),
                              x["days_left"] if x["days_left"] is not None else 99999))

    # 汇总
    summary = {
        "total": len(todos),
        "overdue": sum(1 for t in todos if t["urgency"] == "overdue"),
        "urgent": sum(1 for t in todos if t["urgency"] == "urgent"),
        "soon": sum(1 for t in todos if t["urgency"] == "soon"),
        "normal": sum(1 for t in todos if t["urgency"] == "normal"),
    }
    return {"todos": todos, "summary": summary, "generated_at": datetime.now().isoformat(timespec="seconds")}
