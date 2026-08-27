#!/bin/bash
# 双击运行 → 扫码 → 剩下的全自动：
#   起 WeWe → 等你扫码 → 逐个公众号拉新文章（间隔 2 分钟）→ 筛选抽取 → 投稿到服务器
# 中间那段很长（13 个号 × 2 分钟），窗口放着别关，跑完会有汇总。
cd "$(dirname "$0")/.." || exit 1

GREEN=$'\033[0;32m'; YELLOW=$'\033[1;33m'; RED=$'\033[0;31m'; NC=$'\033[0m'
ok()   { printf "%s✓ %s%s\n" "$GREEN" "$1" "$NC"; }
warn() { printf "%s! %s%s\n" "$YELLOW" "$1" "$NC"; }
die()  { printf "%s✗ %s%s\n" "$RED" "$1" "$NC"; printf "\n按回车关闭。"; read -r _; exit 1; }
step() { echo; echo "========================================"; echo " $1"; echo "========================================"; }

ROOT="$(pwd)"
LOG_DIR="$ROOT/data/logs"; mkdir -p "$LOG_DIR"
exec > >(tee "$LOG_DIR/daily-$(date +%Y%m%d).log") 2>&1

BASE="${WEWE_BASE_URL:-http://127.0.0.1:4000}"
DB="${WEWE_DB_PATH:-$ROOT/data/wewe-rss.db}"
INTERVAL="${WEWE_REFRESH_INTERVAL:-120}"   # 实测下限，别往下调
SCAN_TIMEOUT="${SCAN_TIMEOUT:-900}"

echo "每日采集 · $(date '+%Y-%m-%d %H:%M')"
echo "日志：$LOG_DIR/daily-$(date +%Y%m%d).log"

# --- 0. 代理探活 ---------------------------------------------------------
export no_proxy="localhost,127.0.0.1,::1${no_proxy:+,$no_proxy}"; export NO_PROXY="$no_proxy"
port_alive() { (exec 3<>"/dev/tcp/$1/$2") 2>/dev/null && exec 3<&- 3>&- && return 0; return 1; }
DEAD=""; SEEN=""
for VALUE in "${https_proxy:-}" "${HTTPS_PROXY:-}" "${http_proxy:-}" "${HTTP_PROXY:-}" "${all_proxy:-}" "${ALL_PROXY:-}"; do
  [ -z "$VALUE" ] && continue
  HP="${VALUE#*://}"; HP="${HP%%/*}"; HP="${HP##*@}"
  case " $SEEN " in *" $HP "*) continue;; esac
  SEEN="$SEEN $HP"; PH="${HP%%:*}"; PP="${HP##*:}"; [ "$PP" = "$PH" ] && PP=8080
  port_alive "$PH" "$PP" || DEAD="$DEAD $PH:$PP"
done
[ -n "$DEAD" ] && {
  warn "代理$DEAD 没人监听，本次运行清掉（不动 ~/.zshrc）"
  unset https_proxy HTTPS_PROXY http_proxy HTTP_PROXY all_proxy ALL_PROXY
}

account_ok() {  # 微信读书账号可用返回 0
  python3 - "$DB" <<'PY' 2>/dev/null
import sqlite3, sys
try:
    c = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
    hit = c.execute("SELECT 1 FROM accounts WHERE status = 1 LIMIT 1").fetchone()
except Exception:
    hit = None
raise SystemExit(0 if hit else 1)
PY
}

# --- 1. 起 WeWe ----------------------------------------------------------
step "第 1 步 · 起 WeWe"
if curl -fsS --max-time 3 "$BASE/feeds" >/dev/null 2>&1; then
  ok "已经在跑：$BASE"
else
  if [ -z "${WEWE_SERVER_DIR:-}" ]; then
    for CANDIDATE in "$(cd .. && pwd)/wewe-rss-src/apps/server" "$HOME/wewe-rss/apps/server"; do
      [ -f "$CANDIDATE/dist/main.js" ] && { export WEWE_SERVER_DIR="$CANDIDATE"; break; }
    done
  fi
  [ -n "${WEWE_SERVER_DIR:-}" ] || die "没找到 WeWe 构建产物，先双击「WeWe重建.command」。"
  nohup bash scripts/start_local_wewe.sh > "$LOG_DIR/wewe-service.log" 2>&1 &
  for _ in $(seq 1 30); do
    curl -fsS --max-time 2 "$BASE/feeds" >/dev/null 2>&1 && break
    sleep 1
  done
  curl -fsS --max-time 3 "$BASE/feeds" >/dev/null 2>&1 ||
    { tail -15 "$LOG_DIR/wewe-service.log"; die "WeWe 起不来，日志在上面"; }
  ok "起好了：$BASE"
