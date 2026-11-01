"""打包「云服务器一键部署包」（含 install.sh）。

产物：D:/金融科技专项赛/fenghuo_server_bundle.tar.gz
内容：deploy/ 全部代码（含 .env 凭据）+ install.sh + README_部署.txt

用法：python scripts/make_server_bundle.py
"""
import os
import tarfile
import pathlib

SRC = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
DEPLOY = os.path.abspath(os.path.join(SRC, "..", "deploy"))
OUT = os.path.abspath(os.path.join(SRC, "..", "fenghuo_server_bundle.tar.gz"))

INSTALL_SH = """#!/usr/bin/env bash
# 烽火 · 信息科技外包风险智能预警系统 —— 云服务器一键部署
# 适用：Ubuntu 20.04 / 22.04 / 24.04
# 用法：sudo bash install.sh
set -e

HERE="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="/opt/fenghuo"
PORT=8000

echo "==> 1/5 安装系统依赖（Python / Nginx）"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip nginx curl

echo "==> 2/5 复制代码到 $APP_DIR"
mkdir -p "$APP_DIR"
cp -r "$HERE"/. "$APP_DIR"/

echo "==> 3/5 创建虚拟环境并安装依赖（约 1-3 分钟）"
cd "$APP_DIR"
python3 -m venv venv
PIP_MIRROR="https://pypi.tuna.tsinghua.edu.cn/simple"
./venv/bin/pip install -q --upgrade pip -i "$PIP_MIRROR"
./venv/bin/pip install -q -i "$PIP_MIRROR" -r requirements.txt

echo "==> 4/5 注册 systemd 服务（开机自启 + 崩溃自动重启）"
cat > /etc/systemd/system/fenghuo.service <<EOF
[Unit]
Description=Fenghuo Risk Monitor
After=network.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
Environment=PORT=$PORT
ExecStart=$APP_DIR/venv/bin/python server.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable fenghuo >/dev/null 2>&1 || true
systemctl restart fenghuo

echo "==> 4b/5 注册涉密通道独立实例（仅监听 127.0.0.1:8001，不对外暴露）"
cat > /etc/systemd/system/fenghuo-classified.service <<EOF
[Unit]
Description=Fenghuo Classified Channel (loopback only)
After=network.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
Environment=CLASSIFIED_HOST=127.0.0.1
Environment=CLASSIFIED_PORT=8001
ExecStart=$APP_DIR/venv/bin/python classified_server.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable fenghuo-classified >/dev/null 2>&1 || true
# 起不来不影响主站（例如该机器不需要独立涉密通道），故容错处理
systemctl restart fenghuo-classified 2>/dev/null || echo "    (涉密通道未启动，不影响主站；可稍后手动排查)"

echo "==> 5/5 配置 Nginx 反向代理（80 -> $PORT，不代理 8001）"
cat > /etc/nginx/sites-available/fenghuo <<'NGINX'
server {
    listen 80 default_server;
    server_name _;
    client_max_body_size 50m;
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_read_timeout 180s;
    }
}
NGINX
ln -sf /etc/nginx/sites-available/fenghuo /etc/nginx/sites-enabled/fenghuo
rm -f /etc/nginx/sites-enabled/default
nginx -t >/dev/null 2>&1 && systemctl restart nginx

# ★ 恢复 HTTPS：本脚本的 nginx 配置只写了 80，会覆盖 certbot 之前加的 443/SSL。
#   若服务器上已签发过证书，自动重新挂载，避免每次重新部署后 HTTPS 失效。
for _certdir in /etc/letsencrypt/live/*/; do
  _dom=$(basename "$_certdir")
  [ "$_dom" = "README" ] && continue
  if [ -f "$_certdir/fullchain.pem" ]; then
    echo "==> 检测到已有 HTTPS 证书（$_dom），恢复 443/SSL ..."
    certbot install --nginx --cert-name "$_dom" --redirect --non-interactive 2>&1 | tail -3 || true
  fi
done

IP=$(curl -s --max-time 5 ifconfig.me || echo "<服务器公网IP>")
echo ""
echo "======================================================"
echo " 部署完成！"
echo "   浏览器访问： http://$IP"
echo "   查看服务：   systemctl status fenghuo"
echo "   涉密通道：   systemctl status fenghuo-classified   (127.0.0.1:8001，仅本机)"
echo "   查看日志：   journalctl -u fenghuo -f"
echo "======================================================"
"""

