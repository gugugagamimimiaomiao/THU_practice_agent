#!/usr/bin/env bash
# 在一台干净的 Linux 服务器上把实践小搭装成常驻服务（systemd），
# 供清小搭以标准协议接入。可重复执行：密钥只生成一次，之后复用。
#
# 用法（在服务器上以 root 执行）：
#   1. 把仓库内容放到 /opt/practice-xiaoda
#   2. bash deploy/systemd-install.sh
#
# 这条路径不需要 Docker，也不装任何第三方包——本项目只用 Python 标准库。
# 若要走容器化或公网 HTTPS，见 DEPLOYMENT.md。
set -euo pipefail

APP_DIR=${APP_DIR:-/opt/practice-xiaoda}
PORT=${PORT:-8000}
ENV_FILE=/etc/practice-xiaoda.env
UNIT=/etc/systemd/system/practice-xiaoda.service

mkdir -p "$APP_DIR/data"

# --- 密钥 ---------------------------------------------------------------
# RATE_LIMIT_PER_MINUTE 特意调高：限流按 Bearer token 的指纹分桶，而清小搭
# 对所有终端用户使用同一个 token，默认的 60/分钟会被全体用户共享，评审期间
# 十几个人同时对话就会集体收到 429。
if [ ! -f "$ENV_FILE" ]; then
  XKEY=$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')
  AKEY=$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')
  cat > "$ENV_FILE" <<EOF
PRACTICE_XIAODA_ENV=production
HOST=0.0.0.0
PORT=$PORT
PRACTICE_XIAODA_DB=$APP_DIR/data/practice_xiaoda.db
XIAODA_API_KEY=$XKEY
XIAODA_MODEL_ID=practice-xiaoda
ADMIN_API_KEY=$AKEY
RATE_LIMIT_PER_MINUTE=600
PUBLIC_DASHBOARD=false
EOF
  chmod 600 "$ENV_FILE"
  echo "[deploy] 已生成新密钥，取用：sudo grep XIAODA_API_KEY $ENV_FILE"
else
  echo "[deploy] 复用 $ENV_FILE 中已有的密钥"
fi

# --- systemd ------------------------------------------------------------
cat > "$UNIT" <<EOF
[Unit]
Description=Practice Xiaoda OpenAI-compatible agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
ExecStart=/usr/bin/python3 $APP_DIR/server.py --host 0.0.0.0 --port $PORT
Restart=always
RestartSec=3
StandardOutput=append:/var/log/practice-xiaoda.log
StandardError=append:/var/log/practice-xiaoda.log

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable practice-xiaoda >/dev/null 2>&1 || true
systemctl restart practice-xiaoda
sleep 3

echo "[deploy] 运行状态：$(systemctl is-active practice-xiaoda)（开机自启：$(systemctl is-enabled practice-xiaoda)）"
echo "[deploy] 健康检查："
curl -s --max-time 10 "http://127.0.0.1:$PORT/health" || echo "健康检查失败，看 /var/log/practice-xiaoda.log"
echo
echo "[deploy] 记得在云厂商控制台的防火墙/安全组放行 TCP $PORT。"
echo "[deploy] 清小搭接入地址：http://<公网IP>:$PORT/v1"
