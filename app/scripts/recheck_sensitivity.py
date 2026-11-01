# -*- coding: utf-8 -*-
"""权重敏感度复核（随包脚本，离线自包含，无需起服务）。

回答评委/评委之外任何人都该问的一个问题：
「AHP 的权重是专家拍出来的，如果拍偏了 20%，结论会不会翻？」
做法：对六个维度各做 ±10% / ±20% 与 ×0.5 / ×2.0 共 36 组扰动，
对全部在管供应商逐一重算综合分与等级，统计等级翻转次数。

口径与 core/rating.py 完全一致：阈值分档 → 短板约束 → 信号升级 → 维度不足封顶。

用法：
    python scripts/recheck_sensitivity.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import roster as roster_mod          # noqa: E402
from core.rating import rate_supplier         # noqa: E402

ORDER = {"red": 0, "yellow": 1, "green": 2}
LEVEL_THRESHOLDS = [("red", 60), ("yellow", 80), ("green", 100)]
FLOOR = ((35.0, "red"), (50.0, "yellow"))
FORECAST_ALERT = 70.0


def level_of(total, dims, perf):
    """按 rating.py 的同一套规则定级（纯函数，便于扰动重算）。"""
    lv = "green"
    for name, th in LEVEL_THRESHOLDS:
        if total < th:
            lv = name
            break
    vals = [v for v in dims.values() if v is not None]
    if vals:
        low = min(vals)
        for th, cap in FLOOR:
            if low < th and ORDER[lv] > ORDER[cap]:
                lv = cap
    p = perf or {}
    sig = (p.get("consecutive_count") or 0) >= 3
    if (p.get("cliff") or {}).get("severity") == "high":
        sig = True
    fc = p.get("forecast") or {}
    if (fc.get("value") is not None and fc["value"] < FORECAST_ALERT
            and fc.get("direction") == "down"):
        up = fc.get("p_upper")
        if up is None or up < FORECAST_ALERT:      # 区间上界也跌破才算数
            sig = True
    if sig and ORDER[lv] > ORDER["yellow"]:
        lv = "yellow"
    if len(vals) < 3 and ORDER[lv] > ORDER["yellow"]:
        lv = "yellow"
    return lv


def score_with(weights, dims):
    avail = [d for d, v in dims.items() if v is not None]
    tot = sum(weights.get(d, 0) for d in avail)
    if tot <= 0:
        return None
    return min(sum(dims[d] * weights[d] / tot for d in avail), 100.0)


def main():
    items = roster_mod.load_roster()
    rows = []
    base_weights = None
    red_line_names = set()
    from server import _fetch_profile                     # 复用同一取数口径
    for it in items:
        name = it.get("name")
        if not name:
            continue
        try:
            res = _fetch_profile(name, "public")
        except Exception as exc:                           # noqa: BLE001
            print("跳过（取数失败）", name, type(exc).__name__)
            continue
        rating = rate_supplier(res.profile, supplier_id=it.get("id", ""))
        if rating.total_score is None:
            continue
        if base_weights is None:
            base_weights = dict(rating.raw_weights)
        dims = {k: v.get("score") for k, v in rating.dimension_scores.items()}
        rows.append({
            "name": name,
            "dims": dims,
            "perf": rating.performance,
            "total": rating.total_score,
            "level": rating.level,
        })
        if rating.red_lines:
            red_line_names.add(name)

    if not rows or not base_weights:
        print("没有可评分的供应商，无法复核。")
        return 1

    variants = []
    for d in base_weights:
        for f in (0.8, 0.9, 1.1, 1.2, 0.5, 2.0):
            w = dict(base_weights)
            w[d] = w[d] * f
            variants.append((d, f, w))

    flips = []
    mismatched = []
    for r in rows:
        recomputed = score_with(base_weights, r["dims"])
        if recomputed is None:
            continue
        if abs(recomputed - r["total"]) > 0.02 or level_of(recomputed, r["dims"], r["perf"]) != r["level"]:
            mismatched.append((r["name"], r["total"], round(recomputed, 2), r["level"]))

    for d, f, w in variants:
        for r in rows:
            s = score_with(w, r["dims"])
            if s is None:
                continue
            if level_of(s, r["dims"], r["perf"]) != r["level"]:
                flips.append((r["name"], d, f, round(s, 2), r["level"],
                              level_of(s, r["dims"], r["perf"])))

    print("权重基线：", {k: round(v, 4) for k, v in base_weights.items()})
    print("供应商数：", len(rows), "| 扰动组数：", len(variants),
          "| 重算次数：", len(variants) * len(rows))
    print("与在线口径不一致的供应商：", len(mismatched))
    for m in mismatched[:5]:
        print("   ", m)
    print("等级翻转次数：", len(flips), f"（占 {len(flips) / (len(variants) * len(rows)) * 100:.1f}%）")
    for name, d, f, s, a, b in flips:
        print("    %-16s %-14s ×%-4s  %5.2f 分  %s → %s" % (name[:16], d, f, s, a, b))
    return 0 if not mismatched else 2


if __name__ == "__main__":
    sys.exit(main())
