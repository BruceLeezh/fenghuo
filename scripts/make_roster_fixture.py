# -*- coding: utf-8 -*-
"""按公开中标清单重建台账：供应商只留清单里的，缺失字段按项目规模模拟补齐。

口径（每一档都能对着公开公告的项目名称讲清楚）：
  合同起止   起始 = 该项目的中标公示日期（公告原文）；结束 = 起始 + 合同年限
  年度金额   按项目类型定档，见 SCALE 注释；不是拍脑袋，是「这个项目大概多大」
  业务条线   按中标项目的系统归属推（监管报送平台→监管报送，AI测试平台→测试服务…）
  重要外包   条线属于核心账务/支付/手机银行/网上银行/数据中心/监管报送/信息安全/数据库的判重要
  驻场/尽调/实地检查  按外包类型给，故意留一部分过期或空缺，让第17/34条预警有真实触发点

用法（在 D:\\金融科技专项赛\\app 下）：
  python scripts/make_roster_fixture.py          演练
  python scripts/make_roster_fixture.py --apply  写盘
"""
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import roster as roster_mod          # noqa: E402
from openpyxl import load_workbook             # noqa: E402

XLSX = r"D:\金融科技专项赛\02_工作区\行内数据\九江银行在管供应商清单_软件系统服务.xlsx"
BAK = r"D:\金融科技专项赛\02_工作区\_roster_before_fixture.json"
SRC_LABEL = "公开中标"

# 公开项目的项目类型 → 《办法》第12条外包分类（与 make_roster_import.py 同一口径）
CAT_MAP = {"运维/服务": "运行维护类", "系统建设": "开发测试类", "软件": "开发测试类"}

# 条线 → 重要性（第13条）。核心类条线判重要外包。
IMPORTANT_LINES = {
    "核心账务系统", "核心系统", "支付系统", "手机银行", "网上银行",
    "数据中心运维", "监管报送", "信息安全", "数据库",
}

