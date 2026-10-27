"""合同条款与知识产权风险评价 —— 旁路模块，**不参与综合安全度加权**。

为什么不进 rate_supplier：
  综合安全度（六维 AHP）、CR、权重同时出现在方案书 / PPT / 系统界面 / 介绍视频四处，
  任何重算都要四处同步。合同条款评价因此做成**独立评价线**：
  出预警、出处置建议、出监管报告材料，但不改任何既有分数。

监管依据：
  第22条  不得转包或变相转包
  第29条  到期前评估是否继续外包；服务结束时作出评价
  第30条  业务连续性预案
  第31条  应急演练须纳入服务提供商
  第34条  非驻场外包每三年实地检查（需合同约定审计权）
  第37条  合同签订前 20 个工作日事前报告

本模块回答的是评委提出的那个问题：
  「版权与专利等变化，对业务连续性可能造成什么影响。」
  做法是把「条款缺失」当成**风险源**，把「该供应商支撑的业务条线是否独家」
  当成**放大器**，两者相乘得到传导结论——缺条款本身不致命，
  缺条款 + 无备份供应商才致命。
"""
import os
import threading
from typing import Dict, List, Optional

from core.jsonstore import read_json, write_json

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTRACT_PATH = os.path.join(_BASE, "data", "contracts.json")
# 登记端点是 load→merge→save 三步，必须整体互斥，否则并发登记不同供应商会互相覆盖。
# RLock：同一线程内 load/save 可能被再次调用（持锁调用方），可重入才不死锁。
_LOCK = threading.RLock()

# ---------------------------------------------------------------------------
# 条款目录：19 类，分三组
#   weight  —— 该条款缺失时的风险权重（high = 直接影响业务连续性）
#   path    —— 传导路径（缺失 → 中间环节 → 业务影响）
# ---------------------------------------------------------------------------
GROUPS = {
    "A": "知识产权与权利归属",
    "B": "服务与责任",
    "C": "时限与报告",
}

