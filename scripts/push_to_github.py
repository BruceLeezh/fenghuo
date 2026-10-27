"""把 deploy/ 目录同步到 GitHub 私有仓库（走 REST API，绕开本机损坏的 git SSL）。

背景：本机 git 客户端的 schannel / openssl 后端都验证不了 GitHub 证书，
但 Python 可正常访问，故用 GitHub Contents API 直传。

用法：
    python scripts/push_to_github.py

Token 读取顺序：
    1. 环境变量 GITHUB_TOKEN
    2. 项目根目录下的 .github_token 文件（D:/金融科技专项赛/.github_token）

特性：按 git blob sha 比对，**只上传有变更的文件**（未变的跳过）。
"""
import urllib.request
import urllib.parse
import urllib.error
import json
import base64
import hashlib
import os

OWNER, REPO, BRANCH = "BruceLeezh", "-", "main"

SRC = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
DEPLOY = os.path.abspath(os.path.join(SRC, "..", "deploy"))
TOKEN_FILE = os.path.abspath(os.path.join(SRC, "..", ".github_token"))


def read_token():
    t = (os.environ.get("GITHUB_TOKEN") or "").strip()
    if t:
        return t
    if os.path.exists(TOKEN_FILE):
        return open(TOKEN_FILE, encoding="utf-8").read().strip()
    raise SystemExit("未找到 GitHub Token：请设置环境变量 GITHUB_TOKEN，或创建 " + TOKEN_FILE)


def blob_sha(data: bytes) -> str:
    """计算 git blob sha（与 git 内部算法一致），用于判断文件是否变更。"""
    h = hashlib.sha1()
    h.update(b"blob %d\0" % len(data))
    h.update(data)
    return h.hexdigest()


def collect_files():
    files = []
    for root, dirs, fs in os.walk(DEPLOY):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__")]
        for f in fs:
            if f.endswith((".pyc", ".pyo")):
                continue
            p = os.path.join(root, f)
            files.append((os.path.relpath(p, DEPLOY).replace("\\", "/"), p))
    files.sort()
    return files


def main():
    token = read_token()

    def api(method, path, data=None):
        req = urllib.request.Request(
            "https://api.github.com" + path,
            data=json.dumps(data).encode() if data is not None else None,
            headers={"Authorization": "token " + token, "User-Agent": "fenghuo-sync",
                     "Accept": "application/vnd.github+json", "Content-Type": "application/json"},
            method=method)
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())

    files = collect_files()
    print(f"deploy/ 共 {len(files)} 个文件，比对远端 ...")

    added, updated, skipped, failed = 0, 0, 0, []
    for rel, p in files:
        data = open(p, "rb").read()
        local_sha = blob_sha(data)
        qrel = urllib.parse.quote(rel)

        remote_sha = None
        try:
            ex = api("GET", f"/repos/{OWNER}/{REPO}/contents/{qrel}?ref={BRANCH}")
            remote_sha = ex.get("sha")
        except urllib.error.HTTPError as e:
            if e.code != 404:
                failed.append((rel, f"GET {e.code}"))
                continue

        if remote_sha == local_sha:
            skipped += 1
            continue

        body = {"message": f"update {rel}",
                "content": base64.b64encode(data).decode(),
                "branch": BRANCH}
        if remote_sha:
            body["sha"] = remote_sha
        try:
            api("PUT", f"/repos/{OWNER}/{REPO}/contents/{qrel}", body)
            if remote_sha:
                updated += 1
                print(f"  ~ 更新 {rel}")
            else:
                added += 1
                print(f"  + 新增 {rel}")
        except urllib.error.HTTPError as e:
            msg = e.read().decode("utf-8", "replace")[:100]
            failed.append((rel, f"{e.code} {msg}"))
            print(f"  ✗ {rel}: {e.code} {msg}")

    print()
    print(f"完成：新增 {added} / 更新 {updated} / 未变 {skipped} / 失败 {len(failed)}")
    print(f"仓库：https://github.com/{OWNER}/{REPO}")
    if failed:
        print("失败清单：")
        for rel, msg in failed:
            print(f"  {rel}: {msg}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