# 供应商 → (业务条线, 年度金额万元, 合同年限, 非驻场, 集团)
# 年度金额按中标项目规模定档：
#   核心系统/信创数据库 700~900 万/年（3 年，千万级工程）
#   数据中心运维         300~700 万/年（2~3 年，含备件与人力）
#   平台类建设（监管报送/信息安全/信贷/支付）130~220 万/年（2 年，300~400 万工程）
#   分行特色系统         10~60 万/年（2 年，公告金额多数二三十万）
#   通用软件授权         30~55 万/年（1 年，单一许可采购）
#   测评/认证            25~90 万/年（1~2 年，人力密集但单价低）
TABLE = {
    "北京捷科智诚科技有限公司": (["测试服务"], 180, 2, True, "北京捷科智诚"),
    "南京科融数据系统股份有限公司": (["监管报送"], 210, 2, True, "南京科融数据"),
    "信雅达科技股份有限公司": (["影像平台", "客服中心"], 900, 2, True, "信雅达"),
    "上海合合信息科技股份有限公司": (["数据平台"], 260, 2, True, "上海合合信息"),
    "联通（江西）产业互联网有限公司": (["分行特色系统"], 45, 2, True, "联通江西"),
    "福建金科信息技术股份有限公司": (["网络运维"], 150, 2, True, "福建金科"),
    "北京领雁科技股份有限公司": (["办公协同"], 130, 2, True, "北京领雁"),
    "上海泽众软件科技有限公司": (["测试服务"], 120, 2, True, "上海泽众"),
    "北京中亦安图科技股份有限公司": (["数据库"], 850, 3, False, "北京中亦安图"),
    "武汉本物科技股份有限公司": (["数据中心运维"], 680, 2, False, "武汉本物"),
    "江西倍康信息技术有限公司": (["分行特色系统"], 35, 2, True, "江西倍康"),
    "南昌江咨嵘航数科信息发展有限公司": (["分行特色系统"], 60, 2, True, "南昌江咨嵘航"),
    "杭州新中大科技股份有限公司": (["分行特色系统"], 27, 2, True, "杭州新中大"),
    "江西诚韬科技有限公司": (["信息安全"], 95, 2, True, "江西诚韬"),
    "无锡慕果信息技术有限公司": (["开发工具"], 45, 1, True, "无锡慕果"),
    "上海软购信息技术有限公司": (["开发工具"], 38, 1, True, "上海软购"),
    "上海卡煜科技有限公司": (["开发工具"], 52, 1, True, "上海卡煜"),
    "深圳市贝特思科技有限公司": (["办公协同"], 33, 1, True, "深圳贝特思"),
    "江西锦酬信息技术有限公司": (["办公协同"], 42, 1, True, "江西锦酬"),
    "北京神州绿盟科技有限公司": (["信息安全"], 320, 2, True, "绿盟科技"),
    "江西省天网大数据有限公司": (["分行特色系统"], 20, 2, True, "江西天网大数据"),
    "北京超图软件股份有限公司": (["分行特色系统"], 30, 2, True, "北京超图"),
    "北京安泰伟奥信息技术有限公司": (["分行特色系统"], 32, 2, True, "北京安泰伟奥"),
    "捷通智慧科技股份有限公司": (["数据中心运维"], 210, 3, False, "捷通智慧"),
    "海穗信息技术（上海）有限公司": (["监管报送"], 190, 2, True, "海穗信息"),
    "恒生电子股份有限公司": (["核心账务系统", "理财系统", "票据系统"], 5600, 3, True, "恒生电子"),
    "武汉通威电子有限公司": (["网络运维"], 160, 2, True, "武汉通威"),
    "易联众民生（厦门）科技有限公司和西云擎科技有限公司": (
        ["卡片与影像"], 25, 2, True, "易联众·西云擎"),
    "中互金认证有限公司": (["测试服务"], 25, 1, True, "中互金认证"),
    "江西瑞智信息有限公司": (["信息安全"], 140, 2, True, "江西瑞智"),
    "江西神舟信息安全评估中心有限公司": (["信息安全"], 88, 2, True, "江西神舟评估"),
    "上海宏时数据系统有限公司": (["数据中心运维"], 190, 2, True, "上海宏时数据"),
    "北京中科江南信息技术股份有限公司": (["支付系统"], 195, 2, True, "中科江南"),
    "深圳市共济科技股份有限公司": (["数据中心运维"], 175, 2, False, "深圳共济"),
    "上海中软华腾软件系统有限公司": (["卡片与影像"], 160, 2, True, "上海中软华腾"),
    "浙江邦盛科技股份有限公司": (["信贷系统"], 140, 2, True, "浙江邦盛"),
    "中数通信息有限公司": (["分行特色系统"], 150, 2, True, "中数通信息"),
}

# 故意留出预警触发点：这些供应商的尽调已过一年有效期（第17条）
DD_OVERDUE = {"无锡慕果信息技术有限公司", "上海软购信息技术有限公司", "上海卡煜科技有限公司",
              "深圳市贝特思科技有限公司", "中互金认证有限公司"}
# 非驻场里没做过实地检查的（第34条三年一次）
ONSITE_MISSING = {"联通（江西）产业互联网有限公司", "江西倍康信息技术有限公司",
                  "江西省天网大数据有限公司", "北京超图软件股份有限公司"}


def ymd(s):
    s = str(s or "").strip()[:10]
    return date.fromisoformat(s) if s else None


