"""模拟数据验收 —— 用自建的 6 份模拟数据校验解析器。

⚠️ **本脚本不复制、不落盘任何模拟数据**。
   《大赛方案》第三条(三)要求"模拟数据及脱敏数据仅限竞赛用途，不得外传"，
   因此这里只在**本地路径**上读取，路径不存在时优雅跳过（CI/他人机器不会失败），
   仓库里也不会出现这些文件。

用法：
    python scripts/verify_official_samples.py                 # 默认路径
    python scripts/verify_official_samples.py --dir <路径>    # 指定目录

期望结果（2026-09-15 修复后）：
    A 组 苏州恒智电子科技有限公司：六维全覆盖、无 na、无红线、green
    B 组 深圳市云枢信息技术有限公司：仅"履约表现"na（该组无履约文件）、无红线、green
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
if APP not in sys.path:
    sys.path.insert(0, APP)

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

DEFAULT_DIR = r"D:\金融科技专项赛\01_交付包\8-李卓昊\模拟数据"

# 6 份文件实为 2 家供应商 × 各 3 份，文件名直接对应六个维度
GROUPS = {
    "A 苏州恒智电子": [
        "供应商_履约与法律数据.docx",
        "供应商_经营与财务数据.xlsx",
        "供应商_舆情与关联集中度.pdf",
    ],
    "B 深圳市云枢信息": [
        "法律数据_关联与集中度.docx",
        "经营与财务数据.xlsx",
        "舆情数据.pdf",
    ],
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=DEFAULT_DIR, help="模拟数据所在目录")
    args = ap.parse_args()

    if not os.path.isdir(args.dir):
        print("⚠️ 未找到模拟数据目录：%s" % args.dir)
        print("   本脚本不会复制这些数据（保密要求），请用 --dir 指定本地路径。")
        return 0

    from security.file_parser import (parse_supplier_file, merge_profiles,
                                      profile_contributions, profile_diagnostics)
    from core.rating import rate_supplier

    ok = True
    for label, files in GROUPS.items():
        paths = [os.path.join(args.dir, f) for f in files]
        missing = [p for p in paths if not os.path.exists(p)]
        if missing:
            print("⚠️ %s：缺少文件 %s，跳过" % (label, [os.path.basename(m) for m in missing]))
            continue

        print("=" * 70)
        print("### %s" % label)
        profs = []
        for p, f in zip(paths, files):
            prof = parse_supplier_file(p)
            profs.append(prof)
            print("   %-34s %s" % (f, "、".join(profile_contributions(prof)) or "（无）"))

        m = merge_profiles(profs)
        d = profile_diagnostics(m)
        r = rate_supplier(m, strict_na=True)

        print("   ➜ 识别主体：%s" % m.name)
        print("   ➜ 股东 %d 位：%s" % (
            d["shareholders"],
            "、".join("%s %s%%" % (s.name, s.ratio if s.ratio is not None else "-")
                      for s in m.shareholders)))
        print("   ➜ 财务 %d 项 | 工商 %d 项 | 司法 %d 条 | 舆情 %d 条 | 履约 %s" % (
            len(d["financial_fields"]), len(d["registration_fields"]),
            d["legal_events"], d["news"], d.get("perf_score")))
        print("   ➜ 综合安全度 %s / %s | 未录入维度 %s | 红线 %s" % (
            r.total_score, r.level, r.na_dimensions or "无", r.red_lines or "无"))

        # 断言：这两份样本里都是无被执行/无失信的正常经营主体，
        # 绝不能被判出红线（这是 2026-09-15 修掉的核心误报）
        if r.red_lines:
            print("   ❌ 误报红线：%s" % r.red_lines)
            ok = False
        if r.level == "red":
            print("   ❌ 等级误判为 red")
            ok = False
        if m.name.startswith("涉密供应商") or m.name == "未命名供应商":
            print("   ❌ 未能识别供应商名称")
            ok = False

    print("=" * 70)
    print("结果：%s" % ("✅ 模拟数据验收通过" if ok else "❌ 存在问题"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
