# -*- coding: utf-8 -*-
"""收尾修正：
 ① 统一所有正文节边距为 2.8cm（1588 twips），与 v0.1 一致
 ② 补全图 5-1 图题（原为旧文本）
"""
import io, sys, os, re
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
from docx import Document
from docx.shared import Pt, RGBColor, Cm
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

DOC = r'D:\金融科技专项赛\初赛提交包\02_技术方案书.docx'

d = Document(DOC)
# ① 统一边距
for i, s in enumerate(d.sections):
    if i == 0:
        continue
    s.top_margin = Cm(3.0)
    s.bottom_margin = Cm(2.8)
    s.left_margin = Cm(2.8)
    s.right_margin = Cm(2.8)
    s.header_distance = Cm(1.5)
    s.footer_distance = Cm(1.5)
print('边距统一 OK', flush=True)

# ② 补全图题
FIX = {
    '图 5-1\u3000六维评分总览': '图 5-1\u3000六维评分总览与综合安全度',
    '图 7-1\u3000涉密文件上传入口': '图 7-1\u3000涉密文件上传入口与密级路由',
    '图 7-2\u3000多文件合并与解析明细': '图 7-2\u3000多文件合并与解析明细',
    '图 6-1\u3000组合风险看板': '图 6-1\u3000组合风险看板（9 个子模块总览）',
}
n = 0
for p in d.paragraphs:
    t = p.text.strip()
    if t in FIX:
        for r in p.runs:
            if r.text.strip():
                r.text = FIX[t]
                break
        n += 1
print('图题补全 %d 处' % n, flush=True)

d.save(DOC)
print('SAVED', os.path.getsize(DOC), flush=True)