README_TXT = """烽火风控系统 —— 服务器部署说明
================================

一、上传本包到服务器
   在你自己电脑的终端执行（Windows 用 PowerShell / Git Bash）：
     scp fenghuo_server_bundle.tar.gz root@<服务器公网IP>:/root/

二、登录服务器并安装
     ssh root@<服务器公网IP>
     tar xzf fenghuo_server_bundle.tar.gz -C /root/fenghuo_pkg --one-top-level 2>/dev/null || {
       mkdir -p /root/fenghuo_pkg && tar xzf /root/fenghuo_server_bundle.tar.gz -C /root/fenghuo_pkg
     }
     cd /root/fenghuo_pkg
     sudo bash install.sh

三、完成
    浏览器打开 http://<服务器公网IP> 即可。

关于涉密通道（可选）
    部署脚本已同时注册 fenghuo-classified 服务，监听 127.0.0.1:8001，
    路由表只含 /api/upload，解析入口套网络守卫（出网即抛错）。
    - 它对公网**不可见**：Nginx 只代理 8000，安全组也不需放行 8001。
    - 默认公开实例仍保留 /api/upload，前端无需配置即可用。
    - 若要把涉密能力完全收拢到独立实例：
        1) 在 /etc/systemd/system/fenghuo.service 的 [Service] 加一行
           Environment=CLASSIFIED_STANDALONE=1，然后 systemctl restart fenghuo
        2) 前端 static/index.html 里把 CLASSIFIED_API 设为 "http://127.0.0.1:8001"
    - 本机自测上传：
        curl -F "files=@某文件.xlsx" http://127.0.0.1:8001/api/upload

绑定域名（可选）
    1. 在腾讯云域名解析里，加一条 A 记录指向服务器公网 IP
    2. 服务器上执行：sudo apt install -y certbot python3-certbot-nginx
       sudo certbot --nginx -d 你的域名
     即可自动签发 https 证书。

常见问题
    - 打不开？检查服务器安全组是否放行了 80 端口（腾讯云控制台 → 安全组）
    - 服务起不来？看日志：journalctl -u fenghuo -n 50
"""


def main():
    if not os.path.isdir(DEPLOY):
        raise SystemExit("未找到 deploy/ 目录，请先运行 make_deploy_dir.py")

    with tarfile.open(OUT, "w:gz") as tar:
        # 代码（含 .env）
        for root, dirs, files in os.walk(DEPLOY):
            dirs[:] = [d for d in dirs if d not in ("__pycache__", ".git")]
            for f in files:
                if f.endswith((".pyc", ".pyo")):
                    continue
                p = os.path.join(root, f)
                tar.add(p, arcname=os.path.relpath(p, DEPLOY))

        # install.sh（强制 LF 换行）
        tmp = os.path.join(os.path.dirname(OUT), "_install.sh")
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(INSTALL_SH)
        tar.add(tmp, arcname="install.sh")
        os.remove(tmp)

        # 说明
        tmp2 = os.path.join(os.path.dirname(OUT), "_README_部署.txt")
        with open(tmp2, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(README_TXT)
        tar.add(tmp2, arcname="README_部署.txt")
        os.remove(tmp2)

    print(f"部署包已生成：{OUT}")
    print(f"大小：{os.path.getsize(OUT)/1024:.1f} KB")


if __name__ == "__main__":
    main()
