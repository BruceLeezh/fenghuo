"""涉密文件解析器 —— 解析上传的 Excel/PDF/Word，转成统一 schema。

安全原则：文件只在本地内存/临时目录处理，绝不上传云端。
解析出的结构化数据（财务指标、事件）直接交给评级引擎，全程本地。
"""
from core.quiet_log import log_swallow as _log_swallow
import os
import re
import sys
from dataclasses import fields
from typing import List, Optional

from core.schemas import (SupplierProfile, LegalEvent, NewsItem,
                          FinancialMetrics, Shareholder)


def parse_supplier_file(file_path: str) -> SupplierProfile:
    """按扩展名分发解析，返回统一 SupplierProfile。"""
    ext = os.path.splitext(file_path)[1].lower()
    if ext in (".xlsx", ".xls"):
        return _parse_excel(file_path)
    if ext == ".pdf":
        return _parse_pdf(file_path)
    if ext in (".docx", ".doc"):
        return _parse_docx(file_path)
    raise ValueError(f"不支持的格式: {ext}（支持 Excel/PDF/Word）")


def _detect_vertical_rows(rows: list) -> Optional[list]:
    """若 rows 是"指标名 | 取值"纵向表，返回 [(标签, 取值), ...]；否则 None。

    判定依据：列数很少（≤3），且首列绝大多数单元格是文本标签
    （如"资产负债率""成立时间"），而非"供应商名称"这类字段表头。
    """
    if not rows or len(rows) < 2:
        return None

    def _is_label(v) -> bool:
        """该单元格像"指标名"（纯文本），而不是取值（数字/百分比/日期）。"""
        if v is None:
            return False
        s = str(v).strip()
        if not s or len(s) > 40:
            return False
        if _to_float(s) is not None:      # 纯数字/百分比/日期 → 不是标签
            return False
        return any(ch.isalpha() or "\u4e00" <= ch <= "\u9fff" for ch in s)

    # ⚠️ 不能用原始列宽判定：openpyxl 会把行 padding 到 Excel 的 max_column，
    # 纵向表也常带一堆空列。只统计"真正有值"的列数。
    def _nonempty_width(r) -> int:
        n = 0
        for cell in r:
            if cell not in (None, ""):
                n += 1
        return n

    width = max((_nonempty_width(r) for r in rows), default=0)
    if width > 3:
        return None  # 多列有效值 → 判为横向宽表

    # ⚠️ 只看宽度不够：3 列的普通宽表（供应商名称 | 资产负债率 | 流动比率）宽度也是 3，
    #    会被误判成纵向表再"转置"，把表头当标签、把第二个指标当取值，结果整表丢光
    #    （实测直接报"Excel 中未识别到任何可评估内容"）。
    #    纵向表的本质是**每行只有一个取值**；宽表的数据行常有 ≥2 个数值单元格。
    for r in rows:
        nums = sum(1 for c in r
                   if c not in (None, "") and _to_float(c) is not None)
        if nums >= 2:
            return None  # 同一行有两个以上数值 → 横向宽表

    def _first_value(r):
        for cell in r[1:]:
            if cell not in (None, ""):
                return cell
        return None

    # 首列称得上"标签"的行占比（排除空行 / 纯数字行）
    label_n = 0
    meaningful = 0
    for r in rows:
        if not r or all(c in (None, "") for c in r):
            continue
        meaningful += 1
        if _is_label(r[0]):
            label_n += 1
    if meaningful < 2 or label_n < max(2, int(meaningful * 0.5)):
        return None  # 首列不是标签列 → 视为横向表

    pairs = []
    for r in rows:
        if not r or all(c in (None, "") for c in r):
            continue
        key = str(r[0]).strip() if r[0] is not None else ""
        if not key:
            continue
        val = _first_value(r)
        pairs.append((key, val if val is not None else ""))
    return pairs or None


def _vertical_to_wide(pairs: list) -> list:
    """把纵向 [(标签, 取值)] 转成横向宽表 [表头, 数据行]。

    - 每个标签占用一列；
    - 同名标签（如多行"股东名称"）依次占用第 1、2、3… 个同名列，
      表头追加序号后缀（股东名称、股东名称__2…），下游按前缀匹配即可。
    """
    headers, values = [], []
    seen = {}
    for k, v in pairs:
        n = seen.get(k, 0)
        seen[k] = n + 1
        headers.append(k if n == 0 else f"{k}__{n + 1}")
        values.append(v)
    return [headers, values]


def _parse_excel(file_path: str) -> SupplierProfile:
    """解析 Excel：约定表格包含供应商名、财务指标、事件等列。

    约定列名（第一行表头，可配置）：
    - 供应商名称 / 是否重要外包 / 是否跨境外包
    - 资产负债率 / 流动比率 / 净利率 / ROE / 经营现金流 / 审计意见
    - 事件标题 / 事件类型 / 严重度 / 金额 / 日期 / 来源
    """
    from openpyxl import load_workbook

    # ⚠️ data_only=True 时，若文件由程序生成且从未被 Excel 打开过，
    # 公式单元格会返回 None（无缓存值）→ 整表读不到数。
    # 先按 data_only=True 读；若各表几乎全空，再用 data_only=False 重读取公式文本。
    # read_only=True：大台账整本载入会吃满内存，流式读取即可；
    # 下游只用 iter_rows(values_only=True) 与 ws.title，均支持只读模式。
    try:
        wb = load_workbook(file_path, data_only=True, read_only=True)
    except Exception as e:
        raise ValueError(f"Excel 打开失败（可能不是有效的 xlsx，或为老式 .xls）：{e}")

    # 单表最多读这么多行：read_only 只保证流式读取，`list(iter_rows(...))` 仍会把
    # 整表驻留内存。行数远超真实台账（几十家）时截断并提示，避免一个畸形大表打爆内存。
    _MAX_ROWS_PER_SHEET = 5000

    def _sheet_rows(ws):
        rows = []
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if i >= _MAX_ROWS_PER_SHEET:
                print(f"[file_parser] 工作表「{ws.title}」超过 {_MAX_ROWS_PER_SHEET} 行，"
                      "已截断（仅解析前若干行）", file=sys.stderr)
                break
            rows.append(row)
        return rows

    def _collect(book):
        out = []
        for ws in book.worksheets:
            try:
                rs = _sheet_rows(ws)
            except Exception:
                _log_swallow("security/file_parser.py:137")
                continue
            if rs and any(c not in (None, "") for r in rs for c in r):
                out.append((ws.title, rs))
        return out

    # ⚠️ openpyxl 在 read_only 模式下**持有文件句柄**，不 close() 会让 Windows 一直
    #    锁住这个临时文件，服务端 finally 里的 os.unlink 静默失败 —— 涉密文件的临时
    #    副本会永久残留在 %TEMP%。故读到内存后立即关（异常路径也关），后续解析只用
    #    内存里的 candidates，不再碰 workbook。
    candidates = []
    active_title = ""
    wb2 = None
    try:
        candidates = _collect(wb)
        if not candidates:
            # 回退：可能是公式表（data_only 无缓存）或隐藏 sheet
            try:
                wb2 = load_workbook(file_path, data_only=False, read_only=True)
                candidates = _collect(wb2)
            except Exception:
                candidates = []
        active_title = getattr(wb.active, "title", "")
    finally:
        for book in (wb, wb2):
            if book is None:
                continue
            try:
                book.close()
            except Exception:
                _log_swallow("security/file_parser.py:close_workbook")

    if not candidates:
        raise ValueError("Excel 为空或所有工作表均无数据")

    # ⚠️ 真实台账常把不同维度拆在不同工作表（财务一表、股东一表、事件一表）。
    # 因此逐表解析后做"档案级合并"（字段级补全 + 列表去重），
    # 而不是只挑信息量最大的一张——否则跨表的数据会整块丢失。
    ordered = sorted(candidates, key=lambda t: 0 if t[0] == active_title else 1)
    parsed: List[SupplierProfile] = []
    for _title, rs in ordered:
        try:
            parsed.append(_parse_sheet_rows(rs))
        except ValueError:
            _log_swallow("security/file_parser.py:162")
            continue
        except Exception:
            _log_swallow("security/file_parser.py:164")
            continue
    if not parsed:
        raise ValueError("Excel 中未识别到任何可评估内容")

    # ⚠️ 最后一道兜底：结构解析全空但表格里明显有文字（合并单元格、多级表头、
    # 非常规排版等）。把整本工作簿拍平成纯文本再走一遍通用正则抽取，
    # 避免"文件里明明有数据却显示无有效字段"。
    total_hits = sum(len(profile_contributions(p)) for p in parsed)
    if total_hits == 0:
        flat_lines: List[str] = []
        for _title, rs in candidates:
            for r in rs:
                cells = [str(c).strip() for c in r if c not in (None, "")]
                if cells:
                    flat_lines.append("  ".join(cells))
        flat = "\n".join(flat_lines)
        if flat.strip():
            try:
                text_prof = _parse_from_text(flat)
                if profile_contributions(text_prof):
                    return text_prof
            except Exception:
                _log_swallow("security/file_parser.py:186")

    if len(parsed) == 1:
        final = parsed[0]
    else:
        try:
            final = merge_profiles(parsed)
        except ValueError:
            final = parsed[0]
    # 与 PDF/Word 同口径：结构解析完成但一个字段都没贡献，等于没解析到东西，
    # 必须报错，而不是返回一份「成功但全空」的档案（用户会以为已经录入）。
    _require_evaluable(final, "Excel")
    return final


