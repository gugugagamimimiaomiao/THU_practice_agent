#!/bin/bash
# 双击运行：从 WeWe 已缓存的文章里筛出招募类 → 抓全文 → 自查 → 投稿到服务器。
# 只读 WeWe，不改它的数据；投稿前一定先跑 --check，不合格的不发。
cd "$(dirname "$0")/.." || exit 1

GREEN=$'\033[0;32m'; YELLOW=$'\033[1;33m'; RED=$'\033[0;31m'; NC=$'\033[0m'
ok()   { printf "%s✓ %s%s\n" "$GREEN" "$1" "$NC"; }
warn() { printf "%s! %s%s\n" "$YELLOW" "$1" "$NC"; }
die()  { printf "%s✗ %s%s\n" "$RED" "$1" "$NC"; printf "\n按回车关闭。"; read -r _; exit 1; }

LOG_DIR="$(pwd)/data/logs"; mkdir -p "$LOG_DIR" data/exports
RUN_LOG="$LOG_DIR/export-push.log"
exec > >(tee "$RUN_LOG") 2>&1

# 天数与条数可以用环境变量覆盖：DAYS=60 NEED=80 双击不方便时在终端里跑
DAYS="${DAYS:-30}"
NEED="${NEED:-40}"
DELAY="${DELAY:-2}"
BASE="${WEWE_BASE_URL:-http://127.0.0.1:4000}"
STAMP=$(date +%Y%m%d-%H%M)
OUT="data/exports/wewe-$STAMP.jsonl"

echo "========================================"
echo " 导出 → 自查 → 投稿"
echo " 日志：$RUN_LOG"
echo "========================================"
echo

# --- 代理：死的就清掉，否则抓全文会 ECONNREFUSED ---------------------------
export no_proxy="localhost,127.0.0.1,::1${no_proxy:+,$no_proxy}"
export NO_PROXY="$no_proxy"
port_alive() { (exec 3<>"/dev/tcp/$1/$2") 2>/dev/null && exec 3<&- 3>&- && return 0; return 1; }
DEAD=""; SEEN=""
for VALUE in "${https_proxy:-}" "${HTTPS_PROXY:-}" "${http_proxy:-}" "${HTTP_PROXY:-}" "${all_proxy:-}" "${ALL_PROXY:-}"; do
  [ -z "$VALUE" ] && continue
  HP="${VALUE#*://}"; HP="${HP%%/*}"; HP="${HP##*@}"
  case " $SEEN " in *" $HP "*) continue;; esac
  SEEN="$SEEN $HP"; PH="${HP%%:*}"; PP="${HP##*:}"; [ "$PP" = "$PH" ] && PP=8080
  port_alive "$PH" "$PP" || DEAD="$DEAD $PH:$PP"
done
if [ -n "$DEAD" ]; then
  warn "代理$DEAD 没人监听，本次运行清掉它（不动 ~/.zshrc）"
  unset https_proxy HTTPS_PROXY http_proxy HTTP_PROXY all_proxy ALL_PROXY
fi

# --- 1. WeWe 在不在 ------------------------------------------------------
if ! curl -fsS --max-time 5 "$BASE/feeds" >/dev/null 2>&1; then
  die "连不上 WeWe（$BASE）。先双击「微信读书扫码并更新.command」把服务起起来。"
fi
ok "WeWe 在跑：$BASE"

