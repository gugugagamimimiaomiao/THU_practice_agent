#!/bin/bash
# 双击运行：查清 WeWe 本地服务为什么起不来，能自动修的就修。
# 只读诊断在前，任何会改动东西的操作都会先问你一句。
cd "$(dirname "$0")/.." || exit 1

GREEN=$'\033[0;32m'; YELLOW=$'\033[1;33m'; RED=$'\033[0;31m'; NC=$'\033[0m'
ok()   { printf "%s✓ %s%s\n" "$GREEN" "$1" "$NC"; }
warn() { printf "%s! %s%s\n" "$YELLOW" "$1" "$NC"; }
bad()  { printf "%s✗ %s%s\n" "$RED" "$1" "$NC"; }
ROOT="$(pwd)"
DB="${WEWE_DB_PATH:-$ROOT/data/wewe-rss.db}"
SERVER_DIR="${WEWE_SERVER_DIR:-/private/tmp/wewe-rss-eval/apps/server}"
LOG="${TMPDIR:-/tmp}/practice-xiaoda-wewe-login.log"

echo "========================================"
echo " WeWe 起不来 · 诊断"
echo "========================================"

echo
echo "【1. 启动日志】$LOG"
if [ -f "$LOG" ]; then
  tail -15 "$LOG" | sed 's/^/  /'
else
  warn "没有这个日志文件"
fi

echo
echo "【2. WeWe 服务端程序】$SERVER_DIR"
MISSING=0
if [ ! -d "$SERVER_DIR" ]; then
  bad "整个目录都不在了"
  MISSING=1
else
  for FILE in dist/main.js client/index.hbs; do
    if [ -f "$SERVER_DIR/$FILE" ]; then
      ok "$FILE 在"
    else
      bad "缺 $FILE"
      MISSING=1
    fi
  done
fi
if [ "$MISSING" = "1" ]; then
  warn "它装在 /private/tmp 下面 —— macOS 会定期清理 /tmp，重启后基本必丢。"
  warn "这就是「起不来」的原因：数据库还在，跑数据库的那个程序没了。"
fi

echo
echo "【3. 数据库】$DB"
if [ -f "$DB" ]; then
  CACHED=$(python3 - "$DB" 2>/dev/null <<'PYEOF'
import sqlite3, sys
try:
    c = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
    print(c.execute("SELECT COUNT(*) FROM articles").fetchone()[0])
except Exception:
    print("?")
PYEOF
)
  ok "在，$(du -h "$DB" | awk '{print $1}')，缓存 ${CACHED} 篇文章 —— 数据没丢"
else
  bad "不在"
fi

echo
echo "【4. 手上有什么工具】"
for TOOL in node npm pnpm docker git; do
  if command -v "$TOOL" >/dev/null 2>&1; then
    ok "$TOOL  $("$TOOL" --version 2>&1 | head -1)"
  else
    warn "$TOOL  没有"
  fi
done
DOCKER_UP=0
DOCKER_INSTALLED=0
if command -v docker >/dev/null 2>&1; then
  DOCKER_INSTALLED=1
  if docker info >/dev/null 2>&1; then
    DOCKER_UP=1
    ok "Docker 正在运行"
  else
    warn "装了 Docker，但守护进程没起来"
  fi
fi

echo
echo "【5. 别处还有没有 WeWe 的副本】"
FOUND=""
for CANDIDATE in /private/tmp/wewe-rss-eval "$HOME/wewe-rss" "$HOME/Desktop/wewe-rss" \
                 "$HOME/Downloads/wewe-rss" "$ROOT/../wewe-rss" /tmp/wewe-rss-eval; do
  [ -f "$CANDIDATE/apps/server/dist/main.js" ] && { FOUND="$CANDIDATE"; break; }
done
if [ -n "$FOUND" ]; then
  ok "找到一份可用的：$FOUND"
else
  warn "常见位置里没有找到"
fi

echo
echo "========================================"
echo " 结论与选择"
echo "========================================"

if [ "$MISSING" = "0" ]; then
  echo "  程序还在，起不来多半是端口或别的原因 —— 把上面第 1 段日志发出来。"
  printf "\n按回车关闭。"; read -r _; exit 0
fi

if [ -n "$FOUND" ]; then
  echo "  找到了别处的副本。以后用这条命令启动就行（也可以让我把它写进脚本默认值）："
  echo "    WEWE_SERVER_DIR=\"$FOUND/apps/server\" bash scripts/wewe_scan_and_refresh.sh"
  printf "\n按回车关闭。"; read -r _; exit 0
fi