def _parse_sheet_rows(rows: list) -> SupplierProfile:
    """解析已读出的二维行数据（单一工作表）。被 _parse_excel 按表调用。"""
    if not rows:
        raise ValueError("Excel 为空")

    # ---- 布局探测 ----
    # 支持两种常见写法：
    #   A) 横向宽表：首行是字段名（资产负债率/成立时间…），每行一个供应商
    #   B) 纵向清单：左侧是指标名、右侧是取值（单主体"风险评估表"最常见形态）
    vertical_pairs = _detect_vertical_rows(rows)
    flat_text = ""
    if vertical_pairs is not None:
        # 纵向表：结构字段转成横向一行复用下游逻辑；
        # 同时把全表拍平成文本，供"股东名称：X 持股比例：65%"、
        # 事件描述行等自由文本用正则抽取。
        flat_text = "\n".join(
            f"{k}：{v}" if v not in (None, "") else str(k)
            for k, v in vertical_pairs
        )
        rows = _vertical_to_wide(vertical_pairs)
    else:
        # 横向宽表：表头未必规范（可能是"项目/本期/上期"这类通用列名），
        # 故同样拍平全文做兜底，避免"表头对不上 → 整表全空"。
        flat_text = "\n".join(
            "  ".join(str(c) for c in r if c not in (None, "")) for r in rows
            if any(c not in (None, "") for c in r)
        )

    header = [str(h).strip() if h else "" for h in rows[0]]
    data = rows[1:]

    def col(name):
        return header.index(name) if name in header else None

    # 列名别名（与台账导入一致的容错思路）
    ALIAS = {
        "供应商名称": ("供应商名称", "供应商", "服务商", "公司名称", "企业名称", "单位名称"),
        "是否重要外包": ("是否重要外包", "重要外包", "是否重要"),
        "是否跨境外包": ("是否跨境外包", "跨境外包", "是否跨境"),
        "是否非驻场": ("是否非驻场", "非驻场", "驻场方式"),
        # ---- 工商登记（经营风险维度）----
        "成立时间": ("成立时间", "成立日期", "成立年限", "注册时间"),
        "注册资本": ("注册资本", "注册资金"),
        "参保人数": ("参保人数", "社保人数", "员工人数", "缴纳社保人数"),
        "统一信用代码": ("统一社会信用代码", "统一信用代码", "信用代码", "社会信用代码"),
        "法定代表人": ("法定代表人", "法人代表", "法人", "负责人"),
        "企业状态": ("企业状态", "经营状态", "登记状态", "状态"),
        "所属行业": ("所属行业", "行业", "行业类别"),
        "注册地址": ("注册地址", "注册地", "所在地区", "地区", "省份"),
        "经营范围": ("经营范围", "主营业务", "业务范围"),
        # ---- 股东（关联集中度维度）----
        "股东名称": ("股东名称", "股东", "主要股东", "出资人"),
        "持股比例": ("持股比例", "出资比例", "股权比例", "占比"),
        "认缴出资额": ("认缴出资额", "认缴金额", "出资额", "认缴出资"),
        "股东类型": ("股东类型", "出资人类型"),
        # ---- 财务补充列别名 ----
        "资产负债率": ("资产负债率", "负债率"),
        "流动比率": ("流动比率",),
        "速动比率": ("速动比率",),
        "净利率": ("净利率", "销售净利率", "净利润率"),
        "毛利率": ("毛利率", "销售毛利率"),
        "ROE": ("ROE", "净资产收益率"),
        "ROA": ("ROA", "总资产报酬率"),
        "经营现金流": ("经营现金流", "经营活动现金流", "经营活动产生的现金流量净额"),
        "净利润": ("净利润", "归属净利润"),
        "营业收入": ("营业收入", "营收", "营业总收入"),
        "资产总额": ("资产总额", "总资产"),
        "营收同比": ("营收同比", "营业收入同比增长率", "营收增速"),
        "净利同比": ("净利同比", "净利润同比增长率", "净利增速"),
        "报告期": ("报告期", "期间", "会计期间"),
        "审计意见": ("审计意见", "审计意见类型", "审计结论"),
    }

    def _hdr_match(cell: str, h: str) -> bool:
        """表头匹配：全等、纵向表转置的 "__2" 后缀、或带单位括注（"营业收入（万元）"）。

        带括注的列必须能匹配上，否则表头里写了「万元」的金额列会整列取不到值。
        """
        return (cell == h or cell.startswith(h + "__")
                or cell.startswith(h + "（") or cell.startswith(h + "("))

    def col_any(canonical):
        for h in ALIAS.get(canonical, (canonical,)):
            if h in header:
                return header.index(h)
            # 纵向表转宽表时同名标签会加 "__2" 后缀（如"股东名称__2"）
            for idx, cell in enumerate(header):
                if _hdr_match(cell, h):
                    return idx
        return None

    def col_all(canonical):
        """返回该字段的全部列索引（纵向表可能有"股东名称__2"等多列）。"""
        hits = []
        for idx, cell in enumerate(header):
            for h in ALIAS.get(canonical, (canonical,)):
                if _hdr_match(cell, h):
                    hits.append(idx)
                    break
        return hits

    # 财务指标（取第一行数据）
    def fval(name, money=False):
        """取首个非空数值。

        money=True 时按「元」归一：表头若写了单位（如"营业收入（万元）"），
        把数值列乘以对应数量级；单元格本身带单位（"1.2亿元"）则按文本换算。
        否则"The 万元 column" 会被当成元，金额整体缩小 1 万倍。
        """
        i = col_any(name)
        if i is None:
            return None
        scale = _unit_scale(header[i]) if money else 1.0
        for r in data:
            if i < len(r) and r[i] is not None:
                cell = r[i]
                if not money:
                    return _to_float(cell)
                if isinstance(cell, (int, float)) and not isinstance(cell, bool):
                    return float(cell) * (scale or 1.0)
                return _to_money(cell)
        return None

    def sval(name):
        """取首个非空字符串值（用于工商等文本字段）。"""
        i = col_any(name)
        if i is None:
            return None
        for r in data:
            if i < len(r) and r[i] not in (None, ""):
                return str(r[i]).strip()
        return None

    name = _first_nonempty(data, col_any("供应商名称")) or "未命名供应商"

    # 外包标记：仅当文件中确实提供了对应列时才算"有数据"（strict_na 用）
    important = _to_bool(_first_nonempty(data, col_any("是否重要外包")))
    cross = _to_bool(_first_nonempty(data, col_any("是否跨境外包")))
    non_res = _to_bool(_first_nonempty(data, col_any("是否非驻场")))
    conc_provided = any(_first_nonempty(data, col_any(k)) is not None
                        for k in ("是否重要外包", "是否跨境外包", "是否非驻场"))

    fin = FinancialMetrics(
        debt_ratio=fval("资产负债率"),
        current_ratio=fval("流动比率"),
        quick_ratio=fval("速动比率"),
        net_margin=fval("净利率"),
        gross_margin=fval("毛利率"),
        roe=fval("ROE"),
        roa=fval("ROA"),
        operating_cashflow=fval("经营现金流", money=True),
        net_profit=fval("净利润", money=True),
        revenue=fval("营业收入", money=True),
        total_assets=fval("资产总额", money=True),
        revenue_yoy=fval("营收同比"),
        profit_yoy=fval("净利同比"),
        report_period=sval("报告期"),
        audit_opinion=sval("审计意见"),
    )

    # ---- 工商登记信息（经营风险维度）----
    # 只要任一工商字段被提供即认为"有工商数据"，避免空 dict 被误判为有效。
    reg_map = {
        "established": sval("成立时间"),
        "registered_capital": sval("注册资本"),
        "insured_count": sval("参保人数"),
        "credit_code": sval("统一信用代码"),
        "legal_person": sval("法定代表人"),
        "status": sval("企业状态"),
        "industry": sval("所属行业"),
        "region": sval("注册地址"),
        "business_scope": sval("经营范围"),
    }
    reg = {k: v for k, v in reg_map.items() if v not in (None, "")}
    registration = reg or None
    # 参保人数转为纯数字字符串（评分引擎做 int() 解析，容忍"1,234 人"）
    if registration and registration.get("insured_count"):
        registration["insured_count"] = _clean_int_text(registration["insured_count"])

    # ---- 股东信息（关联集中度维度）----
    shareholders = []
    # 横向宽表：每行一个股东；纵向清单：多个"股东名称"转成多列（股东名称__2…）
    sh_name_cols = col_all("股东名称")
    if sh_name_cols:
        ratio_cols = col_all("持股比例")
        amt_cols = col_all("认缴出资额")
        type_cols = col_all("股东类型")

        def _paired(cols, i):
            """多列时按出现顺序配对；列数不足则回落到第一列。"""
            return cols[i] if i < len(cols) else (cols[0] if cols else None)

        for i, sh_name_i in enumerate(sh_name_cols):
            ratio_i = _paired(ratio_cols, i)
            amt_i = _paired(amt_cols, i)
            type_i = _paired(type_cols, i)
            for r in data:
                if sh_name_i >= len(r) or not r[sh_name_i]:
                    continue
                nm = str(r[sh_name_i]).strip()
                if not nm:
                    continue
                shareholders.append(Shareholder(
                    name=nm,
                    ratio=_to_float(r[ratio_i]) if ratio_i is not None and ratio_i < len(r) else None,
                    amount=(str(r[amt_i]).strip() if amt_i is not None and amt_i < len(r)
                            and r[amt_i] not in (None, "") else None),
                    type=(str(r[type_i]).strip() if type_i is not None and type_i < len(r)
                          and r[type_i] not in (None, "") else ""),
                ))
        shareholders = _dedup_by_key(shareholders, key=lambda s: s.name)

    legal, news = [], []
    title_i = col_any("事件标题") or col("事件标题")
    if title_i is not None:
        type_i = col_any("事件类型") or col("事件类型")
        sev_i = col_any("严重度") or col("严重度")
        amt_i = col_any("金额") or col("金额")
        date_i = col_any("日期") or col("日期")
        src_i = col_any("来源") or col("来源")
        for r in data:
            t = r[title_i] if title_i < len(r) else None
            if not t:
                continue
            etype = str(r[type_i]) if type_i is not None and type_i < len(r) and r[type_i] else ""
            sev = str(r[sev_i]) if sev_i is not None and sev_i < len(r) and r[sev_i] else "medium"
            if "法律" in etype or "诉讼" in etype or "被执行" in etype or "处罚" in etype:
                legal.append(LegalEvent(
                    title=str(t), severity=sev,
                    amount=str(r[amt_i]) if amt_i is not None and amt_i < len(r) and r[amt_i] else None,
                    occurred_at=str(r[date_i]) if date_i is not None and date_i < len(r) and r[date_i] else None,
                    source=str(r[src_i]) if src_i is not None and src_i < len(r) and r[src_i] else "上传文件",
                ))
            else:
                news.append(NewsItem(
                    title=str(t),
                    sentiment="negative" if "负面" in etype else "neutral",
                    severity=sev,
                    source=str(r[src_i]) if src_i is not None and src_i < len(r) and r[src_i] else "上传文件",
                ))

    # ---- 全文正则兜底 ----
    # 表头未必规范（"项目/本期/上期"这类通用列名），或关键指标写成自由文本。
    # 凡是结构化抽取没拿到的字段，一律用全文正则再捞一遍（不覆盖已抽到的值）。
    if flat_text:
        try:
            fb = _parse_from_text(flat_text)
        except Exception:
            fb = None
        if fb:
            if not registration and fb.registration:
                registration = fb.registration
            if not shareholders and fb.shareholders:
                shareholders = fb.shareholders
            if not legal and fb.legal_events:
                legal = fb.legal_events
            if not news and fb.news:
                news = fb.news
            # 财务字段逐个补空缺（此前遗漏：导致"表头不认识 → 财务整块为空"）
            if fb.financials:
                for f in fields(FinancialMetrics):
                    if getattr(fin, f.name, None) is None:
                        v = getattr(fb.financials, f.name, None)
                        if v is not None:
                            setattr(fin, f.name, v)
            if not name or name == "未命名供应商":
                if fb.name and fb.name != "涉密供应商（本地解析）":
                    name = fb.name
            if not (important or cross or non_res):
                important, cross, non_res = (fb.important_outsourcing,
                                             fb.cross_border, fb.non_resident)
                if fb.conc_flags_provided:
                    conc_provided = True

    return SupplierProfile(
        name=name,
        important_outsourcing=important,
        cross_border=cross,
        non_resident=non_res,
        legal_events=legal,
        news=news,
        financials=fin,
        registration=registration,
        shareholders=shareholders,
        conc_flags_provided=conc_provided,  # 文件确实提供了外包标记（供 strict_na 判定）
    )


