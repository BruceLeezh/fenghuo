"""报告生成 —— 把评级结果渲染成可下载的 HTML 报告。

含供应商画像、综合安全度与加权明细、五维风险细则（指标/实测/阈值/条款/提示）、
数据源与交叉验证、红线熔断、处置建议、事件证据链、财务与行情全景。

注意：报告中大量字段来自**抓取内容与用户上传**（供应商名、事件标题、实测值等），
拼接 HTML 前一律经 `_esc()` 转义，避免存储型 XSS。
"""
import html
import re
from typing import Dict


def _esc(v) -> str:
    """转义为 HTML 安全文本。None → 空串，其余先 str 再 escape。"""
    if v is None:
        return ""
    return html.escape(str(v), quote=True)


def _level_cn(level: str) -> str:
    return {"red": "高危", "yellow": "关注", "green": "正常"}.get(level, level)


def _level_color(level: str) -> str:
    return {"red": "#e04b4a", "yellow": "#e9a23b", "green": "#3f9e5a"}.get(level, "#888")


def _score_color(s) -> str:
    try:
        s = float(s)
    except (TypeError, ValueError):
        return "#888"
    if s >= 85:
        return "#3f9e5a"
    if s >= 70:
        return "#7cb342"
    if s >= 60:
        return "#e9a23b"
    return "#e04b4a"


def _money(v) -> str:
    if v is None:
        return "—"
    try:
        n = float(v)
    except (TypeError, ValueError):
        return str(v)
    a = abs(n)
    if a >= 1e8:
        return f"{n/1e8:,.2f} 亿"
    if a >= 1e4:
        return f"{n/1e4:,.2f} 万"
    return f"{n:,.2f}"


