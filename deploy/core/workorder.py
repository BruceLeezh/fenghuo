"""风险处置闭环工单 —— 《办法》第27条、第29条、第30条。

监管要求（第27条）：
  "银行保险机构应当建立信息科技外包风险处置机制，对发现的风险及时采取
   相应措施予以控制和化解。"

监管要求（第29条）：
  "外包服务到期前，应当就是否继续外包进行评估决策；服务结束时，应当对
   服务提供商进行评价，评价结果作为后续准入的重要参考依据。"

监管要求（第30条）：
  "银行保险机构应当制定外包服务中断应急预案，明确应急处置流程。"

本模块实现风险处置的**状态机闭环 + 全程留痕**：

  处置类型（依据风险等级与业务影响）：
    - 约谈      对一般风险，约谈服务提供商，要求说明情况
    - 整改      对中高风险，下发整改通知，限期整改
    - 限制      对高风险，限制新增业务/降低份额/加强驻场监督
    - 退出      对红线熔断或重大事件，启动退出预案

  状态流转：
    待处理 → 处置中 → 待验证 → 已闭环
                        ↘ 已升级（处置无效，升级处置措施）

每条工单记录：编号 / 供应商 / 触发原因（关联评分与条款）/ 处置措施 /
责任人 / 时限 / 处置记录（留痕）/ 状态流转历史。

对应监管条款：第27条（风险处置）、第29条（退出与评价）、第30条（连续性预案）。
"""
from core.quiet_log import log_swallow as _log_swallow
import os
import threading
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

from core.jsonstore import read_json, write_json

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WO_PATH = os.path.join(_BASE, "data", "workorders.json")
_LOCK = threading.Lock()

# 处置措施类型（由轻到重）
DISPOSAL_TYPES = [
    {"key": "约谈", "desc": "约谈服务提供商，要求书面说明并承诺改进", "clause": "办法第27条"},
    {"key": "整改", "desc": "下发整改通知，限期整改并提交整改报告", "clause": "办法第27条"},
    {"key": "限制", "desc": "限制新增业务/降低份额/提高驻场监督频次", "clause": "办法第27条、第33条"},
    {"key": "退出", "desc": "启动外包退出预案，业务平稳迁移", "clause": "办法第29条、第30条"},
]

# 状态机
STATUS_FLOW = {
    "待处理": ["处置中"],
    "处置中": ["待验证", "已升级"],
    "待验证": ["已闭环", "已升级"],
    "已升级": ["处置中"],
    "已闭环": [],
}
STATUS_ORDER = ["待处理", "处置中", "待验证", "已升级", "已闭环"]

# 各处置类型的默认时限（天）
DEFAULT_DAYS = {"约谈": 15, "整改": 30, "限制": 15, "退出": 90}

# 风险等级 → 建议处置措施
LEVEL_DISPOSAL = {
    "red": ["限制", "退出"],
    "yellow": ["整改", "约谈"],
    "green": ["约谈"],
}


def _ensure_file():
    if not os.path.exists(WO_PATH):
        write_json(WO_PATH, [])


def _load_unlocked() -> List[dict]:
    """不加锁的读取（仅供已持锁的调用方内部使用）。坏文件会被隔离留存，不会被静默覆盖。"""
    _ensure_file()
    data = read_json(WO_PATH, [])
    return data if isinstance(data, list) else []


def _save_unlocked(orders: List[dict]) -> List[dict]:
    """不加锁的写入（仅供已持锁的调用方内部使用）。原子替换，断电不会留半截 JSON。"""
    write_json(WO_PATH, orders)
    return orders


def load_orders() -> List[dict]:
    """读取全部工单。"""
    with _LOCK:
        return _load_unlocked()


def save_orders(orders: List[dict]) -> List[dict]:
    with _LOCK:
        return _save_unlocked(orders)


def _next_id(orders: List[dict]) -> str:
    """工单编号：WO-20260910-001。"""
    today = date.today().strftime("%Y%m%d")
    prefix = f"WO-{today}-"
    n = 0
    for o in orders:
        oid = str(o.get("id") or "")
        if oid.startswith(prefix):
            try:
                n = max(n, int(oid[len(prefix):]))
            except ValueError:
                _log_swallow("core/workorder.py:111")
    return f"{prefix}{n + 1:03d}"


def _parse(d):
    if not d:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(str(d).strip(), fmt).date()
        except ValueError:
            _log_swallow("core/workorder.py:122")
            continue
    return None