# 单份 PDF 最多解析的页数：几百页的年报/审计报告全文正则扫描又慢又易误匹配，
# 演示场景只需前 N 页的结论性内容；超出时在 stderr 说明已截断。
_PDF_MAX_PAGES = 200


def _require_evaluable(prof: SupplierProfile, kind: str) -> SupplierProfile:
    """与 Excel 分支同口径：识别不出任何可评估字段就报错，不返回"空档案"。

    Excel 侧见 _parse_excel 的"Excel 中未识别到任何可评估内容"；PDF/Word 若
    只回一个默认名档案，服务端会记 ok=True、fields=[]，看着成功实则什么都没
    识别到，行情/财务维度也就静默失踪。
    """
    if not profile_contributions(prof):
        raise ValueError(
            f"{kind} 中未识别到任何可评估内容（财务/工商/司法/舆情/股东等均未识别到）")
    return prof


def _open_pdf(file_path: str):
    """打开 PDF 并返回 (doc, backend)。

    后端顺序：PyMuPDF(fitz) → pypdf → 明确报错。
    为什么留第二条路：PyMuPDF 是 **AGPL-3.0**，内部自用无碍，但若把本系统
    打包成商业产品分发，AGPL 会要求开源整个衍生作品。pypdf 是 BSD-3，
    体积小、纯 Python，代价是慢一些、对扫描件布局更敏感。两条路都走不通时
    直接报错并说明装哪个，而不是让用户看到一句 ImportError。
    """
    try:
        import fitz                     # PyMuPDF（快，AGPL-3.0）
        return fitz.open(file_path), "pymupdf"
    except ImportError:
        pass
    try:
        from pypdf import PdfReader     # BSD-3-Clause 备选
        return PdfReader(file_path), "pypdf"
    except ImportError as e:
        raise ValueError(
            "缺少 PDF 解析依赖：请安装 PyMuPDF（pip install PyMuPDF，AGPL-3.0，"
            "内部使用或需商业授权）或 pypdf（pip install pypdf，BSD-3，可商用）") from e


