"""只推本轮审计修复涉及的代码文件到线上（不动 data/*.json、.env、venv、nginx）。

用法：  $env:SSH_PW="xxxx"; python -X utf8 scripts/push_code.py
"""
import os
import sys

import paramiko

sys.stdout.reconfigure(encoding="utf-8")

HOST = os.environ.get("SSH_HOST", "118.89.33.239")
USER = os.environ.get("SSH_USER", "ubuntu")
PW = os.environ.get("SSH_PW", "")
APP = "/opt/fenghuo"
TMP = "/home/ubuntu/_pushcode"

FILES = [
    "server.py",
    "core/jsonstore.py",
    "core/rating.py",
    "core/scoring.py",
    "core/aggregator.py",
    "core/simulator.py",
    "core/concentration.py",
    "core/servicelevel.py",
    "core/workorder.py",
    "core/roster.py",
    "core/contract.py",
    "core/alerts.py",
    "core/datasource_config.py",
    "core/outsourcing_special.py",
    "core/report.py",
    "security/file_parser.py",
    "adapters/generic_adapter.py",
    "adapters/eastmoney_adapter.py",
    "integrations/qcc_mcp.py",
    "static/index.html",
]
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def sh(ssh, cmd, check=True, timeout=300):
    print("  $ " + cmd[:150])
    _, out, err = ssh.exec_command(cmd, timeout=timeout)
    o = out.read().decode("utf-8", "replace").strip()
    e = err.read().decode("utf-8", "replace").strip()
    rc = out.channel.recv_exit_status()
    if o:
        print("    " + o[:900])
    if e:
        print("    [stderr] " + e[:400])
    if check and rc != 0:
        raise RuntimeError("rc=%d %s" % (rc, cmd))
    return o


def main():
    if not PW:
        print("缺少 SSH_PW")
        return 2
    for rel in FILES:
        p = os.path.join(BASE, rel.replace("/", os.sep))
        if not os.path.exists(p):
            print("本地缺文件：" + rel)
            return 2

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, username=USER, password=PW, timeout=25,
                allow_agent=False, look_for_keys=False)
    print("已连接 %s@%s" % (USER, HOST))

    print("\n== 1/4 备份 ==")
    sh(ssh, "sudo tar -czf /opt/fenghuo.codebak.$(date +%%s).tgz -C %s "
            "server.py core security adapters integrations static/index.html" % APP)

    print("\n== 2/4 上传 ==")
    sftp = ssh.open_sftp()
    sh(ssh, "rm -rf %s && mkdir -p %s" % (TMP, TMP))
    for rel in FILES:
        local = os.path.join(BASE, rel.replace("/", os.sep))
        remote = TMP + "/" + rel
        sh(ssh, "mkdir -p " + os.path.dirname(remote))
        sftp.put(local, remote)
        print("    %-34s %8.1f KB" % (rel, os.path.getsize(local) / 1024))
    sftp.close()

    print("\n== 3/4 覆盖到 /opt/fenghuo ==")
    for rel in FILES:
        sh(ssh, "sudo cp %s/%s %s/%s" % (TMP, rel, APP, rel))
    sh(ssh, "sudo chown -R root:root %s && sudo chmod -R a+r %s" % (APP, APP))

    print("\n== 4/4 重启并验证 ==")
    sh(ssh, "sudo systemctl restart fenghuo && sleep 5 && systemctl is-active fenghuo")
    sh(ssh, "sudo systemctl restart fenghuo-classified 2>/dev/null || true; sleep 2; "
            "systemctl is-active fenghuo-classified 2>/dev/null || echo '(classified 未安装，跳过)'")
    sh(ssh, "curl -s http://127.0.0.1:8000/api/alerts | python3 -c \""
            "import json,sys;d=json.load(sys.stdin);print('  预警', d['total'], d['by_state'])\"")
    sh(ssh, "curl -s http://127.0.0.1:8000/api/concentration | python3 -c \""
            "import json,sys;d=json.load(sys.stdin);print('  HHI', d['hhi'], d['hhi_label'], '总额', d['total_amount'])\"")
    sh(ssh, "curl -s http://127.0.0.1:8000/api/servicelevel | python3 -c \""
            "import json,sys;d=json.load(sys.stdin);print('  服务水平 rows=%d has_data=%d avg=%s'%(len(d['rows']),d['has_data'],d['avg_score']))\"")
    sh(ssh, "curl -s -o /dev/null -w '  GET / -> %{http_code}\\n' http://127.0.0.1:8000/")
    sh(ssh, "curl -s -o /dev/null -w '  POST /api/roster(空名) -> %{http_code}\\n' -X POST "
            "-H 'Content-Type: application/json' -d '{}' http://127.0.0.1:8000/api/roster || true")
    sh(ssh, "sudo find %s -name '*.pyc' -delete; sudo find %s -name '__pycache__' -prune -exec rm -rf {} +"
       % (APP, APP))
    ssh.close()
    print("\n完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