if [ "$DOCKER_UP" = "1" ]; then
  echo "  程序没了，但你有 Docker —— 可以用官方镜像跑，数据库仍然用你本地这一份，"
  echo "  已缓存的 ${CACHED} 篇文章和全部订阅都保留。这是最省事的一条路。"
  echo
  echo "  要执行的是："
  echo "    docker run -d --name wewe-rss -p 4000:4000 \\"
  echo "      -v \"$ROOT/data\":/app/data \\"
  echo "      -e DATABASE_TYPE=sqlite -e DATABASE_URL=file:/app/data/wewe-rss.db \\"
  echo "      -e SERVER_ORIGIN_URL=http://127.0.0.1:4000 \\"
  echo "      -e PLATFORM_URL=https://weread.111965.xyz \\"
  echo "      -e FEED_MODE=fulltext -e MAX_REQUEST_PER_MINUTE=30 \\"
  echo "      -e AUTH_CODE=<自动生成的随机码> cooderl/wewe-rss-sqlite:latest"
  echo
  printf "  现在就跑吗？(y/N) "
  read -r ANSWER
  if [ "$ANSWER" = "y" ] || [ "$ANSWER" = "Y" ]; then
    AUTH_CODE=$(LC_ALL=C tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 40)
    if ! grep -q '^WEWE_AUTH_CODE=' .env 2>/dev/null; then
      printf '\nWEWE_AUTH_CODE=%s\n' "$AUTH_CODE" >> .env
      chmod 600 .env 2>/dev/null
      ok "已把 WEWE_AUTH_CODE 写进 .env（本机保存，别外传）"
    else
      AUTH_CODE=$(grep '^WEWE_AUTH_CODE=' .env | tail -1 | cut -d= -f2-)
    fi
    docker rm -f wewe-rss >/dev/null 2>&1
    if docker run -d --name wewe-rss -p 4000:4000 \
        -v "$ROOT/data":/app/data \
        -e DATABASE_TYPE=sqlite -e DATABASE_URL=file:/app/data/wewe-rss.db \
        -e SERVER_ORIGIN_URL=http://127.0.0.1:4000 \
        -e PLATFORM_URL=https://weread.111965.xyz \
        -e FEED_MODE=fulltext -e MAX_REQUEST_PER_MINUTE=30 \
        -e AUTH_CODE="$AUTH_CODE" \
        cooderl/wewe-rss-sqlite:latest >/dev/null; then
      echo "  等它就绪…"
      for _ in $(seq 1 40); do
        curl -fsS --max-time 2 http://127.0.0.1:4000/feeds >/dev/null 2>&1 && break
        sleep 1
      done
      if curl -fsS --max-time 2 http://127.0.0.1:4000/feeds >/dev/null 2>&1; then
        ok "WeWe 起来了：http://127.0.0.1:4000"
        echo "  接着双击「微信读书扫码并更新.command」，它会跳过启动直接等你扫码。"
        open "http://127.0.0.1:4000/dash/accounts" >/dev/null 2>&1
      else
        bad "容器起了但没响应，看日志：docker logs wewe-rss"
      fi
    else
      bad "docker run 失败，把上面的报错发出来"
    fi
  else
    echo "  没跑。想跑的时候再双击一次这个文件。"
  fi
  printf "\n按回车关闭。"; read -r _; exit 0
fi

if [ "$DOCKER_INSTALLED" = "1" ]; then
  echo "  程序没了。你装了 Docker 但它没在运行 —— 最省事的一条路："
  echo "    打开 Docker Desktop，等它启动完，再双击一次这个文件，"
  echo "    它会自动用官方镜像把服务起起来（数据库仍用你本地这份，缓存不丢）。"
  echo
  echo "  其它两条："
  echo "    - 别的地方还留着 wewe-rss 构建产物的话，告诉我路径"
  echo "    - 从源码重建：上游仓库已归档，需要 node + pnpm，最费事"
  echo
  echo "  在这之前，WeWe 这条链路是停的。要补数据的话，可以先用"
  echo "  wechat-download-api 的 --links 模式按链接抓正文（见 WDA_DEPLOYMENT.md）。"
  printf "\n按回车关闭。"; read -r _; exit 0
fi

echo "  程序没了，本机也没有 Docker。三条路，从省事到麻烦："
echo "    1) 装 Docker Desktop，再双击一次这个文件，它会自动用官方镜像起服务"
echo "       （数据库还是你本地这份，缓存不丢）"
echo "    2) 如果你别的地方还留着 wewe-rss 的构建产物，告诉我路径"
echo "    3) 从源码重建 —— 上游仓库已归档，需要 node + pnpm，最费事"
echo
echo "  在这之前，WeWe 这条链路是停的。要补数据的话，可以先用"
echo "  wechat-download-api 的 --links 模式按链接抓正文（见 WDA_DEPLOYMENT.md）。"
printf "\n按回车关闭。"; read -r _
