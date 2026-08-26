#!/bin/bash
# 双击运行：给 GitHub 配一把 SSH key。
# 已经有 key 就不重复生成；公钥会自动复制到剪贴板并打开 GitHub 的添加页面。
cd "$(dirname "$0")/.." || exit 1

GREEN=$'\033[0;32m'; YELLOW=$'\033[1;33m'; RED=$'\033[0;31m'; NC=$'\033[0m'
ok()   { printf "%s✓ %s%s\n" "$GREEN" "$1" "$NC"; }
warn() { printf "%s! %s%s\n" "$YELLOW" "$1" "$NC"; }
die()  { printf "%s✗ %s%s\n" "$RED" "$1" "$NC"; printf "\n按回车关闭。"; read -r _; exit 1; }

KEY="$HOME/.ssh/id_ed25519"
CONFIG="$HOME/.ssh/config"

echo "========================================"
echo " 给 GitHub 配 SSH key"
echo "========================================"
echo

# 代理死着的话，最后那步连接测试会连不上 github.com
export no_proxy="localhost,127.0.0.1,::1${no_proxy:+,$no_proxy}"; export NO_PROXY="$no_proxy"
port_alive() { (exec 3<>"/dev/tcp/$1/$2") 2>/dev/null && exec 3<&- 3>&- && return 0; return 1; }
for VALUE in "${https_proxy:-}" "${all_proxy:-}" "${ALL_PROXY:-}"; do
  [ -z "$VALUE" ] && continue
  HP="${VALUE#*://}"; HP="${HP%%/*}"; HP="${HP##*@}"
  PH="${HP%%:*}"; PP="${HP##*:}"; [ "$PP" = "$PH" ] && PP=8080
  port_alive "$PH" "$PP" || { warn "代理 $PH:$PP 没人监听，本次绕过"; unset https_proxy HTTPS_PROXY http_proxy HTTP_PROXY all_proxy ALL_PROXY; break; }
done

command -v ssh-keygen >/dev/null 2>&1 || die "没有 ssh-keygen。先在终端跑 xcode-select --install，装完再双击。"
mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh"

# --- 1. 已经有 key 吗 ----------------------------------------------------
echo "【1. 看看有没有现成的 key】"
FOUND=""
for K in "$HOME/.ssh"/id_*; do
  case "$K" in *.pub) continue;; esac
  [ -f "$K" ] && { echo "    有：$K"; FOUND="$K"; }
done
if [ -n "$FOUND" ] && [ -f "$KEY" ]; then
  ok "已有 $KEY，不重复生成"
elif [ -n "$FOUND" ]; then
  KEY="$FOUND"
  ok "用现成的 $KEY"
else
  echo "    一把都没有，现在生成"
  echo
  printf "    留个邮箱当标注（直接回车用 %s）：" "$(git config --global user.email 2>/dev/null || echo your@email.com)"
  read -r EMAIL
  [ -z "$EMAIL" ] && EMAIL=$(git config --global user.email 2>/dev/null || echo "your@email.com")
  echo
  warn "接下来会问你密码短语（passphrase）："
  echo "    直接按两次回车 = 不设密码，之后 push 不用输任何东西，最省事"
  echo "    设了密码 = 更安全，但下面会存进钥匙串，平时也不用反复输"
  echo
  ssh-keygen -t ed25519 -C "$EMAIL" -f "$KEY" || die "生成失败"
  ok "生成好了：$KEY"
fi

# --- 2. ssh-agent 和钥匙串 ------------------------------------------------
echo
echo "【2. 交给 ssh-agent 和钥匙串保管】"
eval "$(ssh-agent -s)" >/dev/null 2>&1
if ! grep -q "Host github.com" "$CONFIG" 2>/dev/null; then
  cat >> "$CONFIG" <<EOF

Host github.com
  AddKeysToAgent yes
  UseKeychain yes
  IdentityFile $KEY
EOF
  chmod 600 "$CONFIG"
  ok "已写入 ~/.ssh/config"
else
  ok "~/.ssh/config 里已经有 github.com 的配置"
fi
ssh-add --apple-use-keychain "$KEY" >/dev/null 2>&1 && ok "已加入钥匙串" || warn "加入钥匙串这步没成，不影响后面，push 时可能要输一次密码短语"

# --- 3. 复制公钥、打开 GitHub --------------------------------------------
echo
echo "【3. 把公钥加到 GitHub】"
if [ ! -f "$KEY.pub" ]; then die "找不到公钥 $KEY.pub"; fi
pbcopy < "$KEY.pub" 2>/dev/null && ok "公钥已复制到剪贴板（直接 Cmd+V 粘贴即可）" || warn "自动复制失败，下面这一整行手动复制："
echo
echo "----- 公钥内容（就是这一行）-----"
cat "$KEY.pub"
echo "--------------------------------"
echo
echo "  浏览器这就打开 GitHub 的添加页面。操作："
echo "    Title 随便填（比如 MacBook）→ Key 那个框粘贴 → 点 Add SSH key"
open "https://github.com/settings/ssh/new" >/dev/null 2>&1 || echo "    没自动打开就手动访问：https://github.com/settings/ssh/new"
echo
printf "  加完了按回车，我来测连接…"
read -r _

# --- 4. 测连接 -----------------------------------------------------------
echo
echo "【4. 测试连接】"
OUT=$(ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 -T git@github.com 2>&1)
printf "    %s\n" "$OUT" | head -3
if printf "%s" "$OUT" | grep -q "successfully authenticated"; then
  WHO=$(printf "%s" "$OUT" | sed -n 's/^Hi \([^!]*\)!.*/\1/p')
  ok "通了，GitHub 认出你是：$WHO"
  REMOTE=$(git remote get-url origin 2>/dev/null)
  OWNER=$(printf "%s" "$REMOTE" | sed -E 's#.*[:/]([^/]+)/[^/]+(\.git)?$#\1#')
  echo
  echo "  当前仓库地址：$REMOTE"
  echo "  仓库所有者：$OWNER    你的 GitHub 账号：$WHO"
  echo
  if [ -n "$WHO" ] && [ "$WHO" = "$OWNER" ]; then
    ok "账号和仓库所有者对得上，换成 SSH 就能推："
    echo "    git remote set-url origin git@github.com:$OWNER/$(basename "${REMOTE%.git}").git"
    echo "    git push origin HEAD:main"
  else
    warn "账号和仓库所有者对不上 —— 这才是推不上去的真正原因，换 SSH 也没用。"
    echo "    要么让 $OWNER 把你加成协作者，"
    echo "    要么把 origin 换成你自己名下的那个仓库："
    echo "      git remote set-url origin git@github.com:$WHO/$(basename "${REMOTE%.git}").git"
    echo "      git push origin HEAD:main"
  fi
  echo
  echo "  或者直接双击「合并并推送.command」，它会自己发现 SSH 通了并问你要不要切。"
else
  warn "还没通。常见原因：公钥没加成功、加到了别的账号、或者网络被挡。"
  echo "    确认一下 https://github.com/settings/keys 里能看到这把 key。"
fi

printf "\n按回车关闭。"
read -r _