def _parse_pdf(file_path: str) -> SupplierProfile:
    """解析 PDF：提取文本，做启发式字段识别（财报/审计报告场景）。"""
    try:
        doc, backend = _open_pdf(file_path)
    except ValueError:
        raise
    except Exception as e:  # noqa: BLE001
        _log_swallow("security/file_parser.py:_parse_pdf/open")
        raise ValueError(f"PDF 打开失败（可能已加密或文件损坏）：{e}")
    try:
        if backend == "pymupdf":
            if doc.needs_pass:
                raise ValueError("PDF 已加密（需要打开密码），无法在本地解析")
            total = doc.page_count
            text = "\n".join(doc[i].get_text() for i in range(min(total, _PDF_MAX_PAGES)))
        else:
            if getattr(doc, "is_encrypted", False):
                raise ValueError("PDF 已加密（需要打开密码），无法在本地解析")
            total = len(doc.pages)
            text = "\n".join((doc.pages[i].extract_text() or "")
                             for i in range(min(total, _PDF_MAX_PAGES)))
    except ValueError:
        raise
    except Exception as e:  # noqa: BLE001
        _log_swallow("security/file_parser.py:_parse_pdf/extract")
        raise ValueError(f"PDF 文本提取失败（可能已加密或文件损坏）：{e}")
    finally:
        try:
            doc.close()
        except Exception:  # noqa: BLE001
            _log_swallow("security/file_parser.py:_parse_pdf/close")
    if total > _PDF_MAX_PAGES:
        print(f"[file_parser] PDF 共 {total} 页，已截断，仅解析前 {_PDF_MAX_PAGES} 页",
              file=sys.stderr)
    return _require_evaluable(_parse_from_text(text), "PDF")


def _iter_docx_blocks(d):
    """按**文档原始顺序**交替产出段落与表格。

    ⚠️ 早先的实现是"先拼所有段落、再拼所有表格"，会把结构信息打乱：
    章节标题（如"1.2 股东及出资信息""2.1 对外投资/关联企业"）全跑到前面，
    表格数据全堆在后面，于是"按章节标题判断表格语义"的规则全部失效
    （实测导致真正的股东表被误判为"对外投资"章节而整表丢弃）。
    """
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph
    for child in d.element.body.iterchildren():
        if child.tag == qn("w:p"):
            yield ("p", Paragraph(child, d))
        elif child.tag == qn("w:tbl"):
            yield ("t", Table(child, d))


def _parse_docx(file_path: str) -> SupplierProfile:
    """解析 Word：按文档顺序提取段落 + 表格，做启发式字段识别。

    ⚠️ 只读 d.paragraphs 会漏掉表格——真实"风险评估表"大量把
    财务指标/股东/事件放在 Word 表格里，表格内容必须一并拍平。
    """
    from docx import Document
    d = Document(file_path)

    lines: List[str] = []
    for kind, block in _iter_docx_blocks(d):
        if kind == "p":
            t = (block.text or "").strip()
            if t:
                lines.append(t)
        else:
            # 表格：按行拍平成"单元格1 单元格2 …"，与 Excel 平铺格式一致，便于共用正则
            for row in block.rows:
                cells = [(c.text or "").strip() for c in row.cells]
                cells = [c for c in cells if c]
                if cells:
                    lines.append("  ".join(cells))

    text = "\n".join(lines)
    return _require_evaluable(_parse_from_text(text), "Word")