def render_report(result: Dict, channel: str = "public") -> str:
    """渲染 HTML 报告。result 为 _to_result 的输出。"""
    r = result
    color = _level_color(r["level"])
    dims = r.get("dimensions", {})
    # 六维全量（此前写死五维，漏掉 performance 履约表现，与评分口径不一致）
    dim_order = ["performance", "legal", "operating", "financial", "reputation", "concentration"]

    # ---------- 加权明细 ----------
    wb_rows = ""
    for k in dim_order:
        d = dims.get(k)
        if not d:
            continue
        w = d.get("weight", 0) or 0
        sc = d.get("score")
        # score 为 None（na 维度，strict_na 通道常见）时不能直接参与乘法，
        # 否则整份报告在 f-string 里抛 TypeError，用户看到 500。
        wb_rows += (
            f"<tr>"
            f"<td style='padding:6px 8px;border-bottom:1px solid #eef1f5'>{_esc(d.get('label'))}</td>"
            f"<td style='padding:6px 8px;border-bottom:1px solid #eef1f5'>{'—' if sc is None else _esc(sc)}</td>"
            f"<td style='padding:6px 8px;border-bottom:1px solid #eef1f5'>{w*100:.2f}%</td>"
            f"<td style='padding:6px 8px;border-bottom:1px solid #eef1f5;font-weight:600'>"
            f"{'—' if sc is None else f'{sc*w:.2f}'}</td>"
            f"</tr>"
        )

    # ---------- 五维评分细则 ----------
    dim_blocks = ""
    for k in dim_order:
        d = dims.get(k)
        if not d:
            continue
        checks = d.get("checks", [])
        rows = ""
        for c in checks:
            is_hit = c.get("hit")
            bg = "#fdf6f6" if is_hit else "#fff"
            deduct = f"<b style='color:#e04b4a'>-{c['deduct']}</b>" if is_hit else "<span style='color:#c3ccd6'>0</span>"
            actual = c.get("actual", "—")
            actual_color = "#e04b4a" if is_hit else "#3f9e5a"
            rows += (
                f"<tr style='background:{bg}'>"
                f"<td style='padding:7px 8px;border-bottom:1px solid #eef1f5'>{'●' if is_hit else '○'} {_esc(c.get('name',''))}</td>"
                f"<td style='padding:7px 8px;border-bottom:1px solid #eef1f5'>{_esc(c.get('metric',''))}</td>"
                f"<td style='padding:7px 8px;border-bottom:1px solid #eef1f5;color:{actual_color};font-weight:600'>{_esc(actual)}</td>"
                f"<td style='padding:7px 8px;border-bottom:1px solid #eef1f5'>{_esc(c.get('threshold',''))}</td>"
                f"<td style='padding:7px 8px;border-bottom:1px solid #eef1f5'>{deduct}</td>"
                f"<td style='padding:7px 8px;border-bottom:1px solid #eef1f5;color:#7a8699;font-size:11.5px'>{_esc(c.get('clause',''))}</td>"
                f"<td style='padding:7px 8px;border-bottom:1px solid #eef1f5;color:#6d7a8a;font-size:11.5px'>{_esc(c.get('hint',''))}</td>"
                f"</tr>"
            )

        verified_badge = ""
        if d.get("verified") is True:
            verified_badge = "<span style='background:#eaf3de;color:#3f7a2c;font-size:10.5px;padding:2px 7px;border-radius:9px;margin-left:6px'>✓ 已交叉验证</span>"
        elif d.get("verified") is False:
            verified_badge = "<span style='background:#fdf4e6;color:#a8761e;font-size:10.5px;padding:2px 7px;border-radius:9px;margin-left:6px'>单源待验证</span>"

        dim_blocks += f"""
        <div style="margin-bottom:16px;border:1px solid #e4e8ef;border-radius:9px;overflow:hidden">
          <div style="padding:12px 14px;background:#fafbfd;border-bottom:1px solid #e4e8ef">
            <span style="font-weight:600">{_esc(d.get('label'))}</span>
            <span style="color:{_score_color(d.get('score'))};font-weight:600;margin-left:10px">{_esc(d.get('score'))} 分</span>
            <span style="color:#7a8699;font-size:12px;margin-left:10px">权重 {(d.get('weight') or 0)*100:.2f}% · 扣 {d.get('deducted', 0)} 分/{len(d.get('rules',[]))} 项</span>
            {verified_badge}
            <div style="color:#5a6878;font-size:12px;margin-top:6px">{_esc(d.get('note'))}</div>
          </div>
          <table style="width:100%;border-collapse:collapse;font-size:12px">
            <thead><tr style="background:#fff">
              <th style="text-align:left;padding:7px 8px;border-bottom:2px solid #e4e8ef;color:#5a6878">监控指标</th>
              <th style="text-align:left;padding:7px 8px;border-bottom:2px solid #e4e8ef;color:#5a6878">口径</th>
              <th style="text-align:left;padding:7px 8px;border-bottom:2px solid #e4e8ef;color:#5a6878">实测表现</th>
              <th style="text-align:left;padding:7px 8px;border-bottom:2px solid #e4e8ef;color:#5a6878">触发阈值</th>
              <th style="text-align:left;padding:7px 8px;border-bottom:2px solid #e4e8ef;color:#5a6878">扣分</th>
              <th style="text-align:left;padding:7px 8px;border-bottom:2px solid #e4e8ef;color:#5a6878">监管条款</th>
              <th style="text-align:left;padding:7px 8px;border-bottom:2px solid #e4e8ef;color:#5a6878">风险提示</th>
            </tr></thead>
            <tbody>{rows}</tbody>
          </table>
        </div>"""

    # ---------- 数据源 ----------
    src_html = ""
    if r.get("sources"):
        _auth_badge = '<span style="background:#eaf3de;color:#3f7a2c;font-size:10px;padding:1px 6px;border-radius:8px">权威源</span>'
        items = "".join(
            f"<div style='border:1px solid #e4e8ef;border-radius:8px;padding:10px 12px;background:#fbfcfe'>"
            f"<b style='font-size:13px'>{_esc(s.get('label'))}</b>"
            f"{_auth_badge if s.get('authoritative') else ''}"
            f"<div style='color:#7a8699;font-size:11.5px;margin-top:3px'>{_esc(s.get('category'))}</div></div>"
            for s in r["sources"]
        )
        v = r.get("verified", {})
        vtxt = " · ".join([
            f"注册信息 {'✓多源一致' if v.get('registration') else '单源'}",
            f"财务数据 {'✓多源一致' if v.get('financials') else '单源'}",
            f"法律事件 {'✓多源一致' if v.get('legal_events') else '单源'}",
            f"行情估值 {'✓多源一致' if v.get('market') else '单源'}",
        ])
        src_html = f"""
        <h2 style="font-size:15px;margin:24px 0 12px;border-left:3px solid #2f6fd0;padding-left:9px">数据源与交叉验证</h2>
        <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:10px">{items}</div>
        <p style="color:#7a8699;font-size:12px;margin-top:8px">交叉验证：{vtxt}</p>"""

    # ---------- 风险提示 ----------
    alerts_html = ""
    if r.get("risk_alerts"):
        rows = ""
        for a in r["risk_alerts"]:
            lv = a.get("level", "low")
            bg = {"high": "#e04b4a", "medium": "#e9a23b", "low": "#7a8699"}.get(lv, "#7a8699")
            txt = {"high": "高", "medium": "中", "low": "低"}.get(lv, "低")
            rows += (
                f"<div style='padding:7px 0;border-bottom:1px dashed rgba(0,0,0,.08)'>"
                f"<span style='background:{bg};color:#fff;font-size:10.5px;padding:1px 6px;border-radius:8px;margin-right:7px'>{txt}</span>"
                f"<b>{_esc(a.get('title'))}</b> <span style='color:#7c5a5a;font-size:12px'>{_esc(a.get('detail'))}</span></div>"
            )
        alerts_html = f"""
        <h2 style="font-size:15px;margin:24px 0 12px;border-left:3px solid #e04b4a;padding-left:9px">风险提示</h2>
        <div style="background:#fdf3f3;border-radius:8px;padding:12px 14px">{rows}</div>"""

    # ---------- 工商 / 财务 / 行情 ----------
    reg = r.get("registration") or {}
    reg_rows = ""
    for k, lab in [("credit_code", "统一社会信用代码"), ("legal_person", "法定代表人"),
                   ("registered_capital", "注册资本"), ("established", "成立日期"),
                   ("status", "登记状态"), ("company_type", "企业类型"),
                   ("industry", "所属行业"), ("region", "注册地区"),
                   ("staff_scale", "人员规模"), ("insured_count", "参保人数"),
                   ("address", "注册地址")]:
        if reg.get(k):
            reg_rows += f"<div style='font-size:12.5px;margin-bottom:5px'><span style='color:#7a8699'>{lab}</span> <b>{_esc(reg[k])}</b></div>"

    f = r.get("financials") or {}
    fin_rows = ""
    for k, lab, unit in [("debt_ratio", "资产负债率", "%"), ("current_ratio", "流动比率", ""),
                         ("quick_ratio", "速动比率", ""), ("net_margin", "销售净利率", "%"),
                         ("gross_margin", "毛利率", "%"), ("roe", "加权ROE", "%"),
                         ("operating_cashflow", "经营现金流", "$"), ("net_profit", "净利润", "$"),
                         ("revenue", "营业收入", "$"), ("revenue_yoy", "营收同比", "%")]:
        if f.get(k) is not None:
            raw = f[k]
            # 财务字段来源不统一：可能是 float，也可能是已经带单位的字符串
            # （如 "12.5%"）。直接套 :,.2f 会在字符串上抛 ValueError → 整份报告 500。
            if unit == "$":
                vv = _money(raw) + " 元"
            else:
                try:
                    vv = f"{float(raw):,.2f}{unit}"
                except (TypeError, ValueError):
                    vv = f"{_esc(raw)}{unit}"
            fin_rows += (f"<div style='border:1px solid #e4e8ef;border-radius:8px;padding:9px 11px;background:#fbfcfe'>"
                         f"<div style='font-size:11.5px;color:#7a8699'>{lab}</div>"
                         f"<div style='font-size:15px;font-weight:600'>{vv}</div></div>")

    m = r.get("market") or {}
    mkt_rows = ""
    if m:
        for lab, key, unit in [("股票代码", "stock_code", ""), ("最新价", "price", " 元"),
                               ("涨跌幅", "change_pct", "%"), ("市盈率PE", "pe", ""),
                               ("市净率PB", "pb", ""), ("总市值", "total_market_cap", "$")]:
            val = m.get(key)
            if val is None:
                continue
            if unit == "$":
                vv = _money(val)
            elif unit == "" and key == "stock_code":
                vv = str(val)
            elif isinstance(val, (int, float)):
                sign = "+" if (key == "change_pct" and val >= 0) else ""
                vv = f"{sign}{val:,.2f}{unit}"
            else:
                vv = f"{val}{unit}"
            mkt_rows += (f"<div style='border:1px solid #e4e8ef;border-radius:8px;padding:9px 11px;background:#fbfcfe'>"
                         f"<div style='font-size:11.5px;color:#7a8699'>{lab}</div>"
                         f"<div style='font-size:15px;font-weight:600'>{vv}</div></div>")

    # ---------- 主要股东（集中度与关联方识别） ----------
    sh_rows = ""
    for i, s in enumerate(r.get("shareholders", []), 1):
        ratio = s.get("ratio")
        ratio_txt = f"{ratio:.2f}%" if isinstance(ratio, (int, float)) else (s.get("ratio") or "—")
        bar_w = min(float(ratio) * 4, 100) if isinstance(ratio, (int, float)) else 0
        sh_rows += (f"<tr>"
                    f"<td style='padding:6px 8px;border-bottom:1px solid #eef1f5'>{i}</td>"
                    f"<td style='padding:6px 8px;border-bottom:1px solid #eef1f5'>{_esc(s.get('name'))}</td>"
                    f"<td style='padding:6px 8px;border-bottom:1px solid #eef1f5'>{ratio_txt}"
                    f"<div style='height:4px;background:#eef1f5;border-radius:2px;margin-top:4px'>"
                    f"<div style='height:4px;width:{bar_w}%;background:#2f6fd0;border-radius:2px'></div></div></td>"
                    f"<td style='padding:6px 8px;border-bottom:1px solid #eef1f5'>{s.get('amount') or '—'}</td>"
                    f"</tr>")
    sh_table = ""
    if sh_rows:
        sh_table = (
            '<h2 style="font-size:15px;margin:24px 0 12px;border-left:3px solid #2f6fd0;padding-left:9px">'
            '主要股东（集中度与关联方识别）</h2>'
            '<table style="width:100%;border-collapse:collapse;font-size:13px">'
            '<thead><tr>'
            '<th style="text-align:left;padding:7px 8px;border-bottom:2px solid #e4e8ef;color:#5a6878">#</th>'
            '<th style="text-align:left;padding:7px 8px;border-bottom:2px solid #e4e8ef;color:#5a6878">股东名称</th>'
            '<th style="text-align:left;padding:7px 8px;border-bottom:2px solid #e4e8ef;color:#5a6878">持股比例</th>'
            '<th style="text-align:left;padding:7px 8px;border-bottom:2px solid #e4e8ef;color:#5a6878">认缴出资额</th>'
            '</tr></thead><tbody>' + sh_rows + '</tbody></table>')

    # ---------- 事件 ----------
    events = ""
    for e in r.get("legal_events", []):
        lv = e.get("severity", "low")
        bg = {"high": "#e04b4a", "medium": "#e9a23b", "low": "#7a8699"}.get(lv, "#7a8699")
        events += (f"<div style='padding:9px 11px;border:1px solid #eef1f5;border-radius:7px;margin-bottom:6px'>"
                   f"<span style='background:{bg};color:#fff;font-size:10.5px;padding:1px 6px;border-radius:8px;margin-right:7px'>"
                   f"{'高' if lv=='high' else '中' if lv=='medium' else '低'}</span>"
                   f"<b>{_esc(e.get('title'))}</b>"
                   f"<div style='color:#7a8699;font-size:11.5px;margin-top:4px'>来源：{_esc(e.get('source'))}"
                   f"{' · ' + _esc(e.get('evidence')) if e.get('evidence') else ''}</div></div>")

    # ---------- 红线 / AI ----------
    redline_html = ""
    if r.get("red_lines"):
        redline_html = (f"<div style='background:#fcebeb;border:1px solid #e04b4a;padding:11px 13px;"
                        f"border-radius:7px;margin-bottom:14px;color:#a32d2d'>"
                        f"<b>⚠ 红线熔断（一票否决）：</b>{_esc('；'.join(map(str, r['red_lines'])))}</div>")

    # ---------- 等级修正（短板约束 / 信号升级 / 维度不足）----------
    # ⚠️ 后端实际有三种约束类型（core/rating.py）：floor / signal / insufficient。
    #    原实现用 `type == 'floor'` 做二元判断，insufficient（维度不足）会落到 else 分支
    #    被写成「信号升级（出现恶化信号，等级不得判为正常）」—— 把"数据不够"说成
    #    "经营恶化"，性质完全不同的两件事，且会出现在对外导出的报告里。
    _CONS_META = {
        "floor": ("短板", "短板约束（最低维度分过低，等级不得偏高）"),
        "signal": ("信号", "信号升级（出现恶化信号，等级不得判为正常）"),
        "insufficient": ("依据不足", "数据不足（可用维度少于 3 个，等级上限为黄）"),
    }
    constraints_html = ""
    if r.get("constraints"):
        def _cons_row(c):
            badge, title = _CONS_META.get(
                c.get("type"), ("修正", "等级修正"))
            return (
                f"<div style='padding:6px 0;border-bottom:1px dashed rgba(0,0,0,.08)'>"
                f"<span style='background:#fdf3e0;color:#b25e09;font-size:10.5px;padding:1px 6px;"
                f"border-radius:8px;margin-right:7px'>{badge}</span>"
                f"<b>{title}</b>"
                f"<div style='color:#7a8699;font-size:11.5px;margin-top:4px'>"
                f"{_esc(c.get('reason',''))}</div></div>")
        rows = "".join(_cons_row(c) for c in r["constraints"])
        constraints_html = (
            "<div style='background:#fdf8ee;border-left:3px solid #e9a23b;padding:12px 14px;"
            "border-radius:7px;margin-bottom:14px'>"
            "<b style='color:#a8761e'>⚠ 等级已按风控规则修正（未被加权稀释）</b>"
            f"<div style='margin-top:6px'>{rows}</div>"
            "<div style='color:#7a8699;font-size:11.5px;margin-top:8px'>"
            "综合安全度采用 AHP 加权平均，其补偿性会稀释单一维度的重大风险。"
            "故额外施加约束：分数保持加权结果不变，仅修正处置等级。</div></div>")

    ai_html = ""
    if r.get("ai_analysis"):
        ai_html = (f"<div style='background:#f7f9fd;border-left:3px solid #2f6fd0;padding:12px 14px;"
                   f"border-radius:7px;margin-bottom:14px;white-space:pre-wrap;font-size:13px;line-height:1.8'>"
                   f"<b>AI 财务健康度分析：</b><br>{_esc(r['ai_analysis'])}</div>")

    html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<title>烽火 · 供应商风险监控报告 - {_esc(r['supplier'])}</title></head>
