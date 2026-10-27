"""供应商台账（外包供应商清单）—— 周期监控的配置基座。

对应《银行保险机构信息科技外包风险监管办法》：
- 第九条：执行服务提供商准入、尽职调查、服务评价和退出管理，建立并维护关系管理策略
- 第十二条：外包活动分类管理（咨询规划类/开发测试类/运行维护类/安全服务类/业务支持类）
- 第十三条：重要外包与一般外包分级管理
- 第十三条(第7/8项)：涉及重要数据、影响实时服务的外包 → 标记为重要外包

台账为 JSON 落盘（data/roster.json），支持增删改查，是"周期监控"的输入清单。
"""
import os
import threading
from datetime import datetime

from core.jsonstore import read_json, write_json

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROSTER_PATH = os.path.join(_BASE, "data", "roster.json")
_LOCK = threading.RLock()  # 可重入：upsert/remove/import 需要把「读-改-写」圈在同一个锁里

# 《办法》第十二条：外包活动五大类别
OUTSOURCE_CATEGORIES = [
    "咨询规划类", "开发测试类", "运行维护类", "安全服务类", "业务支持类",
]

# 《办法》第十三条：重要外包判定项（用于前端勾选提示）
IMPORTANT_CRITERIA = [
    ("信息科技工作整体外包", "仅保留必要的管理团队和核心职能"),
    ("数据中心（机房）整体外包", "机房整体托管"),
    ("基础设施与架构重大变化", "涉及基础设施和信息系统整体架构发生重大变化"),
    ("核心业务系统整体外包", "核心业务系统开发测试和运行维护的整体外包"),
    ("信息科技战略规划咨询外包", "含中长期规划"),
    ("安全运营整体外包", "安全运营整体外包"),
    ("集中处理重要数据/个人敏感信息", "涉及集中存储或处理重要数据和客户个人敏感信息"),
    ("影响实时服务/账务准确性", "直接影响实时服务、影响账务准确性的重要信息系统"),
    ("其他重要影响", "其它对机构业务运营具有重要影响的外包"),
]

# 默认台账：本地预采集库中的 7 家银行 IT 外包上市公司
# annual_amount = 该供应商年度外包金额（万元），由业务人员维护，用于集中度量化（第33条）
DEFAULT_ROSTER = [
    {"id": "S001", "name": "北京科蓝软件系统股份有限公司", "category": "开发测试类",
     "important": True, "non_resident": True, "cross_border": False,
     "contract_start": "2023-06-01", "contract_end": "2026-05-31",
     "last_dd": "2024-03-15",           # 最近一次尽调日期（第17条：一年内有效）
     "last_onsite": "",                  # 最近一次非驻场实地检查（第34条：每3年）
     "business_lines": ["手机银行", "网上银行"],
     "group": "科蓝软件", "annual_amount": 3200},
    {"id": "S002", "name": "恒生电子股份有限公司", "category": "运行维护类",
     "important": True, "non_resident": False, "cross_border": False,
     "contract_start": "2022-01-01", "contract_end": "2026-12-31",
     "last_dd": "2025-11-20", "last_onsite": "2024-08-10",
     "business_lines": ["核心账务系统", "理财系统"],
     "group": "恒生电子", "annual_amount": 5600},
    {"id": "S003", "name": "北京宇信科技集团股份有限公司", "category": "开发测试类",
     "important": True, "non_resident": True, "cross_border": False,
     "contract_start": "2024-01-01", "contract_end": "2026-12-31",
     "last_dd": "2024-01-10", "last_onsite": "",
     "business_lines": ["信贷系统"],
     "group": "宇信科技", "annual_amount": 2400},
    {"id": "S004", "name": "深圳市长亮科技股份有限公司", "category": "开发测试类",
     "important": True, "non_resident": True, "cross_border": False,
     "contract_start": "2023-09-01", "contract_end": "2026-08-31",
     "last_dd": "2025-06-18", "last_onsite": "2025-03-05",
     "business_lines": ["核心系统", "支付系统"],
     "group": "长亮科技", "annual_amount": 2100},
    {"id": "S005", "name": "京北方信息技术股份有限公司", "category": "业务支持类",
     "important": False, "non_resident": True, "cross_border": False,
     "contract_start": "2024-04-01", "contract_end": "2027-03-31",
     "last_dd": "2025-09-01", "last_onsite": "",
     "business_lines": ["客服中心", "集中作业"],
     "group": "京北方", "annual_amount": 1500},
    {"id": "S006", "name": "信雅达科技股份有限公司", "category": "运行维护类",
     "important": False, "non_resident": False, "cross_border": False,
     "contract_start": "2023-03-01", "contract_end": "2026-02-28",
     "last_dd": "2024-02-20", "last_onsite": "",
     "business_lines": ["影像平台"],
     "group": "信雅达", "annual_amount": 900},
    {"id": "S007", "name": "神州数码信息服务集团股份有限公司", "category": "运行维护类",
     "important": True, "non_resident": False, "cross_border": False,
     "contract_start": "2022-07-01", "contract_end": "2026-06-30",
     "last_dd": "2025-12-05", "last_onsite": "2024-11-20",
     "business_lines": ["数据中心运维", "网络运维"],
     "group": "神州数码", "annual_amount": 1800},
]


def _ensure_file():
    if not os.path.exists(ROSTER_PATH):
        write_json(ROSTER_PATH, DEFAULT_ROSTER)