def _parse_from_text(text: str) -> SupplierProfile:
    """从纯文本启发式抽取字段（演示级，真实接入可换 LLM 本地抽取）。

    这里用规则抽取关键指标，体现"本地解析不联网"。
    涉密文件正式版可替换为 LocalLLM 做结构化抽取。
    """
    import re
    _re = re   # 统一别名（下方正则工具函数共用）

    def find(pattern, text=text):
        m = re.search(pattern, text)
        return _to_float(m.group(1)) if m else None

    # 财务指标的取值写法极其自由：可能带冒号、空格、制表、中文单位、
    # 全角负号，甚至写在同一行的两列里。统一用一个宽松片段描述"指标名 → 数值"。
    _NUM = r"([-−－–—]?\s*[\d,，]+(?:[.．][\d]+)?\s*%?(?:\s*(?:万元|亿元|千元|万|亿|元))?)"
    _SEP = r"[：:\s\t|，,]*"

    def fin_find(*names):
        """按别名依次尝试；分隔符与数值写法都放宽。取首个命中。"""
        for nm in names:
            m = re.search(re.escape(nm) + _SEP + _NUM, text)
            if m:
                v = _to_float(m.group(1))
                if v is not None:
                    return v
        return None

    def money_find(*names):
        """金额类指标：与 fin_find 同解析，但把「万元/亿元」换算成「元」。

        FinancialMetrics 的金额字段约定为「元」，报告 `_money` 也按元展示；
        若这里只剥单位不换算，「营业收入 1.2亿元」会存成 1.2，界面显示「1.20 元」，
        缩小一亿倍 —— 财务维度据此判定的资产负债率/盈利指标都会失真。
        """
        for nm in names:
            m = re.search(re.escape(nm) + _SEP + _NUM, text)
            if m:
                v = _to_money(m.group(1))
                if v is not None:
                    return v
        return None

    fin = FinancialMetrics(
        debt_ratio=fin_find("资产负债率", "负债率"),
        current_ratio=fin_find("流动比率"),
        quick_ratio=fin_find("速动比率"),
        net_margin=fin_find("净利率", "销售净利率", "净利润率"),
        gross_margin=fin_find("毛利率", "销售毛利率"),
        roe=fin_find("ROE", "净资产收益率", "加权净资产收益率"),
        roa=fin_find("ROA", "总资产报酬率"),
        operating_cashflow=money_find("经营活动产生的现金流量净额", "经营活动现金流",
                                      "经营现金流", "经营现金净流量"),
        net_profit=money_find("归属于母公司股东的净利润", "归属净利润", "净利润"),
        revenue=money_find("营业总收入", "营业收入", "营收"),
        total_assets=money_find("资产总额", "总资产"),
        audit_opinion=_find_audit_opinion(text),
    )

    # ---- 工商登记信息（经营风险维度）----
    # 从"字段：值"段落启发式抽取；任一命中即视为有工商数据。
    def reg_find(pattern):
        mm = _re.search(pattern, text)
        return mm.group(1).strip() if mm else None

    reg_map = {
        "established": reg_find(r"(?:成立时间|成立日期|注册时间|成立)[：:\s\t]*([0-9]{4}[-/年][0-9]{1,2}(?:[-/月][0-9]{1,2})?)"),
        "registered_capital": reg_find(r"(?:注册资本|注册资金)[：:\s\t]*([^\s，,。；;\n|]{1,24})"),
        "insured_count": reg_find(r"(?:参保人数|社保人数|员工人数|在职人数)[：:\s\t]*([\d,，]{1,12})"),
        "credit_code": reg_find(r"(?:统一社会信用代码|统一信用代码|信用代码|纳税人识别号)[：:\s\t]*([0-9A-Za-z]{8,20})"),
        "legal_person": reg_find(r"(?:法定代表人|法人代表|法人)[：:\s\t]*([^\s，,。；;\n|]{2,20})"),
        "status": reg_find(r"(?:企业状态|经营状态|登记状态|公司状态)[：:\s\t]*([^\s，,。；;\n|]{2,10})"),
        "industry": reg_find(r"(?:所属行业|行业类别|所属门类|行业)[：:\s\t]*([^\s，,。；;\n|]{2,20})"),
        "region": reg_find(r"(?:注册地址|注册地|所在地区|注册地点|住所)[：:\s\t]*([^\n|]{2,60})"),
        "business_scope": reg_find(r"(?:经营范围|主营业务|业务范围)[：:\s\t]*([^\n|]{4,200})"),
    }
    registration = {k: v for k, v in reg_map.items() if v} or None
    if registration and registration.get("insured_count"):
        registration["insured_count"] = _clean_int_text(registration["insured_count"])

    # ---- 股东信息（关联集中度维度）----
    # 支持三种写法：
    #   ① 股东：XX公司 持股比例：65%
    #   ② XX有限公司 持股 65%
    #   ③ 股东名称：XX  持股比例：65%  认缴出资额：100万
    shareholders = []

    # ---- 情形 0（最强）：股东表格的数据行 ----
    # 拍平后形如"周立恒  38.60%  1930万元  自然人股东  实际控制人"
    # 即"名称 + 比例% + 认缴出资额（带单位）"。
    # 这个组合几乎只出现在股东表里，命中即高置信；
    # 而且"备注/股东类型"列的角色词（实际控制人…）在行尾，不会被误抓成股东名。
    strong = []
    for mm in _re.finditer(
            r"([^\s|｜]{2,40}?)[\s\t|｜]+([\d,，.]+)\s*[%％][\s\t|｜]+"
            r"([\d,，.]+)\s*(?:亿元|万元|千元|万|亿|元)", text):
        nm = mm.group(1).strip()
        if _bad_shareholder_name(nm) or _in_excluded_section(text, mm.start()):
            continue
        strong.append(Shareholder(name=nm, ratio=_to_float(mm.group(2)), type=""))
    # 同一股东可能被年份展开的表格重复命中 → 先去重
    strong = _dedup_by_key(strong, key=lambda s: s.name)

    # ---- 情形 A/B/C（弱特征，用于散文式描述；仅当没有股东表时启用）----
    # ⚠️ SH_LABEL 不含"投资人"：真实数据里"财务投资人"是股东类型，
    #    把"投资人"当标签会抓出角色词而非股东名。
    SH_LABEL = r"(?:股东名称|股东姓名|出资人|合伙人名称|股东)"
    RATIO_LABEL = r"(?:持股比例|出资比例|股权比例|认缴比例|持股|占比)"

    weak = []
    # 情形 A：同一行内 股东名称 + 比例
    for mm in _re.finditer(
            SH_LABEL + r"[：:\s\t]*([^\s，,。；;（(|]{2,40}?)"
            r"(?:[\s\t]+[^\n]{0,40}?)?" + RATIO_LABEL +
            r"[：:\s]*([-−－]?[\d,，.]+)\s*[%％]?", text):
        nm = mm.group(1).strip()
        if _bad_shareholder_name(nm) or _in_excluded_section(text, mm.start()):
            continue
        weak.append(Shareholder(name=nm, ratio=_to_float(mm.group(2)), type=""))

    # 情形 B：纵向成对行 —— "股东名称：张三\n持股比例：65%"
    for mm in _re.finditer(
            SH_LABEL + r"[：:\s\t]*([^\s，,。；;（(|]{2,40})"
            r"(?:\s*\n\s*[^\n：:]{0,30}){0,3}\s*\n?\s*" + RATIO_LABEL +
            r"[：:\s]*([-−－]?[\d,，.]+)\s*[%％]?", text):
        nm = mm.group(1).strip()
        if _bad_shareholder_name(nm) or _in_excluded_section(text, mm.start()):
            continue
        weak.append(Shareholder(name=nm, ratio=_to_float(mm.group(2)), type=""))

    # 情形 C：仅出现"股东名称：张三"（无比例）——保留主体，ratio=None
    for mm in _re.finditer(
            SH_LABEL + r"[：:\s\t]+([^\s，,。；;（(|]{2,40})", text):
        nm = mm.group(1).strip()
        if _bad_shareholder_name(nm) or _in_excluded_section(text, mm.start()):
            continue
        weak.append(Shareholder(name=nm, ratio=None, type=""))

    # 写法②兜底：句中"XX有限公司 持股 65%"
    for mm in _re.finditer(
            r"(" + _COMPANY_RE + r")"
            r"[^\n]{0,12}?(?:持股|出资|占比|比例)?[：:\s]*([\d,，.]+)\s*[%％]", text):
        nm = mm.group(1).strip()
        if _bad_shareholder_name(nm) or _in_excluded_section(text, mm.start()):
            continue
        weak.append(Shareholder(name=nm, ratio=_to_float(mm.group(2)), type="企业"))

    # 有股东表就以它为准（权威），否则用弱特征
    shareholders = strong if strong else weak
    shareholders = _dedup_by_key(shareholders, key=lambda s: s.name)
    # 同一股东被多处命中时，保留有比例的那条
    _by_name: dict = {}
    for s in shareholders:
        prev = _by_name.get(s.name)
        if prev is None or (prev.ratio is None and s.ratio is not None):
            _by_name[s.name] = s
    shareholders = list(_by_name.values())


    # ---- 事件：识别关键词所在句（法律 + 舆情）----
    legal, news = [], []
    # 法律事件关键词（司法程序 / 监管处罚）：命中即归法律维度
    # ⚠️ 不放"司法"：它在真实数据里是分节标题"司法风险｜低"这类**标签**，
    #    不是事件，误命中会污染法律维度。
    LEGAL_KW = {
        "被执行": "high", "失信被执行": "high", "经营异常": "high",
        "行政处罚": "medium", "监管处罚": "medium", "罚款": "medium",
        "责令改正": "medium", "责令": "medium",
        "诉讼": "medium", "起诉": "medium", "被诉": "medium",
        "开庭": "medium", "判决": "medium", "仲裁": "medium",
        "强制执行": "high", "失信": "high",
        "破产": "high", "清算": "high", "查封": "high", "冻结": "high",
    }
    # 舆情关键词：与法律事件的区分在于"负面评价/经营影响"而非司法程序
    NEWS_KW = {
        "欠薪": "high", "拖欠": "medium", "裁员": "medium", "停业": "high",
        "倒闭": "high", "违约": "high",
        "投诉": "medium", "负面": "medium", "质疑": "medium",
        "通报": "medium", "警告": "low", "纠纷": "medium",
    }
    # 先按句子切分
    sentences = _re.split(r"[。\n；;]", text)
    for sent in sentences:
        sent = sent.strip()
        if not sent:
            continue
        # 跳过表格表头行（如"年度 一次验收合格率 退换货率 质量投诉(次) 备注"）——
        # 它们只是列名，不是事实陈述，否则会污染舆情维度
        if _looks_like_header(sent):
            continue

        # ① 裁判文书案号：案号本身就是法律事件的强证据，
        #    且它常出现在"案号 | 案件类型…"的表行里，不含 LEGAL_KW 词，
        #    旧实现会把它漏到舆情维度。这里优先识别。
        #    ⚠️ 识别到案号后**不能 continue**：同一句里可能还有更强的信号
        #    （如"被列为被执行人，案号(2025)京01执1234号"），
        #    跳过关键词检查会把该触发的红线漏掉。
        case = _CASE_NO_RE.search(sent)
        if case:
            cno = case.group(0)
            ctype = _CASE_TYPE_RE.search(cno)
            ctype = ctype.group(1) if ctype else ""
            if ctype.startswith("执"):
                # 执行案件（含执异/执恢）＝被执行人身份的直接证据
                legal.append(LegalEvent(title="执行案件", severity="high",
                                        source="上传文件", evidence=cno))
            else:
                m_type = _re.search(r"(买卖合同纠纷|知识产权纠纷|劳动争议|合同纠纷|"
                                    r"借款合同纠纷|建设工程纠纷|侵权责任纠纷)", sent)
                legal.append(LegalEvent(
                    title=(m_type.group(1) if m_type else "诉讼文书"),
                    severity="medium", source="上传文件", evidence=cno))

        # ② 法律关键词（带否定语境判定）
        hit = case is not None
        for kw, sev in LEGAL_KW.items():
            if _kw_hit(sent, kw):
                legal.append(LegalEvent(title=kw, severity=sev,
                                        source="上传文件", evidence=sent[:60]))
                hit = True
                break   # 一句只取一个关键词
        if hit:
            continue
        # ③ 非法律句 → 尝试识别舆情（同样带否定语境判定）
        #    过短的句子（如表格里的一个"负面"单元格）没有信息量，丢弃
        if len(sent) < 6:
            continue
        for kw, sev in NEWS_KW.items():
            if _kw_hit(sent, kw):
                news.append(NewsItem(title=sent[:40], sentiment="negative",
                                     severity=sev, source="上传文件",
                                     evidence=sent[:60]))
                break

    # 去重：同一句话/同一标题重复出现只保留一次
    legal = _dedup_by_key(legal, key=lambda e: (e.title, e.evidence))
    news = _dedup_by_key(news, key=lambda n: (n.title, n.evidence))

    # ---- 供应商名称 ----
    # ⚠️ 不能用宽松的 `供应商[：:\s]+(.)`：表格拍平后表头行形如
    #    "物料类别  主要供应商  集中度  风险提示"，会把"集中度"当成公司名
    #    （实测自建模拟样本出现过 name="集中度" / name="持股"）。
    # 改为三步：
    #   ① 优先"公司名称｜XXX有限公司"这类带标签的表格行（值必须像公司名）
    #   ② 否则取全文出现次数最多的"公司形态串"（文档会反复提到自己）
    #   ③ 都没有才用占位名
    name = "涉密供应商（本地解析）"

    lab = _re.search(
        r"(?:公司名称|供应商名称|企业名称|服务商名称|单位名称|主体名称|客户名称)"
        r"[：:\s\t|｜]*(" + _COMPANY_RE + r")", text)
    if lab:
        name = lab.group(1).strip()
    else:
        from collections import Counter
        # 排除"某某分公司/办事处"这类分支机构与明显的模板词
        cands = [m.group(0).strip() for m in _re.finditer(_COMPANY_RE, text)]
        cands = [c for c in cands
                 if not _re.search(r"(分公司|办事处|子公司|支行|管理合伙企业（有限合伙）)$", c)
                 and c not in ("有限公司", "股份有限公司")]
        if cands:
            cnt = Counter(cands)
            top, n = cnt.most_common(1)[0]
            if n >= 2:
                name = top
            else:
                # 只出现一次时，优先取最长的（更可能是完整全称）
                name = max(cands, key=len)

    # 外包标记：识别「重要外包：是 / 跨境外包：否 / 非驻场：是」模式
    def flag(pattern):
        mm = _re.search(pattern, text)
        return _to_bool(mm.group(1)) if mm else False, bool(mm)

    important, imp_hit = flag(r"(?:是否)?重要外包[：:]\s*(是|否|Y|N)")
    cross, cross_hit = flag(r"(?:是否)?跨境外包[：:]\s*(是|否|Y|N)")
    non_res, nonres_hit = flag(r"(?:是否)?非驻场[：:]\s*(是|否|Y|N)")

    # ---- 履约指标快照（第25条效能质量指标）----
    # 尽调 / 履约报告常自带这些数据。此前完全没抽取，
    # 导致"文件里明明有合同履约率，履约表现维度却显示未录入"。
    def pct_find(*names):
        for nm in names:
            mm = _re.search(re.escape(nm) + r"[：:\s\t|｜]*([\d,，.]+)\s*[%％]", text)
            if mm:
                v = _to_float(mm.group(1))
                if v is not None:
                    return v
        return None

    _RATE_LABELS = (
        ("合同履约率", ("合同履约率",)),
        ("订单准时交付率", ("订单准时交付率", "准时交付率")),
        ("一次验收合格率", ("产品一次验收合格率", "一次验收合格率")),
        ("SLA达成率", ("SLA 达成率", "SLA达成率", "服务承诺达成率")),
        ("客户满意度", ("客户满意度", "满意度")),
    )
    rate_items = {}
    for label, names in _RATE_LABELS:
        v = pct_find(*names)
        if v is not None and 0 <= v <= 100:
            rate_items[label] = v

    # 履约综合评价的"评价维度 | 得分"表（拍平后形状："产品质量  96"）
    _PERF_DIMS = ("产品质量", "交付能力", "价格竞争力", "售后服务", "响应速度",
                  "合规诚信", "服务能力", "技术水平", "项目管理", "履约能力")
    perf_items = {}
    for ln in text.splitlines():
        s = ln.strip()
        for d in _PERF_DIMS:
            mm = _re.match(re.escape(d) + r"[\s\t|｜]+(\d{1,3}(?:\.\d+)?)\s*$", s)
            if mm:
                v = _to_float(mm.group(1))
                if v is not None and 0 <= v <= 100:
                    perf_items[d] = v

    # 综合得分：优先"履约评价维度"均值（文件本身就是按百分制打的分），
    # 否则退回各比率指标的均值。
    if len(perf_items) >= 3:
        perf_score = round(sum(perf_items.values()) / len(perf_items), 1)
    elif rate_items:
        perf_score = round(sum(rate_items.values()) / len(rate_items), 1)
    else:
        perf_score = None

    merged_perf_items = dict(rate_items)
    merged_perf_items.update(perf_items)
    perf_snapshot = ({"score": perf_score, "items": merged_perf_items}
                     if perf_score is not None else None)

    return SupplierProfile(
        name=name,
        important_outsourcing=important,
        cross_border=cross,
        non_resident=non_res,
        legal_events=legal,
        news=news,
        financials=fin,
        registration=registration,
        shareholders=shareholders,
        conc_flags_provided=(imp_hit or cross_hit or nonres_hit),
        perf_snapshot=perf_snapshot,
    )


