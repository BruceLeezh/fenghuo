# -*- coding: utf-8 -*-
"""统一预警聚合（F2）与预警闭环状态（F3）。

设计约束：
  * **旁路模块**，不进 `rate_supplier`，不改任何既有分数。六维权重、CR 与 7 家供应商
    综合安全度已同时存在于方案书 / PPT / 系统界面 / 介绍视频四处，重算即四处失同步。
    预警的职责是"发现并按第27条推动处置"，不是"改分"。
  * 只读**本地已落盘**数据（台账 / 服务水平 / 合同条款 / 集中度 / 合规日历），不主动联网。
    因此 `/api/alerts` 可做首页默认视图，秒开；企业级风险提示（由 adapters 联网抓取的
    通用信用风险）只在调用方传入 profile 时附带，否则该来源标记为缺失，而不是伪装成"无预警"。
  * 预警 key 必须**稳定可追踪**：同一风险事件在多次刷新后 key 不变，闭环状态才不会丢。
    因此 key 只由「来源 + 归属 + 风险项」构成，不含会变动的文案与数值。

来源与等级口径：
  concentration 集中度（第13/16条）  high/warn/low/normal → high/medium/low/info
  performance   履约表现（第23/25/27/30条）
  contract      合同与知识产权（第22/29/30/31/34/37条）
  compliance    合规时限（第17/24/29/31/34/35/36条）
  profile       企业级风险提示（通用信用风险，需联网，默认不采集）
"""
from core.quiet_log import log_swallow as _log_swallow
import os
import sys
import threading
from datetime import datetime
from typing import Dict, List, Optional

# 允许直接 `python core/alerts.py` 跑自检（server.py 启动时已把 app/ 加进 sys.path）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import compliance as _compliance
from core.jsonstore import read_json, write_json
from core import concentration as _concentration
from core import contract as _contract
from core import performance as _performance
from core import roster as _roster
from core import servicelevel as _servicelevel

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_PATH = os.path.join(_BASE, "data", "alert_states.json")

LEVEL_ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3}
LEVEL_LABEL = {"high": "高", "medium": "中", "low": "低", "info": "提示"}
STATES = ["待处理", "处置中", "已闭环", "已忽略"]

SOURCE_LABEL = {
    "concentration": "集中度",
    "performance": "履约表现",
    "contract": "合同与知识产权",
    "compliance": "合规时限",
    "profile": "企业级风险",
}
_SOURCE_ORDER = ["concentration", "performance", "contract", "compliance", "profile"]

# 服务水平检查项 → 预警等级；None 表示"由评级决定"（见 _perf_alerts）
_PERF_LEVEL = {
    "服务水平悬崖式下滑": "high",
    "指标连续未达标": "medium",
    "服务水平前瞻预警": "medium",
    "服务水平时效衰减加权分": None,
}
_GRADE_LEVEL = {"预警": "high", "关注": "medium"}

_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# 闭环状态（F3）
# ---------------------------------------------------------------------------

def _ensure_state_file():
    if not os.path.exists(STATE_PATH):
        write_json(STATE_PATH, {})


def load_states() -> Dict[str, dict]:
    """读预警闭环状态表。键 = 预警 key。坏文件会被隔离留存，不会被空表静默覆盖。"""
    if not os.path.exists(STATE_PATH):
        return {}
    data = read_json(STATE_PATH, {})
    return data if isinstance(data, dict) else {}


def update_state(key: str, state: str = "", workorder_id: str = "",
                 note: str = "", operator: str = "") -> Dict[str, dict]:
    """更新（或新建）一条预警的闭环状态。只传需要改的字段，其余保持不变。

    读-改-写必须在同一把锁内完成，否则并发请求会互相覆盖。

    `operator` 用于处置留痕：谁把这条预警改成了什么状态，必须落到盘上，
    否则"闭环"只是一句话，事后无法还原是谁在什么时候做的处置。
    """
    if state and state not in STATES:
        raise ValueError(f"未知状态「{state}」（可选：{'、'.join(STATES)}）")
    with _LOCK:
        _ensure_state_file()
        data = load_states()
        cur = data.get(key) or {}
        if state:
            cur["state"] = state
        if workorder_id:
            cur["workorder_id"] = workorder_id
        if note:
            cur["note"] = note
        if operator:
            cur["operator"] = operator
        cur.setdefault("state", "待处理")
        cur["updated_at"] = datetime.now().isoformat(timespec="seconds")
        data[key] = cur
        write_json(STATE_PATH, data)
        return data


