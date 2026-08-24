#!/bin/bash
# 双击运行：看一眼 WeWe 现状 → 起本地服务并等你扫码 → 跑完当天的增量导入 → 再看一眼变化。
# 只是把 scripts/wewe_scan_and_refresh.sh 包一层，方便双击；逻辑还在那个脚本里。
cd "$(dirname "$0")/.." || exit 1

GREEN=$'\033[0;32m'; YELLOW=$'\033[1;33m'; RED=$'\033[0;31m'; NC=$'\033[0m'
STATE_BEFORE="${TMPDIR:-/tmp}/wewe-state-before.json"
STATE_AFTER="${TMPDIR:-/tmp}/wewe-state-after.json"

command -v python3 >/dev/null 2>&1 || {
  printf "%s✗ 没找到 python3，先在终端跑 xcode-select --install%s\n" "$RED" "$NC"
  printf "\n按回车关闭。"; read -r _; exit 1
}

echo "========================================"
echo " 第 1 步 · 刷新前的现状"
echo "========================================"
python3 scripts/wewe_state.py
python3 scripts/wewe_state.py --json > "$STATE_BEFORE" 2>/dev/null

echo
echo "========================================"
echo " 第 2 步 · 扫码"
echo "========================================"
printf "%s接下来会打开 WeWe 的账号页。点「添加读书账号」，用微信扫二维码。%s\n" "$YELLOW" "$NC"
printf "%s扫完这个窗口会自己往下走，别关。%s\n\n" "$YELLOW" "$NC"

if bash scripts/wewe_scan_and_refresh.sh; then
  RESULT=0
else
  RESULT=$?
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
