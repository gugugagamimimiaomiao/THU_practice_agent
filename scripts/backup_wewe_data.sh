#!/usr/bin/env bash
set -euo pipefail

# Back up a local persistent SQLite database or the production Docker volume
# without exposing account credentials to logs.
backup_dir="${1:-backups/wewe}"
timestamp="$(date +%Y%m%d-%H%M%S)"
mkdir -p "$backup_dir"

local_db="${WEWE_DB_PATH:-data/wewe-rss.db}"
if [[ -f "$local_db" ]]; then
  destination="$backup_dir/wewe-rss-$timestamp.db"
  sqlite3 "$local_db" ".backup '$destination'"
  chmod 600 "$destination"
  sqlite3 "$destination" "PRAGMA quick_check;" | grep -qx ok
  printf 'WeWe SQLite backup: %s\n' "$destination"
  exit 0
fi

if ! command -v docker >/dev/null 2>&1; then
  printf 'No local WeWe SQLite at %s and Docker is unavailable.\n' "$local_db" >&2
  exit 2
fi

docker run --rm \
  -v practice-xiaoda-mvp_wewe_data:/source:ro \
  -v "$(pwd)/$backup_dir:/backup" \
  alpine:3.20 \
  tar czf "/backup/wewe-data-$timestamp.tar.gz" -C /source .
printf 'WeWe data backup: %s/wewe-data-%s.tar.gz\n' "$backup_dir" "$timestamp"