# ---------------------------------------------------------------------------
# 各来源 → 统一预警条目
# ---------------------------------------------------------------------------

def _norm_level(lv: str) -> str:
    return {"high": "high", "warn": "medium", "warning": "medium",
            "medium": "medium", "low": "low", "normal": "info"}.get(
        str(lv or "").lower(), "info")


def _alert(key, source, level, title, text, supplier="", supplier_id="",
           scope="supplier", clause="", suggestion="", metric="", deadline="",
           scope_label="", target=""):
    return {
        "key": key,
        "supplier_id": supplier_id or "",
        "supplier": supplier or "",
        "source": source,
        "source_label": SOURCE_LABEL.get(source, source),
        "scope": scope,
        "scope_label": scope_label,
        "target": target,
        "level": level,
        "title": title,
        "text": text,
        "clause": clause,
        "suggestion": suggestion,
        "metric": metric,
        "deadline": deadline,
    }


def _resolve_sid(name: str) -> str:
    try:
        return _servicelevel.find_supplier_id_by_name(name) or ""
    except Exception:
        return ""


# concentration.py 的 scope 是中文口径；统一为英文枚举，原值放 scope_label 供前端展示
_CONC_SCOPE = {"单供应商": "supplier", "集团合并": "group", "业务条线": "line"}


def _concentration_alerts() -> List[dict]:
    out = []
    try:
        conc = _concentration.build_concentration()
    except Exception:
        return out
    # 业务条线预警的 target 是条线名，真正的供应商在 lines[].top_vendor 里
    line2vendor = {l.get("line"): l.get("top_vendor")
                   for l in conc.get("lines") or []}
    for a in conc.get("alerts") or []:
        raw_scope = a.get("scope") or ""
        scope = _CONC_SCOPE.get(raw_scope, "supplier")
        target = a.get("target") or ""
        level = _norm_level(a.get("level"))
        vendor = target if scope == "supplier" else (line2vendor.get(target) or "")
        sid = _resolve_sid(vendor)
        out.append(_alert(
            # key 里不能带 level：跨阈值时 level 变化会生成新 key，已闭环的预警会
            # 以「新预警」身份复活，处置记录也就此断链。
            key=f"conc|{scope}|{target}",
            source="concentration",
            level=level,
            title=f"集中度预警：{target}",
            text=a.get("text") or "",
            supplier=vendor if sid else "",
            supplier_id=sid,
            scope=scope,
            scope_label=raw_scope,
            target=target,
            clause=a.get("clause") or "办法第13条、第16条",
            suggestion=("核减份额或引入备选供应商，降低单点依赖；"
                        "重要系统被独家支撑的，须同步制定切换预案。"),
            metric=str(a.get("value") if a.get("value") is not None else ""),
        ))
    return out


# 每项履约检查各自的处置动作。
# 不能统一用 build_dimension()["advice"]：那是维度级文案（有悬崖时永远是悬崖那句），
# 会让「连续未达标」「前瞻预警」的行都挂上悬崖的处置建议，张冠李戴。
_PERF_SUGGEST = {
    "服务水平时效衰减加权分":
        "近期恶化优先于历史平均：安排约谈，要求书面说明当期下滑原因（第25条）。",
    "服务水平悬崖式下滑":
        "立即约谈并要求书面说明原因，同步核查是否存在应报未报的重大事件（第27条、第37条）。",
    "指标连续未达标":
        "下发整改通知，限期纠正并要求提交整改报告；整改不到位的按第27条升级处置。",
    "服务水平前瞻预警":
        "不等跌破警戒线：加密监控频次、要求提交改善计划，在下一报告期前提前介入。",
}


