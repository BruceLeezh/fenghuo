"""监管报送 —— 《办法》第六章要求的报告义务自动化。

对应条款：
- 第37条：6 类事前报告，合同签订前 20 个工作日向监管报告（附件1《监管报告材料目录》）
- 第38条：7 类重大风险事件，按突发事件要求报告，未规定者 24 小时内报告

系统职责：
1. 判定某外包活动是否触发第37条事前报告义务
2. 从供应商档案自动生成《监管报告材料》（附件1 目录：服务基本情况 6 项 + 提供商 8 项）
3. 对命中重大事件（第38条 7 类）的供应商生成 24 小时倒计时待报告事项
"""
import html
import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional


def _esc(v) -> str:
    """转义为 HTML 安全文本（报告内容含抓取/上传的供应商字段）。"""
    if v is None:
        return ""
    return html.escape(str(v), quote=True)


# ---- 第37条：需要事前报告的外包活动（合同签订前 20 个工作日）----
PRE_REPORT_TRIGGERS = [
    ("R37-1", "信息科技工作整体外包", "仅保留必要的管理团队和核心职能"),
    ("R37-2", "数据中心（机房）整体外包", "机房整体托管"),
    ("R37-3", "涉及基础设施和信息系统整体架构发生重大变化的外包", "架构重大变化"),
    ("R37-4", "信息科技战略规划（含中长期规划）咨询外包", "战略规划咨询"),
    ("R37-5", "符合重要外包条件的非驻场外包、关联外包和跨境外包", "重要非驻场/关联/跨境"),
    ("R37-6", "其他银保监会认为重要的信息科技外包", "监管认定"),
]

# ---- 第38条：重大风险事件 7 类（24 小时内报告）----
MAJOR_EVENT_TYPES = [
    ("E38-1", "重要数据或客户个人信息泄露",
     "第38条(一)", ["数据泄露", "信息泄露", "个人信息泄露", "数据外泄", "客户信息泄露"]),
    ("E38-2", "数据损毁或者重要业务运营中断",
     "第38条(二)", ["数据损毁", "数据丢失", "业务中断", "系统宕机", "服务中断", "停机"]),
    ("E38-3", "导致或可能导致多家机构外包服务中断",
     "第38条(三)", ["大面积中断", "多家机构", "行业性中断", "批量故障"]),
    ("E38-4", "重要外包服务非正常中断、终止或其提供商非正常退出",
     "第38条(四)", ["非正常中断", "非正常终止", "非正常退出", "突然退出", "停止服务", "清算", "注销", "破产"]),
    ("E38-5", "客户重大资金损失",
     "第38条(五)", ["资金损失", "资金被盗", "盗取", "诈骗", "客户资金"]),
    ("E38-6", "发现重大的服务提供商违法违规事件",
     "第38条(六)", ["违法违规", "严重违法", "失信", "刑事", "立案侦查", "证监会处罚", "银保监处罚", "重大处罚"]),
    ("E38-7", "银保监会规定需要报告的其他重大事件",
     "第38条(七)", ["监管要求报告"]),
]

# 与"重要外包"直接相关的 37 条触发项（按供应商标记自动判定）
def judge_pre_report(roster_item: dict) -> Dict:
    """判定某条台账记录是否触发第37条事前报告义务。"""
    hits = []
    important = bool(roster_item.get("important"))
    non_resident = bool(roster_item.get("non_resident"))
    cross_border = bool(roster_item.get("cross_border"))
    category = roster_item.get("category", "")
    business = roster_item.get("business_lines") or []

    # 第37条(五)：重要外包 + 非驻场 / 关联 / 跨境
    if important and non_resident:
        hits.append(("R37-5", "符合重要外包条件的非驻场外包"))
    if cross_border:
        hits.append(("R37-5", "跨境外包"))

    # 第37条(一)(二)(四)：按分类与业务条线推断
    if any(k in str(b) for b in business for k in ("数据中心", "机房")):
        hits.append(("R37-2", "数据中心（机房）整体外包"))
    if "战略" in str(roster_item.get("name", "")) or "咨询规划类" == category:
        hits.append(("R37-4", "信息科技战略规划咨询外包"))
    if category == "业务支持类" and not hits:
        hits.append(("R37-6", "其他重要信息科技外包（监管认定）"))

    # 重要外包中未被上述任一具体情形覆盖的（如驻场的开发测试类）：
    # 按「其他重要信息科技外包（监管认定）」兜底，保留报告义务，但**不能**标成 R37-5
    # —— 37条(五) 的口径是"非驻场"，驻场外包套这一条是错贴条款（实测 S009 被误标）。
    if not hits and important and category in ("运行维护类", "开发测试类"):
        hits.append(("R37-6", "其他重要信息科技外包（监管认定）"))

    return {
        "required": bool(hits),
        "hits": [{"code": c, "name": n} for c, n in hits],
        "deadline_note": "应在外包合同签订前 20 个工作日向银保监会或其派出机构信息科技监管部门报告",
    }


