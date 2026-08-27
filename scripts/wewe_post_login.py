#!/usr/bin/env python3
"""Run the complete, rate-limited workflow that follows a WeWe QR-code login.

The only interactive step is the QR scan.  All credentials remain in WeWe's
local SQLite database; this coordinator never prints or copies a login token.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Optional


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
QUEUE = ROOT / "config" / "wewe-subscriptions.json"
WEWE_DB = Path(os.getenv("WEWE_DB_PATH", str(ROOT / "data" / "wewe-rss.db")))
PRACTICE_DB = Path(os.getenv("PRACTICE_XIAODA_DB", str(ROOT / "data" / "practice_xiaoda.db")))
EXPORT_DIR = ROOT / "data" / "exports"
BACKFILL_STATE = ROOT / "data" / "wewe-backfill-pending.json"


def _write_queue(data: dict) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix="wewe-subscriptions-", dir=str(QUEUE.parent), text=True)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, QUEUE)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _load_backfill_state() -> Optional[dict]:
    if not BACKFILL_STATE.is_file():
        return None
    try:
        payload = json.loads(BACKFILL_STATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(payload, dict) and payload.get("name") and payload.get("feed_id"):
        return payload
    return None


def _save_backfill_state(feed: dict) -> None:
    BACKFILL_STATE.parent.mkdir(parents=True, exist_ok=True)
    payload = {"name": str(feed["name"]), "feed_id": str(feed["feed_id"]), "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    temporary = BACKFILL_STATE.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, BACKFILL_STATE)


def _enabled_account() -> bool:
    if not WEWE_DB.is_file():
        return False
    connection = sqlite3.connect(f"file:{WEWE_DB}?mode=ro", uri=True)
    try:
        return connection.execute("SELECT 1 FROM accounts WHERE status = 1 LIMIT 1").fetchone() is not None
    finally:
        connection.close()


def _resolve_pending(item: dict) -> dict:
    from resolve_wewe_links import enabled_account, resolve

    account_id, token = enabled_account(WEWE_DB)
    matches = resolve(os.getenv("WEWE_PLATFORM_URL", "https://weread.111965.xyz"), account_id, token, str(item["article_url"]))
    expected = str(item["name"])
    for match in matches:
        if str(match.get("name") or "") == expected and str(match.get("id") or ""):
            return {
                "name": expected,
                "article_url": str(item["article_url"]),
                "feed_id": str(match["id"]),
                "cover": str(match.get("cover") or ""),
                "intro": str(match.get("intro") or ""),
                "update_time": int(match.get("updateTime") or 0),
            }
    raise ValueError("resolved account name did not exactly match the pending source")


def _subscribe(feed: dict) -> str:
    connection = sqlite3.connect(WEWE_DB)
    try:
        existing = connection.execute("SELECT 1 FROM feeds WHERE id = ?", (feed["feed_id"],)).fetchone()
        if existing:
            return "already_subscribed"
        now_ms = int(time.time() * 1000)
        now_s = int(time.time())
        connection.execute(
            """INSERT INTO feeds
               (id, mp_name, mp_cover, mp_intro, status, sync_time, update_time, created_at, updated_at, has_history)
               VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, 1)""",
            (feed["feed_id"], feed["name"], feed["cover"], feed["intro"], now_s, feed["update_time"] or now_s, now_ms, now_ms),
        )
        connection.commit()
        return "subscribed"
    finally:
        connection.close()


def _run(command: list[str], environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Execute a child stage without exposing its environment in logs."""
    return subprocess.run(command, cwd=ROOT, env=environment, check=False, text=True, capture_output=True)


def _json_output(completed: subprocess.CompletedProcess[str]) -> dict:
    """Read the final JSON line emitted by the small worker scripts."""
    for line in reversed((completed.stdout or "").splitlines()):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return {}


def _activate_one(environment: dict[str, str]) -> dict:
    completed = _run([sys.executable, str(ROOT / "scripts" / "activate_next_wewe_subscription.py")], environment)
    payload = _json_output(completed)
    return {"ok": completed.returncode == 0 and bool(payload.get("ok")), **payload}


def _sync_new_feed(feed_id: str, environment: dict[str, str]) -> dict:
    completed = _run(
        [sys.executable, str(ROOT / "scripts" / "wewe_sync_feed_page.py"), feed_id, "--page", "1"], environment,
    )
    payload = _json_output(completed)
    return {"ok": completed.returncode == 0 and bool(payload.get("ok")), **payload}


