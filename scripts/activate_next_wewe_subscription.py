#!/usr/bin/env python3
"""Activate at most one already-verified WeWe feed per run.

This intentionally writes only feed metadata to WeWe's local SQLite database;
login tokens are never read by this script. Article refresh is left to WeWe's
normal low-frequency scheduler.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
QUEUE = ROOT / "config" / "wewe-subscriptions.json"
WEWE_DB = Path("/private/tmp/wewe-rss-eval/apps/server/data/wewe-rss.db")


def main() -> int:
    data = json.loads(QUEUE.read_text(encoding="utf-8"))
    candidates = [item for item in data.get("verified", []) if item.get("status") == "queued_for_subscription"]
    if not candidates:
        print(json.dumps({"ok": True, "activated": None, "remaining": 0}, ensure_ascii=False))
        return 0
    item = candidates[0]
    if not WEWE_DB.exists():
        print(json.dumps({"ok": False, "error": "wewe database unavailable"}, ensure_ascii=False))
        return 2
    connection = sqlite3.connect(WEWE_DB)
    try:
        existing = connection.execute("SELECT 1 FROM feeds WHERE id = ?", (item["feed_id"],)).fetchone()
        if existing:
            item["status"] = "subscribed"
            item["activated_at"] = time.strftime("%Y-%m-%d")
            result = "already_present"
        else:
            now_ms = int(time.time() * 1000)
            now_s = int(time.time())
            connection.execute(
                """INSERT INTO feeds
                   (id, mp_name, mp_cover, mp_intro, status, sync_time, update_time, created_at, updated_at, has_history)
                   VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, 1)""",
                (item["feed_id"], item["name"], item.get("cover", ""), item.get("intro", ""), now_s, item.get("update_time", now_s), now_ms, now_ms),
            )
            item["status"] = "subscribed"
            item["activated_at"] = time.strftime("%Y-%m-%d")
            result = "activated"
        connection.commit()
    finally:
        connection.close()
    QUEUE.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"ok": True, "activated": item["name"], "feed_id": item["feed_id"], "result": result, "remaining": len(candidates) - 1}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