def detect_major_events(profile) -> List[Dict]:
    """从供应商档案扫描第38条七类重大风险事件。"""
    found = []
    texts = []
    for ev in getattr(profile, "legal_events", []) or []:
        texts.append(f"{getattr(ev, 'title', '')} {getattr(ev, 'evidence', '')}")
    for n in getattr(profile, "news", []) or []:
        texts.append(f"{getattr(n, 'title', '')}")
    for a in getattr(profile, "risk_alerts", []) or []:
        if isinstance(a, dict):
            texts.append(f"{a.get('title','')} {a.get('detail','')} {a.get('hint','')}")
        else:
            texts.append(str(a))

    blob = " ".join(texts)
    for code, name, clause, keywords in MAJOR_EVENT_TYPES:
        if any(kw in blob for kw in keywords):
            found.append({
                "code": code,
                "name": name,
                "clause": clause,
                "report_within_hours": 24,
                "action": f"应在 24 小时内向银保监会或其派出机构报告",
            })
    return found


def build_report_material(profile, roster_item: Optional[dict] = None) -> Dict:
    """按附件1《监管报告材料目录》结构化生成报告材料。

    一、外包服务基本情况（6 项）
    二、服务提供商情况（8 项）
    """
    item = roster_item or {}
    reg = getattr(profile, "registration", {}) or {}
    fin = getattr(profile, "financials", None)
    registered = bool(item)

    # 台账未登记时**不能**用默认值编造事实（"运行维护类/驻场外包/共享支持类"），
    # 否则生成的报送材料会把"未知"写成确定的业务事实，直接被监管当作口径依据。
    service = {
        "外包服务名称": item.get("service_name") or "-".join(item.get("business_lines") or [])
                      or ("信息科技外包服务" if registered else "未登记（该主体不在供应商台账）"),
        "外包服务类型": item.get("category") or ("未登记" if not registered else "运行维护类"),
        "外包服务的主要内容": "、".join(item.get("business_lines") or [])
                         or reg.get("industry") or ("未登记" if not registered else "—"),
        "实施方式": ("非驻场外包" if item.get("non_resident") else "驻场外包")
                    if registered else "未登记",
        "影响的业务类型": _business_types(item.get("business_lines") or []) if registered else "未登记",
        "外包服务起止时间": (f"{item.get('contract_start','—')} 至 {item.get('contract_end','—')}"
                     if registered else "未登记"),
    }

    provider = {
        "服务提供商全称": getattr(profile, "name", "") or item.get("name", ""),
        "国别": "中国",
        "尽职调查报告": f"最近尽调日期：{item.get('last_dd') or '未登记'}（第17条：一年内有效）",
        "法人代表": reg.get("legal_person") or "—",
        "注册资本": reg.get("registered_capital") or "—",
        "上级机构/母机构": reg.get("parent") or "—",
        "成立时间": reg.get("established") or "—",
        "企业性质": reg.get("company_type") or "—",
        "统一社会信用代码": reg.get("credit_code") or "—",
    }

    major = detect_major_events(profile)
    # required=None 表示"无法判定"（主体未在台账登记），与"判定为不需要报告"必须区分
    pre = judge_pre_report(item) if registered else {
        "required": None, "hits": [],
        "deadline_note": "该主体未在供应商台账登记，无法按第37条判定事前报告义务。",
    }

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "roster_registered": registered,
        "service": service,
        "provider": provider,
        "pre_report": pre,
        "major_events": major,
        "deadline": {
            "pre_report": "合同签订前 20 个工作日",
            "major_event": "事件发现后 24 小时内",
        },
        "legal_basis": "《银行保险机构信息科技外包风险监管办法》第37条、第38条及附件1",
    }


def _business_types(lines: List[str]) -> str:
    """按《办法》附件1 的业务类型口径归类。"""
    m = {
        "渠道管理类": ["手机银行", "网上银行", "渠道", "APP", "掌上"],
        "客户管理类": ["客服", "客户", "呼叫中心", "CRM"],
        "产品管理类": ["理财", "产品", "信贷", "贷款"],
        "财务管理类": ["账务", "核心系统", "支付", "清算", "影像"],
        "决策支持类": ["数据", "报表", "决策", "分析"],
        "共享支持类": ["机房", "数据中心", "网络", "运维", "基础设施"],
    }
    kinds = []
    for k, kws in m.items():
        if any(kw in " ".join(lines) for kw in kws):
            kinds.append(k)
    return "、".join(kinds) or "共享支持类"