CLAUSES: List[dict] = [
    # ---- A 组：知识产权与权利归属（评委「版权与专利」方向）----
    {"key": "ip_ownership", "group": "A", "weight": "high",
     "name": "软件著作权归属（定制开发成果）",
     "risk": "未约定归属时，定制开发成果的著作权默认归开发方，本行只能取得使用许可。",
     "path": "成果权属不清 → 本行无权自行改造或委托他人改造 → 后续需求排期受制于原供应商"},
    {"key": "source_escrow", "group": "A", "weight": "high",
     "name": "源代码托管 / escrow 条款",
     "risk": "供应商停业、破产或被收购时，无源码即无法接管维护。",
     "path": "供应商出局 → 无源码无法接管 → 系统不可维护 → 只能紧急重新采购"},
    {"key": "derivative_ownership", "group": "A", "weight": "medium",
     "name": "二次开发与衍生成果归属",
     "risk": "后续改造、接口开发产生的成果可能被认定为供应商所有。",
     "path": "衍生成果权属争议 → 接口与改造无法延续 → 集成工作重复投入"},
    {"key": "third_party_oss", "group": "A", "weight": "high",
     "name": "第三方组件与开源许可（GPL / AGPL 等）",
     "risk": "传染性开源许可可能要求本行开源自有系统，或引来第三方侵权索赔。",
     "path": "许可冲突或侵权主张 → 被迫下架整改或开源 → 相关系统停摆"},
    {"key": "data_ownership", "group": "A", "weight": "high",
     "name": "数据权属与数据导出义务",
     "risk": "退出时数据取不回、格式不可读，形成事实锁定。",
     "path": "退出时数据不可导出 → 迁移无法完成 → 被迫续约或长期双轨"},
    {"key": "deliverables", "group": "A", "weight": "medium",
     "name": "交付物清单（源码 / 文档 / 环境 / 账号）",
     "risk": "交付物不全，验收后仍需长期依赖供应商。",
     "path": "交付物缺失 → 本行无法独立运维 → 依赖期被动延长"},
    {"key": "ip_indemnity", "group": "A", "weight": "medium",
     "name": "侵权赔偿与免责",
     "risk": "第三方主张侵权时，赔偿责任若由本行承担则风险倒挂。",
     "path": "侵权责任倒挂 → 索赔成本与整改成本由本行承担"},

    # ---- B 组：服务与责任 ----
    {"key": "sla_penalty", "group": "B", "weight": "medium",
     "name": "SLA 与违约罚则",
     "risk": "无罚则则服务水平下降没有成本约束（第30条）。",
     "path": "服务水平下滑无代价 → 服务质量持续劣化 → 影响业务条线可用性"},
    {"key": "subcontract_limit", "group": "B", "weight": "high",
     "name": "转包 / 分包限制",
     "risk": "《办法》第22条明确不得转包或变相转包；条款缺失则无法追责。",
     "path": "发生转包 → 实际服务方不可控、不在尽调与准入范围内 → 风险敞口脱离管控"},
    {"key": "key_personnel", "group": "B", "weight": "high",
     "name": "关键人员锁定与更换审批",
     "risk": "关键人流失直接导致交付质量断崖。",
     "path": "关键人员流失 → 知识与上下文丢失 → 交付与运维质量骤降"},
    {"key": "exit_transition", "group": "B", "weight": "high",
     "name": "退出与过渡期条款",
     "risk": "无过渡期安排即无法平滑退出，只能被迫续约（第29条）。",
     "path": "缺少过渡义务 → 退出无缓冲 → 切换窗口不足 → 业务中断"},
    {"key": "data_security", "group": "B", "weight": "medium",
     "name": "数据安全与保密义务",
     "risk": "数据泄露的责任边界不清。",
     "path": "责任边界不清 → 泄露事件处置与追责困难"},
    {"key": "audit_right", "group": "B", "weight": "high",
     "name": "本行审计权 / 检查权",
     "risk": "无合同授权，现场检查与穿透审计无法开展（第34条）。",
     "path": "无法检查 → 第34条实地检查义务无法履行 → 风险不可见"},
    {"key": "incident_plan", "group": "B", "weight": "high",
     "name": "不可抗力与应急预案",
     "risk": "灾害或突发事件下对方无强制恢复义务（第31条）。",
     "path": "突发事件无恢复义务 → 恢复时间不可控 → 业务连续性无保障"},

    # ---- C 组：时限与报告 ----
    {"key": "renewal_eval", "group": "C", "weight": "medium",
     "name": "合同起止与续约机制",
     "risk": "到期前需就是否继续外包作出评估（第29条）。",
     "path": "续约机制不清 → 评估无依据 → 到期决策仓促"},
    {"key": "termination_notice", "group": "C", "weight": "medium",
     "name": "终止条件与通知期",
     "risk": "通知期过短会导致切换窗口不足。",
     "path": "通知期过短 → 切换准备不足 → 交接质量下降"},
    {"key": "pre_report", "group": "C", "weight": "medium",
     "name": "是否属第37条事前报告范围",
     "risk": "重要外包且非驻场 / 关联 / 跨境，须于签订前 20 个工作日向监管报告。",
     "path": "应报未报 → 监管报告义务缺失 → 合规瑕疵"},
    {"key": "onsite_cycle", "group": "C", "weight": "medium",
     "name": "三年实地检查约定",
     "risk": "重要非驻场外包原则上每三年覆盖全部（第34条）。",
     "path": "未约定检查周期 → 检查义务难以落实"},
    {"key": "eval_linkage", "group": "C", "weight": "low",
     "name": "服务评价结果与后续准入挂钩",
     "risk": "第29条要求服务评价作为后续准入的重要参考依据。",
     "path": "评价与准入脱钩 → 评价结果无约束力"},
]

CLAUSE_MAP = {c["key"]: c for c in CLAUSES}

# A 组（知识产权）条款 key，供快速取用
IP_KEYS = [c["key"] for c in CLAUSES if c["group"] == "A"]


# ---------------------------------------------------------------------------
# 数据读取
# ---------------------------------------------------------------------------

def _load_unlocked() -> Dict[str, dict]:
    """不加锁的读取（仅供已持锁的调用方内部使用）。坏文件会被隔离留存。"""
    if not os.path.exists(CONTRACT_PATH):
        return {}
    data = read_json(CONTRACT_PATH, {})
    if isinstance(data, dict):
        return {k: v for k, v in data.items() if isinstance(v, dict)}
    if isinstance(data, list):
        return {str(x.get("supplier_id") or ""): x for x in data if isinstance(x, dict)}
    return {}


def _save_unlocked(data: Dict[str, dict]) -> Dict[str, dict]:
    """不加锁的写入（仅供已持锁的调用方内部使用）。原子替换，断电不会留半截 JSON。"""
    write_json(CONTRACT_PATH, data)
    return data


