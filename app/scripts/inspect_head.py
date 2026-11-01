# -*- coding: utf-8 -*-
"""收尾：
 ① 处理 P2 空白页（标题+落款独占页）→ 合并为单页紧凑封面副页
 ② 确认标题层级样式可见
"""
import io, sys, os, re
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
from docx import Document
from docx.shared import Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

DOC = r'D:\金融科技专项赛\初赛提交包\02_技术方案书.docx'
SERIF = 'Noto Serif CJK SC'
d = Document(DOC)

print('--- 前 12 段 ---')
for i, p in enumerate(d.paragraphs[:12]):
    print('%2d [%-12s] %r' % (i, p.style.name, p.text[:56]))

# 找 P2（标题）与 P3（落款）——把它们并入目录页
# 目标：标题 + 落款 → 与目录同页（删掉它们与目录之间的分节符影响）
# 实现：把标题段与落款段移到「目录」段之前，且不加分页
paras = d.paragraphs
toc_i = None
for i, p in enumerate(paras):
    if p.text.strip() == '目录':
        toc_i = i; break
print('\n目录段 idx =', toc_i, flush=True)

# 检查这几段的 sectPr
for i in range(0, min(6, len(paras))):
    pPr = paras[i]._element.find(qn('w:pPr'))
    has = pPr is not None and pPr.find(qn('w:sectPr')) is not None
    print('  p%d sectPr=%s  %r' % (i, has, paras[i].text[:40]))

d.save(DOC)