def to_html(material: Dict, roster_item: Optional[dict] = None) -> str:
    """把报告材料渲染为可打印的 HTML。"""
    def rows(d: Dict) -> str:
        return "".join(
            f"<tr><td style='padding:7px 10px;border:1px solid #e4e8ef;background:#fafbfd;"
            f"color:#5a6878;width:180px'>{_esc(k)}</td>"
            f"<td style='padding:7px 10px;border:1px solid #e4e8ef'>{_esc(v)}</td></tr>"
            for k, v in d.items()
        )

    pre = material["pre_report"]
    if pre.get("required") is None:
        pre_html = ("<div style='padding:10px 12px;background:#fff8ec;border-left:4px solid #d99b28;"
                    "border-radius:6px;margin-bottom:10px'><b>无法判定第37条事前报告义务</b><br>"
                    f"<span style='color:#7a6a4a;font-size:12px'>{_esc(pre.get('deadline_note') or '')}</span></div>")
    elif pre["required"]:
        pre_html = ("<div style='padding:10px 12px;background:#fdf3f3;border-left:4px solid #e04b4a;"
                    "border-radius:6px;margin-bottom:10px'><b>触发事前报告义务</b><br>"
                    + "；".join(f"{_esc(h['code'])} {_esc(h['name'])}" for h in pre["hits"])
                    + f"<br><span style='color:#7a8699;font-size:12px'>{_esc(pre['deadline_note'])}</span></div>")
    else:
        pre_html = ("<div style='padding:10px 12px;background:#f2f9f3;border-left:4px solid #3f9e5a;"
                    "border-radius:6px;margin-bottom:10px'>未触发第37条事前报告义务</div>")

    if material["major_events"]:
        major_html = "".join(
            f"<div style='padding:9px 11px;border:1px solid #f3d6d6;border-radius:7px;margin-bottom:6px;"
            f"background:#fdf8f8'><b style='color:#c0392b'>{_esc(e['code'])} {_esc(e['name'])}</b>"
            f" <span style='color:#7a8699;font-size:11.5px'>（{_esc(e['clause'])}）</span>"
            f"<div style='font-size:12px;color:#8a5a5a;margin-top:4px'>{_esc(e['action'])}</div></div>"
            for e in material["major_events"])
    else:
        major_html = ("<div style='padding:10px 12px;background:#f2f9f3;border-left:4px solid #3f9e5a;"
                      "border-radius:6px'>未发现第38条七类重大风险事件</div>")

    return f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>烽火 · 监管报告材料 - {_esc(material['provider'].get('服务提供商全称',''))}</title></head>
<body style="font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;max-width:860px;
margin:0 auto;padding:32px;color:#1f2733;line-height:1.75;background:#fff">
  <div style="display:flex;align-items:center;gap:10px;margin:0 0 8px">
    <span style="display:inline-grid;place-items:center;width:26px;height:26px;border-radius:8px;background:#2c4677;color:#fff;font-size:13px;font-weight:700">烽</span>
    <h1 style="font-size:20px;margin:0">银行保险机构信息科技外包监管报告材料</h1>
  </div>
  <p style="color:#7a8699;font-size:12.5px;margin:0 0 22px">
    依据《银行保险机构信息科技外包风险监管办法》（银保监办发〔2021〕141号）第37条、第38条及附件1 编制
    · 生成时间 {material['generated_at']}</p>

  <h2 style="font-size:15px;border-left:3px solid #2f6fd0;padding-left:9px;margin:22px 0 12px">报告义务判定</h2>
  <div style="font-weight:600;font-size:13px;margin-bottom:6px">第37条 事前报告（合同签订前 20 个工作日）</div>
  {pre_html}
  <div style="font-weight:600;font-size:13px;margin:14px 0 6px">第38条 重大事件报告（24 小时内）</div>
  {major_html}

  <h2 style="font-size:15px;border-left:3px solid #2f6fd0;padding-left:9px;margin:26px 0 12px">一、外包服务基本情况</h2>
  <table style="width:100%;border-collapse:collapse;font-size:13px">{rows(material['service'])}</table>

  <h2 style="font-size:15px;border-left:3px solid #2f6fd0;padding-left:9px;margin:26px 0 12px">二、服务提供商情况</h2>
  <table style="width:100%;border-collapse:collapse;font-size:13px">{rows(material['provider'])}</table>

  <p style="color:#aab4c0;font-size:11.5px;margin-top:30px;text-align:center">
    本材料由「烽火 · 信息科技外包风险智能预警系统」自动生成，法理依据：{_esc(material['legal_basis'])}<br>
    报告时限：事前报告 {_esc(material['deadline']['pre_report'])}；重大事件 {_esc(material['deadline']['major_event'])}</p>
</body></html>"""