def _decorate(o: dict) -> dict:
    """补算派生字段：剩余天数、是否逾期。"""
    o = dict(o)
    due = _parse(o.get("due_date"))
    today = date.today()
    if due:
        o["days_left"] = (due - today).days
        o["overdue"] = due < today and o.get("status") not in ("已闭环",)
    else:
        o["days_left"] = None
        o["overdue"] = False
    # 流转历史条数
    o["history_count"] = len(o.get("history") or [])
    o["last_history"] = (o.get("history") or [{}])[-1] if o.get("history") else {}
    # 演示用的模拟工单：界面据此打「模拟」标记，避免与真实处置记录混淆
    o["simulated"] = bool(o.get("_simulated"))
    return o


MAX_DAYS = 3650          # 处置时限上限（10 年）：更大值会 timedelta 溢出或明显录错
MIN_DAYS = -365          # 允许小幅追溯，但别把到期日放到很久以前
MAX_NAME_LEN = 100
_DISPOSAL_KEYS = {d["key"] for d in DISPOSAL_TYPES}


def validate_payload(payload: dict) -> None:
    """新建工单的入参校验。没有校验时，空供应商、非法处置类型、days=-5 都会落盘。"""
    supplier = str(payload.get("supplier") or "").strip()
    if not supplier:
        raise ValueError("供应商名称不能为空")
    if len(supplier) > MAX_NAME_LEN:
        raise ValueError(f"供应商名称过长（上限 {MAX_NAME_LEN} 字）")
    dtype = payload.get("disposal_type")
    if dtype not in (None, "") and dtype not in _DISPOSAL_KEYS:
        raise ValueError(f"未知处置类型「{dtype}」，可选：{' / '.join(sorted(_DISPOSAL_KEYS))}")
    days = payload.get("days")
    if days not in (None, "", 0):
        try:
            d = int(days)
        except (TypeError, ValueError):
            raise ValueError("处置时限必须是整数天")
        if not MIN_DAYS <= d <= MAX_DAYS:
            raise ValueError(f"处置时限超出范围（{MIN_DAYS}~{MAX_DAYS} 天）")


def create_order(payload: dict) -> dict:
    """新建处置工单。

    ⚠️ 读-改-写必须在**同一个** _LOCK 作用域内完成：否则两个并发请求会各自
    读到同一份旧列表，后写者覆盖前者，造成工单丢失、编号重复。
    """
    validate_payload(payload)
    with _LOCK:
        orders = _load_unlocked()
        now = datetime.now().isoformat(timespec="seconds")
        dtype = payload.get("disposal_type") or "约谈"
        days = payload.get("days") or DEFAULT_DAYS.get(dtype, 15)
        due = payload.get("due_date") or (date.today() + timedelta(days=int(days))).isoformat()

        order = {
            "id": _next_id(orders),
            "supplier": payload.get("supplier", ""),
            "supplier_id": payload.get("supplier_id", ""),
            "level": payload.get("level", ""),
            "score": payload.get("score"),
            "trigger": payload.get("trigger", ""),        # 触发原因（关联评分/条款）
            "clause": payload.get("clause", "办法第27条"),
            "disposal_type": dtype,
            "measures": payload.get("measures", ""),      # 具体处置措施
            "owner": payload.get("owner", ""),            # 责任人
            "dept": payload.get("dept", ""),              # 责任部门
            "due_date": due,
            "status": "待处理",
            "created_at": now,
            "updated_at": now,
            "history": [{
                "at": now, "status": "待处理", "by": payload.get("owner", "系统"),
                "note": f"工单创建：{dtype}｜{payload.get('trigger', '')}",
            }],
        }
        orders.insert(0, order)
        _save_unlocked(orders)
        return _decorate(order)


def advance(wo_id: str, to_status: str, note: str = "", by: str = "") -> dict:
    """推进工单状态（状态机校验 + 留痕）。读-改-写同锁，避免并发覆盖。"""
    with _LOCK:
        orders = _load_unlocked()
        now = datetime.now().isoformat(timespec="seconds")
        target = None
        for o in orders:
            if o.get("id") == wo_id:
                target = o
                break
        if not target:
            raise ValueError(f"工单 {wo_id} 不存在")

        cur = target.get("status", "待处理")
        allowed = STATUS_FLOW.get(cur, [])
        if to_status not in allowed:
            raise ValueError(f"工单状态不允许从「{cur}」直接变为「{to_status}」（可选：{allowed or '无'}）")

        target["status"] = to_status
        target["updated_at"] = now
        target.setdefault("history", []).append({
            "at": now, "status": to_status, "by": by or target.get("owner", ""),
            "note": note or f"状态流转：{cur} → {to_status}",
        })
        _save_unlocked(orders)
        return _decorate(target)


