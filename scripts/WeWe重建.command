#!/bin/bash
# 双击运行：把 WeWe 的服务端从源码重建到一个**不会被系统清理**的位置。
# 原来那份装在 /private/tmp 下，macOS 清 /tmp 时连人带货一起没了。
# 这次装到 ~/Desktop/agent/wewe-rss-src，重启也不丢。
cd "$(dirname "$0")/.." || exit 1

GREEN=$'\033[0;32m'; YELLOW=$'\033[1;33m'; RED=$'\033[0;31m'; NC=$'\033[0m'
ok()   { printf "%s✓ %s%s\n" "$GREEN" "$1" "$NC"; }
warn() { printf "%s! %s%s\n" "$YELLOW" "$1" "$NC"; }
die()  { printf "%s✗ %s%s\n" "$RED" "$1" "$NC"; printf "\n按回车关闭。"; read -r _; exit 1; }

ROOT="$(pwd)"
SRC="${WEWE_SRC_DIR:-$(cd .. && pwd)/wewe-rss-src}"
SERVER="$SRC/apps/server"
export PRISMA_ENGINES_CHECKSUM_IGNORE_MISSING=1

echo "========================================"
echo " 从源码重建 WeWe 服务端"
echo " 目标：$SRC"
echo "========================================"
echo
warn "整个过程 5～15 分钟，取决于网速。中间别关窗口。"
echo

# --- 1. node -----------------------------------------------------------
if ! command -v node >/dev/null 2>&1; then
  echo "  没有 node。装一个再来，二选一："
  echo "    brew install node        # 有 Homebrew 的话"
  echo "    或去 https://nodejs.org 下 LTS 版安装包"
  die "缺 node"
fi
NODE_MAJOR=$(node -v | sed 's/^v//' | cut -d. -f1)
if [ "$NODE_MAJOR" -lt 20 ]; then
  die "node 版本是 $(node -v)，这个项目要求 >= 20.9。升级后再双击。"
fi
ok "node $(node -v)"

# --- 2. pnpm -----------------------------------------------------------
# 项目要求 pnpm >= 8。pnpm 10 对这份 v6 lockfile 的处理不一样，钉在 8 最稳。
if ! command -v pnpm >/dev/null 2>&1; then
  echo "  没有 pnpm，正在装…"
  corepack enable >/dev/null 2>&1
  if ! command -v pnpm >/dev/null 2>&1; then
    npm i -g pnpm@8.15.9 >/dev/null 2>&1 || die "pnpm 装不上，手动跑一次：npm i -g pnpm@8.15.9"
  fi
fi
ok "pnpm $(pnpm -v)"

# --- 3. 源码 -----------------------------------------------------------
if [ -d "$SRC/.git" ]; then
  ok "源码已经在了：$SRC"
else
  echo "  正在下载源码…"
  git clone --depth 1 https://github.com/cooderl/wewe-rss.git "$SRC" 2>&1 | tail -2 ||
    die "下载失败。检查网络；用代理的话先把代理软件打开。"
  ok "源码就位"
fi
cd "$SRC" || die "进不去 $SRC"

# --- 4. 依赖 -----------------------------------------------------------
echo "  正在装依赖（最慢的一步）…"
if ! pnpm install --frozen-lockfile 2>&1 | tail -5; then
  warn "带 --frozen-lockfile 失败，退回普通安装再试一次"
  pnpm install 2>&1 | tail -5 || die "依赖装不上，把上面的报错发出来"
fi
ok "依赖就绪"

# --- 5. 前端 -----------------------------------------------------------
# 上游 package.json 里的 build 是 `tsc && vite build`，而当前 HEAD 有一处
# TS 报错（feeds/index.tsx 的 possibly undefined），跑 tsc 必失败。
# vite build 不做类型检查，产物一样，所以直接用它。
echo "  正在编译前端…"
( cd apps/web && npx vite build >/dev/null 2>&1 ) || die "前端编译失败"
[ -f "$SERVER/client/index.hbs" ] || die "前端编译完了但没看到 client/index.hbs"
ok "前端就绪（client/index.hbs）"

# --- 6. Prisma（必须用 sqlite 那份 schema）-------------------------------
# 默认 schema 是 mysql；装依赖时的 postinstall 生成的也是 mysql 版，
# 直接拿来连 sqlite 会在运行时报 provider 不匹配。
echo "  正在生成 Prisma 客户端（sqlite）…"
( cd "$SERVER" && npx prisma generate --schema=prisma-sqlite/schema.prisma 2>&1 | tail -3 ) ||
  die "prisma generate 失败。它要从 binaries.prisma.sh 下引擎，被墙或断网都会卡在这。"
ok "Prisma 客户端就绪"

# --- 7. 后端 -----------------------------------------------------------
echo "  正在编译后端…"
( cd "$SERVER" && npx nest build >/dev/null 2>&1 ) || die "后端编译失败"
[ -f "$SERVER/dist/main.js" ] || die "编译完了但没看到 dist/main.js"
ok "后端就绪（dist/main.js）"

# --- 8. 收尾 -----------------------------------------------------------
echo
echo "========================================"
ok "重建完成"
echo "========================================"
echo
echo "  位置：$SERVER"
echo "  这个目录在 Desktop 下面，不会被系统清理，重启也还在。"
echo
echo "  接下来双击「微信读书扫码并更新.command」——它会自动找到这份新构建，"
echo "  起服务、等你扫码、跑当天的增量导入。"
echo
echo "  想手动启动的话："
echo "    WEWE_SERVER_DIR=\"$SERVER\" bash scripts/wewe_scan_and_refresh.sh"
echo
printf "按回车关闭。"
read -r _
