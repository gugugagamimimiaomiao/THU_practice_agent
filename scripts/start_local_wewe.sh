#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
server_dir="${WEWE_SERVER_DIR:-/private/tmp/wewe-rss-eval/apps/server}"
database="${WEWE_DB_PATH:-$root_dir/data/wewe-rss.db}"
host="${WEWE_HOST:-127.0.0.1}"
port="${WEWE_PORT:-4000}"

if [[ "$host" != "127.0.0.1" ]]; then
  printf 'Refusing to expose the local recovery service on %s.\n' "$host" >&2
  exit 2
fi
if [[ ! -f "$database" ]]; then
  printf 'Persistent WeWe SQLite not found: %s\n' "$database" >&2
  exit 2
fi
if [[ ! -f "$server_dir/dist/main.js" || ! -f "$server_dir/client/index.hbs" ]]; then
  printf 'WeWe server build is incomplete: %s\n' "$server_dir" >&2
  exit 2
fi

chmod 600 "$database"
cd "$server_dir"
exec env -u AUTH_CODE \
  DATABASE_URL="file:$database" \
  DATABASE_TYPE=sqlite \
  NODE_ENV=production \
  HOST="$host" \
  PORT="$port" \
  PLATFORM_URL="${WEWE_PLATFORM_URL:-https://weread.111965.xyz}" \
  node dist/main
