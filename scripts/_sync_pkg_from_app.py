# -*- coding: utf-8 -*-
"""把 `app/` 的代码同步进下游副本（「正式提交包」/「deploy/」），使它们不再落后于开发树。

规则（保守，只做「同名文件内容更新」，不新增文件）：
  · 只处理 **两棵树都有** 的文件；
  · 内容相同 → 跳过；
  · 内容不同 → 用 app/ 覆盖目标；
  · 绝不触碰：`.env`（目标里是脱敏占位符）、`1.pptx`、`烽火方案书.docx`、`data/`。

用法：python _sync_pkg_from_app.py                     演练（两个目标）
      python _sync_pkg_from_app.py --apply             执行
      python _sync_pkg_from_app.py --only pkg --apply  只同步提交包
      python _sync_pkg_from_app.py --only deploy --apply
"""
import hashlib
import os
import shutil
import sys

DEV = r"D:\金融科技专项赛\app"
TARGETS = {
    "pkg": r"C:\Users\lenovo\Desktop\金融科技专项赛\正式提交包",
    "deploy": r"D:\金融科技专项赛\deploy",
}
SKIP_DIRS = {"data", "__pycache__", "node_modules", ".git", "_archive", "_shots", "venv"}
SKIP_FILES = {".env", "1.pptx", "烽火方案书.docx", "package-lock.json"}


def md5(p):
    return hashlib.md5(open(p, "rb").read()).hexdigest()


def sync_one(label, target, apply):
    changed, same, new_files = [], 0, []
    for dp, dns, fns in os.walk(DEV):
        dns[:] = [d for d in dns if d not in SKIP_DIRS]
        rel = os.path.relpath(dp, DEV)
        for fn in fns:
            if fn in SKIP_FILES or fn.endswith(".pyc") or fn.startswith("~$"):
                continue
            src = os.path.join(dp, fn)
            r = (os.path.normpath(os.path.join(rel, fn)) if rel != "." else fn)
            dst = os.path.join(target, r)
            if not os.path.exists(dst):
                new_files.append(r)
                continue
            if md5(src) == md5(dst):
                same += 1
                continue
            changed.append(r)
            if apply:
                os.makedirs(os.path.dirname(dst) or target, exist_ok=True)
                shutil.copy2(src, dst)
    print("=" * 74)
    print("【%s】一致 %d ｜ 内容不同 %d ｜ app/ 独有（不动）%d"
          % (label, same, len(changed), len(new_files)))
    for r in sorted(changed):
        print("   %s  %s → %s" % ("覆盖" if apply else "将覆盖", r,
                                  md5(os.path.join(target, r))[:8] if os.path.exists(os.path.join(target, r)) else "—"))
    return len(changed)


def main():
    apply = "--apply" in sys.argv
    only = None
    if "--only" in sys.argv:
        only = sys.argv[sys.argv.index("--only") + 1]
    total = 0
    for k, v in TARGETS.items():
        if only and k != only:
            continue
        total += sync_one(k, v, apply)
    if not apply:
        print("\n[演练] 未写入。加 --apply 执行。")


if __name__ == "__main__":
    main()