# --- 2. 订阅号名单（直接问服务要，不写死）---------------------------------
ACCOUNTS=$(curl -fsS --max-time 10 "$BASE/feeds" | python3 -c '
import json,sys
rows = json.load(sys.stdin)
rows = rows if isinstance(rows, list) else rows.get("items", [])
for r in rows:
    name = r.get("mpName") or r.get("name") or ""
    if name: print(name)
')
COUNT=$(printf "%s\n" "$ACCOUNTS" | grep -c . || true)
[ "$COUNT" -gt 0 ] || die "没读到订阅号名单"
ok "$COUNT 个订阅号"

ARGS=()
while IFS= read -r NAME; do [ -n "$NAME" ] && ARGS+=(--account "$NAME"); done <<< "$ACCOUNTS"

# --- 3. 起始日期 ---------------------------------------------------------
SINCE=$(date -v-"${DAYS}"d +%F 2>/dev/null || date -d "$DAYS days ago" +%F)
echo
echo "  范围：最近 $DAYS 天（$SINCE 起），最多导 $NEED 条，每篇间隔 $DELAY 秒"
echo "  筛选：标题先过招募规则；抽字段的活儿仍归服务端 domain.py"
echo

# --- 4. 导出 -------------------------------------------------------------
# 已经导好的文件可以直接复用，跳过重新抓全文：
#   EXPORT_FILE=data/exports/xxx.jsonl bash scripts/导出并投稿.command
if [ -n "${EXPORT_FILE:-}" ] && [ -s "$EXPORT_FILE" ]; then
  OUT="$EXPORT_FILE"
  ok "复用已导出的文件，跳过抓取：$OUT"
else
  echo "  正在导出（要逐篇抓全文，慢，别关窗口）…"
  python3 scripts/wewe_export_handoff.py \
      "${ARGS[@]}" --since "$SINCE" --need "$NEED" --delay "$DELAY" \
      --mode current --output "$OUT"
  RC=$?
  # 注意：wewe_export_handoff.py 用退出码 1 表示「没凑够 --need 条」，
  # 不是出错。判断成没成要看有没有产出文件，不能看退出码。
  if [ ! -s "$OUT" ]; then
    if [ "$RC" != "0" ]; then
      die "导出没有产出文件。若上面是 429 / 今日小黑屋，今天就到此为止，别重试。"
    fi
    warn "这次一条都没导出来 —— 最近没有标题过筛的招募类新文章。"
    printf "\n按回车关闭。"; read -r _; exit 0
  fi
fi
LINES=$(grep -c . "$OUT")
if [ "${RC:-0}" != "0" ]; then
  ok "导出 $LINES 条 → $OUT"
  warn "没凑够 $NEED 条：满足条件的就这些，不是出错。想扩范围就 DAYS=60 再跑。"
else
  ok "导出 $LINES 条 → $OUT"
fi

# --- 5. 自查 -------------------------------------------------------------
echo
echo "  自查（不写库）…"
CHECK_OUT=$(python3 scripts/import_articles.py "$OUT" --check 2>&1)
printf "%s\n" "$CHECK_OUT"
USABLE=$(printf "%s" "$CHECK_OUT" | sed -n 's/^可用 \([0-9]*\) 条.*/\1/p' | tail -1)
[ -n "$USABLE" ] && [ "$USABLE" -gt 0 ] 2>/dev/null ||
  die "没有一条可用，先按上面的提示修，别投。"
if printf "%s" "$CHECK_OUT" | grep -q "需要修 [1-9]"; then
  warn "有几条不合格（多半是正文没抓全）。服务端会按条校验，不合格的不会入库；"
  warn "想先修再投就现在关掉，处理完用 EXPORT_FILE=$OUT 重跑，不会重新抓全文。"
fi

# --- 6. 投稿 -------------------------------------------------------------
echo
[ -f .env ] && { set -a; . ./.env; set +a; }
if [ -z "${XIAODA_INGEST_KEY:-}" ]; then
  warn "没有投稿密钥，停在投稿前一步。"
  echo "    文件已经导好了：$OUT"
  echo "    在 .env 里补上这两行（密钥找宋子睿要），再双击一次："
  echo "      XIAODA_INGEST_URL=http://8.217.145.109:8000/api/ingest"
  echo "      XIAODA_INGEST_KEY=……"
  printf "\n按回车关闭。"; read -r _; exit 0
fi
echo "  正在投稿…"
python3 scripts/push_article.py "$OUT" || warn "投稿有失败项，看上面逐条的结果"

echo
ok "跑完了"
echo "  这批文件：$OUT"
echo "  历史总结、纪实这类写作语料不在这批里；要单独回采就在终端跑："
echo "    python3 scripts/wewe_export_handoff.py --mode corpus --account 清华大学社会实践 \\"
echo "        --since $SINCE --need 15 --delay 2 --output data/exports/corpus-$STAMP.jsonl"
printf "\n按回车关闭。"
read -r _
