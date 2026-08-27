#!/usr/bin/env bash
# Start a loopback-only WeWe login page, wait for the user's QR scan, then
# finish source onboarding and today's incremental import without exposing a
# token in terminal output or project configuration.
set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
database="${WEWE_DB_PATH:-$root_dir/data/wewe-rss.db}"
host="${WEWE_HOST:-127.0.0.1}"
port="${WEWE_PORT:-4000}"
started_here=false
server_pid=""
log_file="${TMPDIR:-/tmp}/practice-xiaoda-wewe-login.log"

if [[ "$host" != "127.0.0.1" ]]; then
  printf 'Refusing to open the login flow on %s; use 127.0.0.1.\n' "$host" >&2
  exit 2
fi
if [[ ! -f "$database" ]]; then
  printf 'WeWe database not found: %s\n' "$database" >&2
  exit 2
fi

cleanup() {
  if [[ "$started_here" == true && -n "$server_pid" ]] && kill -0 "$server_pid" 2>/dev/null; then
    kill "$server_pid" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

start_local_server() {
  WEWE_PORT="$port" "$root_dir/scripts/start_local_wewe.sh" >"$log_file" 2>&1 &
  server_pid=$!
  started_here=true
  for _ in {1..15}; do
    if curl -fsS --max-time 2 "http://$host:$port/feeds" >/dev/null 2>&1; then
      return 0
    fi
    if ! kill -0 "$server_pid" 2>/dev/null; then
      return 1
    fi
    sleep 1
  done
  return 1
}

if ! curl -fsS --max-time 2 "http://$host:$port/feeds" >/dev/null 2>&1; then
  # A stale or unrelated local service can occupy 4000 without being a usable
  # WeWe endpoint. Retry a few adjacent loopback ports rather than asking the
  # user to identify or kill another process.
  initial_port="$port"
  started=false
  for offset in {0..5}; do
    port=$((initial_port + offset))
    if start_local_server; then
      started=true
      break
    fi
    wait "$server_pid" 2>/dev/null || true
    server_pid=""
  done
  if [[ "$started" != true ]]; then
    printf 'WeWe login page did not start on ports %s-%s. See %s\n' "$initial_port" "$port" "$log_file" >&2
    exit 1
  fi
fi

if ! curl -fsS --max-time 2 "http://$host:$port/feeds" >/dev/null 2>&1; then
  printf 'WeWe login page did not answer. See %s\n' "$log_file" >&2
  exit 1
fi

url="http://$host:$port"
login_url="$url/dash/accounts"
printf 'Open %s\n' "$login_url"
printf 'Click “添加读书账号”; when the QR code appears, scan it in WeChat Reading.\n'
if command -v open >/dev/null 2>&1; then
  open "$login_url" >/dev/null 2>&1 || true
fi

waited=0
while (( waited < 900 )); do
  if python3 - "$database" <<'PY'
import sqlite3
import sys

connection = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
try:
    enabled = connection.execute("SELECT 1 FROM accounts WHERE status = 1 LIMIT 1").fetchone()
finally:
    connection.close()
raise SystemExit(0 if enabled else 1)
PY
  then
    break
  fi
  sleep 3
  waited=$((waited + 3))
  if (( waited % 15 == 0 )); then
    printf 'Still waiting for the QR scan (%ss elapsed).\n' "$waited"
  fi
done

if (( waited >= 900 )); then
  printf 'No completed scan was detected after 15 minutes. The local WeWe service is still available at %s.\n' "$login_url" >&2
  exit 3
fi

printf 'Login detected. Verifying subscriptions and importing today\047s updates...\n'
cd "$root_dir"
WEWE_BASE_URL="$url" python3 scripts/wewe_post_login.py
