"""生成涉密文件分析（多文件合并）的 E2E 测试夹具。

语义：**一次上传的多个文件 = 同一家供应商的多份资料**，后端合并后统一评分。

涉密夹具（同一家「北京华信永创科技有限公司」，各贡献一部分数据）：
  _t_fin.xlsx    财务表   —— 只含财务指标（负债率 76.9 等）
  _t_legal.docx  司法清单 —— 只含"被列为被执行人"事件（触发红线）
  _t_mark.pdf    外包标记 —— 只含"重要外包：是 / 非驻场：是"
  _test_import.xlsx  台账批量导入夹具（供 ui_verify_import.js 使用，两家：海康威视 / 汉得信息）

合并后应得：一家供应商 · 财务 + 司法事件(红线) + 外包标记 三块数据齐全，
其余维度（经营/舆情/履约）显示"无信息"。

踩过的坑（详见 security/file_parser.py 约定）：
  1. `_parse_excel` 只把**第一行当表头**，事件列必须并进首行，不能另起小表
  2. PDF 必须用 PyMuPDF 内置中文字体 `china-s`（reportlab 默认 Helvetica 写中文→空白）
  3. `_parse_from_text` 靠关键词所在句识别事件，靠「重要外包：是」模式识别外包标记

用法：python scripts/make_upload_fixtures.py
"""
import os

OUT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "_shots"))

NAME = "北京华信永创科技有限公司"


def make_fin_xlsx():
    """财务表：财务指标 + 工商登记 + 股东结构（覆盖财务/经营/关联集中度三维）。"""
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "财务数据"
    ws.append([
        "供应商名称", "资产负债率", "流动比率", "速动比率", "净利率", "毛利率",
        "ROE", "经营现金流", "审计意见",
        # 工商登记（经营风险维度）
        "统一社会信用代码", "法定代表人", "成立时间", "注册资本", "参保人数",
        "企业状态", "所属行业", "注册地址",
        # 股东结构（关联集中度维度）
        "股东名称", "持股比例", "认缴出资额", "股东类型",
    ])
    ws.append([
        NAME, 76.9, 1.12, 0.88, -12.5, 18.6, -8.4, -32000000, "标准无保留意见",
        "91110108MA01ABCD2X", "张永创", "2018-06-12", "2000万元", "38", "存续",
        "软件和信息技术服务业", "北京市海淀区",
        "张永创", 65, "1300万元", "自然人",
    ])
    # 表头共 21 列：0-8 基础财务（9列）+ 9-16 工商（8列）+ 17-20 股东（4列）
    # 第 2/3 行只填股东列，前面留空（None），保证列索引与表头对齐
    _BLANK = [None] * 17   # 对齐到「股东名称」列（index 17）
    ws.append(_BLANK + ["北京华信控股有限公司", 25, "500万元", "企业"])
    ws.append(_BLANK + ["李慧", 10, "200万元", "自然人"])
    path = os.path.join(OUT, "_t_fin.xlsx")
    wb.save(path)
    return path


def make_legal_docx():
    """司法清单 + 舆情：含"被执行"事件（触发红线）与负面舆情。"""
    from docx import Document
    doc = Document()
    doc.add_heading("司法与舆情线索清单", level=1)
    doc.add_paragraph(f"供应商名称：{NAME}")
    doc.add_paragraph(f"{NAME}被列为被执行人，执行法院北京市第一中级人民法院，案号(2025)京01执1234号，执行标的1850万元。")
    doc.add_paragraph(f"{NAME}因拖欠供应商货款被多家媒体报道，存在较大的负面舆情。")
    path = os.path.join(OUT, "_t_legal.docx")
    doc.save(path)
    return path


def make_mark_pdf():
    """外包标记 + 工商补充：重要外包/非驻场标记 + 经营范围。"""
    try:
        import pymupdf as fitz   # PyMuPDF >= 1.24 新包名
    except ImportError:          # 兼容旧版（会提示 fitz API 已弃用）
        import fitz
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    lines = [
        f"供应商名称：{NAME}",
        "",
        "外包属性标记",
        "重要外包：是",
        "非驻场：是",
        "跨境外包：否",
        "",
        "经营范围：计算机软件开发、系统集成与信息技术咨询服务",
    ]
    y = 90
    for ln in lines:
        if ln:
            page.insert_text((60, y), ln, fontname="china-s", fontsize=12)
        y += 26
    path = os.path.join(OUT, "_t_mark.pdf")
    doc.save(path)
    doc.close()
    return path


def make_vertical_xlsx():
    """纵向清单夹具：左列指标名、右列取值（单主体"风险评估表"最常见形态）。

    覆盖 2026-09-14 修复的布局兼容场景：此前 `_parse_excel` 只支持横向宽表，
    用户上传纵向表会导致工商/股东/舆情/财务全部落空 → 多维显示"未录入"。
    """
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "供应商风险评估表"
    rows = [
        ("项目", "内容"),
        ("供应商名称", "北京华信永创科技有限公司"),
        ("统一社会信用代码", "91110108MA01ABCD2X"),
        ("法定代表人", "张永创"),
        ("成立时间", "2018-06-12"),
        ("注册资本", "2000万元"),
        ("参保人数", "38"),
        ("企业状态", "存续"),
        ("所属行业", "软件和信息技术服务业"),
        ("注册地址", "北京市海淀区"),
        ("资产负债率", "76.9%"),
        ("流动比率", "1.12"),
        ("速动比率", "0.88"),
        ("净利率", "-12.5%"),
        ("ROE", "-8.4%"),
        ("经营现金流", "-3200"),
        ("审计意见", "标准无保留意见"),
        ("股东名称", "张永创"),
        ("持股比例", "65%"),
        ("股东名称", "北京华信控股有限公司"),
        ("持股比例", "25%"),
        ("股东名称", "李慧"),
        ("持股比例", "10%"),
        ("是否重要外包", "是"),
        ("2025年3月，因拖欠供应商货款被起诉，涉案金额1850万元。",),
        ("2025年6月，媒体报道公司裁员30%，多名员工投诉薪资发放延迟。",),
    ]
    for r in rows:
        ws.append([c for c in r])   # 注意：单元素行必须写成 ("文本",) 带逗号
    path = os.path.join(OUT, "_t_vertical.xlsx")
    wb.save(path)
    return path


