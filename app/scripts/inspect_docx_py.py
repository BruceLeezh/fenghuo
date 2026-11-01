# -*- coding: utf-8 -*-
import io, sys, os
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
from docx import Document

SRC = r'D:\金融科技专项赛\初赛提交包\02_技术方案书.docx'
d = Document(SRC)
print('paras', len(d.paragraphs), 'tables', len(d.tables), 'sections', len(d.sections))
def pt(v):
    return 'None' if v is None else '%.1f' % v.pt

for i, s in enumerate(d.sections):
    print(' sec%d w=%s h=%s  L%s R%s T%s B%s  hdr%s ftr%s' % (
        i, pt(s.page_width), pt(s.page_height), pt(s.left_margin), pt(s.right_margin),
        pt(s.top_margin), pt(s.bottom_margin), pt(s.header_distance), pt(s.footer_distance)))
print()
print('--- paragraphs 0..30 (style | text) ---')
for i, p in enumerate(d.paragraphs[:31]):
    print('%3d | %-14s | %s' % (i, p.style.name, p.text[:64]))
print()
print('--- style defs ---')
for nm in ['Normal', 'Heading 1', 'Heading 2', 'Heading 3', 'Title']:
    try:
        st = d.styles[nm]
        f = st.font
        print('%-12s size=%s color=%s font=%s bold=%s' % (
            nm, f.size.pt if f.size else None,
            f.color.rgb if f.color and f.color.type is not None else None,
            f.name, f.bold))
    except Exception as e:
        print(nm, 'ERR', e)
