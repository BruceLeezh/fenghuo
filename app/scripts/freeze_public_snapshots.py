# -*- coding: utf-8 -*-
"""把实时多源查询结果冻结成 data/public/<供应商>.json 快照。

为什么需要：台账换成 37 家之后，data/public/ 里只有恒生、信雅达两个旧文件，
其余 35 家在 source=public 下会走 na 原则（数据不足→不评级），
而 source=auto 又是实时数据、每天在变，方案书引用的数字无法复现。
冻结一次之后 source=public 对全部台账供应商可复现，方案书可标注采集日期。

用法（在 app 目录下）：
    python scripts/freeze_public_snapshots.py            # 只写缺失的
    python scripts/freeze_public_snapshots.py --force    # 全部重写
    python scripts/freeze_public_snapshots.py --dry      # 只预演不落盘
"""
import datetime
import json
import os
import sys
import time
from dataclasses import asdict

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

PUB_DIR = os.path.join(HERE, "data", "public")


def to_snapshot(profile) -> dict:
    """转成 LocalAdapter._dict_to_profile 能读回的形态。

    LegalEvent / NewsItem / FinancialMetrics 都是 @dataclass，不是 pydantic，用 asdict。
    """
    return {
        "name": profile.name,
        "important_outsourcing": bool(profile.important_outsourcing),
        "non_resident": bool(profile.non_resident),
        "cross_border": bool(profile.cross_border),
        "legal_events": [asdict(e) for e in profile.legal_events],
        "news": [asdict(n) for n in profile.news],
        "financials": asdict(profile.financials) if profile.financials else None,
        "registration": profile.registration or {},
    }


def main():
    force = "--force" in sys.argv
    dry = "--dry" in sys.argv

    from server import _fetch_profile, _has_subject_data
    from core.roster import load_roster

    roster = load_roster()
    stamp = datetime.date.today().isoformat()
    ok, skip, fail = [], [], []
    t0 = time.time()

    for i, it in enumerate(roster, 1):
        name = it["name"]
        path = os.path.join(PUB_DIR, name + ".json")
        if os.path.exists(path) and not force:
            skip.append((name, "已有快照"))
            continue
        try:
            res = _fetch_profile(name, "auto")
            prof = res.profile
        except Exception as e:
            fail.append((name, str(e)[:80]))
            continue
        if not _has_subject_data(prof):
            skip.append((name, "多源均无有效主体数据"))
            continue
        snap = to_snapshot(prof)
        snap["registration"]["_snapshot_at"] = stamp
        snap["registration"]["_snapshot_sources"] = ",".join(res.used)
        if dry:
            ok.append((name, "预演"))
            continue
        os.makedirs(PUB_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False, indent=1)
        ok.append((name, ",".join(res.used)))
        if i % 5 == 0:
            print("  ... %d/%d  %.0fs" % (i, len(roster), time.time() - t0), flush=True)

    print("\n写入 %d 个快照；跳过 %d；失败 %d  （%.0fs）"
          % (len(ok), len(skip), len(fail), time.time() - t0))
    for n, why in skip:
        print("  跳过 %-30s %s" % (n[:30], why))
    for n, why in fail:
        print("  失败 %-30s %s" % (n[:30], why))
    if ok:
        print("\n快照文件：")
        for n, src in ok:
            print("  %-30s ← %s" % (n[:30], src))


if __name__ == "__main__":
    main()
