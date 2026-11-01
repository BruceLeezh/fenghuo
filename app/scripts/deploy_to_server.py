"""把最新代码推到腾讯云服务器（**代码级更新**，不动 nginx / venv / 系统依赖）。

为什么不用服务器上的 install.sh：那个脚本会做 apt-get、重建 venv、并重写
nginx 配置（只写 80，靠 certbot 再挂回 443）——慢，而且有覆盖 HTTPS 配置的风险。
日常「只更新代码」用本脚本更快更稳。

流程：备份 /opt/fenghuo → 上传 bundle → 解包 → 覆盖代码目录 → 重启 fenghuo → 校验版本特征与接口。
⚠️ 服务器上的 `.env` **不会被覆盖**（保留线上凭据）。

用法：
    SSH_PW='<服务器密码>' python scripts/deploy_to_server.py

前置：`python scripts/make_server_bundle.py` 先产出 fenghuo_server_bundle.tar.gz。
依赖：paramiko（建议装在隔离 venv 里，不要污染项目环境）。
"""
import os
import sys
import time
import uuid

import paramiko

sys.stdout.reconfigure(encoding="utf-8")

# 目标服务器：可用环境变量覆盖，避免每次换机都要改代码
#   SSH_HOST=118.89.33.239 SSH_USER=ubuntu SSH_PW='...' python scripts/deploy_to_server.py
# 换机历史：106.52.87.237（首台，2026-10-12 到期）→ 118.89.33.239（广州，2026-11-12 到期）
# ⚠️ 登录用户是 ubuntu（不是 root）：/opt/fenghuo 属 root:root，靠**免密 sudo** 操作。
HOST = os.environ.get("SSH_HOST", "118.89.33.239")
USER = os.environ.get("SSH_USER", "ubuntu")
PW = os.environ.get("SSH_PW", "")
LOCAL_BUNDLE = "fenghuo_server_bundle.tar.gz"
REMOTE_HOME = "/home/ubuntu"
REMOTE_TGZ = REMOTE_HOME + "/fenghuo_server_bundle.tar.gz"
REMOTE_TMP = REMOTE_HOME + "/_bundle_new"
APP = "/opt/fenghuo"
# 写接口令牌的落盘位置（与 deploy/ 同级，加 .gitignore 排除）。
# 为什么持久化：令牌若每次部署随机重生成，**已打开的页面会立刻失效**
# （页面上注入的是旧令牌），演示中途部署一次就得让所有人刷新。固化后保持稳定。
TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "..", "..", "_fh_token.txt")


