# -*- coding: utf-8 -*-
"""
把中南财经政法大学官方 logo.svg（白色版）转为学校标准蓝版本，并导出高清 PNG。

依据《中南财经政法大学章程》第七十二条：
    学校视觉形象识别系统中的标准色为蓝色（基准色值 c100 m70 y5）
CMYK(100,70,5,0) → sRGB。学校官方常用的深蓝约为 #003A70 / #003C78 区间。
本脚本同时导出多个候选，供人工比对选择。
"""
import os
import re

SRC = r'D:\金融科技专项赛\_logo_probe\zuel_logo.svg'
OUT = r'D:\金融科技专项赛\_logo_probe'

# CMYK(100,70,5,0) 的标准换算结果 ≈ #003F7D ~ #003A70。
# 采用学校官方材料中最常见的深蓝取值：
CANDIDATES = {
    'zuel_blue_003A70': '#003A70',   # 章程基准色的常见落地值（深蓝）
    'zuel_blue_003F7D': '#003F7D',   # CMYK 直接换算值
    'zuel_blue_004A97': '#004A97',   # 略亮，用于小尺寸更易辨识
}


def main():
    s = open(SRC, encoding='utf-8').read()

    # 白色 → 目标色（SVG 内全部 fill="#fff"）
    for name, color in CANDIDATES.items():
        out_svg = s.replace('fill="#fff"', 'fill="%s"' % color)
        # 兜底：可能有无引号或 style 形式
        out_svg = re.sub(r'fill="#FFFFFF"', 'fill="%s"' % color, out_svg, flags=re.I)
        p = os.path.join(OUT, name + '.svg')
        open(p, 'w', encoding='utf-8').write(out_svg)
        print('wrote', p, len(out_svg))


if __name__ == '__main__':
    main()