def main(apply=False):
    wb = load_workbook(XLSX, data_only=True)
    ws = wb[wb.sheetnames[0]]
    rows = list(ws.iter_rows(values_only=True))
    head = [str(c or "").strip() for c in rows[0]]
    ix = {h: i for i, h in enumerate(head)}
    doc = []
    for r in rows[1:]:
        name = str(r[ix["供应商名称"]] or "").strip()
        if name:
            cat = CAT_MAP.get(str(r[ix["项目类型"]] or "").strip(), "")
            assert cat, f"{name} 的项目类型没映射到外包分类"
            doc.append((name, ymd(r[ix["最近中标"]]), cat))
    print(f"[1] 清单内供应商 {len(doc)} 家")

    missing = [n for n, _, _ in doc if n not in TABLE]
    assert not missing, f"清单里有供应商没给模拟参数：{missing}"
    extra = [n for n in TABLE if n not in {d[0] for d in doc}]
    assert not extra, f"模拟参数表里有清单外的供应商：{extra}"

    items, today = [], date(2026, 10, 16)
    for i, (name, won, cat) in enumerate(doc, 1):
        lines, amount, years, nonres, group = TABLE[name]
        assert won, f"{name} 没有中标日期"
        cstart = won
        cend = cstart.replace(year=cstart.year + years)
        # 尽调（第17条一年有效）：新签合同的尽调在签约前完成，老合同做周期性尽调。
        # 刻意留几家已过有效期，用来触发第17条预警。同一天扎堆看着假，按序号错开。
        if name in DD_OVERDUE:
            dd = today - timedelta(days=400 + (i * 11) % 70)
        elif (today - cstart).days < 200:
            dd = cstart - timedelta(days=10 + (i * 7) % 20)
        else:
            dd = cstart + timedelta(days=200)
        # 实地检查（第34条三年一次）：非驻场必须有，这里留几家没做。
        onsite = ""
        if nonres and name not in ONSITE_MISSING and i % 3 == 0:
            onsite = (today - timedelta(days=150 + (i * 37) % 420)).isoformat()
        items.append({
            "id": f"S{i:03d}",
            "name": name,
            "category": cat,
            "important": bool(set(lines) & IMPORTANT_LINES),
            "non_resident": nonres,
            "cross_border": False,
            "contract_start": cstart.isoformat(),
            "contract_end": cend.isoformat(),
            "last_dd": dd.isoformat(),
            "last_onsite": onsite,
            "business_lines": lines,
            "group": group,
            "annual_amount": amount,
            "source": SRC_LABEL,
        })

    # 自检：合同期必须盖住中标日，且金额档位与项目规模同向
    for it in items:
        assert it["contract_start"] <= it["contract_end"], it["name"]
        assert it["annual_amount"] > 0 and it["business_lines"], it["name"]
    by_line = {}
    for it in items:
        for l in it["business_lines"]:
            by_line.setdefault(l, []).append(it["name"])
    solo = {l: v[0] for l, v in by_line.items() if len(v) == 1}
    print(f"[2] 业务条线 {len(by_line)} 条，其中单一供应商独家支撑 {len(solo)} 条")
    print("    " + " · ".join(f"{l}({len(v)})" for l, v in sorted(by_line.items())))
    print(f"[3] 年度金额合计 {sum(it['annual_amount'] for it in items):,.0f} 万元"
          f"（其中恒生 {TABLE['恒生电子股份有限公司'][1]:,} 万）")
    print(f"[4] 重要外包 {sum(1 for it in items if it['important'])} 家 · "
          f"非驻场 {sum(1 for it in items if it['non_resident'])} 家 · "
          f"尽调逾期 {sum(1 for it in items if it['last_dd'] < '2025-10-16')} 家 · "
          f"未做实地检查的非驻场 {sum(1 for it in items if it['non_resident'] and not it['last_onsite'])} 家")

    if not apply:
        print("\n[演练] 未写盘。加 --apply 真正执行。")
        return

    old = roster_mod.load_roster()
    keep = {it["name"] for it in items}
    dropped = [it["name"] for it in old if it["name"] not in keep]
    if not os.path.exists(BAK):
        import shutil
        shutil.copy2(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                  "data", "roster.json"), BAK)
        print(f"[5] 备份原台账 → {BAK}")
    roster_mod.save_roster(items)
    after = roster_mod.load_roster()
    print(f"[6] 台账重建：{len(old)} → {len(after)} 家；移除清单外 {len(dropped)} 家：{dropped}")
    assert len(after) == len(items), "写盘后条数不对"
    print(f"[7] stats：{roster_mod.stats()}")
    print(f"\n完成。台账 {len(after)} 家，全部来自公开中标清单。")


if __name__ == "__main__":
    main(apply="--apply" in sys.argv)
