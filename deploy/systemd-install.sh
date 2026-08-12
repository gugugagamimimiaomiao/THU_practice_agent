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
# -u 关掉输出缓冲。写文件时 Python 默认按块缓冲，排查问题时会发现日志停在
# 一个多小时之前，误以为服务没在跑——正是遇到过的情况。
ExecStart=/usr/bin/python3 -u $APP_DIR/server.py --host 0.0.0.0 --port $PORT
Restart=always
RestartSec=1
# 服务收到 SIGTERM 后会停止接受新连接，并留几秒让进行中的响应写完，
# 这样重新部署时正在对话的人不会被砍断在半句话上。
KillSignal=SIGTERM
TimeoutStopSec=10
StandardOutput=append:/var/log/practice-xiaoda.log
StandardError=append:/var/log/practice-xiaoda.log

[Install]
WantedBy=multi-user.target
EOF

# --- 自检定时任务 -------------------------------------------------------
# 进程活着不等于能服务，所以每分钟真的打一次对话接口。
# 连续失败 3 次才告警；配了 HEALTH_ALERT_SCKEY 就推到微信，没配只写日志。
if [ -f "$APP_DIR/deploy/healthcheck.sh" ]; then
  chmod +x "$APP_DIR/deploy/healthcheck.sh"
  cat > /etc/systemd/system/practice-xiaoda-health.service <<EOF
[Unit]
Description=Practice Xiaoda health probe

[Service]
Type=oneshot
ExecStart=/usr/bin/env bash $APP_DIR/deploy/healthcheck.sh
EOF
  cat > /etc/systemd/system/practice-xiaoda-health.timer <<'EOF'
[Unit]
Description=Run the Practice Xiaoda health probe every minute

[Timer]
OnBootSec=2min
OnUnitActiveSec=1min
AccuracySec=10s

[Install]
WantedBy=timers.target
EOF
  systemctl daemon-reload
  systemctl enable --now practice-xiaoda-health.timer >/dev/null 2>&1 || true
fi

# --- 每日备份 -----------------------------------------------------------
# 爬虫接上之后库里是人工核验过的真实项目，重建成本远高于这点磁盘。
# 用 sqlite3 的 backup API 而不是 cp：服务常驻、随时在写，直接复制可能拷到
# 写了一半的状态，等要恢复时才发现备份本身是坏的。
if [ -f "$APP_DIR/deploy/backup.py" ]; then
  cat > /etc/systemd/system/practice-xiaoda-backup.service <<EOF
[Unit]
Description=Practice Xiaoda database backup

[Service]
Type=oneshot
EnvironmentFile=$ENV_FILE
ExecStart=/usr/bin/python3 $APP_DIR/deploy/backup.py
StandardOutput=append:/var/log/practice-xiaoda-backup.log
StandardError=append:/var/log/practice-xiaoda-backup.log
EOF
  cat > /etc/systemd/system/practice-xiaoda-backup.timer <<'EOF'
[Unit]
Description=Back up the Practice Xiaoda database daily

[Timer]
OnCalendar=*-*-* 04:30:00
Persistent=true
AccuracySec=5min

[Install]
WantedBy=timers.target
EOF
  systemctl daemon-reload
  systemctl enable --now practice-xiaoda-backup.timer >/dev/null 2>&1 || true
  # 装完先备一次，别等到明天凌晨才有第一份。
  systemctl start practice-xiaoda-backup >/dev/null 2>&1 || true
fi

# --- 日志轮转 -----------------------------------------------------------
# 健康自检每分钟一条，一天一千四百多行；磁盘写满时 SQLite 会以很难看懂的
# 方式报错，与其事后排查不如提前压住。
cat > /etc/logrotate.d/practice-xiaoda <<'EOF'
/var/log/practice-xiaoda*.log {
    weekly
    rotate 6
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
}
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