def _find_audit_opinion(text: str) -> Optional[str]:
    """识别审计意见类型。

    ⚠️ 顺序必须"先长后短"：'标准无保留意见' 含子串 '保留意见'，
    若先判 '保留意见'，任何标准意见都会被误判为保留意见并触发红线熔断。
    """
    for kw in ("标准无保留意见", "无保留意见", "带强调事项段的无保留意见",
               "无法表示意见", "否定意见", "保留意见"):
        if kw in text:
            # 归一化：带强调事项段/标准 统一成标准口径，避免下游误判
            if kw in ("标准无保留意见", "无保留意见"):
                return "标准无保留意见"
            return kw
    return None


def _clean_int_text(v) -> Optional[str]:
    """把"1,234 人"/"1234人" 等文本清成纯数字串，供评分引擎 int() 解析。

    解析不出数字时返回原样（评分引擎会自行容错返回"无数据"）。
    """
    if v is None:
        return None
    s = str(v).strip()
    import re as _re
    m = _re.search(r"\d[\d,，]*", s)
    if not m:
        return s
    return m.group(0).replace(",", "").replace("，", "")


def _first_nonempty(rows, col_idx):
    if col_idx is None:
        return None
    for r in rows:
        if col_idx < len(r) and r[col_idx] not in (None, ""):
            return r[col_idx]
    return None


def _unit_scale(text) -> float:
    """从「1.2亿元 / 500万元 / 800千元」里读出数量级。默认 1（按元计）。"""
    s = str(text or "")
    # 外币不做汇率假设：宁可留空（字段缺失 → 该维度按 na 处理），也不猜测金额
    if any(cur in s for cur in ("美元", "港元", "欧元", "日元", "英镑")):
        return 0.0
    if "亿" in s:
        return 1e8
    if "百万元" in s:
        return 1e6
    if "万" in s:
        return 1e4
    if "千元" in s or "千" in s:
        return 1e3
    return 1.0


def _to_money(v):
    """金额字段专用：把带中文单位的金额换算成「元」。

    `_to_float` 出于"不悄悄改数量级"的考虑只剥单位不换算，但 FinancialMetrics
    的金额字段约定为元、报告也按元展示，因此金额字段必须在这里统一换算。
    外币（美元/港元…）没有汇率依据，返回 None 而不是按人民币误记。
    """
    if v is None:
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)                      # 纯数值：按元理解
    scale = _unit_scale(v)
    if scale == 0.0:
        return None
    base = _to_float(v)
    return None if base is None else base * scale


