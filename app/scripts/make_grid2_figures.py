# -*- coding: utf-8 -*-
"""
超长整页截图 -> 双栏拼版（列优先，符合阅读顺序，一页放完）

与 2x2 网格不同：本版把面板按**列优先**装进左右两栏
（左栏=第 1..ceil(n/2) 段，右栏=其余），两栏各自纵向堆叠，
高度差用弹性缩放抹平，整体一页容纳。阅读顺序 = 左栏从上到下 -> 右栏从上到下，
与截图原始自上而下的浏览顺序一致。
"""
import os
from PIL import Image, ImageDraw

PANELS = r'D:\金融科技专项赛\_shots\panels'
OUT = r'D:\金融科技专项赛\_shots\grid2'
os.makedirs(OUT, exist_ok=True)

GAP = 24
PAD = 16
LINE = (222, 228, 236)
TAG_BG = (27, 58, 107)
TAG_FG = (255, 255, 255)
COL_W = 1480                    # 单栏宽（2 栏时的缩放基准）
MAX_H_CM = 17.6                 # 15.4cm 宽下的最大允许图高（+图注仍在单页版心 23.9cm 内）

# 只声明「源图 basename -> 输出名」，段数从 *_shots\panels\ 下实际存在的
# `<base>__pN.png` **自动探测**，避免切分脚本调整段数后此处硬编码失配
# （曾因写死 05=3 段而切分只给 2 段导致 FileNotFoundError）。
JOBS = [
    ('02_单点扫描_六维评分总览', 'g2_02.png'),
    ('14_涉密文件分析_多文件合并与解析明细', 'g2_14.png'),
    ('05_周期监控_集中度HHI', 'g2_05.png'),
    ('09_周期监控_合规日历', 'g2_09.png'),
]

LOG = []


def detect_n(base):
    """探测 panels 目录下实际存在的面板段数（__p1..__pN 连续）"""
    n = 0
    while os.path.exists(os.path.join(PANELS, f'{base}__p{n + 1}.png')):
        n += 1
    return n


def load_scaled(base, k, width):
    p = os.path.join(PANELS, f'{base}__p{k}.png')
    im = Image.open(p).convert('RGB')
    w, h = im.size
    return im.resize((width, max(1, int(h * width / w))), Image.LANCZOS)


def plan(base, n, col_w, cols=2):
    """按给定栏宽/栏数试排，返回布局"""
    per = (n + cols - 1) // cols
    groups = [list(range(i * per + 1, min(n, (i + 1) * per) + 1)) for i in range(cols)]
    groups = [g for g in groups if g]
    cols = len(groups)
    ims_groups = [[load_scaled(base, k, col_w) for k in g] for g in groups]
    hs = [sum(i.size[1] for i in g) + GAP * (len(g) - 1) for g in ims_groups]
    H = PAD * 2 + max(hs)
    W = PAD * 2 + col_w * cols + GAP * (cols - 1)
    return cols, col_w, W, H, ims_groups


def plan3(base, n, col_w):
    return plan(base, n, col_w, cols=3)


for base, outname in JOBS:
    n = detect_n(base)
    if n < 2:
        LOG.append(f'{base}: 跳过（实际面板数 {n} < 2，无需拼版）')
        continue

    # 2 栏基准试排。成品高（在 15.4cm 版心宽下）= 15.4 * H/W。
    # 若超预算，用「3 栏」降低总高（栏数 ↑ → 每栏面板更少）。但 3 栏会让每栏更窄，
    # 因此仅在确实超预算且面板数 ≥5 时才启用；否则接受略高（图注仍能同页）。
    cols, col_w, W, H, ims_groups = plan(base, n, COL_W)
    h_cm = 15.4 * (H / W)
    if h_cm > MAX_H_CM and n >= 5:
        cols, col_w, W, H, ims_groups = plan3(base, n, COL_W)
        h_cm = 15.4 * (H / W)

    canvas = Image.new('RGB', (W, H), (255, 255, 255))
    dr = ImageDraw.Draw(canvas)

    tag_i = 0
    for gi, group in enumerate(ims_groups):
        x0 = PAD + gi * (col_w + GAP)
        y = PAD
        for im in group:
            canvas.paste(im, (x0, y))
            dr.rectangle([x0 - 1, y - 1, x0 + im.size[0], y + im.size[1]], outline=LINE, width=2)
            tag = chr(ord('a') + tag_i)
            tag_i += 1
            tw, th = 30, 26
            dr.rectangle([x0 + 6, y + 6, x0 + 6 + tw, y + 6 + th], fill=TAG_BG)
            dr.text((x0 + 6 + 8, y + 6 + 5), tag, fill=TAG_FG)
            y += im.size[1] + GAP

    op = os.path.join(OUT, outname)
    canvas.save(op)
    hr = canvas.size[1] / canvas.size[0]
    LOG.append(f'{base}: {n}面板({cols}栏, 每栏{col_w}px) -> {outname}  '
               f'{canvas.size[0]}x{canvas.size[1]}  r={hr:.2f}  h@15.4cm={15.4*hr:.1f}cm')

open(r'D:\金融科技专项赛\_shots\_grid2.txt', 'w', encoding='utf-8').write('\n'.join(LOG))
print('\n'.join(LOG))