def _perf_alerts() -> List[dict]:
    out = []
    for it in _roster.load_roster():
        sid, name = it.get("id") or "", it.get("name") or ""
        try:
            ev = _servicelevel.evaluate_supplier(sid)
            dim = _performance.build_dimension(ev)
        except Exception:
            _log_swallow("core/alerts.py:216")
            continue
        if dim.get("na"):
            continue
        for c in dim.get("triggered") or []:
            cname = c.get("name") or ""
            level = _PERF_LEVEL.get(cname)
            if level is None:                       # 时效衰减：由评级决定，避免"人人有份"
                level = _GRADE_LEVEL.get(dim.get("grade") or "")
                if not level:
                    continue
            out.append(_alert(
                key=f"perf|{sid}|{cname}",
                source="performance",
                level=level,
                title=f"{cname}",
                text=f"{c.get('actual') or ''}；{c.get('hint') or ''}".strip("；"),
                supplier=name, supplier_id=sid,
                clause=c.get("clause") or "办法第25条",
                suggestion=(_PERF_SUGGEST.get(cname)
                            or dim.get("advice")
                            or "督促服务提供商限期纠正并加密监控（第27条）。"),
                metric=c.get("actual") or "",
            ))
    return out


def _contract_alerts() -> List[dict]:
    out = []
    try:
        built = _contract.build_all()
    except Exception:
        return out
    for row in built.get("rows") or []:
        lv = row.get("level")
        if lv in ("none",):
            continue
        sid = row.get("supplier_id") or ""
        if lv == "unknown":
            registered = bool(row.get("has_contract"))
            out.append(_alert(
                # 两类合同预警必须用不同 key：共用 `contract|{sid}` 会让「关闭未登记」
                # 把「条款缺失」一起关掉（反之亦然）。
                key=f"contract|{sid}|unregistered", source="contract", level="info",
                title=("合同条款登记不完整" if registered else "合同条款未登记"),
                text=(row.get("summary") or "") if registered else
                     "尚未登记该供应商的合同关键条款，无法评价知识产权与业务连续性风险。",
                supplier=row.get("supplier") or "", supplier_id=sid,
                clause="办法第22条、第29条",
                suggestion=("补齐 19 类关键条款的齐备状态（有/无/不适用），"
                            if registered else "补充登记合同条款（19 类），或上传合同文本由系统解析。")))
            continue
        miss = row.get("missing") or []
        names = "、".join(str(m.get("name") or "") for m in miss[:4])
        more = f" 等 {len(miss)} 项" if len(miss) > 4 else ""
        out.append(_alert(
            key=f"contract|{sid}|missing", source="contract",
            level=_norm_level(lv),
            title=f"合同条款缺口：{row.get('supplier') or sid}",
            text=f"{row.get('summary') or ''} 缺口：{names}{more}。",
            supplier=row.get("supplier") or "", supplier_id=sid,
            clause=row.get("clause") or "办法第22条、第29条",
            suggestion=row.get("advice") or "在续约时补入缺失条款并明确违约责任。",
            metric=f"{row.get('missing_count', 0)}/{len(_contract.CLAUSES)}",
        ))
    return out


_COMP_LEVEL = {"overdue": "high", "urgent": "medium", "soon": "low", "normal": "info"}


def _compliance_alerts() -> List[dict]:
    out = []
    try:
        cal = _compliance.build_calendar()
    except Exception:
        return out
    for t in cal.get("todos") or []:
        target = t.get("target") or ""
        sid = _resolve_sid(target) if target and target != "全行（机构级）" else ""
        days = t.get("days_left")
        tail = "" if days is None else (f"（已逾期 {abs(days)} 天）" if days < 0
                                        else f"（剩余 {days} 天）")
        out.append(_alert(
            key=f"comp|{t.get('rule')}|{target}|{t.get('deadline')}",
            source="compliance",
            level=_COMP_LEVEL.get(t.get("urgency"), "info"),
            title=f"{t.get('title')}",
            text=f"{t.get('detail') or ''} 截止 {t.get('deadline')}{tail}",
            supplier=target, supplier_id=sid,
            scope="supplier" if sid else "org",
            clause=t.get("clause") or "办法",
            suggestion=t.get("action") or "",
            deadline=t.get("deadline") or "",
        ))
    return out


