#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Markdown → doc-typeset 版式 HTML（供 tencent-docx 流水线 Stage 2 使用）。

为什么需要它：doc-typeset 提供的是「HTML 模板 + 排版提示词」，需要把 Stage 1 产出的
Markdown 按模板骨架落成 HTML（供 Stage 3 html-to-docx 转 .docx）。
本脚本做纯机械的 Markdown→HTML 结构映射 + 注入 design-token 的 CSS 变量，
不做任何内容创作或改写。

用法：
  python md_to_typeset_html.py \
    --md   <stage1/final_draft.md> \
    --template base|business-report \
    --tokens <design-token/tokens/compiled/XXX.json> \
    --out  <stage2/formatted-主题.html> \
    --title "文档标题" [--category 分类] [--subtitle 副标题] [--meta "单位 | 日期"]
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass


# ---------------------------------------------------------------- inline
def inline(text: str) -> str:
    """行内标记：转义 → 粗体 → 行内代码 → 链接。"""
    t = html.escape(text, quote=False)
    t = re.sub(r"`([^`]+)`", lambda m: "<code>%s</code>" % m.group(1), t)
    t = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", t)
    t = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", t)
    t = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', t)
    return t


def slug(n: int) -> str:
    return "section-%d" % n


# ---------------------------------------------------------------- blocks
def md_to_blocks(md: str):
    """把 Markdown 切成块：(kind, payload)。kind ∈ h1/h2/h3/p/ul/ol/table/quote/hr。"""
    lines = md.replace("\r\n", "\n").split("\n")
    blocks, i, n = [], 0, len(lines)
    while i < n:
        ln = lines[i]
        s = ln.strip()
        if not s:
            i += 1
            continue
        # 分隔线
        if re.fullmatch(r"-{3,}|\*{3,}", s):
            blocks.append(("hr", None)); i += 1; continue
        # 表格
        if s.startswith("|") and i + 1 < n and re.match(r"^\|[\s:\-|]+\|$", lines[i + 1].strip()):
            head = [c.strip() for c in s.strip("|").split("|")]
            rows, j = [], i + 2
            while j < n and lines[j].strip().startswith("|"):
                rows.append([c.strip() for c in lines[j].strip().strip("|").split("|")])
                j += 1
            blocks.append(("table", (head, rows))); i = j; continue
        # 标题
        m = re.match(r"^(#{1,4})\s+(.*)$", s)
        if m:
            blocks.append(("h%d" % len(m.group(1)), m.group(2).strip())); i += 1; continue
        # 引用
        if s.startswith(">"):
            buf = []
            while i < n and lines[i].strip().startswith(">"):
                buf.append(lines[i].strip().lstrip(">").strip()); i += 1
            blocks.append(("quote", " ".join(x for x in buf if x))); continue
        # 有序 / 无序列表
        if re.match(r"^[-*]\s+", s) or re.match(r"^\d+[.)]\s+", s):
            ordered = bool(re.match(r"^\d+[.)]\s+", s))
            items = []
            while i < n:
                t = lines[i].strip()
                mm = re.match(r"^(?:[-*]|\d+[.)])\s+(.*)$", t)
                if mm:
                    items.append(mm.group(1).strip()); i += 1
                elif t and not re.match(r"^([#>|]|[-*]\s|\d+[.)]\s)", t) and items:
                    items[-1] += " " + t; i += 1      # 列表项续行
                else:
                    break
            blocks.append(("ol" if ordered else "ul", items)); continue
        # 段落
        buf = []
        while i < n and lines[i].strip() and not re.match(r"^(#{1,4}\s|[-*]\s|\d+[.)]\s|>|\||-{3,})", lines[i].strip()):
            buf.append(lines[i].strip()); i += 1
        blocks.append(("p", " ".join(buf)))
    return blocks


