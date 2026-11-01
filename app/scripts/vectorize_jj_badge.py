# -*- coding: utf-8 -*-
"""
把九江银行行徽从官方位图重建为**平滑矢量**（SVG）。

思路：
  1. 从官方 PNG 提红色掩膜
  2. 8x 超采样
  3. 提取「外轮廓 + 内孔轮廓」的边界点（marching squares / 边界跟踪）
  4. Douglas-Peucker 简化 → 得到少量关键点
  5. 用平滑曲线（Catmull-Rom → 三次贝塞尔）连接，得到印刷级顺滑边缘

产出：_logo_probe/jj_badge.svg（纯矢量）
"""
import os
import math
import numpy as np
from PIL import Image

SRC = r'D:\金融科技专项赛\_logo_probe\jj_2024092622173348030.png'
OUT_SVG = r'D:\金融科技专项赛\_logo_probe\jj_badge.svg'
RED = '#DC0817'   # 官方实测 rgb(220,8,23)

SS = 8            # 超采样倍率
EPS = 0.55        # DP 简化容差（超采样像素）


def build_mask():
    im = Image.open(SRC).convert('RGBA')
    a = np.asarray(im).astype(int)
    sub = a[60 - 60:60, 0:60]
    sub = a[0:60, 0:60]
    rr, gg, bb, aa = sub[:, :, 0], sub[:, :, 1], sub[:, :, 2], sub[:, :, 3]
    # 取「非白」即墨：红色徽标 vs 白色背景（含抗锯齿灰边）
    ink = (aa > 60) & ~((rr > 215) & (gg > 215) & (bb > 215))
    im.close()
    m = Image.fromarray((ink * 255).astype(np.uint8), 'L')
    big = m.resize((60 * SS, 60 * SS), Image.LANCZOS)
    arr = (np.asarray(big) > 127).astype(np.uint8)
    # 补掉最外一圈（原图裁切边缘）
    arr[0, :] = 0; arr[-1, :] = 0; arr[:, 0] = 0; arr[:, -1] = 0
    return arr


def moore_trace(mask, start, first_dir):
    """Moore 邻域边界跟踪，返回闭合边界点序列（y,x）。"""
    H, W = mask.shape
    # 8 邻域，顺时针
    NB = [(-1, 0), (-1, 1), (0, 1), (1, 1), (1, 0), (1, -1), (0, -1), (-1, -1)]
    contour = []
    cur = start
    bdir = first_dir
    for _ in range(H * W * 4):
        contour.append(cur)
        found = False
        # 从回溯方向的下一个开始，顺时针搜
        for k in range(8):
            d = (bdir + 1 + k) % 8
            dy, dx = NB[d]
            ny, nx = cur[0] + dy, cur[1] + dx
            if 0 <= ny < H and 0 <= nx < W and mask[ny, nx]:
                cur = (ny, nx)
                bdir = (d + 4) % 8  # 回溯方向
                found = True
                break
        if not found:
            break
        if cur == start and len(contour) > 2:
            break
    return contour


def rdp(points, eps):
    """Douglas-Peucker 折线简化。"""
    if len(points) < 3:
        return points
    pts = np.array(points, dtype=float)

    def _simplify(lo, hi, keep):
        if hi <= lo + 1:
            return
        p0, p1 = pts[lo], pts[hi]
        d = p1 - p0
        L = math.hypot(d[0], d[1])
        if L == 0:
            dists = np.hypot(pts[lo + 1:hi, 0] - p0[0], pts[lo + 1:hi, 1] - p0[1])
        else:
            dists = np.abs(d[0] * (p0[1] - pts[lo + 1:hi, 1]) -
                           (p0[0] - pts[lo + 1:hi, 0]) * d[1]) / L
        if len(dists) == 0:
            return
        i = int(np.argmax(dists))
        if dists[i] > eps:
            idx = lo + 1 + i
            keep.add(idx)
            _simplify(lo, idx, keep)
            _simplify(idx, hi, keep)

    keep = {0, len(pts) - 1}
    _simplify(0, len(pts) - 1, keep)
    return [tuple(pts[i]) for i in sorted(keep)]