def _profile_alerts(profile_map: Optional[Dict[str, object]]) -> List[dict]:
    """企业级风险提示：仅当调用方已持有 profile（联网抓取）时附带。"""
    out = []
    for sid, p in (profile_map or {}).items():
        for a in getattr(p, "risk_alerts", None) or []:
            title = a.get("title") or a.get("name") or "企业风险提示"
            out.append(_alert(
                key=f"profile|{sid}|{title}",
                source="profile",
                level=_norm_level(a.get("level")),
                title=title,
                text=a.get("text") or a.get("detail") or "",
                supplier=getattr(p, "name", "") or "", supplier_id=sid,
                clause="办法第17条、第26条",
                suggestion="在准入与续约评估中纳入该项，必要时要求书面说明。",
            ))
    return out


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------

def local_profile_map() -> Dict[str, object]:
    """用**本地台账库**（data/public/*.json）构造 profile 映射，不联网。

    ⚠️ 本地库字段较薄，profile.risk_alerts 只包含公开信息里能拿到的那几条；
       完整的企业级风险提示需走联网 adapter。这里刻意只用于预警中心的"企业级风险"
       一栏，且该栏与集中度 / 履约 / 合同 / 合规四个**外包专属**来源分开展示——
       「这家公司本身怎么样」与「这次外包干得怎么样」是两件事。
    """
    out: Dict[str, object] = {}
    try:
        from adapters.local_adapter import LocalAdapter
    except Exception:
        return out
    adapter = LocalAdapter()
    for it in _roster.load_roster():
        sid, name = it.get("id") or "", it.get("name") or ""
        if not sid or not name:
            continue
        try:
            r = adapter.fetch_supplier(name)
            p = getattr(r, "profile", r)
            if p is not None:
                out[sid] = p
        except Exception:
            _log_swallow("core/alerts.py:357")
            continue
    return out


def collect_alerts(supplier_id: str = "", level: str = "", source: str = "",
                   profile_map: Optional[Dict[str, object]] = None,
                   with_profile: bool = False) -> dict:
    """聚合全部预警。

    supplier_id / level / source 为筛选条件，留空表示不过滤。
    profile_map（{台账ID: SupplierProfile}）可选，仅用于附带企业级风险提示；
    with_profile=True 时改用本地台账库自动装载（不联网）。
    """
    src_note = ""
    if with_profile and not profile_map:
        profile_map = local_profile_map()
        src_note = "来源：本地台账库（未联网）"
    sources = {
        "concentration": _concentration_alerts,
        "performance": _perf_alerts,
        "contract": _contract_alerts,
        "compliance": _compliance_alerts,
    }
    alerts: List[dict] = []
    coverage = {}
    for key in _SOURCE_ORDER:
        if key == "profile":
            got = _profile_alerts(profile_map)
            if profile_map:
                coverage[key] = ("ok", len(got), src_note)
            else:
                coverage[key] = ("absent", 0,
                                 "需联网抓取企业信息；加 with_profile=1 用本地台账库")
        else:
            got = sources[key]()
            coverage[key] = ("ok", len(got), "")
        alerts.extend(got)

    if supplier_id:
        alerts = [a for a in alerts if a.get("supplier_id") == supplier_id]
    if level:
        alerts = [a for a in alerts if a.get("level") == level]
    if source:
        alerts = [a for a in alerts if a.get("source") == source]

    states = load_states()
    for a in alerts:
        st = states.get(a["key"]) or {}
        a["state"] = st.get("state") or "待处理"
        a["workorder_id"] = st.get("workorder_id") or ""
        a["state_updated_at"] = st.get("updated_at") or ""
        a["closed"] = a["state"] in ("已闭环", "已忽略")

    alerts.sort(key=lambda a: (a["closed"], LEVEL_ORDER.get(a["level"], 9),
                               _SOURCE_ORDER.index(a["source"])
                               if a["source"] in _SOURCE_ORDER else 9,
                               a.get("supplier_id") or "zzz",
                               a.get("title") or ""))

    active = [a for a in alerts if not a["closed"]]
    by_level = {lv: sum(1 for a in active if a["level"] == lv) for lv in LEVEL_ORDER}
    by_source = {s: sum(1 for a in active if a["source"] == s) for s in _SOURCE_ORDER}
    by_state = {s: sum(1 for a in alerts if a["state"] == s) for s in STATES}

    sup: Dict[str, dict] = {}
    for a in active:
        sid = a.get("supplier_id") or ""
        scope = a.get("scope") or "org"
        target = a.get("target") or ""
        if sid:
            k, label = sid, a.get("supplier") or sid
        elif scope == "line":
            k, label = f"line:{target}", f"业务条线：{target}"
        elif scope == "group":
            k, label = f"group:{target}", f"集团：{target}"
        else:
            k, label = "org", "全行（机构级）"
        row = sup.setdefault(k, {
            "supplier_id": sid, "supplier": label, "scope": scope if not sid else "supplier",
            "total": 0, "high": 0, "medium": 0, "sources": [],
        })
        row["total"] += 1
        if a["level"] in ("high", "medium"):
            row[a["level"]] += 1
        if a["source"] not in row["sources"]:
            row["sources"].append(a["source"])
    by_supplier = sorted(sup.values(),
                         key=lambda r: (r["high"], r["total"]), reverse=True)

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "total": len(alerts),
        "active": len(active),
        "by_level": by_level,
        "by_source": by_source,
        "by_state": by_state,
        "by_supplier": by_supplier,
        "coverage": [{"source": s, "label": SOURCE_LABEL[s],
                      "status": coverage[s][0], "count": coverage[s][1],
                      "note": coverage[s][2]} for s in _SOURCE_ORDER],
        "alerts": alerts,
        "level_label": LEVEL_LABEL,
        "states": STATES,
    }