fi

# --- 2. 扫码（唯一要你动手的一步）-----------------------------------------
step "第 2 步 · 扫码"
if account_ok; then
  ok "微信读书账号还是可用的，不用扫码"
else
  warn "账号失效了，需要扫码"
  echo "  浏览器这就打开账号页：点「添加读书账号」→ 用微信扫码"
  echo "  地址：$BASE/dash/accounts"
  echo "  （页面要 AuthCode 的话随便输一个字符点确认 —— 本地没设密码，上游的显示 bug）"
  open "$BASE/dash/accounts" >/dev/null 2>&1 || true
  echo
  WAITED=0
  while [ "$WAITED" -lt "$SCAN_TIMEOUT" ]; do
    account_ok && break
    sleep 5; WAITED=$((WAITED + 5))
    [ $((WAITED % 30)) -eq 0 ] && printf "  等你扫码…（已等 %s 秒）\n" "$WAITED"
  done
  account_ok || die "$((SCAN_TIMEOUT / 60)) 分钟内没等到扫码，本次到此为止。"
  echo
  ok "扫到了"
fi

# --- 3. 逐个公众号拉新文章 -------------------------------------------------
step "第 3 步 · 拉新文章（每个号间隔 ${INTERVAL} 秒）"
warn "这一段最久，中途别关窗口，也别去网页上手点更新。"
echo
python3 scripts/wewe_refresh_feeds.py --base "$BASE" --interval "$INTERVAL" --db "$DB"
REFRESH_RC=$?
if [ "$REFRESH_RC" = "2" ]; then
  die "刷新没跑起来（连不上或没有订阅号）"
elif [ "$REFRESH_RC" != "0" ]; then
  warn "有公众号没刷成。已经拿到的照样往下走。"
fi

# --- 4. 筛选 + 抽取 + 投稿 -------------------------------------------------
# 这一步用你们自己的 wewe_post_login.py：激活排队中的订阅 → 给新号补索引 →
# 按 DAILY_PRIORITY_ACCOUNTS 做当天增量导入 → 导出并投稿。服务端按
# source_url 去重，重复跑是安全的。
step "第 4 步 · 筛选、抽取、投稿"
[ -f .env ] && { set -a; . ./.env; set +a; }
if [ -z "${XIAODA_INGEST_KEY:-}" ]; then
  warn "没有投稿密钥，会走到导出为止、不投。"
  echo "  想让它一路投到服务器，在 .env 里补一行："
  echo "    XIAODA_INGEST_KEY=……"
  echo
fi
WEWE_BASE_URL="$BASE" python3 scripts/wewe_post_login.py
POST_RC=$?
echo

# --- 5. 汇总 -------------------------------------------------------------
step "今天的结果"
python3 - "$DB" "$ROOT/data/practice_xiaoda.db" <<'PY'
import sqlite3, sys, datetime

def one(path, sql, args=()):
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        return connection.execute(sql, args).fetchone()[0]
    except Exception:
        return "?"

wewe, practice = sys.argv[1], sys.argv[2]
today = datetime.date.today().isoformat()
print("  WeWe 缓存文章总数：", one(wewe, "SELECT COUNT(*) FROM articles"))
print("  其中今天入库的：  ", one(wewe, "SELECT COUNT(*) FROM articles WHERE date(created_at)=?", (today,)))
print("  实践小搭项目总数：", one(practice, "SELECT COUNT(*) FROM projects"))
PY
echo
if [ "$POST_RC" = "0" ]; then
  ok "跑完了，投稿那一步返回成功"
else
  warn "最后一步返回非 0，上面那行 JSON 里的 server_push 会说明卡在哪"
fi
echo "  完整日志：$LOG_DIR/daily-$(date +%Y%m%d).log"
echo
printf "按回车关闭窗口。"
read -r _
