# -*- coding: utf-8 -*-
import io, sys, os
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
from docx import Document
from docx.shared import Cm

DOC = r'D:\金融科技专项赛\初赛提交包\02_技术方案书.docx'
d = Document(DOC)
print('sections', len(d.sections), 'paras', len(d.paragraphs), 'tables', len(d.tables))
for i, s in enumerate(d.sections):
    def pt(v):
        return 'None' if v is None else '%.1f' % v.pt
    print(' sec%d  L%s R%s T%s B%s' % (i, pt(s.left_margin), pt(s.right_margin),
                                      pt(s.top_margin), pt(s.bottom_margin)))
print()
for i, p in enumerate(d.paragraphs[:8]):
    imgs = p._element.findall('.//' + '{http://schemas.openxmlformats.org/drawingml/2006/main}blip')
    print('%2d [%s] img=%d %r' % (i, p.style.name, len(imgs), p.text[:56]))
print()
print('zipsize', os.path.getsize(DOC))