def make_table_docx():
    """Word 表格夹具：数据全部位于 Word 表格中（非段落）。

    覆盖 2026-09-14 修复场景：`_parse_docx` 此前只读 d.paragraphs，
    漏掉表格 → 真实"风险评估表"若用 Word 表格排版则全维度落空。
    """
    from docx import Document
    d = Document()
    d.add_heading("供应商风险评估表（表格版）", 0)
    d.add_paragraph("本表数据全部位于表格中，用于验证 Word 表格解析。")
    t = d.add_table(rows=0, cols=2)
    t.style = "Table Grid"
    rows = [
        ("供应商名称", "北京华信永创科技有限公司"),
        ("统一社会信用代码", "91110108MA01ABCD2X"),
        ("法定代表人", "张永创"),
        ("成立时间", "2018-06-12"),
        ("注册资本", "5000万元"),
        ("参保人数", "38人"),
        ("所属行业", "软件和信息技术服务业"),
        ("资产负债率", "76.9%"),
        ("流动比率", "1.12"),
        ("速动比率", "0.88"),
        ("销售净利率", "-12.5%"),
        ("加权净资产收益率", "-8.4%"),
        ("经营活动产生的现金流量净额", "-3200万元"),
        ("审计意见", "标准无保留意见"),
        ("股东名称", "张永创"),
        ("持股比例", "65%"),
        ("股东名称", "北京华信投资管理中心"),
        ("持股比例", "35%"),
        ("是否重要外包", "是"),
        ("是否跨境外包", "否"),
    ]
    for k, v in rows:
        c = t.add_row().cells
        c[0].text = k
        c[1].text = v
    path = os.path.join(OUT, "_t_table.docx")
    d.save(path)
    return path


def make_import_xlsx():
    """台账批量导入夹具（供 scripts/ui_verify_import.js 使用）。

    列名需与 core/roster_import.py 的 _COL_ALIASES 对齐；
    导入后日志验断言"海康威视""汉得信息"两家出现在台账中。
    """
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "供应商台账"
    ws.append(["供应商名称", "外包分类", "是否重要外包", "是否非驻场", "合同开始", "合同结束",
               "最近尽调日期", "年度金额（万元）", "集团/关联方", "业务条线"])
    ws.append(["海康威视数字技术股份有限公司", "安全服务类", "是", "否",
               "2024-01-01", "2028-12-31", "2026-03-15", 1800, "海康威视", "网络运维、影像平台"])
    ws.append(["上海汉得信息技术股份有限公司", "开发测试类", "否", "是",
               "2025-01-01", "2027-12-31", "2026-05-20", 950, "汉得信息", "信贷系统"])
    path = os.path.join(OUT, "_test_import.xlsx")
    wb.save(path)
    return path


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    for fn in (make_fin_xlsx, make_legal_docx, make_mark_pdf,
               make_vertical_xlsx, make_table_docx, make_import_xlsx):
        p = fn()
        print(f"{os.path.getsize(p):>8} bytes  {p}")

    # 自检：解析 + 合并
    # ⚠️ 路径按「本脚本所在目录的上一级」定位，而不是旧布局的 OUT/../app：
    #    交付包里没有同级 app/ 目录，硬编码会让自检永远 ModuleNotFoundError 被跳过
    #    （表现为"夹具生成了但解析自检从未真正跑过"）。
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from security.file_parser import parse_supplier_file, merge_profiles, profile_contributions

    print("\n--- 解析 + 合并自检 ---")
    profiles = []
    for f in ("_t_fin.xlsx", "_t_legal.docx", "_t_mark.pdf"):
        p = parse_supplier_file(os.path.join(OUT, f))
        profiles.append(p)
        print(f"{f:16} 贡献 = {profile_contributions(p)}")
    m = merge_profiles(profiles)
    print("合并后 name =", m.name)
    print("  important =", m.important_outsourcing, "| non_resident =", m.non_resident,
          "| conc_provided =", m.conc_flags_provided)
    print("  debt_ratio =", m.financials.debt_ratio, "| net_margin =", m.financials.net_margin)
    print("  legal =", [e.title for e in m.legal_events])

    # 纵向布局自检（回归守护）
    print("\n--- 纵向清单布局自检 ---")
    v = parse_supplier_file(os.path.join(OUT, "_t_vertical.xlsx"))
    from core.scoring import dim_has_data
    print("  name =", v.name)
    print("  贡献 =", profile_contributions(v))
    print("  五维可用 =", {d: dim_has_data(v, d) for d in
                           ("legal", "operating", "financial", "reputation", "concentration")})

    # Word 表格布局自检（回归守护）
    print("\n--- Word 表格布局自检 ---")
    tw = parse_supplier_file(os.path.join(OUT, "_t_table.docx"))
    print("  name =", tw.name)
    print("  贡献 =", profile_contributions(tw))
    print("  股东 =", [(s.name, s.ratio) for s in tw.shareholders])