def load_roster() -> list:
    """读取供应商台账。文件损坏时隔离原文件并回落到默认台账，绝不静默覆盖证据。"""
    with _LOCK:
        _ensure_file()
        data = read_json(ROSTER_PATH, None)
        if data is None:
            return list(DEFAULT_ROSTER)
        return data if isinstance(data, list) else []


def save_roster(items: list) -> list:
    """覆盖保存台账（自动补 id）。原子替换。"""
    with _LOCK:
        max_n = 0
        for it in items:
            m = str(it.get("id") or "")
            if m.startswith("S") and m[1:].isdigit():
                max_n = max(max_n, int(m[1:]))
        for it in items:
            if not it.get("id"):
                max_n += 1
                it["id"] = f"S{max_n:03d}"
        write_json(ROSTER_PATH, items)
        return items


# 台账写入的取值边界（万元）。上限 100 亿元：超过这个数的单家外包金额一定是录入错误，
# 而它会被直接计入集中度 HHI，把整页结论带偏，所以宁可在入口拒绝。
MAX_AMOUNT = 1_000_000.0
MAX_NAME_LEN = 100


def validate_item(item: dict) -> dict:
    """校验并规范化一条台账记录，非法则抛 ValueError（由路由转成 400）。"""
    name = str(item.get("name") or "").strip()
    if not name:
        raise ValueError("供应商名称不能为空")
    if len(name) > MAX_NAME_LEN:
        raise ValueError(f"供应商名称过长（>{MAX_NAME_LEN} 字）")
    item["name"] = name

    amt = item.get("annual_amount", 0)
    if amt in (None, ""):
        amt = 0
    try:
        amt = float(amt)
    except (TypeError, ValueError):
        raise ValueError("年度外包金额必须是数字")
    if amt != amt or amt in (float("inf"), float("-inf")):
        raise ValueError("年度外包金额不是有效数字")
    if amt < 0:
        raise ValueError("年度外包金额不能为负")
    if amt > MAX_AMOUNT:
        raise ValueError(f"年度外包金额超出上限（{MAX_AMOUNT:,.0f} 万元）")
    item["annual_amount"] = amt
    return item


def upsert(item: dict) -> list:
    """新增或更新一条台账记录。读-改-写圈在一把锁内，避免并发覆盖。"""
    item = validate_item(dict(item))
    with _LOCK:
        items = load_roster()
        rid = item.get("id")
        if rid:
            for i, it in enumerate(items):
                if it.get("id") == rid:
                    items[i] = {**it, **item}
                    break
            else:
                items.append(item)
        else:
            items.append(item)
        return save_roster(items)


def remove(rid: str) -> list:
    """删除一条台账记录。ID 不存在抛 KeyError（调用方转 404）。

    旧实现不管删没删到都返回剩余列表，调用方只能回一句成功的假话；
    删除必须能证明删到了，否则前端"删了个不存在的 ID"会被当成成功。
    """
    with _LOCK:
        cur = load_roster()
        items = [it for it in cur if it.get("id") != rid]
        if len(items) == len(cur):
            raise KeyError(rid)
        return save_roster(items)


# 台账字段白名单（导入时只保留这些字段，剔除 valid/summary 等中间字段）
ROSTER_FIELDS = [
    "name", "category", "important", "non_resident", "cross_border",
    "contract_start", "contract_end", "last_dd", "last_onsite",
    "business_lines", "group", "annual_amount", "source",
]


def _blank(v) -> bool:
    """导入时判「这一格没给信息」。空值不覆盖台账里已有的值——缺列或空单元格
    只表示「这次没提供」，不表示「清空」。布尔是明确表态，0 是明确金额，都不算空。"""
    if v is None:
        return True
    if isinstance(v, bool):
        return False
    if isinstance(v, str):
        return not v.strip()
    if isinstance(v, (list, dict)):
        return not v
    if isinstance(v, (int, float)):
        return v == 0
    return False


def import_rows(rows: list) -> dict:
    """批量导入供应商（按名称去重：同名合并更新，新名称新增）。

    返回 {added, updated, items, stats}。
    """
    with _LOCK:
        items = load_roster()
        name2idx = {str(it.get("name") or ""): i for i, it in enumerate(items)}
        added = updated = skipped = 0
        for r in rows:
            try:
                item = {k: r.get(k) for k in ROSTER_FIELDS if k in r}
                item["name"] = str(r.get("name") or "")
                item = validate_item(item)
            except ValueError:
                skipped += 1
                continue
            name = item["name"]
            if name in name2idx:
                old = items[name2idx[name]]
                for k, v in item.items():
                    if not _blank(v):
                        old[k] = v
                updated += 1
            else:
                items.append(item)   # 无 id，save_roster 会自动分配
                added += 1
        items = save_roster(items)
        return {"added": added, "updated": updated, "skipped": skipped,
                "items": items, "stats": stats()}


def stats() -> dict:
    """台账概览（供前端展示）。"""
    items = load_roster()
    return {
        "total": len(items),
        "important": sum(1 for it in items if it.get("important")),
        "non_resident": sum(1 for it in items if it.get("non_resident")),
        "cross_border": sum(1 for it in items if it.get("cross_border")),
        "categories": {c: sum(1 for it in items if it.get("category") == c)
                       for c in OUTSOURCE_CATEGORIES},
    }
