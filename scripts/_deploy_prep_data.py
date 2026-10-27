"""部署前置：备份线上关键数据 + 迁移 alert_states 键结构。

为什么单独做这一步（而不是交给 deploy_to_server.py）：
  deploy_to_server.py 刻意不覆盖 data/（保护台账、工单、凭据），但线上
  alert_states.json 仍是审计修复前的旧键结构（conc|line|xxx|high、contract|Sxxx），
  而本地已迁移为 conc|line|xxx、contract|Sxxx|unregistered|missing。
  值完全一致，只是键名不同 —— 需要一次显式迁移，并保留可回滚的备份。

用法：SSH_PW='...' python _deploy_prep_data.py [--apply]
"""
import json
import os
import sys
import time

import paramiko

sys.stdout.reconfigure(encoding="utf-8")
HOST = os.environ.get("SSH_HOST", "118.89.33.239")
USER = os.environ.get("SSH_USER", "ubuntu")
PW = os.environ.get("SSH_PW", "")
APP = "/opt/fenghuo"


def main():
    apply = "--apply" in sys.argv
    if not PW:
        print("缺少 SSH_PW")
        return 2
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, username=USER, password=PW, timeout=20,
                allow_agent=False, look_for_keys=False)
    print("已连接 %s@%s  (%s)\n" % (USER, HOST, "执行" if apply else "演练"))

    # ---- 1. 备份整个 data/ ----
    ts = time.strftime("%Y%m%d_%H%M%S")
    if apply:
        cmd = ("sudo cp -r %s/data %s/data.bak_deploy_%s && "
               "sudo du -sh %s/data.bak_deploy_%s" % (APP, APP, ts, APP, ts))
        _, out, err = ssh.exec_command(cmd, timeout=180)
        print("== 1. 备份 ==")
        print(" ", out.read().decode("utf-8", "replace").strip())
        e = err.read().decode("utf-8", "replace").strip()
        if e:
            print("  [stderr]", e[:300])
    else:
        print("== 1. 备份（演练，跳过）==")

    # ---- 2. 读取线上与本地 alert_states ----
    sftp = ssh.open_sftp()
    with sftp.open(APP + "/data/alert_states.json") as f:
        remote = json.loads(f.read().decode("utf-8"))
    sftp.close()
    local = json.load(open("deploy/data/alert_states.json", encoding="utf-8"))

    print("\n== 2. alert_states 键迁移 ==")
    print("  线上 %d 条 / 本地（目标）%d 条" % (len(remote), len(local)))
    only_r = sorted(set(remote) - set(local))
    only_l = sorted(set(local) - set(remote))

    # 逐条核对：线上每个待迁移键，都能在本地找到"值相同、仅键名不同"的对应项，
    # 否则说明不只是键名变化 —— 此时中止，人工确认后再动。
    mismatched = []
    for k in only_r:
        cands = [lk for lk in only_l if local[lk] == remote[k]]
        if not cands:
            mismatched.append(k)
    print("  待迁移键 %d 个；能在本地找到同值对应项的 %d 个"
          % (len(only_r), len(only_r) - len(mismatched)))
    if mismatched:
        print("  ★ 以下键在本地找不到同值对应项，**已中止**：")
        for k in mismatched[:10]:
            print("     ", k, "->", json.dumps(remote[k], ensure_ascii=False)[:110])
        ssh.close()
        return 3

    if not apply:
        print("\n  [演练] 将用本地版本覆盖线上 alert_states.json")
        print("  样例：")
        for k in only_r[:5]:
            dst = [lk for lk in only_l if local[lk] == remote[k]][0]
            print("     %-42s → %s" % (k, dst))
        print("\n  加 --apply 执行。")
        ssh.close()
        return 0

    # ---- 3. 覆盖（只这一个文件）----
    # ⚠️ 不能直接 SFTP 写 /opt/fenghuo/data/（属 root:root，ubuntu 无权写）→ 先落 /tmp 再 sudo cp。
    data = json.dumps(local, ensure_ascii=False, indent=2).encode("utf-8")
    sftp = ssh.open_sftp()
    with sftp.open("/tmp/_alert_states_new.json", "wb") as f:
        f.write(data)
    sftp.close()
    cmd = ("sudo cp /tmp/_alert_states_new.json %s/data/alert_states.json && "
           "rm -f /tmp/_alert_states_new.json && "
           "sudo chown root:root %s/data/alert_states.json && "
           "python3 -c \"import json;d=json.load(open('%s/data/alert_states.json'));"
           "print('  写入后条数 =', len(d));"
           "bad=[k for k in d if k.startswith('contract|S') and len(k.split('|'))==2];"
           "print('  残留旧式 contract 键 =', bad)\""
           % (APP, APP, APP))
    _, out, err = ssh.exec_command(cmd, timeout=120)
    print("\n== 3. 覆盖 ==")
    print(out.read().decode("utf-8", "replace").strip())
    e = err.read().decode("utf-8", "replace").strip()
    if e:
        print("  [stderr]", e[:300])

    # ---- 4. 重启并抽查 ----
    _, out, _ = ssh.exec_command(
        "sudo systemctl restart fenghuo && sleep 4 && systemctl is-active fenghuo",
        timeout=120)
    print("\n== 4. 重启 fenghuo ==")
    print(" ", out.read().decode("utf-8", "replace").strip())
    ssh.close()
    print("\n完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())