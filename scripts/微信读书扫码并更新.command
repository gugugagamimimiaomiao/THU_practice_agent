#!/bin/bash
# 双击运行：看一眼 WeWe 现状 → 起本地服务并等你扫码 → 跑完当天的增量导入 → 再看一眼变化。
# 只是把 scripts/wewe_scan_and_refresh.sh 包一层，方便双击；逻辑还在那个脚本里。
cd "$(dirname "$0")/.." || exit 1

GREEN=$'\033[0;32m'; YELLOW=$'\033[1;33m'; RED=$'\033[0;31m'; NC=$'\033[0m'
STATE_BEFORE="${TMPDIR:-/tmp}/wewe-state-before.json"
STATE_AFTER="${TMPDIR:-/tmp}/wewe-state-after.json"

# 整窗输出留一份到 data/logs/。出问题时不用你手抄屏幕，我这边能直接读。
LOG_DIR="$(pwd)/data/logs"
mkdir -p "$LOG_DIR"
RUN_LOG="$LOG_DIR/wewe-run.log"
exec > >(tee "$RUN_LOG") 2>&1
printf "本次输出同时写到 %s\n\n" "$RUN_LOG"

command -v python3 >/dev/null 2>&1 || {
  printf "%s✗ 没找到 python3，先在终端跑 xcode-select --install%s\n" "$RED" "$NC"
  printf "\n按回车关闭。"; read -r _; exit 1
}

# 原来的构建产物在 /private/tmp 下，被系统清掉过一次。没设 WEWE_SERVER_DIR 时
# 先在几个持久位置里找一份可用的，找到就用，省得每次手动传环境变量。
if [ -z "${WEWE_SERVER_DIR:-}" ]; then
  for CANDIDATE in "$(cd .. && pwd)/wewe-rss-src/apps/server" \
                   "$HOME/wewe-rss/apps/server" \
                   "/private/tmp/wewe-rss-eval/apps/server"; do
    if [ -f "$CANDIDATE/dist/main.js" ] && [ -f "$CANDIDATE/client/index.hbs" ]; then
      export WEWE_SERVER_DIR="$CANDIDATE"
      printf "%s✓ 用这份构建：%s%s\n\n" "$GREEN" "$CANDIDATE" "$NC"
      break
    fi
  done
  if [ -z "${WEWE_SERVER_DIR:-}" ]; then
    printf "%s✗ 没找到可用的 WeWe 构建产物。%s\n" "$RED" "$NC"
    printf "%s  先双击「WeWe重建.command」把服务端建出来，再回来跑这个。%s\n" "$YELLOW" "$NC"
    printf "\n按回车关闭。"; read -r _; exit 1
  fi
fi

echo "========================================"
echo " 第 1 步 · 刷新前的现状"
echo "========================================"
python3 scripts/wewe_state.py
python3 scripts/wewe_state.py --json > "$STATE_BEFORE" 2>/dev/null

echo
echo "========================================"
echo " 第 2 步 · 扫码"
echo "========================================"
# 4000 被别的东西占着的话，下面那句 curl 会连上一个不是 WeWe 的服务，
# 报错会变得莫名其妙。先说清楚谁在占。
if command -v lsof >/dev/null 2>&1 && lsof -nP -iTCP:4000 -sTCP:LISTEN >/dev/null 2>&1; then
  printf "%s! 4000 端口已经有东西在听：%s\n" "$YELLOW" "$NC"
  lsof -nP -iTCP:4000 -sTCP:LISTEN | sed 's/^/    /'
  printf "%s  如果那是上一次没关干净的 WeWe，可以直接用；不是的话先把它停掉。%s\n\n" "$YELLOW" "$NC"
fi
printf "%s接下来会打开 WeWe 的账号页。点「添加读书账号」，用微信扫二维码。%s\n" "$YELLOW" "$NC"
printf "%s扫完这个窗口会自己往下走，别关。%s\n" "$YELLOW" "$NC"
printf "%s浏览器没自动弹出来的话，手动打开：http://127.0.0.1:4000/dash/accounts%s\n\n" "$YELLOW" "$NC"

if bash scripts/wewe_scan_and_refresh.sh; then
  RESULT=0
else
  RESULT=$?
fi

# 服务起不来时，真正的原因在这个日志里，而屏幕上只会看到一句
# "did not start on ports 4000-4005"。直接摊开，并留一份到 data/logs/。
LOGIN_LOG="${TMPDIR:-/tmp}/practice-xiaoda-wewe-login.log"
if [ -f "$LOGIN_LOG" ]; then
  cp "$LOGIN_LOG" "$LOG_DIR/wewe-login.log" 2>/dev/null
  if [ "$RESULT" != "0" ]; then
    echo
    printf "%s服务端日志（最后 25 行）：%s\n" "$YELLOW" "$NC"
    tail -25 "$LOGIN_LOG" | sed 's/^/    /'
  fi
fi

echo
echo "========================================"
echo " 第 3 步 · 刷新后的变化"
echo "========================================"
python3 scripts/wewe_state.py
python3 scripts/wewe_state.py --json > "$STATE_AFTER" 2>/dev/null

python3 - "$STATE_BEFORE" "$STATE_AFTER" <<'PY'
import json, sys
try:
    before = json.load(open(sys.argv[1], encoding="utf-8"))
    after = json.load(open(sys.argv[2], encoding="utf-8"))
except Exception:
    raise SystemExit
gained = after["total_articles"] - before["total_articles"]
print()
if gained > 0:
    print(f"  这次新增 {gained} 篇缓存文章。")
    old = {f["mp_name"]: f["articles"] for f in before["feeds"]}
    for feed in after["feeds"]:
        delta = feed["articles"] - old.get(feed["mp_name"], 0)
        if delta:
            print(f"    {feed['mp_name']}  +{delta}")
else:
    print("  这次一篇新的都没有。要么本来就没有新文章，要么账号状态又回到 0——")
    print("  看上面【微信读书账号】那一行。若又是 0，今天就到此为止，别重试。")
PY

echo
if [ "$RESULT" = "3" ]; then
  printf "%s! 15 分钟内没等到扫码，本次没有刷新。%s\n" "$YELLOW" "$NC"
elif [ "$RESULT" != "0" ]; then
  printf "%s! 刷新脚本以退出码 %s 结束，上面的输出里有原因。%s\n" "$YELLOW" "$RESULT" "$NC"
else
  printf "%s✓ 跑完了%s\n" "$GREEN" "$NC"
fi

echo
echo "接下来一般是这两步（在终端里）："
echo "  python3 scripts/wewe_export_handoff.py --help    # 导出要投稿的文章"
echo "  python3 scripts/import_articles.py 文件.jsonl --check"
echo
printf "按回车关闭窗口。"
read -r _
