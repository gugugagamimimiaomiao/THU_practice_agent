#!/usr/bin/env python3
"""Resolve canonical WeChat article links through an authorized WeWe account.

The account token is read from the local WeWe SQLite database, kept in memory,
and sent only to the configured WeWe relay. It is never printed or persisted.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import ssl
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = Path(os.getenv("WEWE_DB_PATH", str(ROOT / "data" / "wewe-rss.db")))
DEFAULT_RELAY = "https://weread.111965.xyz"
DEFAULT_CA_BUNDLE = Path(
    "/Users/sonnette/.codex/venvs/social-practice-wechat-digest/lib/python3.8/site-packages/certifi/cacert.pem"
)
CANONICAL_PREFIX = "https://mp.weixin.qq.com/s/"


def enabled_account(database: Path) -> tuple[str, str]:
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        row = connection.execute(
            "SELECT id, token FROM accounts WHERE status = 1 ORDER BY created_at LIMIT 1"
        ).fetchone()
    finally:
        connection.close()
    if not row:
        raise RuntimeError("no enabled WeWe account")
    return str(row[0]), str(row[1])


def tls_context() -> ssl.SSLContext:
    configured = os.getenv("WEWE_CA_BUNDLE", "").strip()
    bundle = Path(configured) if configured else DEFAULT_CA_BUNDLE
    return ssl.create_default_context(cafile=str(bundle)) if bundle.is_file() else ssl.create_default_context()


def resolve(relay: str, account_id: str, token: str, link: str) -> list[dict[str, object]]:
    request = Request(
        relay.rstrip("/") + "/api/v2/platform/wxs2mp",
        data=json.dumps({"url": link}).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "xid": account_id,
            "User-Agent": "Practice-Xiaoda/1.0",
        },
        method="POST",
    )
    with urlopen(request, timeout=30, context=tls_context()) as response:
        value = json.load(response)
    return value if isinstance(value, list) else []


def main() -> int:
    parser = argparse.ArgumentParser(description="Resolve canonical WeChat links without exposing WeWe credentials")
    parser.add_argument("links", nargs="+")
    parser.add_argument("--database", type=Path, default=DEFAULT_DB)
    parser.add_argument("--relay", default=os.getenv("WEWE_PLATFORM_URL", DEFAULT_RELAY))
    args = parser.parse_args()
    account_id, token = enabled_account(args.database)
    output: list[dict[str, object]] = []
    stop = False
    for link in dict.fromkeys(value.strip() for value in args.links):
        if not link.startswith(CANONICAL_PREFIX):
            output.append({"link": link, "ok": False, "error": "not_canonical"})
            continue
        try:
            rows = resolve(args.relay, account_id, token, link)
            matches = [
                {
                    "feed_id": str(row.get("id") or ""),
                    "name": str(row.get("name") or ""),
                    "intro": str(row.get("intro") or ""),
                    "cover": str(row.get("cover") or ""),
                    "update_time": int(row.get("updateTime") or 0),
                }
                for row in rows
                if isinstance(row, dict)
            ]
            output.append({"link": link, "ok": bool(matches), "matches": matches})
        except HTTPError as exc:
            message = exc.read().decode("utf-8", errors="replace")[:500]
            output.append({"link": link, "ok": False, "error": f"HTTP {exc.code}", "message": message})
            if exc.code in {401, 429} or "WeReadError429" in message or "今日小黑屋" in message:
                stop = True
        except URLError as exc:
            reason = getattr(exc, "reason", None)
            output.append({
                "link": link,
                "ok": False,
                "error": "URLError",
                "reason_type": type(reason).__name__ if reason is not None else "unknown",
                "reason": str(reason)[:240] if reason is not None else "",
            })
        except (TimeoutError, json.JSONDecodeError) as exc:
            output.append({"link": link, "ok": False, "error": type(exc).__name__})
        if stop:
            break
    print(json.dumps({"ok": not stop, "stopped": stop, "results": output}, ensure_ascii=False, indent=2))
    return 2 if stop else 0


if __name__ == "__main__":
    raise SystemExit(main())