def load_contracts() -> Dict[str, dict]:
    """读合同条款登记表。键 = 台账 ID（S001…）。坏文件会被隔离留存。"""
    with _LOCK:
        return _load_unlocked()


def save_contracts(data: Dict[str, dict]) -> Dict[str, dict]:
    with _LOCK:
        return _save_unlocked(data)


# ---------------------------------------------------------------------------
# 评价
# ---------------------------------------------------------------------------

def _norm_present(v) -> Optional[bool]:
    """把「有 / 无 / 不适用」归一化为 True / False / None（None = 不适用）。"""
    if isinstance(v, bool):
        return v
    s = str(v or "").strip()
    if s in ("有", "是", "Y", "y", "true", "True", "具备", "已约定"):
        return True
    if s in ("无", "否", "N", "n", "false", "False", "缺失"):
        return False
    return None  # 不适用 / 未填


def _severity(supplier_lines: List[str], solo_map: Dict[str, dict]) -> tuple:
    """放大器：该供应商支撑的业务条线里，有多少条是重要且独家支撑的。

    返回 (severity, lines_solo_high, lines_solo_other)。
      severity = high   —— 存在「重要 + 独家支撑」的条线，一旦断供直接中断业务
      severity = medium —— 存在独家支撑条线，但重要性为中/低
      severity = low    —— 无独家支撑条线
    """
    solo_high, solo_other = [], []
    for ln in supplier_lines or []:
        info = solo_map.get(ln)
        if not info or not info.get("solo"):
            continue
        if info.get("criticality") == "high":
            solo_high.append(ln)
        else:
            solo_other.append(ln)
    if solo_high:
        return "high", solo_high, solo_other
    if solo_other:
        return "medium", solo_high, solo_other
    return "low", solo_high, solo_other


