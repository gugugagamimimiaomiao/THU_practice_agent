#!/bin/bash
# 双击运行：把 WeWe 的服务端建到一个**不会被系统清理**的位置。
# 原来那份装在 /private/tmp 下，macOS 清 /tmp 时连人带货一起没了。
# 这次装到 ~/Desktop/agent/wewe-rss-src，重启也不丢。
cd "$(dirname "$0")/.." || exit 1

GREEN=$'\033[0;32m'; YELLOW=$'\033[1;33m'; RED=$'\033[0;31m'; NC=$'\033[0m'
ok()   { printf "%s✓ %s%s\n" "$GREEN" "$1" "$NC"; }
warn() { printf "%s! %s%s\n" "$YELLOW" "$1" "$NC"; }
die()  { printf "%s✗ %s%s\n" "$RED" "$1" "$NC"; printf "\n按回车关闭。"; read -r _; exit 1; }

ROOT="$(pwd)"
AGENT="$(cd .. && pwd)"
SRC="${WEWE_SRC_DIR:-$AGENT/wewe-rss-src}"
SERVER="$SRC/apps/server"
TARBALL="$AGENT/wewe-rss-src.tar.gz"

echo "========================================"
echo " 重建 WeWe 服务端"
echo " 目标：$SRC"
echo "========================================"
echo

# --- 0. 代理自检 ---------------------------------------------------------
# 本机代理（Clash 之类）常年写在 ~/.zshrc 和 ~/.gitconfig 里。代理软件一关，
# 配置还在，于是 git / npm 全都发往一个没人监听的端口。上一次就栽在这。
export no_proxy="localhost,127.0.0.1,::1${no_proxy:+,$no_proxy}"
export NO_PROXY="$no_proxy"
port_alive() { (exec 3<>"/dev/tcp/$1/$2") 2>/dev/null && exec 3<&- 3>&- && return 0; return 1; }

GIT_PROXY_ARGS=()
PROXY_DEAD=""
PROXY_LIVE=""
SEEN=""
for VALUE in "${https_proxy:-}" "${HTTPS_PROXY:-}" "${http_proxy:-}" "${HTTP_PROXY:-}" \
             "${all_proxy:-}" "${ALL_PROXY:-}" "$(git config --global --get http.proxy 2>/dev/null)" \
             "$(npm config get proxy 2>/dev/null | grep -v '^null$')"; do
  [ -z "$VALUE" ] && continue
  HP="${VALUE#*://}"; HP="${HP%%/*}"; HP="${HP##*@}"
  case " $SEEN " in *" $HP "*) continue;; esac
  SEEN="$SEEN $HP"
  PH="${HP%%:*}"; PP="${HP##*:}"; [ "$PP" = "$PH" ] && PP=8080
  if port_alive "$PH" "$PP"; then PROXY_LIVE="$PROXY_LIVE $PH:$PP"; else PROXY_DEAD="$PROXY_DEAD $PH:$PP"; fi
done

if [ -n "$PROXY_DEAD" ]; then
  warn "配置里的代理$PROXY_DEAD 没人监听（代理软件没开？），本次运行绕过它"
  warn "不改你的 ~/.zshrc 和 ~/.gitconfig"
  unset https_proxy HTTPS_PROXY http_proxy HTTP_PROXY all_proxy ALL_PROXY
  # git 和 npm 各有各的配置文件，光清环境变量不够，得在命令行上压掉。
  GIT_PROXY_ARGS=(-c http.proxy= -c https.proxy=)
  export npm_config_proxy="" npm_config_https_proxy=""
elif [ -n "$PROXY_LIVE" ]; then
  ok "代理$PROXY_LIVE 活着，走它"
fi

# --- 1. node -------------------------------------------------------------
command -v node >/dev/null 2>&1 || die "没有 node。brew install node，或去 https://nodejs.org 下 LTS 装。"
NODE_MAJOR=$(node -v | sed 's/^v//' | cut -d. -f1)
[ "$NODE_MAJOR" -lt 20 ] && die "node 是 $(node -v)，这个项目要求 >= 20.9。"
ok "node $(node -v)"

# --- 2. pnpm -------------------------------------------------------------
if ! command -v pnpm >/dev/null 2>&1; then
  echo "  没有 pnpm，正在装…"
  corepack enable >/dev/null 2>&1
  command -v pnpm >/dev/null 2>&1 || npm i -g pnpm@8.15.9 >/dev/null 2>&1
  command -v pnpm >/dev/null 2>&1 || die "pnpm 装不上，手动跑：npm i -g pnpm@8.15.9"
fi
ok "pnpm $(pnpm -v)"

# --- 3. 源码 -------------------------------------------------------------
# 优先用本地压缩包：里面已经带了编译好的前端，省掉一次 GitHub 访问和一步编译。
if [ -f "$SERVER/package.json" ]; then
  ok "源码已经在了：$SRC"