if __name__ == "__main__":
    r = collect_alerts()
    assert r["total"] == r["active"] + r["by_state"]["已闭环"] + r["by_state"]["已忽略"], \
        "总数应等于未闭环 + 已闭环 + 已忽略"
    assert all(a["key"] for a in r["alerts"]), "每条预警必须有稳定 key"

    # 各来源都要能出预警；profile 在未传 profile_map 时必须标记为 absent 而不是 0
    cov = {c["source"]: c["status"] for c in r["coverage"]}
    assert cov["profile"] == "absent", cov
    for s in ("concentration", "performance", "contract", "compliance"):
        assert cov[s] == "ok", (s, cov)
    assert r["by_source"]["contract"] > 0, "合同条款预警应有命中"
    assert r["by_source"]["compliance"] > 0, "合规时限预警应有命中"

    # 等级归一化：不应出现 warn / normal 这类来源内部口径
    assert set(r["by_level"]) <= set(LEVEL_ORDER), r["by_level"]

    # 筛选与状态闭环
    one = collect_alerts(supplier_id="S002")
    assert all(a["supplier_id"] == "S002" for a in one["alerts"])
    assert collect_alerts(level="high")["active"] == r["by_level"]["high"]

    k = r["alerts"][0]["key"]
    update_state(k, state="处置中", note="自检写入")
    assert collect_alerts()["by_state"]["处置中"] >= 1
    update_state(k, state="待处理", note="")
    assert load_states()[k]["state"] == "待处理"
    try:
        update_state(k, state="不存在的状态")
        raise AssertionError("非法状态应抛 ValueError")
    except ValueError:
        _log_swallow("core/alerts.py:494")

    print(f"alerts.py 自检通过：合计 {r['total']} 条（未闭环 {r['active']}），"
          f"高 {r['by_level']['high']} / 中 {r['by_level']['medium']} / "
          f"低 {r['by_level']['low']} / 提示 {r['by_level']['info']}")
    print("  来源分布：" + "、".join(f"{SOURCE_LABEL[s]} {r['by_source'][s]}"
                                     for s in _SOURCE_ORDER))
    for row in r["by_supplier"][:3]:
        print(f"  {row['supplier_id'] or '-':5s} {row['supplier'][:14]:16s} "
              f"合计 {row['total']} 条（高 {row['high']} 中 {row['medium']}）")