def _daily_import(accounts: list[str], since: str, count: int, environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return _run(
        [
            sys.executable, str(ROOT / "scripts" / "daily_wechat_update.py"), "--accounts", *accounts,
            "--since", since, "--count", str(count),
        ],
        environment,
    )


def _push_server(environment: dict[str, str]) -> dict:
    """Ship currently actionable records; server-side source URL dedupe is idempotent."""
    if not environment.get("XIAODA_INGEST_KEY", "").strip():
        return {"ok": True, "action": "not_configured", "pushed": 0}
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    output = EXPORT_DIR / f"wewe-server-handoff-{time.strftime('%Y%m%dT%H%M%S')}.jsonl"
    since = os.getenv("WEWE_PUSH_SINCE", (date.today() - timedelta(days=35)).isoformat())
    export = _run(
        [
            sys.executable, str(ROOT / "scripts" / "export_ingest_batch.py"), "--database", str(PRACTICE_DB),
            "--output", str(output), "--since", since, "--limit", os.getenv("WEWE_PUSH_LIMIT", "80"),
        ],
        environment,
    )
    payload = _json_output(export)
    count = int(payload.get("count") or 0)
    if export.returncode != 0:
        return {"ok": False, "action": "export_failed", "pushed": 0}
    if not count:
        return {"ok": True, "action": "nothing_to_push", "pushed": 0, "output": str(output)}
    pushed = _run([sys.executable, str(ROOT / "scripts" / "push_article.py"), str(output)], environment)
    return {
        "ok": pushed.returncode == 0,
        "action": "pushed" if pushed.returncode == 0 else "push_failed",
        "pushed": count if pushed.returncode == 0 else 0,
        "output": str(output),
    }


def main() -> int:
    if not _enabled_account():
        print(json.dumps({"ok": False, "action": "scan_required", "message": "Open WeWe and scan the QR code, then run this command again."}, ensure_ascii=False))
        return 2
    data = json.loads(QUEUE.read_text(encoding="utf-8"))
    pending = list(data.get("pending_resolution") or [])
    source_action = "no_pending_source"
    newly_subscribed: Optional[dict] = None
    if pending:
        item = pending[0]
        try:
            feed = _resolve_pending(item)
            source_action = _subscribe(feed)
            newly_subscribed = feed
        except Exception as exc:
            print(json.dumps({"ok": False, "action": "source_not_changed", "error": type(exc).__name__}, ensure_ascii=False))
            return 1
        data["pending_resolution"] = pending[1:]
        data.setdefault("verified", []).append({
            **feed, "status": "subscribed", "verified_at": time.strftime("%Y-%m-%d"), "activated_at": time.strftime("%Y-%m-%d"),
        })
        _write_queue(data)
    environment = {
        **os.environ,
        "WECHAT_COLLECTOR_PATH": os.getenv("WECHAT_COLLECTOR_PATH", str(ROOT / "scripts" / "wewe_collector.py")),
        "WECHAT_COLLECTOR_PYTHON": os.getenv("WECHAT_COLLECTOR_PYTHON", sys.executable),
    }
    backfill_pending = _load_backfill_state()
    if backfill_pending:
        # Resume before activating anything else if a prior local run stopped.
        activation = {"ok": True, "activated": backfill_pending["name"], "feed_id": backfill_pending["feed_id"], "result": "resuming_backfill"}
    elif newly_subscribed:
        # Resolving a canonical link subscribes that feed immediately. It is
        # this run's one new activation, so do not also dequeue another feed.
        activation = {
            "ok": True,
            "activated": newly_subscribed["name"],
            "feed_id": newly_subscribed["feed_id"],
            "result": source_action,
        }
    else:
        activation = _activate_one(environment)
    activated_name = str(activation.get("activated") or "").strip()
    activated_feed = str(activation.get("feed_id") or "").strip()
    history: dict = {"ok": True, "action": "not_needed"}
    if activated_feed:
        if not backfill_pending:
            _save_backfill_state({"name": activated_name, "feed_id": activated_feed})
        history = _sync_new_feed(activated_feed, environment)

    # A new feed gets a one-time longer window immediately after activation.
    # Existing priority feeds retain the short incremental scan, keeping the
    # daily WeChat Reading load bounded.
    from wechat_sources import DAILY_PRIORITY_ACCOUNTS

    accounts = list(DAILY_PRIORITY_ACCOUNTS)
    since = (date.today() - timedelta(days=28)).isoformat()
    count = int(os.getenv("WECHAT_DAILY_COUNT", "12"))
    if activated_name and activated_name not in accounts:
        accounts.append(activated_name)
        since = os.getenv("WEWE_NEW_FEED_BACKFILL_SINCE", (date.today() - timedelta(days=365)).isoformat())
        count = int(os.getenv("WEWE_NEW_FEED_BACKFILL_COUNT", "80"))
    imported = _daily_import(accounts, since, count, environment)
    push = _push_server(environment) if imported.returncode == 0 else {"ok": False, "action": "skipped_import_failed", "pushed": 0}
    ok = imported.returncode == 0 and bool(activation.get("ok")) and bool(history.get("ok")) and bool(push.get("ok"))
    if ok and activated_feed:
        BACKFILL_STATE.unlink(missing_ok=True)
    print(json.dumps({
        "ok": ok,
        "source_action": source_action,
        "activation": activation,
        "history": history,
        "daily_accounts": accounts,
        "daily_since": since,
        "import_exit_code": imported.returncode,
        "server_push": push,
    }, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