def _to_float(v):
    """把真实报表里五花八门的数值写法转成 float，转不了返回 None。

    覆盖：百分号 / 千分位 / 全角与 Unicode 负号 / 会计括号负数 /
    "万元·亿元·万元人民币"等单位 / "倍""个百分点"等后缀 / "—""N/A"等占位。
    单位只剥不换算（亿元、万元留给上层按口径处理），避免数量级被悄悄改掉。
    """
    if v is None:
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)

    s = str(v).strip()
    if not s:
        return None

    # 占位符：无数据
    if s in ("-", "--", "—", "－", "/", "N/A", "n/a", "NA", "无", "不适用", "None", "null"):
        return None

    # 会计括号写法：(12.5) 表示 -12.5 ；全角括号同样处理
    neg = False
    if (s.startswith("(") and s.endswith(")")) or (s.startswith("（") and s.endswith("）")):
        neg = True
        s = s[1:-1].strip()

    # 负号归一：ASCII '-'、全角 '－'(U+FF0D)、Unicode '−'(U+2212)、'–'(U+2013)
    s = s.replace("－", "-").replace("−", "-").replace("–", "-").replace("—", "-")
    # 千分位与全角逗号
    s = s.replace(",", "").replace("，", "")
    # 全角句点、百分号
    s = s.replace("．", ".").replace("。", ".")
    s = s.replace("%", "").replace("％", "")

    # 单位/后缀：只剥离，不改数量级
    for unit in ("个百分点", "人民币", "万元", "亿元", "千元", "百万元",
                 "亿美元", "万", "亿", "元", "倍", "次", "人"):
        s = s.replace(unit, "")

    m = re.search(r"[-+]?\d*\.?\d+", s)
    if not m:
        return None
    try:
        num = float(m.group(0))
    except ValueError:
        return None
    return -num if neg else num



def _to_bool(v):
    if v is None:
        return False
    return str(v).strip() in ("是", "true", "True", "1", "Y", "y", "重要")


# ---------------------------------------------------------------------------
# 多文件合并（同一家供应商的多个文件 → 一份完整档案）
# ---------------------------------------------------------------------------

_DEFAULT_NAMES = ("涉密供应商（本地解析）", "未命名供应商", "")


def profile_diagnostics(p: SupplierProfile) -> dict:
    """给出"哪些字段解析到了"的细粒度诊断，用于排查"有数据却显示未录入"。

    返回示例：
      {"financial_fields": ["debt_ratio", "net_margin"], "registration_fields": [...],
       "legal_events": 2, "news": 1, "shareholders": 3, "conc_flags": True}
    """
    fin_fields = []
    if p.financials:
        for f in fields(FinancialMetrics):
            if getattr(p.financials, f.name, None) is not None:
                fin_fields.append(f.name)
    return {
        "financial_fields": fin_fields,
        "registration_fields": sorted((p.registration or {}).keys()),
        "shareholders": len(p.shareholders or []),
        "legal_events": len(p.legal_events or []),
        "news": len(p.news or []),
        "conc_flags": bool(p.conc_flags_provided),
        "perf_score": (p.perf_snapshot or {}).get("score"),
        "perf_items": sorted((p.perf_snapshot or {}).get("items", {}).keys()),
    }


def profile_contributions(p: SupplierProfile) -> List[str]:
    """计算单个文件贡献了哪些维度数据（供前端展示"文件 A 提供了财务+司法事件"）。

    返回值是**人类可读的中文标签**，非维度 key。空列表表示该文件几乎无有效内容。
    """
    tags: List[str] = []
    if p.financials and any(getattr(p.financials, f.name) is not None
                            for f in fields(FinancialMetrics)):
        tags.append("财务指标")
    if p.legal_events:
        tags.append("司法事件")
    if p.news:
        tags.append("舆情资讯")
    if p.registration:
        tags.append("工商信息")
    if p.shareholders:
        tags.append("股东结构")
    if p.market:
        tags.append("行情估值")
    if p.perf_snapshot and p.perf_snapshot.get("score") is not None:
        tags.append("履约指标")
    if p.conc_flags_provided:
        tags.append("外包标记")
    return tags


def _bad_shareholder_name(nm: str) -> bool:
    """过滤明显不是股东名称的"标签词/指标词"，避免把表头当成股东。"""
    if not nm:
        return True
    s = nm.strip()
    if len(s) < 2 or len(s) > 40:
        return True
    # 纯数字 / 纯比例不算名称
    if re.fullmatch(r"[\d,，.\-%％\s]+", s):
        return True
    BAD = {
        "名称", "类型", "比例", "出资额", "认缴出资额", "持股比例", "股东",
        "股东名称", "股东类型", "序号", "合计", "总计", "备注", "单位",
        "注册资本", "资产负债率", "销售净利率", "加权净资产收益率", "流动比率",
        "速动比率", "毛利率", "净利率", "营业收入", "净利润", "总资产",
        "经营活动产生的现金流量净额", "审计意见", "成立时间", "参保人数",
        "法定代表人", "统一社会信用代码", "所属行业", "经营范围", "注册地址",
        "企业状态", "是否重要外包", "是否跨境外包", "是否非驻场",
    }
    if s in BAD:
        return True
    # ⚠️ 角色/身份词：这些是"股东类型/备注"列的内容，不是股东名称。
    #    真实模拟数据里"实际控制人""财务投资人""产业引导基金"出现在备注列，
    #    旧实现把它们当成了股东，并因此漏掉真正的最大股东。
    ROLE = {
        "实际控制人", "控股股东", "控股股东/实际控制人", "财务投资人", "战略投资人",
        "联合创始人", "创始人", "自然人股东", "法人股东", "国有法人股东",
        "社会公众股", "机构投资者", "员工持股平台", "产业引导基金", "私募基金",
        "合伙企业", "其他", "其他股东", "公众股东", "管理层", "高管",
    }
    if s in ROLE:
        return True
    # 以"率/额/比/数/意见/时间/名称/类型"等指标词收尾，判定为指标标签而非主体名
    if re.search(r"(率|额|比例|人数|意见|时间|日期|地址|行业|范围|状态|代码|资本|类型|名称|备注)$", s):
        return True
    # 以"股东/投资人/出资人"等标签词收尾的，是表头而非主体
    if re.search(r"(股东|投资人|出资人|合伙人名称)$", s):
        return True
    return False


# 公司/主体名的形态（用于判断"哪个串才是供应商名"）
_COMPANY_RE = (r"[\u4e00-\u9fa5A-Za-z0-9（）()·]{2,30}"
               r"(?:股份有限公司|有限公司|集团|合伙企业|研究院|研究所|事务所|中心)")

# 前置否定词
_NEG_PREV = r"(?:无|未|没有|不存在|非|不|否|并无|均无|从未|无任何|未发现|无相关)"
# 后置值型否定（表格两列写法）：关键词后面跟"记录/情况…｜无"
_NEG_NEXT = (r"^[\s\t|｜:：,，]*"
             r"(?:记录|信息|情况|事项|情形|数据|行为|事件|名单|处罚|案件){0,2}"
             r"[\s\t|｜:：,，]*"
             r"(?:无|未|没有|不存在|否|均无|无记录|未发生|未发现|0\s*次|0)")


def _kw_hit(sent: str, kw: str) -> bool:
    """判断关键词 kw 在 sent 中是否构成"真命中"（考虑否定语境）。

    ⚠️ 这是 2026-09-15 修复的核心：自建模拟样本里明确写着
    "（4 起民事诉讼，**无**被执行、**无**失信记录）"，
    旧实现只要看到"被执行"三个字就判命中 → 把一家 AA+ 优质供应商
    误判为"被列为被执行人"并触发红线熔断。**否定语境必须识别。**

    规则：kw 的任意一处出现未被否定即算命中；全部被否定则不算。
    """
    for m in re.finditer(re.escape(kw), sent):
        s, e = m.start(), m.end()

        # ① 前置否定：从"最近的否定词"到关键词之间**没有标点/空格分隔**才成立。
        #    这样 "无被执行"、"无股权冻结" 判否定，
        #    而 "无对外担保，存在股权冻结" 仍能正确判命中。
        prev = sent[max(0, s - 14):s]
        neg = None
        for nm in re.finditer(_NEG_PREV, prev):
            neg = nm
        if neg and not re.search(r"[、，,。；;：:（(|\s]", prev[neg.end():]):
            continue

        # ② 后置值型否定（表格两列写法）："经营异常记录｜无"
        if re.match(_NEG_NEXT, sent[e:e + 16]):
            continue

        return True
    return False