def resolve_token():
    """取写接口令牌：环境变量 > 落盘文件 > 新生成并落盘。"""
    tok = (os.environ.get("FENGHUO_TOKEN") or "").strip()
    if tok:
        return tok
    p = os.path.abspath(TOKEN_FILE)
    if os.path.exists(p):
        try:
            tok = open(p, encoding="utf-8").read().strip()
            if tok:
                return tok
        except Exception:
            pass
    tok = "fh-demo-" + uuid.uuid4().hex[:20]
    try:
        with open(p, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(tok + "\n")
    except Exception as e:
        print("  !! 令牌落盘失败（不影响部署）：%s" % e)
    return tok


def sh(ssh, cmd, timeout=300, check=True):
    print("  $ " + cmd[:150])
    _, out, err = ssh.exec_command(cmd, timeout=timeout)
    o = out.read().decode("utf-8", "replace")
    e = err.read().decode("utf-8", "replace")
    rc = out.channel.recv_exit_status()
    if o.strip():
        print("    " + o.strip().replace("\n", "\n    ")[:1200])
    if e.strip():
        print("    [stderr] " + e.strip()[:600])
    if check and rc != 0:
        raise RuntimeError("命令失败 rc=%d: %s" % (rc, cmd))
    return o


def main():
    if not PW:
        print("缺少 SSH_PW")
        return 2
    if not os.path.exists(LOCAL_BUNDLE):
        print("找不到 " + LOCAL_BUNDLE)
        return 2

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, username=USER, password=PW, timeout=20,
                allow_agent=False, look_for_keys=False)
    print("已连接 %s@%s\n" % (USER, HOST))

    # ---- 1. 备份 ----
    print("== 1/5 备份 /opt/fenghuo ==")
    sh(ssh, "sudo rm -rf /opt/fenghuo.bak && sudo cp -r %s /opt/fenghuo.bak && "
            "sudo du -sh /opt/fenghuo.bak" % APP)

    # ---- 2. 上传 ----
    print("\n== 2/5 上传部署包 ==")
    sftp = ssh.open_sftp()
    t0 = time.time()
    sftp.put(LOCAL_BUNDLE, REMOTE_TGZ)
    print("  已上传 %.1f KB，用时 %.1fs" % (
        os.path.getsize(LOCAL_BUNDLE) / 1024, time.time() - t0))

    # ---- 3. 解包 ----
    print("\n== 3/5 解包 ==")
    sh(ssh, "rm -rf %s && mkdir -p %s && tar xzf %s -C %s && ls %s | head -25"
       % (REMOTE_TMP, REMOTE_TMP, REMOTE_TGZ, REMOTE_TMP, REMOTE_TMP))

    # ---- 4. 只覆盖代码（保留 venv / .env / 数据）----
    print("\n== 4/5 覆盖代码（不碰 venv / nginx）==")
    code_dirs = "adapters core integrations llm security static"
    for d in code_dirs.split():
        sh(ssh, "sudo rm -rf %s/%s && sudo cp -r %s/%s %s/%s"
           % (APP, d, REMOTE_TMP, d, APP, d))
    for f in ["server.py", "classified_server.py", "pipeline.py",
              "requirements.txt", "requirements.lock.txt", "README.md"]:
        sh(ssh, "sudo cp %s/%s %s/%s" % (REMOTE_TMP, f, APP, f))
    # 备份 .env 保留服务器上的（含线上凭据），不覆盖
    sh(ssh, "echo '  保留服务器原有 .env（未覆盖）'; ls -la %s/.env" % APP)

    # ---- 4.5 同步演示数据 ----
    # 为什么需要这一步：本脚本刻意不覆盖 data/（保护台账、工单、凭据等运行时数据），
    # 但 data/public 与 data/service_levels.json 是**演示基准数据**——
    # 方案书附录四的「7 家实测」（长亮 99.33 / 科蓝 67.33 / 恒生 86.02 …）就是由它们算出来的。
    # 2026-09-30 实测踩过：线上 service_levels.json 停留在部署日，
    # 导致线上科蓝显示 69.79/yellow，与方案书的 67.33/red 矛盾。
    print("\n== 4.5/5 同步演示数据（可用 SKIP_DEMO_DATA=1 跳过）==")
    if os.environ.get("SKIP_DEMO_DATA") == "1":
        print("  SKIP_DEMO_DATA=1，跳过（线上评分可能与方案书不一致）")
    else:
        sh(ssh, "sudo cp -r %s/data %s/data.bak_$(date +%%H%%M%%S)" % (APP, APP))
        sh(ssh, "ls -d %s/data.bak_* | tail -1" % APP)
        demo = ["data/public", "data/mock", "data/service_levels.json"]
        for item in demo:
            sh(ssh, "sudo rm -rf %s/%s && sudo cp -r %s/%s %s/%s"
               % (APP, item, REMOTE_TMP, item, APP, item))
        sh(ssh, "echo -n '  线上科蓝（应与方案书一致 67.33/red）: '; "
                "sudo grep -o '\"score\"[^,]*' %s/data/public/北京科蓝* 2>/dev/null | head -1; "
                "md5sum %s/data/service_levels.json" % (APP, APP))
        sh(ssh, "echo '  ⚠ 已覆盖 data/service_levels.json —— 若线上曾由界面录入过服务水平数据，'"
                "'请从上面的 data.bak_* 恢复'")

    sh(ssh, "sudo chown -R root:root %s && sudo chmod -R a+r %s" % (APP, APP))

    # ---- 5. 重启并验证 ----
    print("\n== 5/5 重启服务并验证 ==")
    # ⚠️ TRUST_PROXY=1 是本部署形态的**必需**配置，不是可选项：
    #    新版 security/auth.py 的 client_ip() 只在 TRUST_PROXY=1 时才采信
    #    X-Real-IP/X-Forwarded-For，否则一律用 request.client.host。
    #    线上是 nginx 反代，后端看到的 client.host 恒为 127.0.0.1 ——
    #    不设该变量，**所有公网请求都会被判为「本机」**：
    #      · /api/sources/config 等本机专用写接口对公网放开（防护回退）
    #      · _write_guard 的跨主机令牌校验被短路（等于没有鉴权）
    #    nginx 侧已用 $remote_addr 覆盖 X-Real-IP，客户端无法伪造（见 sites-available/fenghuo）。
    print("\n  --- 写入安全配置（TRUST_PROXY + 写接口令牌）---")
    # ⚠️ 三项配置全部必需，缺一项就会出问题（2026-10-21）：
    #
    #  1) TRUST_PROXY=1 —— 新版 security/auth.py 的 client_ip() 只在此时采信转发头。
    #     线上是 nginx 反代，后端看到的 client.host 恒为 127.0.0.1：
    #     · 不设 → **所有公网请求被判为「本机」**，写接口鉴权与本机专用限制双双失效
    #       （实测过：公网 DELETE /api/roster/S998 直接返回 200 且真的删了）；
    #     · 设了 → 公网请求识别为跨主机，走令牌校验。
    #     nginx 两个 server 块都用 $remote_addr 覆盖 X-Real-IP，客户端无法伪造。
    #
    #  2) FENGHUO_API_TOKEN —— 跨主机写操作的令牌（fail-closed：不配就全拒）。
    #
    #  3) FENGHUO_FRONTEND_TOKEN —— 与上者同值，由 server.py 的 root() 注入首页，
    #     使前端 fetchWithTimeout 自动带上 Authorization 头。
    #     这是"公网可演示写入"形态：任何访客打开的页面都带令牌，按钮可用。
    #     ⚠️ 该形态等于把写权限公开（令牌可见于页面源码），仅适用于演示/内网环境。
    #     若要改回"公网只读"，删掉这一项即可（写操作立即对公网 403，本机不受影响）。
    fh_token = resolve_token()
    unit = "/etc/systemd/system/fenghuo.service"
    setup_cmds = [
        # 先清掉旧的同类行，避免反复部署时堆积重复项
        "sudo sed -i '/^Environment=TRUST_PROXY=/d;/^Environment=FENGHUO_API_TOKEN=/d;"
        "/^Environment=FENGHUO_FRONTEND_TOKEN=/d' " + unit,
        # 统一插在 BIND_HOST 之后
        "sudo sed -i '/^Environment=BIND_HOST=/a Environment=TRUST_PROXY=1\\n"
        "Environment=FENGHUO_API_TOKEN=%s\\nEnvironment=FENGHUO_FRONTEND_TOKEN=%s' %s"
        % (fh_token, fh_token, unit),
        "sudo systemctl daemon-reload",
        "systemctl cat fenghuo | grep -E 'TRUST_PROXY|FENGHUO_'",
    ]
    for c in setup_cmds:
        sh(ssh, c)
    sh(ssh, "sudo systemctl restart fenghuo && sleep 4 && systemctl is-active fenghuo")
    # ⚠️ 涉密通道必须一起重启：fenghuo-classified（127.0.0.1:8001）**与主站共用同一份代码**
    #    （classified_server.py + core/ + security/），只重启主站会让它继续跑内存里的旧代码。
    #    尤其上传路径的加固（体积上限/分块写盘）就在这里，漏掉等于留一个 DoS 面。
    sh(ssh, "sudo systemctl restart fenghuo-classified 2>/dev/null || true; "
            "sleep 2; systemctl is-active fenghuo-classified 2>/dev/null || echo '(未安装该单元，跳过)'")
    print("\n  --- 版本特征核验（对齐方案书 7.1 的三条承诺）---")
    sh(ssh, "echo -n '  security/netguard.py 存在: '; ls %s/security/netguard.py "
            ">/dev/null 2>&1 && echo 是 || echo 否" % APP)
    # 新版拆分出的三个文件——漏传会导致 ImportError 或鉴权/日志能力缺失
    sh(ssh, "echo -n '  security/auth.py 存在: '; ls %s/security/auth.py "
            ">/dev/null 2>&1 && echo 是 || echo 否" % APP)
    sh(ssh, "echo -n '  core/jsonstore.py 存在: '; ls %s/core/jsonstore.py "
            ">/dev/null 2>&1 && echo 是 || echo 否" % APP)
    sh(ssh, "echo -n '  core/quiet_log.py 存在: '; ls %s/core/quiet_log.py "
            ">/dev/null 2>&1 && echo 是 || echo 否" % APP)
    sh(ssh, "echo -n '  requirements.lock.txt 存在: '; ls %s/requirements.lock.txt "
            ">/dev/null 2>&1 && echo 是 || echo 否" % APP)
    sh(ssh, "echo -n '  server.py 套用网络守卫: '; sudo grep -c 'no_network()' %s/server.py" % APP)
    sh(ssh, "echo -n '  server.py 出网前脱敏: '; sudo grep -c '_mask_text' %s/server.py" % APP)
    sh(ssh, "echo -n '  server.py 密级自检: '; sudo grep -c '_assert_classified_route' %s/server.py" % APP)
    sh(ssh, "echo -n '  server.py 写接口闸门: '; sudo grep -c '_write_guard' %s/server.py" % APP)
    sh(ssh, "echo -n '  CORS 白名单已收紧: '; "
            "sudo grep -c '_allowed_origins or _DEFAULT_ORIGINS' %s/server.py" % APP)
    sh(ssh, "echo -n '  index.html 含 CLASSIFIED_API: '; "
            "sudo grep -c 'CLASSIFIED_API' %s/static/index.html" % APP)
    sh(ssh, "echo -n '  classified_server.py 存在: '; ls %s/classified_server.py "
            ">/dev/null 2>&1 && echo 是 || echo 否" % APP)
    sh(ssh, "ls -la %s/server.py %s/static/index.html" % (APP, APP))
    print("\n  --- 本机接口自测 ---")
    sh(ssh, "curl -s -o /dev/null -w '  GET / -> %{http_code} (%{size_download} bytes)\\n' "
            "http://127.0.0.1:8000/")
    sh(ssh, "curl -s -X POST http://127.0.0.1:8000/api/query "
            "-H 'Content-Type: application/json' "
            "-d '{\"name\":\"北京科蓝软件系统股份有限公司\"}' "
            "-o /tmp/_q.json -w '  POST /api/query -> %{http_code}\\n'; "
            "python3 -c \"import json;d=json.load(open('/tmp/_q.json'));r=d.get('result') or {};"
            "print('  分数=%s 等级=%s'%(r.get('total_score'),r.get('level')))\" 2>&1 | tail -3")
    # ★ 安全断言：伪造 X-Real-IP 不应再被当成本机。
    #   直连 127.0.0.1 时 client.host 本就是 127.0.0.1，无法区分；
    #   故用「带 X-Real-IP: 8.8.8.8 的自称本机请求」验证——TRUST_PROXY=1 生效后
    #   它会被判为跨主机，从而被 _require_local_ip / authorize_write 拦下。
    print("\n  --- 安全回归：转发头信任链 ---")
    sh(ssh, "echo -n '  伪造 X-Real-IP 访问本机专用接口（应 403）: '; "
            "curl -s -o /dev/null -w '%{http_code}\\n' -X POST "
            "http://127.0.0.1:8000/api/sources/config "
            "-H 'Content-Type: application/json' -H 'X-Real-IP: 8.8.8.8' -d '{}'")
    sh(ssh, "echo -n '  不带转发头访问本机专用接口（本机应放行->非403）: '; "
            "curl -s -o /dev/null -w '%{http_code}\\n' -X POST "
            "http://127.0.0.1:8000/api/sources/config "
            "-H 'Content-Type: application/json' -d '{}'")
    sh(ssh, "echo -n '  伪造 X-Real-IP 的写操作（应 403 需令牌）: '; "
            "curl -s -o /dev/null -w '%{http_code}\\n' -X POST "
            "http://127.0.0.1:8000/api/roster "
            "-H 'Content-Type: application/json' -H 'X-Real-IP: 8.8.8.8' -d '{}'")
    sh(ssh, "echo -n '  只读 GET 仍可用（应 200）: '; "
            "curl -s -o /dev/null -w '%{http_code}\\n' "
            "-H 'X-Real-IP: 8.8.8.8' http://127.0.0.1:8000/api/roster")
    sh(ssh, "sudo journalctl -u fenghuo -n 8 --no-pager | tail -8")

    sftp.close()
    ssh.close()
    print("\n完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
