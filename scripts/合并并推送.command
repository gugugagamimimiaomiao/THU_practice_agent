#!/bin/bash
# 双击运行：把远端的更新合进来 → 跑全量测试 → 确认后推送。
# 用的是 merge 不是 rebase：别人的 23 个提交原样保留，不改写任何人的历史。
cd "$(dirname "$0")/.." || exit 1

GREEN=$'\033[0;32m'; YELLOW=$'\033[1;33m'; RED=$'\033[0;31m'; NC=$'\033[0m'
ok()   { printf "%s✓ %s%s\n" "$GREEN" "$1" "$NC"; }
warn() { printf "%s! %s%s\n" "$YELLOW" "$1" "$NC"; }
die()  { printf "%s✗ %s%s\n" "$RED" "$1" "$NC"; printf "\n按回车关闭。"; read -r _; exit 1; }

LOG_DIR="$(pwd)/data/logs"; mkdir -p "$LOG_DIR"
exec > >(tee "$LOG_DIR/merge-push.log") 2>&1

echo "========================================"
echo " 合并远端更新 → 测试 → 推送"
echo "========================================"
echo

# --- 代理：死的清掉，否则 fetch/push 连不上 GitHub -------------------------
export no_proxy="localhost,127.0.0.1,::1${no_proxy:+,$no_proxy}"; export NO_PROXY="$no_proxy"
port_alive() { (exec 3<>"/dev/tcp/$1/$2") 2>/dev/null && exec 3<&- 3>&- && return 0; return 1; }
GIT_NP=(); DEAD=""; SEEN=""
for VALUE in "${https_proxy:-}" "${HTTPS_PROXY:-}" "${http_proxy:-}" "${HTTP_PROXY:-}" \
             "${all_proxy:-}" "${ALL_PROXY:-}" "$(git config --global --get http.proxy 2>/dev/null)"; do
  [ -z "$VALUE" ] && continue
  HP="${VALUE#*://}"; HP="${HP%%/*}"; HP="${HP##*@}"
  case " $SEEN " in *" $HP "*) continue;; esac
  SEEN="$SEEN $HP"; PH="${HP%%:*}"; PP="${HP##*:}"; [ "$PP" = "$PH" ] && PP=8080
  port_alive "$PH" "$PP" || DEAD="$DEAD $PH:$PP"
done
if [ -n "$DEAD" ]; then
  warn "代理$DEAD 没人监听，本次运行绕过（不动 ~/.zshrc 和 ~/.gitconfig）"
  unset https_proxy HTTPS_PROXY http_proxy HTTP_PROXY all_proxy ALL_PROXY
  GIT_NP=(-c http.proxy= -c https.proxy=)
fi

# --- 1. 先确认工作区是干净的 ----------------------------------------------
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  git status --short
  die "还有没提交的改动。先处理掉再合并 —— 合并中途出问题的话，未提交的东西最难救。"
fi
ok "工作区干净，当前在 $(git rev-parse --abbrev-ref HEAD)"

# --- 2. 取远端 -----------------------------------------------------------
echo
echo "  取远端…"
git "${GIT_NP[@]}" fetch origin main || die "连不上 GitHub。把代理软件打开再试。"
BEHIND=$(git rev-list --count HEAD..origin/main)
AHEAD=$(git rev-list --count origin/main..HEAD)
ok "本地领先 $AHEAD 个提交，落后 $BEHIND 个"
[ "$BEHIND" = "0" ] && [ "$AHEAD" = "0" ] && { ok "已经同步，无事可做"; printf "\n按回车关闭。"; read -r _; exit 0; }

# --- 3. 合并 -------------------------------------------------------------
if [ "$BEHIND" != "0" ]; then
  echo
  echo "  合并（保留双方历史，不改写别人的提交）…"
  git merge origin/main --no-edit -m "merge: 合入远端 $BEHIND 个提交" 2>&1 | tail -8

  # 唯一预期的冲突是 .gitignore：两边各加了几行，取并集即可。
  if git diff --name-only --diff-filter=U | grep -qx ".gitignore"; then
    cat > .gitignore <<'IGNORE'
__pycache__/
*.py[cod]
.DS_Store
data/*.db
data/*.db-*
data/collector_settings.json
data/collector_audits/
data/exports/
data/collection_quality_report.json
data/collection_quality_samples.jsonl
data/wechat_backfill_state.json
data/logs/
data/wda_state.json
.env

# 临时脚本、探测日志、跑出来的中间产物一律放这里，不入库。
tmp/
IGNORE
    git add .gitignore
    ok ".gitignore 冲突已按并集解好（两边的规则都保留）"
  fi

  REST=$(git diff --name-only --diff-filter=U)
  if [ -n "$REST" ]; then
    echo; printf "%s还有冲突要人工处理：%s\n" "$YELLOW" "$NC"; printf "%s\n" "$REST" | sed 's/^/    /'
    echo "  处理完跑：git add -A && git commit --no-edit，然后重新双击本文件。"
    echo "  想放弃这次合并：git merge --abort"
    die "有未解决的冲突"
  fi

  if [ -f .git/MERGE_HEAD ]; then
    git commit -q --no-edit || die "合并提交失败"
  fi
  ok "合并完成"
fi

# --- 4. 测试 -------------------------------------------------------------
echo
echo "  跑全量测试…"
TEST_OUT=$(python3 -m unittest discover -s tests -t . 2>&1)
printf "%s\n" "$TEST_OUT" | grep -E "^(Ran |OK|FAILED|ERROR)"
printf "%s" "$TEST_OUT" | grep -q "^OK" || {
  printf "%s\n" "$TEST_OUT" | grep -A5 -E "^(FAIL|ERROR):" | head -30
  die "测试没过，不推。"
}
ok "测试全过"

# --- 5. 确认后推送 -------------------------------------------------------
echo
echo "========================================"
echo " 将要推送的提交（远端还没有的）"
echo "========================================"
git log --oneline origin/main..HEAD | sed 's/^/  /'
echo
echo "  远端：$(git remote get-url origin)"
printf "  确认推送吗？(y/N) "
read -r ANSWER
case "$ANSWER" in
  y|Y)
    git "${GIT_NP[@]}" push origin HEAD:main && ok "推送成功" || die "推送失败，原因在上面"
    ;;
  *)
    warn "没推。合并结果已经在本地了，想推的时候再双击一次，或者跑 git push origin HEAD:main"
    ;;
esac

printf "\n按回车关闭。"
read -r _
