# -*- coding: utf-8 -*-
"""
超长整页截图 -> 2x2 拼版（一页放完，保留全部内容）

目标：把 02/03/13（各 4 面板）与 14（3 面板）合成为单张"多面板拼图"，
      在版心宽 15.4cm 内一页可容纳，避免一章被 4 张整页图撑成 10 页。
布局：
  - 4 面板 -> 2 列 x 2 行
  - 3 面板 -> 2 列（上 2 + 下 1 居中）
每格之间留白，外框淡灰细线，格内可选角标（a/b/c/d）。
"""
import os
from PIL import Image, ImageDraw

PANELS = r'D:\金融科技专项赛\_shots\panels'
OUT = r'D:\金融科技专项赛\_shots\grid'
os.makedirs(OUT, exist_ok=True)

GAP = 26            # 格间距（像素）
PAD = 18            # 外边距
LINE = (222, 228, 236)
TAG_BG = (27, 58, 107)
TAG_FG = (255, 255, 255)
CELL_W = 1500       # 目标格宽（缩放基准）

JOBS = [
    ('02_单点扫描_六维评分总览', 4, 'grid_02.png'),
    ('03_单点扫描_评分规则与监管条款', 4, 'grid_03.png'),
    ('13_涉密文件分析_上传入口', 4, 'grid_13.png'),
    ('14_涉密文件分析_多文件合并与解析明细', 3, 'grid_14.png'),
    ('05_周期监控_集中度HHI', 3, 'grid_05.png'),
]

LOG = []

for base, n, outname in JOBS:
    parts = []
    for k in range(1, n + 1):
        p = os.path.join(PANELS, f'{base}__p{k}.png')
        if os.path.exists(p):
            parts.append(Image.open(p).convert('RGB'))
    if len(parts) != n:
        LOG.append(f'{base}: 面板缺失 ({len(parts)}/{n})，跳过')
        continue

    # 统一格宽
    scaled = []
    for im in parts:
        w, h = im.size
        nh = int(h * CELL_W / w)
        scaled.append(im.resize((CELL_W, nh), Image.LANCZOS))

    cols = 2
    rows = (n + 1) // 2
    # 每行高 = 该行最高
    row_h = []
    for r in range(rows):
        hh = max(scaled[r * cols + c].size[1] for c in range(cols) if r * cols + c < n)
        row_h.append(hh)

    W = PAD * 2 + CELL_W * cols + GAP * (cols - 1)
    H = PAD * 2 + sum(row_h) + GAP * (rows - 1)
    canvas = Image.new('RGB', (W, H), (255, 255, 255))
    dr = ImageDraw.Draw(canvas)

    y = PAD
    for r in range(rows):
        x = PAD
        for c in range(cols):
            idx = r * cols + c
            if idx >= n:
                break
            im = scaled[idx]
            # 若该格比行高矮，垂直居中
            oy = y + (row_h[r] - im.size[1]) // 2
            canvas.paste(im, (x, oy))
            # 淡灰细框
            dr.rectangle([x - 1, oy - 1, x + im.size[0], oy + im.size[1]], outline=LINE, width=2)
            # 角标 a/b/c/d
            tag = chr(ord('a') + idx)
            tw, th = 30, 26
            dr.rectangle([x + 6, oy + 6, x + 6 + tw, oy + 6 + th], fill=TAG_BG)
            dr.text((x + 6 + 8, oy + 6 + 5), tag, fill=TAG_FG)
            x += CELL_W + GAP
        y += row_h[r] + GAP

    op = os.path.join(OUT, outname)
    canvas.save(op)
    ratio = canvas.size[1] / canvas.size[0]
    LOG.append(f'{base}: {n}面板 -> {outname}  {canvas.size[0]}x{canvas.size[1]}  r={ratio:.2f}  h@15.4cm={15.4*ratio:.1f}cm')

open(r'D:\金融科技专项赛\_shots\_grid.txt', 'w', encoding='utf-8').write('\n'.join(LOG))
print('\n'.join(LOG))