elif [ -f "$TARBALL" ]; then
  echo "  正在解压本地源码包（已含编译好的前端）…"
  mkdir -p "$SRC"
  # --strip-components=1 去掉包里那层 wewe-rss/，直接落进 $SRC，不留空壳目录
  tar xzf "$TARBALL" -C "$SRC" --strip-components=1 || die "解压失败：$TARBALL"
  [ -f "$SERVER/package.json" ] || die "解压完了但目录结构不对，把 $SRC 删掉再试一次"
  ok "源码就位（来自本地包，没走 GitHub）"
else
  echo "  正在从 GitHub 下载源码…"
  if ! git "${GIT_PROXY_ARGS[@]}" clone --depth 1 https://github.com/cooderl/wewe-rss.git "$SRC"; then
    echo
    echo "  连不上 GitHub。两条路："
    echo "    1) 把代理软件（Clash 等）打开，再双击一次"
    echo "    2) 让我把源码包发给你，放到 $TARBALL，再双击一次就不用连 GitHub 了"
    die "下载失败"
  fi
  ok "源码就位"
fi
cd "$SRC" || die "进不去 $SRC"

# --- 4. 依赖 -------------------------------------------------------------
# prisma 的引擎从 binaries.prisma.sh 下，国内经常连不上；npmmirror 有镜像。
export PRISMA_ENGINES_CHECKSUM_IGNORE_MISSING=1
echo "  正在装依赖（最慢的一步，几分钟）…"
INSTALLED=0
for SOURCE in "默认源|" "npmmirror 镜像|https://registry.npmmirror.com"; do
  NAME="${SOURCE%%|*}"; URL="${SOURCE#*|}"
  echo "    → 试 $NAME"
  if [ -n "$URL" ]; then
    export PRISMA_ENGINES_MIRROR="https://registry.npmmirror.com/-/binary/prisma"
    REG_ARGS=(--registry "$URL")
  else
    REG_ARGS=()
  fi
  if pnpm install --frozen-lockfile "${REG_ARGS[@]}" 2>&1 | tail -4; then
    [ "${PIPESTATUS[0]}" = "0" ] && { INSTALLED=1; }
  fi
  if [ "$INSTALLED" != "1" ]; then
    warn "带 --frozen-lockfile 没成（pnpm 9 和这份旧 lockfile 常不合），换普通安装"
    if pnpm install "${REG_ARGS[@]}" 2>&1 | tail -4; then
      [ "${PIPESTATUS[0]}" = "0" ] && INSTALLED=1
    fi
  fi
  [ "$INSTALLED" = "1" ] && { ok "依赖装好了（$NAME）"; break; }
  warn "$NAME 不通，换下一个"
done
[ "$INSTALLED" = "1" ] || die "依赖装不上。把上面的报错发出来；如果是超时，多半还是代理的问题。"

# --- 5. 前端 -------------------------------------------------------------
# 上游 package.json 的 build 是 `tsc && vite build`，当前 HEAD 有一处 TS 报错
# （feeds/index.tsx possibly undefined），跑 tsc 必挂。vite build 不做类型检查，
# 产物一样。本地源码包里已经带了编译好的，这一步通常直接跳过。
if [ -f "$SERVER/client/index.hbs" ]; then
  ok "前端已经是编译好的，跳过"
else
  echo "  正在编译前端…"
  ( cd apps/web && npx vite build ) >/dev/null 2>&1 || die "前端编译失败"
  [ -f "$SERVER/client/index.hbs" ] || die "编译完了但没看到 client/index.hbs"
  ok "前端就绪"
fi

# --- 6. Prisma（必须用 sqlite 那份 schema）---------------------------------
# 默认 schema 是 mysql，postinstall 生成的也是 mysql 版，拿来连 sqlite 会在
# 运行时报 provider 不匹配。
echo "  正在生成 Prisma 客户端（sqlite）…"
if ! ( cd "$SERVER" && npx prisma generate --schema=prisma-sqlite/schema.prisma ) 2>&1 | tail -3; then
  die "prisma generate 失败"
fi
[ -d "$SERVER/node_modules/.prisma/client" ] || [ -d "$SRC/node_modules/.prisma/client" ] ||
  die "prisma generate 跑完了但没生成客户端。它要从 binaries.prisma.sh 下引擎，被墙或断网都会卡在这。"
ok "Prisma 客户端就绪"

# --- 7. 后端 -------------------------------------------------------------
echo "  正在编译后端…"
( cd "$SERVER" && npx nest build ) >/dev/null 2>&1 || die "后端编译失败"
[ -f "$SERVER/dist/main.js" ] || die "编译完了但没看到 dist/main.js"
ok "后端就绪（dist/main.js）"

echo
echo "========================================"
ok "重建完成"
echo "========================================"
echo
echo "  位置：$SERVER（在 Desktop 下面，不会被系统清理）"
echo
echo "  接下来双击「微信读书扫码并更新.command」，它会自动找到这份新构建，"
echo "  起服务、等你扫码、跑当天的增量导入。"
echo
printf "按回车关闭。"
read -r _
