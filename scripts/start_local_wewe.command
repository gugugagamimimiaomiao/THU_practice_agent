#!/bin/bash
set -u

script_dir="$(cd "$(dirname "$0")" && pwd)"

echo "Starting local WeWe service..."
echo

if "$script_dir/start_local_wewe.sh"; then
  echo
  echo "WeWe exited normally."
else
  status=$?
  echo
  echo "WeWe failed with exit code $status."
  echo "If you see a port or permission error, check whether another process is using 127.0.0.1:4000."
fi

echo
echo "Feeds page: http://127.0.0.1:4000/feeds"
echo "Press Enter to close this window."
read -r
