#!/usr/bin/env python3
"""Resumable, batched WeChat backfill for the curated account list."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from collector_settings import credentials, load_private  # noqa: E402
from wechat_sources import DEFAULT_ACCOUNTS  # noqa: E402


def batches(items: list[str], size: int) -> list[list[str]]:
    return [items[index:index + size] for index in range(0, len(items), size)]


def load_state(path: Path, since: str, accounts: list[str]) -> dict:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        state = {}
    if state.get("since") != since or state.get("accounts") != accounts:
        return {"since": since, "accounts": accounts, "completed_batches": [], "runs": []}
    return state


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix="wechat-backfill-", dir=str(path.parent), text=True)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main() -> int:
    parser = argparse.ArgumentParser(description="分批、可断点续跑地回补公众号历史文章")
    parser.add_argument("--since", default=(date.today() - timedelta(days=21)).isoformat())
    parser.add_argument("--count", type=int, default=30, help="每个公众号最多抓取文章数")
    parser.add_argument("--batch-size", type=int, default=6, help="每批公众号数")
    parser.add_argument("--pause", type=int, default=180, help="批次之间冷却秒数")
    parser.add_argument("--database", default=os.getenv("PRACTICE_XIAODA_DB", str(ROOT / "data" / "practice_xiaoda.db")))
    parser.add_argument("--audit-dir", default=str(ROOT / "data" / "collector_audits"))
    parser.add_argument("--state", type=Path, default=ROOT / "data" / "wechat_backfill_state.json")
    parser.add_argument("--accounts", nargs="+", default=list(DEFAULT_ACCOUNTS))
    args = parser.parse_args()
    if not 1 <= args.batch_size <= 12:
        parser.error("--batch-size 必须在 1 到 12 之间")

    settings = load_private()
    token, cookie = credentials()
    uses_werss = Path(str(settings.get("collector_path"))).name == "werss_collector.py"
    if not settings.get("collector_path") or (not uses_werss and (not token or not cookie)):
        print(json.dumps({"ok": False, "error": "采集器路径或微信 Token/Cookie 未配置"}, ensure_ascii=False))
        return 2

    accounts = list(dict.fromkeys(item.strip() for item in args.accounts if item.strip()))
    groups = batches(accounts, args.batch_size)
    state = load_state(args.state, args.since, accounts)
    environment = {
        **os.environ,
        "WECHAT_TOKEN": token,
        "WECHAT_COOKIE": cookie,
        "WECHAT_COLLECTOR_PATH": settings["collector_path"],
        "WECHAT_COLLECTOR_PYTHON": settings.get("collector_python") or sys.executable,
        "SEED_DEMO_DATA": "false",
    }
    for index, group in enumerate(groups):
        if index in state["completed_batches"]:
            continue
        command = [
            sys.executable, str(ROOT / "scripts" / "daily_wechat_update.py"),
            "--collector", settings["collector_path"], "--database", args.database,
            "--audit-dir", args.audit_dir, "--since", args.since, "--count", str(args.count),
            "--accounts", *group,
        ]
        completed = subprocess.run(command, env=environment, capture_output=True, text=True, check=False)
        final = {}
        for line in reversed(completed.stdout.splitlines()):
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict) and "ok" in parsed:
                final = parsed
                break
        state["runs"].append({"batch": index, "accounts": group, "returncode": completed.returncode, "result": final})
        if completed.returncode != 0 or not final.get("ok") or final.get("collector_partial"):
            save_state(args.state, state)
            print(json.dumps({
                "ok": False, "batch": index, "accounts": group,
                "error": "微信频控导致批次仅部分完成" if final.get("collector_partial") else "采集子任务失败",
                "result": final or "采集子任务失败", "state": str(args.state),
            }, ensure_ascii=False))
            return 1
        state["completed_batches"].append(index)
        save_state(args.state, state)
        print(json.dumps({"event": "batch_completed", "batch": index + 1, "total_batches": len(groups), "accounts": group, "result": final}, ensure_ascii=False), flush=True)
        if index + 1 < len(groups) and args.pause:
            import time
            time.sleep(args.pause)
    print(json.dumps({"ok": True, "since": args.since, "accounts": len(accounts), "batches": len(groups), "state": str(args.state)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
