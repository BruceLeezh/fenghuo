# -*- coding: utf-8 -*-
"""
九江银行行徽 —— 几何精确重建（纯矢量，第三版 · 按官方图逐行量测定案）。

【量测方法】官方 230x60 PNG 的行徽区（0..60 见方）。
用"非红即白"判据 `not (r>g+40 and r>b+40)` 统计每行白色区段，
规避位图抗锯齿产生的中间色被硬阈值漏判的问题。

【逐行实测数据】（x 区间为白色像素范围）
    y=21 : 中央方块 (20,38)
    y=24 : 方块 (20,40) + 右缺 (42,59)
    y=25 : 方块 (20,39) + 右缺 (44,59)
    y=26 : 方块 (20,38) + 右缺 (47,59)
    y=28 : 左缺 (0, 2) + 方块 (20,38) + 右缺 (51,59)
    y=29 : 左缺 (0, 5) + 方块 (20,38) + 右缺 (53,59)
    y=31 : 左缺 (0, 7) + 方块 (20,38) + 右缺 (56,59)
    y=33 : 左缺 (0,13) + 方块 (20,38)
    y=35 : 左缺 (0,16) + 方块 (19,38)
    y=36 : 方块 (19,38)
    y=38 : 方块 (20,38)

【几何定案】
  中央白方块 : x 19.5 → 39.0 , y 19.5 → 39.5
  左缺口     : 上边近水平 y≈27.0；下边自 (0, 37.6) 向右上斜到 (17.5, 33.4)
  右缺口     : 下边近水平 y≈31.8；上边自 (39.0, 23.4) 向右下斜到 (61, 31.2)
  两缺口上下交错，构成双「J」首末相依的回旋结构。
"""
import os

OUT_DIR = r'D:\金融科技专项赛\_logo_probe'
RED = '#DC0817'
W = 60

# —— 圆形徽底 ——
CX, CY, R = 29.5, 29.0, 29.3

# —— 中央方块（实测 x 19.5→39.0, y 19.5→39.5）——
SQ_X0, SQ_X1 = 19.5, 39.0
SQ_Y0, SQ_Y1 = 19.5, 39.5

# —— 左侧缺口（自圆左缘水平切入方块左缘）——
# 上边 y≈27.0 近水平；下边自 (0, 37.6) 斜向右上收至 (17.5, 33.4)
L_PTS = [
    (-3.0, 26.6),        # 左上（超出圆外，保证切透）
    (SQ_X0, 26.6),       # 右上（贴方块左缘）
    (SQ_X0, 33.4),       # 右下（贴方块左缘，已收窄）
    (-3.0, 37.6),        # 左下
]

# —— 右侧缺口（自方块右缘水平切出圆外）——
# 上边自 (39.0, 23.4) 斜向右下；下边 y≈31.8 近水平
R_PTS = [
    (SQ_X1, 23.4),       # 左上（贴方块右缘）
    (W + 3, 31.2),       # 右上（超出圆外）
    (W + 3, 31.8),       # 右下（超出圆外）
    (SQ_X1, 31.8),       # 左下（贴方块右缘）
]


def poly(pts):
    return 'M' + 'L'.join('%.2f,%.2f' % (x, y) for x, y in pts) + 'Z'


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    fmt = dict(
        W=W, red=RED, cx=CX, cy=CY, r=R,
        lp=poly(L_PTS), rp=poly(R_PTS),
        sx0=SQ_X0, sy0=SQ_Y0, sw=SQ_X1 - SQ_X0, sh=SQ_Y1 - SQ_Y0,
    )

    # ---- 白底版（浅色背景用）----
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {W}" width="{W}" height="{W}">'
        '<circle cx="{cx}" cy="{cy}" r="{r}" fill="{red}"/>'
        '<path d="{lp}" fill="#FFFFFF"/>'
        '<path d="{rp}" fill="#FFFFFF"/>'
        '<rect x="{sx0}" y="{sy0}" width="{sw}" height="{sh}" fill="#FFFFFF"/>'
        '</svg>'
    ).format(**fmt)
    p1 = os.path.join(OUT_DIR, 'jj_badge.svg')
    open(p1, 'w', encoding='utf-8').write(svg)
    print('wrote', p1, len(svg))

    # ---- 透明底版（mask 挖空）----
    svg_a = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {W}" width="{W}" height="{W}">'
        '<defs><mask id="m">'
        '<rect width="{W}" height="{W}" fill="#000"/>'
        '<circle cx="{cx}" cy="{cy}" r="{r}" fill="#fff"/>'
        '<path d="{lp}" fill="#000"/>'
        '<path d="{rp}" fill="#000"/>'
        '<rect x="{sx0}" y="{sy0}" width="{sw}" height="{sh}" fill="#000"/>'
        '</mask></defs>'
        '<rect width="{W}" height="{W}" fill="{red}" mask="url(#m)"/>'
        '</svg>'
    ).format(**fmt)
    p2 = os.path.join(OUT_DIR, 'jj_badge_alpha.svg')
    open(p2, 'w', encoding='utf-8').write(svg_a)
    print('wrote', p2, len(svg_a))


if __name__ == '__main__':
    main()