def smooth_path(pts, scale):
    """Catmull-Rom → 三次贝塞尔，得到顺滑闭合路径。"""
    n = len(pts)
    if n < 3:
        return ''
    def P(i):
        y, x = pts[i % n]
        return (x * scale, y * scale)
    d = ['M%.3f,%.3f' % P(0)]
    for i in range(n):
        p0, p1, p2, p3 = P(i - 1), P(i), P(i + 1), P(i + 2)
        c1 = (p1[0] + (p2[0] - p0[0]) / 6.0, p1[1] + (p2[1] - p0[1]) / 6.0)
        c2 = (p2[0] - (p3[0] - p1[0]) / 6.0, p2[1] - (p3[1] - p1[1]) / 6.0)
        d.append('C%.3f,%.3f %.3f,%.3f %.3f,%.3f' %
                 (c1[0], c1[1], c2[0], c2[1], p2[0], p2[1]))
    d.append('Z')
    return ''.join(d)


def find_contours(mask):
    """找所有边界（外轮廓 + 孔洞）。返回 [(contour, is_outer), ...]"""
    H, W = mask.shape
    labeled = np.zeros((H, W), dtype=np.uint8)
    out = []
    # 外轮廓：从左上第一个前景点开始
    for y in range(H):
        for x in range(W):
            if mask[y, x] and not labeled[y, x]:
                c = moore_trace(mask, (y, x), 0)
                if len(c) > 20:
                    for (cy, cx) in c:
                        labeled[cy, cx] = 1
                    out.append((c, True))
    # 孔洞：背景中不与边界连通的区域
    bg = (mask == 0)
    visited = np.zeros((H, W), dtype=bool)
    from collections import deque
    q = deque()
    for x in range(W):
        for y in (0, H - 1):
            if bg[y, x] and not visited[y, x]:
                visited[y, x] = True; q.append((y, x))
    for y in range(H):
        for x in (0, W - 1):
            if bg[y, x] and not visited[y, x]:
                visited[y, x] = True; q.append((y, x))
    while q:
        cy, cx = q.popleft()
        for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            ny, nx = cy + dy, cx + dx
            if 0 <= ny < H and 0 <= nx < W and bg[ny, nx] and not visited[ny, nx]:
                visited[ny, nx] = True; q.append((ny, nx))
    holes = bg & ~visited
    hl = np.zeros((H, W), dtype=bool)
    for y in range(H):
        for x in range(W):
            if holes[y, x] and not hl[y, x]:
                c = moore_trace(holes.astype(np.uint8), (y, x), 0)
                if len(c) > 20:
                    for (cy, cx) in c:
                        hl[cy, cx] = True
                    out.append((c, False))
    return out


def main():
    mask = build_mask()
    cs = find_contours(mask)
    print('contours:', len(cs), [len(c) for c, _ in cs])

    H, W = mask.shape
    scale = 100.0 / H
    paths = []
    for c, is_outer in cs:
        simp = rdp(c, EPS)
        paths.append((simp, is_outer))
        print('  contour len=%d -> simplified=%d  outer=%s' % (len(c), len(simp), is_outer))

    # 外轮廓用 evenodd：把孔洞作为子路径一起放，靠 fill-rule 挖洞
    d_all = ''.join(smooth_path(p, scale) for p, _ in paths)
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" '
        'width="100" height="100">'
        '<path d="%s" fill="%s" fill-rule="evenodd"/></svg>' % (d_all, RED)
    )
    open(OUT_SVG, 'w', encoding='utf-8').write(svg)
    print('wrote', OUT_SVG, len(svg), 'bytes')


if __name__ == '__main__':
    main()