def evaluate_contract(supplier_id: str, supplier: str, record: Optional[dict],
                      supplier_lines: Optional[List[str]] = None,
                      solo_map: Optional[Dict[str, dict]] = None,
                      category: str = "") -> dict:
    """评价一家供应商的合同条款，输出缺失项、知识产权风险与业务连续性传导。"""
    supplier_lines = supplier_lines or []
    solo_map = solo_map or {}
    record = record or {}
    stated = record.get("clauses") or {}

    severity, solo_high, solo_other = _severity(supplier_lines, solo_map)

    rows, missing, ip_missing = [], [], []
    unanswered = []
    for c in CLAUSES:
        present = _norm_present(stated.get(c["key"]))
        pos = (record.get("positions") or {}).get(c["key"], "")
        note = (record.get("notes") or {}).get(c["key"], "")
        rows.append({
            "key": c["key"], "group": c["group"], "group_name": GROUPS[c["group"]],
            "name": c["name"], "weight": c["weight"],
            "present": present,               # True / False / None(不适用或未填)
            "position": pos, "note": note,
            "risk": c["risk"], "path": c["path"],
        })
        if present is False:
            missing.append(rows[-1])
            if c["group"] == "A":
                ip_missing.append(rows[-1])
        elif present is None:
            # ⚠️ 「不适用」与「压根没填」在 _norm_present 里都是 None，必须分开计数：
            #    否则一条都没填的登记也会走"无缺失"分支，输出「19 项关键条款齐备」，
            #    把"没登记"包装成"全合规" —— 风控系统最危险的方向。
            unanswered.append(rows[-1])
    answered_n = len(CLAUSES) - len(unanswered)

    # ---- 业务连续性传导：只对 high 权重的缺失条款展开 ----
    continuity = []
    for m in missing:
        if m["weight"] != "high":
            continue
        tail = m["path"].split("→")[-1].strip()
        if severity == "high":
            impact = (f"该供应商独家支撑重要业务条线「{'、'.join(solo_high)}」，"
                      f"无备份供应商——{tail}，将直接中断该业务，"
                      f"且不存在可切换的替代方。")
            level = "high"
        elif severity == "medium":
            impact = (f"该供应商独家支撑「{'、'.join(solo_other)}」，"
                      f"{tail}，该条线重要性中等，影响可控但需备选方案。")
            level = "medium"
        else:
            impact = f"{tail}，该条线有可切换的供应商，影响有限。"
            level = "low"
        continuity.append({
            "key": m["key"], "clause_name": m["name"], "group": m["group"],
            "path": m["path"], "impact": impact, "level": level,
            "clause": "办法第30条",
        })

    # ---- 定级 ----
    # 等级只反映**业务连续性影响**（即传导结果的最高档），不反映条款缺口条数。
    # 缺 7 条但条线有备份，连续性影响仍然有限；缺口本身的严重度写在 summary 里。
    # 这样"同一条款缺失、不同暴露度"才能拉开差距——正是本模块要证明的论点。
    n_high = len([m for m in missing if m["weight"] == "high"])
    n_medium = len([m for m in missing if m["weight"] == "medium"])
    total_clauses = len(CLAUSES)
    gap_txt = f"缺失 {len(missing)} 项条款" + (f"（含 {n_high} 项高影响）" if n_high else "")
    if not record:
        level, summary = "unknown", "尚未登记合同条款，无法评价知识产权与业务连续性风险。"
    elif not missing and unanswered:
        # 没有缺失项，但有条款没填 —— 不能据此认定齐备。全空更是如此。
        level = "unknown"
        if answered_n == 0:
            summary = (f"已登记合同，但 {total_clauses} 项关键条款的齐备状态均未填写，"
                       "无法判断条款是否齐备（不能视为合规）。")
        else:
            summary = (f"已填写 {answered_n}/{total_clauses} 项，已填项未见缺口；"
                       f"另有 {len(unanswered)} 项未填写，齐备性无法确认。")
    elif any(c["level"] == "high" for c in continuity):
        level = "high"
        summary = (f"{gap_txt}，其中 {n_high} 项直接影响业务连续性，"
                   f"且该供应商独家支撑重要业务条线「{'、'.join(solo_high)}」，无备份可切换。")
    elif any(c["level"] == "medium" for c in continuity):
        level = "medium"
        summary = (f"{gap_txt}，独支撑条线「{'、'.join(solo_other)}」重要性中等，"
                   f"连续性影响可控，但退出切换需提前备选方案。")
    elif missing:
        level = "low"
        if solo_high:
            summary = (f"{gap_txt}，虽独家支撑「{'、'.join(solo_high)}」，"
                       f"但所缺条款不直接导致断供或不可维护，建议在续约时补入。")
        else:
            summary = (f"{gap_txt}，且相关业务条线未被独家支撑或存在可切换供应商，"
                       f"业务连续性影响有限；建议在下一次续约时补入。")
    else:
        level = "none"
        summary = f"{total_clauses} 项关键条款齐备，未发现知识产权与业务连续性条款缺口。"

    # ---- 修订建议（只对缺失项给，按权重排序）----
    _w = {"high": 0, "medium": 1, "low": 2}
    advice = [{
        "clause_name": m["name"], "group_name": m["group_name"], "weight": m["weight"],
        "text": f"补入「{m['name']}」条款：{m['risk']}",
    } for m in sorted(missing, key=lambda x: _w.get(x["weight"], 9))]

    return {
        "supplier_id": supplier_id, "supplier": supplier, "category": category,
        "has_contract": bool(record),
        "contract_name": record.get("contract_name", ""),
        "signed_at": record.get("signed_at", ""),
        "level": level,
        "summary": summary,
        "clauses": rows,
        "missing": missing,
        "missing_count": len(missing),
        # 「未填写」与「缺失」是两件事：前者是证据不足（不能判齐备），后者是明确缺条款。
        "unanswered": unanswered,
        "unanswered_count": len(unanswered),
        "answered_count": answered_n,
        "total_count": len(CLAUSES),
        "ip_missing": ip_missing,
        "ip_missing_count": len(ip_missing),
        "continuity": continuity,
        "business_lines": supplier_lines,
        "solo_high_lines": solo_high,
        "solo_other_lines": solo_other,
        "exposure": severity,           # 放大器：供应商侧的断供暴露度
        "advice": advice,
        "clause": "办法第22条、第29条、第30条、第34条、第37条",
    }