def add_note(wo_id: str, note: str, by: str = "") -> dict:
    """追加处置记录（不改变状态，只留痕）。读-改-写同锁。"""
    with _LOCK:
        orders = _load_unlocked()
        now = datetime.now().isoformat(timespec="seconds")
        for o in orders:
            if o.get("id") == wo_id:
                o.setdefault("history", []).append({
                    "at": now, "status": o.get("status", ""), "by": by or o.get("owner", ""),
                    "note": note, "is_note": True,
                })
                o["updated_at"] = now
                _save_unlocked(orders)
                return _decorate(o)
        raise ValueError(f"工单 {wo_id} 不存在")


def remove_order(wo_id: str) -> List[dict]:
    """删除工单。ID 不存在抛 KeyError（调用方转 404），与 advance/note 的语义一致。"""
    with _LOCK:
        cur = _load_unlocked()
        orders = [o for o in cur if o.get("id") != wo_id]
        if len(orders) == len(cur):
            raise KeyError(wo_id)
        _save_unlocked(orders)
        return [_decorate(o) for o in orders]


def stats() -> Dict:
    """工单概览。"""
    orders = [_decorate(o) for o in load_orders()]
    by_status = {s: 0 for s in STATUS_ORDER}
    for o in orders:
        st = o.get("status", "待处理")
        by_status[st] = by_status.get(st, 0) + 1
    return {
        "total": len(orders),
        "open": sum(1 for o in orders if o.get("status") != "已闭环"),
        "overdue": sum(1 for o in orders if o.get("overdue")),
        "closed": by_status.get("已闭环", 0),
        "by_status": by_status,
        "by_type": {t["key"]: sum(1 for o in orders if o.get("disposal_type") == t["key"])
                    for t in DISPOSAL_TYPES},
    }


def suggest_disposal(level: str, red_lines: Optional[List[str]] = None) -> Dict:
    """根据风险等级与红线，给出建议处置措施（供前端一键带出）。"""
    rl = red_lines or []
    if level == "red" or rl:
        dtypes = LEVEL_DISPOSAL["red"]
        return {
            "level": "red",
            "types": dtypes,
            "recommended": "限制" if rl else "整改",
            "reason": ("触发红线熔断：" + "；".join(rl)) if rl else "综合安全度落入高危区间",
            "clause": "办法第27条、第29条、第30条",
            "measures": (
                "1) 立即限制新增外包业务并评估降低份额；"
                "2) 要求服务提供商就风险事项书面说明并提交整改方案；"
                "3) 核查业务连续性预案与备份供应商可用性；"
                "4) 必要时启动退出预案，制定业务平稳迁移计划。"
            ),
        }
    if level == "yellow":
        return {
            "level": "yellow",
            "types": LEVEL_DISPOSAL["yellow"],
            "recommended": "整改",
            "reason": "综合安全度落入关注区间，存在需改进的风险信号",
            "clause": "办法第27条",
            "measures": (
                "1) 下发整改通知，明确整改事项与时限；"
                "2) 要求限期提交整改报告并跟踪验证；"
                "3) 提高监控频次，纳入下期重点观察名单。"
            ),
        }
    return {
        "level": "green",
        "types": LEVEL_DISPOSAL["green"],
        "recommended": "约谈",
        "reason": "综合安全度正常，无需强制处置",
        "clause": "办法第27条",
        "measures": "纳入常规周期监控，保持定期沟通与服务评价。",
    }


def evaluate_provider(supplier: str, rating: str, note: str = "") -> dict:
    """服务评价（第29条）：服务结束/到期前对提供商作出评价结论。

    返回结构化评价记录，供前端展示与归档。
    """
    return {
        "supplier": supplier,
        "rating": rating,        # 优秀/良好/合格/不合格
        "note": note,
        "at": datetime.now().isoformat(timespec="seconds"),
        "clause": "办法第29条",
        "usage": "评价结果作为后续准入的重要参考依据",
    }
