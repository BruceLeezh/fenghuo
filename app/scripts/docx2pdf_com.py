# -*- coding: utf-8 -*-
import win32com.client as wc
import os

src = r"D:\金融科技专项赛\初赛提交包\02_技术方案书.docx"
dst = r"D:\金融科技专项赛\_shots\techprop.pdf"
app = wc.DispatchEx('Word.Application')
app.Visible = False
try:
    doc = app.Documents.Open(src, ReadOnly=True)
    doc.SaveAs(dst, FileFormat=17)
    pages = doc.ComputeStatistics(2)
    doc.Close(False)
    print('PAGES', pages)
finally:
    app.Quit()
print('PDF', os.path.getsize(dst))
