#!/usr/bin/env bash
# 每分钟自检一次：服务活着吗？对话真的能回吗？
#
# 为什么不只看 systemctl is-active：进程活着不等于能服务。数据库锁死、端口
# 被占、依赖挂掉，进程都还是 active。所以这里打真实的 /v1/chat/completions。
#
# 连续失败到阈值才告警，避免一次抖动就吵人；恢复后发一条恢复通知。
# 告警走 Server 酱（微信），没配 SCKEY 就只写日志。
set -uo pipefail

PORT=${PORT:-8000}
STATE=/var/lib/practice-xiaoda-health
LOG=/var/log/practice-xiaoda-health.log
FAIL_THRESHOLD=${FAIL_THRESHOLD:-3}

mkdir -p "$STATE"
FAILFILE="$STATE/consecutive_failures"
[ -f "$FAILFILE" ] || echo 0 > "$FAILFILE"

set -a; [ -f /etc/practice-xiaoda.env ] && . /etc/practice-xiaoda.env; set +a
SCKEY=${HEALTH_ALERT_SCKEY:-}

log () { echo "$(date '+%F %T') $*" >> "$LOG"; }

notify () {   # notify "<标题>" "<正文>"
  log "ALERT $1 :: $2"
  [ -z "$SCKEY" ] && return 0
  curl -s --max-time 15 -o /dev/null \
    -d "title=$1" --data-urlencode "desp=$2" \
    "https://sctapi.ftqq.com/${SCKEY}.send" || log "告警发送失败"
}

reason=""

# 1) 健康端点
health=$(curl -s --max-time 10 "http://127.0.0.1:$PORT/health" || true)
case "$health" in
  *'"status": "ok"'*) : ;;
  *) reason="健康检查异常：${health:-无响应}" ;;
esac

# 2) 真的走一遍对话（这才是用户实际用到的路径）
if [ -z "$reason" ] && [ -n "${XIAODA_API_KEY:-}" ]; then
  body='{"model":"practice-xiaoda","max_tokens":8,"messages":[{"role":"user","content":"推荐实践"}]}'
  # X-Health-Probe 让服务端知道这是自检，不要写进活动日志——
  # 否则一天一千四百多条，真实用户的行为记录会被彻底淹没。
  code=$(curl -s --max-time 20 -o /tmp/pxd_health_body -w '%{http_code}' \
         -H "Authorization: Bearer $XIAODA_API_KEY" -H 'Content-Type: application/json' \
         -H 'X-Health-Probe: 1' \
         -d "$body" "http://127.0.0.1:$PORT/v1/chat/completions" || echo 000)
  if [ "$code" != "200" ]; then
    reason="对话接口返回 HTTP $code"
  elif ! grep -q '"choices"' /tmp/pxd_health_body 2>/dev/null; then
    reason="对话接口响应结构异常"
  fi
  rm -f /tmp/pxd_health_body
fi

# 3) 磁盘（SQLite 写满会以很难懂的方式失败）
use=$(df --output=pcent / | tail -1 | tr -dc '0-9')
[ -z "$reason" ] && [ "${use:-0}" -ge 90 ] && reason="磁盘使用率 ${use}%"

fails=$(cat "$FAILFILE")
if [ -n "$reason" ]; then
  fails=$((fails + 1)); echo "$fails" > "$FAILFILE"
  log "FAIL($fails/$FAIL_THRESHOLD) $reason"
  if [ "$fails" -eq "$FAIL_THRESHOLD" ]; then
    notify "实践小搭异常" "$reason（已连续 $fails 次）。服务器 $(hostname)，systemd 会自动重启，若持续请登录排查。"
  fi
else
  if [ "$fails" -ge "$FAIL_THRESHOLD" ]; then
    notify "实践小搭已恢复" "服务恢复正常。"
  fi
  echo 0 > "$FAILFILE"
  log "OK"
fi
