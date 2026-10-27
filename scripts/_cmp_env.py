"""对比线上与本地 .env 的键值长度（不打印任何密钥内容）。"""
import os
import sys

import paramiko

sys.stdout.reconfigure(encoding="utf-8")
HOST = os.environ.get("SSH_HOST", "118.89.33.239")
USER = os.environ.get("SSH_USER", "ubuntu")
PW = os.environ.get("SSH_PW", "")
APP = "/opt/fenghuo"


def parse_env(text):
    out = {}
    for ln in text.splitlines():
        ln = ln.strip()
        if ln and not ln.startswith("#") and "=" in ln:
            k, v = ln.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def main():
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, username=USER, password=PW, timeout=20,
                allow_agent=False, look_for_keys=False)
    sftp = ssh.open_sftp()
    with sftp.open(APP + "/.env") as f:
        remote = parse_env(f.read().decode("utf-8", "replace"))
    sftp.close()
    ssh.close()
    local = parse_env(open("app/.env", encoding="utf-8").read())

    keys = ["DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL",
            "QCC_API_TOKEN", "QCC_API_BASE", "IFIND_MCP_KEY", "WIND_API_KEY"]
    print("%-20s %-12s %-12s %s" % ("键", "线上长度", "本地长度", "状态"))
    print("-" * 62)
    for k in keys:
        rv = remote.get(k, "<缺失>")
        lv = local.get(k, "<缺失>")
        rl = "缺失" if rv == "<缺失>" else len(rv)
        ll = "缺失" if lv == "<缺失>" else len(lv)
        if rv == "<缺失>":
            flag = "线上未配置"
        elif not rv:
            flag = "★ 线上为空"
        elif lv != "<缺失>" and rv == lv:
            flag = "一致"
        else:
            flag = "值不同（长度差异）"
        print("%-20s %-12s %-12s %s" % (k, rl, ll, flag))
    print()
    print("线上 .env 全部键:", sorted(remote.keys()))
    return 0


if __name__ == "__main__":
    sys.exit(main())