def build_all() -> dict:
    """评价全部已登记合同的供应商（读台账 + 集中度算放大器）。"""
    from core import roster as roster_mod
    from core import concentration as conc_mod

    contracts = load_contracts()
    items = roster_mod.load_roster()

    # 业务条线 → 独占信息（放大器）
    solo_map = {}
    try:
        conc = conc_mod.build_concentration()
        solo_map = {l["line"]: l for l in conc.get("lines", [])}
    except Exception:
        solo_map = {}

    out, by_id = [], {}
    for it in items:
        sid = it.get("id", "")
        r = evaluate_contract(
            sid, it.get("name", ""), contracts.get(sid),
            supplier_lines=it.get("business_lines") or [],
            solo_map=solo_map,
            category=it.get("category", ""),
        )
        out.append(r)
        by_id[sid] = r

    order = {"high": 0, "medium": 1, "unknown": 2, "low": 3, "none": 4}
    out.sort(key=lambda x: order.get(x["level"], 9))

    return {
        "total": len(out),
        "registered": sum(1 for r in out if r["has_contract"]),
        "by_level": {lv: sum(1 for r in out if r["level"] == lv)
                     for lv in ("high", "medium", "low", "none", "unknown")},
        "total_missing": sum(r["missing_count"] for r in out),
        "total_ip_missing": sum(r["ip_missing_count"] for r in out),
        "rows": out,
        "by_id": by_id,
        "clause_catalog": CLAUSES,
        "groups": GROUPS,
    }


if __name__ == "__main__":
    # 自检：条款目录完整性与评价逻辑（无需外部数据）
    assert len(CLAUSES) == 19, f"条款目录应为 19 类，实为 {len(CLAUSES)}"
    assert len(CLAUSE_MAP) == 19, "条款 key 存在重复"
    assert len(IP_KEYS) == 7, f"A 组（知识产权）应为 7 项，实为 {len(IP_KEYS)}"
    for c in CLAUSES:
        assert c["group"] in GROUPS, f"{c['key']} 组别非法"
        assert c["weight"] in ("high", "medium", "low"), f"{c['key']} 权重非法"
        assert c.get("path"), f"{c['key']} 缺传导路径"

    # 归一化
    assert _norm_present("有") is True and _norm_present("无") is False
    assert _norm_present("不适用") is None and _norm_present("") is None

    # 放大器：独家 + 重要 → high
    sm = {"核心账务系统": {"solo": True, "criticality": "high"},
          "客服中心": {"solo": True, "criticality": "medium"},
          "影像平台": {"solo": False, "criticality": "low"}}
    assert _severity(["核心账务系统"], sm)[0] == "high"
    assert _severity(["客服中心"], sm)[0] == "medium"
    assert _severity(["影像平台"], sm)[0] == "low"
    assert _severity([], sm)[0] == "low"

    # 缺源码托管 + 独家重要条线 → 传导为 high，等级 high
    rec = {"clauses": {c["key"]: "有" for c in CLAUSES}}
    rec["clauses"]["source_escrow"] = "无"
    r = evaluate_contract("S999", "测试供应商", rec,
                          supplier_lines=["核心账务系统"], solo_map=sm)
    assert r["missing_count"] == 1 and r["ip_missing_count"] == 1, r["missing_count"]
    assert r["level"] == "high", r["level"]
    assert len(r["continuity"]) == 1 and r["continuity"][0]["level"] == "high"
    assert r["advice"] and "源代码托管" in r["advice"][0]["clause_name"]

    # 同一条款缺失，但条线非独家支撑 → 连续性影响降为 low（缺口条数不变，等级却不同）
    r2 = evaluate_contract("S998", "测试供应商2", rec,
                           supplier_lines=["影像平台"], solo_map=sm)
    assert r2["level"] == "low", r2["level"]
    assert r2["continuity"][0]["level"] == "low"
    assert r2["missing_count"] == 1, "示例供应商2 缺口条数应与示例1相同"

    # 独家支撑但条线重要性中等 → 传导 medium，等级 medium
    r2b = evaluate_contract("S995", "测试供应商5", rec,
                            supplier_lines=["客服中心"], solo_map=sm)
    assert r2b["level"] == "medium", r2b["level"]
    assert r2b["continuity"][0]["level"] == "medium"

    # 条款齐备 → none
    full = {"clauses": {c["key"]: "有" for c in CLAUSES}}
    r3 = evaluate_contract("S997", "测试供应商3", full,
                           supplier_lines=["核心账务系统"], solo_map=sm)
    assert r3["level"] == "none" and r3["missing_count"] == 0, r3["level"]

    # 未登记合同 → unknown，不误判为合规
    r4 = evaluate_contract("S996", "测试供应商4", None)
    assert r4["level"] == "unknown" and not r4["has_contract"]

    print("contract.py 自检通过：19 条款 / 7 知识产权项 / 传导定级 5 例（high·medium·low·none·unknown）")