# 表格表头特征词：命中 ≥3 个视为表头行，不当事件
_HEADER_TOKENS = (
    "年度", "年份", "项目", "内容", "指标", "数值", "备注", "类型", "日期",
    "名称", "序号", "合计", "比例", "金额", "状态", "说明", "部门", "占比",
)


def _looks_like_header(sent: str) -> bool:
    """识别表格表头行（如"年度 一次验收合格率 退换货率 质量投诉(次) 备注"）。

    这类行因含"投诉/违约"等词常被误判成舆情，但它只是列名，不是事实。
    """
    if sum(1 for t in _HEADER_TOKENS if t in sent) >= 2:
        return True
    # 章节标题式："1.6 违约 / 异常记录"、"2.1 工商登记基本信息"
    if re.match(r"^\s*\d+(?:\.\d+)*\s*[、.．\s]", sent):
        return True
    return False


# 裁判文书案号：(2024)苏0591民初5566号 / （2025）粤01民终2233号
_CASE_NO_RE = re.compile(
    r"[（(]\s*\d{4}\s*[）)][^\s，,。；;]{0,14}?"
    r"(?:民初|民终|民再|民申|民特|刑初|刑终|行初|行终|执异|执恢|财保|知民初|商初|执)"
    r"[^\s，,。；;]{0,10}号")

# 案号里的"案件性质"标记；"执*"开头表示这是执行案件（＝被执行身份的直接证据）
_CASE_TYPE_RE = re.compile(r"(执异|执恢|民初|民终|民再|民申|民特|刑初|刑终|"
                           r"行初|行终|财保|知民初|商初|执)")

# 章节标题（形如 "6.1 对外投资 / 关联企业"）
_SECTION_RE = re.compile(r"(?m)^[ \t]*(\d+(?:\.\d+)+)[ \t]*([^\n]{0,40})")

# 这些章节里的百分比**不是供应商的股东持股**，而是别的关系，必须排除：
#   - 对外投资/关联企业：是"供应商持有别人的股份"，方向相反
#   - 客户/供应商/地域/行业集中度：是收入或采购占比
#     （⚠️ 自建模拟样本「供应商_舆情与关联集中度.pdf」6.1 节的 100%/70%/35%/25%
#       曾被当成股东，导致关联集中度维度被污染）
_EXCLUDED_SECTION = (r"(对外投资|关联企业|被投资|参股公司|分支机构"
                     r"|客户集中度|供应商集中度|地域集中度|行业集中度"
                     r"|第一大客户|第一大供应商)")


def _in_excluded_section(text: str, pos: int) -> bool:
    """判断 pos 位置是否落在"不应抽取股东"的章节内。"""
    title = ""
    for m in _SECTION_RE.finditer(text):
        if m.start() > pos:
            break
        title = m.group(1) + " " + m.group(2)
    return bool(re.search(_EXCLUDED_SECTION, title))




def _dedup_by_key(items, key):
    """按 key 去重，保留首次出现的顺序。"""
    seen, out = set(), []
    for it in items:
        k = key(it)
        if k and k not in seen:
            seen.add(k)
            out.append(it)
    return out


def merge_profiles(profiles: List[SupplierProfile]) -> SupplierProfile:
    """把同一家供应商的多份文件档案合并成一份完整档案。

    合并规则（与多源聚合同思路，但这里粒度是"文件"）：
    - name：取第一个非默认占位名（默认名="涉密供应商（本地解析）"等）
    - 财务指标：字段级合并，每个字段取第一个非 None 的值（跨文件补全）
    - 司法事件 / 舆情：列表合并 + 按 title 去重
    - 外包标记（重要/跨境/非驻场）：任一文件命中即视为 True
    - 股东 / 行情 / 工商 / AI 分析：取第一个非空
    """
    if not profiles:
        raise ValueError("没有可合并的文件内容")
    if len(profiles) == 1:
        return profiles[0]

    # 供应商名：取**跨文件出现次数最多**的非占位名。
    # ⚠️ 不能简单取"第一个非占位名"：真实模拟数据里，
    #    「舆情与关联集中度.pdf」通篇在讲关联方（客户/供应商），
    #    靠词频会得出"苏州恒智软件技术有限公司"这个**关联方**，
    #    而正解是另外两份文件里的"苏州恒智电子科技有限公司"。
    #    跨文件投票能纠正单文件的偏差。
    from collections import Counter as _Counter
    name = profiles[0].name
    _names = [p.name for p in profiles if p.name and p.name not in _DEFAULT_NAMES]
    if _names:
        _cnt = _Counter(_names)
        _best_n = max(_cnt.values())
        # 并列时取"先出现的"，保持确定性
        name = next(n for n in _names if _cnt[n] == _best_n)

    important = any(p.important_outsourcing for p in profiles)
    non_resident = any(p.non_resident for p in profiles)
    cross_border = any(p.cross_border for p in profiles)
    conc_provided = any(p.conc_flags_provided for p in profiles)

    # 财务：字段级补全（第一个非 None 优先）
    fin = FinancialMetrics()
    for p in profiles:
        if not p.financials:
            continue
        for f in fields(FinancialMetrics):
            if getattr(fin, f.name) is None and getattr(p.financials, f.name) is not None:
                setattr(fin, f.name, getattr(p.financials, f.name))
    financials = fin if any(getattr(fin, f.name) is not None
                            for f in fields(FinancialMetrics)) else None

    legal = _dedup_by_key(
        [e for p in profiles for e in p.legal_events], lambda e: e.title)
    news = _dedup_by_key(
        [n for p in profiles for n in p.news], lambda n: n.title)

    market = next((p.market for p in profiles if p.market), None)
    registration = next((p.registration for p in profiles if p.registration), None)
    ai_analysis = next((p.ai_analysis for p in profiles if p.ai_analysis), None)

    shareholders = _dedup_by_key(
        [s for p in profiles for s in p.shareholders], lambda s: s.name)

    risk_alerts: List[dict] = []
    for p in profiles:
        risk_alerts.extend(p.risk_alerts)

    sources: List[str] = []
    source_meta: dict = {}
    for p in profiles:
        for k in p.sources:
            if k not in sources:
                sources.append(k)
        source_meta.update(p.source_meta or {})

    # 履约快照：取第一个非空的；若多份文件都有，指标做字段级合并（先到先得）
    perf_snapshot = None
    _perf_items: dict = {}
    for p in profiles:
        snap = getattr(p, "perf_snapshot", None)
        if not snap:
            continue
        for k, v in (snap.get("items") or {}).items():
            _perf_items.setdefault(k, v)
        if perf_snapshot is None:
            perf_snapshot = dict(snap)
    if perf_snapshot is not None and _perf_items:
        perf_snapshot["items"] = _perf_items
        # 综合分：优先用各文件评分指标的平均值（多文件可能各带一部分指标）
        _scores = [p.perf_snapshot["score"] for p in profiles
                   if getattr(p, "perf_snapshot", None)
                   and p.perf_snapshot.get("score") is not None]
        if _scores:
            perf_snapshot["score"] = round(sum(_scores) / len(_scores), 1)

    return SupplierProfile(
        name=name,
        important_outsourcing=important,
        non_resident=non_resident,
        cross_border=cross_border,
        legal_events=legal,
        news=news,
        financials=financials,
        market=market,
        shareholders=shareholders,
        ai_analysis=ai_analysis,
        registration=registration,
        sources=sources,
        source_meta=source_meta,
        risk_alerts=risk_alerts,
        conc_flags_provided=conc_provided,
        perf_snapshot=perf_snapshot,
    )