<body style="font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;background:#f4f6f9;margin:0;padding:24px;color:#1f2733">
<div style="max-width:1000px;margin:0 auto;background:#fff;border-radius:12px;padding:32px;box-shadow:0 2px 12px rgba(20,30,50,.06)">
  <div style="display:flex;align-items:center;gap:10px;margin:0 0 6px">
    <span style="display:inline-grid;place-items:center;width:26px;height:26px;border-radius:8px;background:#2c4677;color:#fff;font-size:13px;font-weight:700">烽</span>
    <h1 style="font-size:20px;margin:0">供应商风险监控报告</h1>
  </div>
  <p style="color:#7a8699;font-size:13px;margin:0 0 24px">
    依据《银行保险机构信息科技外包风险监管办法》· {('公开数据通道（多源聚合）' if channel=='public' else '涉密文件本地分析通道')}
  </p>

  <div style="display:flex;align-items:center;gap:16px;margin-bottom:16px;flex-wrap:wrap">
    <span style="font-size:20px;font-weight:600">{_esc(r['supplier'])}</span>
    <span style="background:{color};color:#fff;font-weight:600;padding:4px 14px;border-radius:16px">{_level_cn(r['level'])}</span>
    <span style="font-size:28px;font-weight:600;color:{color}">{r['total_score']} 分</span>
  </div>

  <div style="background:#fafbfd;border-left:3px solid {'#3f9e5a' if r['level']=='green' else '#e9a23b' if r['level']=='yellow' else '#e04b4a'};
              padding:12px 14px;border-radius:7px;margin-bottom:20px">
    <b>处置建议：</b>{_esc(r['action'])}
  </div>

  {redline_html}
  {constraints_html}
  {alerts_html}
  {ai_html}
  {src_html}

  <h2 style="font-size:15px;margin:24px 0 12px;border-left:3px solid #2f6fd0;padding-left:9px">综合安全度构成（AHP 加权）</h2>
  <table style="width:100%;border-collapse:collapse;font-size:13px">
    <thead><tr>
      <th style="text-align:left;padding:7px 8px;border-bottom:2px solid #e4e8ef;color:#5a6878">风险维度</th>
      <th style="text-align:left;padding:7px 8px;border-bottom:2px solid #e4e8ef;color:#5a6878">维度得分</th>
      <th style="text-align:left;padding:7px 8px;border-bottom:2px solid #e4e8ef;color:#5a6878">AHP 权重</th>
      <th style="text-align:left;padding:7px 8px;border-bottom:2px solid #e4e8ef;color:#5a6878">加权贡献</th>
    </tr></thead>
    <tbody>{wb_rows}</tbody>
  </table>
  <p style="color:#7a8699;font-size:12px;margin-top:8px">
    一致性比率 CR = {r.get('cr','—')}（&lt; 0.1 通过一致性检验）。综合安全度 = Σ(维度得分 × 维度权重)。
  </p>

  <h2 style="font-size:15px;margin:24px 0 12px;border-left:3px solid #2f6fd0;padding-left:9px">五维评分细则</h2>
  {dim_blocks}

  <h2 style="font-size:15px;margin:24px 0 12px;border-left:3px solid #2f6fd0;padding-left:9px">主体与财务全景</h2>
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:20px">
    <div>
      <div style="font-weight:600;font-size:13.5px;margin-bottom:8px">工商与主体信息</div>
      {reg_rows or "<p style='color:#aaa'>暂无数据</p>"}
    </div>
    <div>
      <div style="font-weight:600;font-size:13.5px;margin-bottom:8px">核心财务指标</div>
      <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(120px,1fr));gap:8px">{fin_rows or "<p style='color:#aaa'>暂无数据</p>"}</div>
    </div>
  </div>

  {f'<div style="font-weight:600;font-size:13.5px;margin:16px 0 8px">二级市场行情</div><div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(120px,1fr));gap:8px">{mkt_rows}</div>' if mkt_rows else ''}

  {sh_table}

  <h2 style="font-size:15px;margin:24px 0 12px;border-left:3px solid #2f6fd0;padding-left:9px">风险事件与证据链</h2>
  {events or "<p style='color:#7a8699;font-size:13px'>未发现风险事件</p>"}

  <p style="color:#aab4c0;font-size:12px;margin-top:32px;text-align:center">
    本报告由「烽火 · 信息科技外包风险智能预警系统」自动生成 · 结论仅供参考，不构成授信或合作决策的唯一依据
  </p>
</div>
</body></html>"""
    return html
