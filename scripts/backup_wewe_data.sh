#!/usr/bin/env bash
set -euo pipefail

# Back up the named Docker volume without exposing its SQLite contents to logs.
backup_dir="${1:-backups/wewe}"
timestamp="$(date +%Y%m%d-%H%M%S)"
mkdir -p "$backup_dir"
docker run --rm \
  -v practice-xiaoda-mvp_wewe_data:/source:ro \
  -v "$(pwd)/$backup_dir:/backup" \
  alpine:3.20 \
  tar czf "/backup/wewe-data-$timestamp.tar.gz" -C /source .
printf 'WeWe data backup: %s/wewe-data-%s.tar.gz\n' "$backup_dir" "$timestamp"
