# -*- coding: utf-8 -*-
import io, sys, os
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
from docx import Document
from docx.shared import Cm

DOC = r'D:\金融科技专项赛\初赛提交包\02_技术方案书.docx'
d = Document(DOC)
print('=== 概要 ===')
print('sections %d | paras %d | tables %d | size %.1f MB' % (
    len(d.sections), len(d.paragraphs), len(d.tables), os.path.getsize(DOC) / 1e6))
print()
print('=== 节 ===')
for i, s in enumerate(d.sections):
    def pt(v):
        return 'None' if v is None else '%.1f' % v.pt
    htxt = ''
    try:
        htxt = s.header.paragraphs[0].text[:40] if s.header.paragraphs else ''
    except Exception:
        htxt = '(linked)'
    print(' sec%-2d L%-5s T%-5s hdr=%-42r' % (i, pt(s.left_margin), pt(s.top_margin), htxt))
print()
print('=== 图片统计 ===')
BLIP = '{http://schemas.openxmlformats.org/drawingml/2006/main}blip'
cnt = 0
for i, p in enumerate(d.paragraphs):
    n = len(p._element.findall('.//' + BLIP))
    if n:
        cnt += n
        print('  p%-3d img=%d  next=%r' % (i, n, d.paragraphs[i+1].text[:48] if i+1 < len(d.paragraphs) else ''))
print('  合计图片 %d 张' % cnt)
