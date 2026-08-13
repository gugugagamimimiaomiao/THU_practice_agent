#!/usr/bin/env python3
"""Run the configured WeChat incremental importer once per local calendar day."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def seconds_until(run_at: str, now: datetime | None = None) -> float:
    current = now or datetime.now().astimezone()
    hour, minute = (int(part) for part in run_at.split(":", 1))
    target = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= current:
        target += timedelta(days=1)
    return max(1.0, (target - current).total_seconds())


def run_once() -> int:
    since = (datetime.now().astimezone().date() - timedelta(days=2)).isoformat()
    command = [
        sys.executable, str(ROOT / "scripts" / "daily_wechat_update.py"),
        "--since", since, "--count", os.getenv("WECHAT_DAILY_COUNT", "50"),
    ]
    return subprocess.run(command, cwd=ROOT, check=False).returncode


def main() -> int:
    run_at = os.getenv("WECHAT_DAILY_TIME", "08:25")
    try:
        seconds_until(run_at)
    except (TypeError, ValueError):
        print("WECHAT_DAILY_TIME must use HH:MM", file=sys.stderr, flush=True)
        return 2
    if os.getenv("WECHAT_RUN_ON_START", "false").lower() in {"1", "true", "yes"}:
        run_once()
    while True:
        delay = seconds_until(run_at)
        print(f"next WeChat import in {int(delay)} seconds", flush=True)
        time.sleep(delay)
        run_once()


if __name__ == "__main__":
    raise SystemExit(main())