# ---------------------------------------------------------------- render
def render_blocks(blocks, skip_first_h1=True):
    """渲染为正文 HTML；返回 (html, toc_items, first_h1_text)。"""
    out, toc, first_h1, sec = [], [], None, 0
    skipped = not skip_first_h1
    for kind, payload in blocks:
        if kind == "h1":
            if not skipped:
                first_h1 = payload; skipped = True; continue
            sec += 1
            out.append('<h1 id="%s">%s</h1>' % (slug(sec), inline(payload)))
        elif kind == "h2":
            sec += 1
            out.append('<h2 id="%s">%s</h2>' % (slug(sec), inline(payload)))
            toc.append('<li><a href="#%s">%s</a></li>' % (slug(sec), html.escape(payload)))
        elif kind == "h3":
            out.append("<h3>%s</h3>" % inline(payload))
        elif kind == "h4":
            out.append("<h4>%s</h4>" % inline(payload))
        elif kind == "p":
            out.append("<p>%s</p>" % inline(payload))
        elif kind == "quote":
            out.append("<blockquote><p>%s</p></blockquote>" % inline(payload))
        elif kind == "hr":
            out.append("<hr/>")
        elif kind == "ul":
            out.append("<ul>" + "".join("<li>%s</li>" % inline(x) for x in payload) + "</ul>")
        elif kind == "ol":
            out.append("<ol>" + "".join("<li>%s</li>" % inline(x) for x in payload) + "</ol>")
        elif kind == "table":
            head, rows = payload
            th = "".join("<th>%s</th>" % inline(c) for c in head)
            tr = "".join(
                "<tr>" + "".join("<td>%s</td>" % inline(c) for c in r[:len(head)]) + "</tr>"
                for r in rows)
            out.append("<table><thead><tr>%s</tr></thead><tbody>%s</tbody></table>" % (th, tr))
    return "\n".join(out), toc, first_h1


TEMPLATE_DIR = os.path.dirname(os.path.abspath(__file__))


def load_tokens(path):
    d = json.load(open(path, encoding="utf-8"))
    return d.get("css_variables") or {}, d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--md", required=True)
    ap.add_argument("--template", default="base", choices=["base", "business-report"])
    ap.add_argument("--tokens", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--title", required=True)
    ap.add_argument("--category", default="")
    ap.add_argument("--subtitle", default="")
    ap.add_argument("--meta", default="")
    ap.add_argument("--summary", default="", help="执行摘要（纯文本，business-report 模板用）")
    args = ap.parse_args()

    md = open(args.md, encoding="utf-8").read()
    blocks = md_to_blocks(md)
    body, toc, first_h1 = render_blocks(blocks)
    title = args.title or first_h1 or "文档"

    cssvars, tok = load_tokens(args.tokens)
    root_vars = "\n".join("    %s: %s;" % (k, v) for k, v in cssvars.items())

    # ⚠️ 不在此处注入额外 CSS：html-review 的 DT-02/DT-03 规则禁止 `<style>` 里出现
    #    裸字号/裸间距（必须走 var(--fs-*)/var(--spacing-*)）。模板已自带全部样式，
    #    且只用 CSS 变量表达，所以这里只注入 design-token 变量本身。
    if args.template == "business-report":
        tpl = open(os.path.join(TEMPLATE_DIR, "_tpl_business_report.html"), encoding="utf-8").read()
        htmlout = (tpl
                   .replace("{{CSS_VARS}}", root_vars)
                   .replace("{{report_title}}", html.escape(title))
                   .replace("{{report_category}}", html.escape(args.category))
                   .replace("{{report_subtitle}}", html.escape(args.subtitle))
                   .replace("{{cover_meta}}", html.escape(args.meta))
                   .replace("{{toc_items}}", "\n".join(toc))
                   .replace("{{executive_summary}}",
                            "".join("<p>%s</p>" % inline(x) for x in args.summary.split("\n") if x.strip()))
                   .replace("{{report_content}}", body))
    else:
        tpl = open(os.path.join(TEMPLATE_DIR, "_tpl_base.html"), encoding="utf-8").read()
        header = ('<h1 class="doc-title">%s</h1>%s' % (
            html.escape(title),
            ('<p class="doc-meta">%s</p>' % html.escape(args.meta)) if args.meta else ""))
        htmlout = (tpl
                   .replace("{{CSS_VARS}}", root_vars)
                   .replace("{{document_title}}", html.escape(title))
                   .replace("{{header_content}}", header)
                   .replace("{{main_content}}", body))

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(htmlout)
    print(json.dumps({
        "success": True, "out": os.path.abspath(args.out),
        "bytes": os.path.getsize(args.out), "h2_count": len(toc),
        "template": args.template, "theme": tok.get("theme_name"),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
