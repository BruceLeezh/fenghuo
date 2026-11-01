# -*- coding: utf-8 -*-
"""
九江银行官方标准标（行徽 + 中英文行名）—— 混合重构。

【策略】
官方站全站只有 230x60 位图（已核实：`2024092622173348030.png` 与页脚
`2024092622174316527.png` 逐像素完全相同，无更高清版本）。

- **行徽**（x 0..58）：位图里抗锯齿发虚 → 用 `make_jj_badge.py` 的几何
  精确重建替换（纯矢量，边缘绝对干净，色彩取官方正红 #DC0817）。
- **中英文行名**（x 70..230）：官方原创字体（行书风格"九江银行"+ 定制
  BANK OF JIUJIANG），无法用系统字体复刻 → 保留官方位图，仅做 4x
  LANCZOS 上采样 + 轻微 USM 锐化，恢复被 JPEG/PNG 压缩糊掉的笔画边缘。

【官方设计语义】（来源：公开企业标识说明）
  "行徽图形似内方外圆…似双'J'（九和江第一个拼音字母）首末相依，循环转动。
   行名为深蓝色的中、英文字体，稳重厚实。以蓝、红为主色。"
"""
import os
from PIL import Image, ImageFilter

OUT_DIR = r'D:\金融科技专项赛\_logo_probe'
SRC = os.path.join(OUT_DIR, 'jj_2024092622173348030.png')
BADGE_SVG = os.path.join(OUT_DIR, 'jj_badge_alpha.svg')

# 官方文字深蓝（实测众数 rgb(7,73,142)）
TEXT_BLUE = (7, 73, 142)

# 官方位图中行徽/文字的分界（实测：行徽 0..58，文字自 70 起）
BADGE_X1 = 59
TEXT_X0 = 70

# 目标输出尺寸：按行徽高度 60px 为基准，整标宽高比 = 230:60
OUT_H = 240
OUT_W = int(round(OUT_H * 230 / 60))


def upscale_wordmark(im, factor=8):
    """对官方文字的 alpha 通道做上采样+锐化，颜色重铺为官方深蓝。"""
    w = im.crop((TEXT_X0, 0, 230, 60))
    w = w.resize((w.width * factor, w.height * factor), Image.LANCZOS)
    a = w.split()[3].filter(ImageFilter.UnsharpMask(radius=2.2, percent=115, threshold=2))
    # 用锐化后的 alpha 重新着官方深蓝，彻底消除压缩色噪
    solid = Image.new('RGBA', w.size, TEXT_BLUE + (255,))
    solid.putalpha(a)
    return solid


def main():
    src = Image.open(SRC).convert('RGBA')
    factor = OUT_H / 60.0

    # ---- 1. 行徽：几何重建的透明底 SVG 已渲染为 PNG ----
    badge_png = os.path.join(OUT_DIR, '_jj_badge_alpha_render.png')
    if not os.path.exists(badge_png):
        raise SystemExit('请先运行 render_svg.js 生成 ' + badge_png)
    badge = Image.open(badge_png).convert('RGBA')
    bw = int(round(BADGE_X1 * factor))
    badge = badge.resize((bw, int(round(bw * badge.height / badge.width))), Image.LANCZOS)

    # ---- 2. 文字：官方位图放大+锐化 ----
    text = upscale_wordmark(src, factor=8)
    tw = int(round((230 - TEXT_X0) * factor))
    text = text.resize((tw, int(round(tw * text.height / text.width))), Image.LANCZOS)

    # ---- 3. 拼合（基线对齐：行徽居中于整标高度）----
    canvas = Image.new('RGBA', (OUT_W, OUT_H), (0, 0, 0, 0))
    canvas.alpha_composite(badge, (0, (OUT_H - badge.height) // 2))
    # 文字块基线：官方位图中文字占 y 8..52 → 按比例放置
    ty = int(round((OUT_H - text.height) / 2))
    canvas.alpha_composite(text, (bw, ty))

    out = os.path.join(OUT_DIR, 'jj_logo_official.png')
    canvas.save(out)
    print('wrote', out, canvas.size)


if __name__ == '__main__':
    main()
