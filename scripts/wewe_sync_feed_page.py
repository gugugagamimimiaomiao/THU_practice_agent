#!/usr/bin/env python3
"""Low-frequency single-page WeWe history sync without exposing credentials."""
from __future__ import annotations

import argparse
import json
import sqlite3
import ssl
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


DEFAULT_DB = Path("/private/tmp/wewe-rss-eval/apps/server/data/wewe-rss.db")
DEFAULT_RELAY = "https://weread.111965.xyz"
DEFAULT_CA = Path("/Users/sonnette/.codex/venvs/social-practice-wechat-digest/lib/python3.8/site-packages/certifi/cacert.pem")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("feed_id")
    parser.add_argument("--page", type=int, default=1)
    parser.add_argument("--database", type=Path, default=DEFAULT_DB)
    parser.add_argument("--relay", default=DEFAULT_RELAY)
    args = parser.parse_args()
    connection = sqlite3.connect(args.database)
    try:
        account = connection.execute("SELECT id, token FROM accounts WHERE status=1 ORDER BY created_at LIMIT 1").fetchone()
        feed = connection.execute("SELECT id FROM feeds WHERE id=?", (args.feed_id,)).fetchone()
        if not account or not feed:
            print(json.dumps({"ok": False, "error": "enabled account or feed missing"}))
            return 2
        url = args.relay.rstrip("/") + f"/api/v2/platform/mps/{args.feed_id}/articles?" + urlencode({"page": args.page})
        request = Request(url, headers={"Authorization": f"Bearer {account[1]}", "xid": str(account[0]), "Accept": "application/json", "User-Agent": "Practice-Xiaoda/1.0"})
        context = ssl.create_default_context(cafile=str(DEFAULT_CA)) if DEFAULT_CA.is_file() else ssl.create_default_context()
        with urlopen(request, timeout=30, context=context) as response:
            rows = json.load(response)
        if not isinstance(rows, list):
            raise ValueError("relay response is not a list")
        for row in rows:
            if not isinstance(row, dict) or not row.get("id"):
                continue
            connection.execute(
                """INSERT INTO articles(id,mp_id,title,pic_url,publish_time,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET title=excluded.title,pic_url=excluded.pic_url,publish_time=excluded.publish_time,updated_at=excluded.updated_at""",
                (str(row["id"]), args.feed_id, str(row.get("title") or ""), str(row.get("picUrl") or ""), int(row.get("publishTime") or 0), int(row.get("publishTime") or 0) * 1000, int(row.get("publishTime") or 0) * 1000),
            )
        connection.execute("UPDATE feeds SET sync_time=strftime('%s','now') WHERE id=?", (args.feed_id,))
        connection.commit()
        print(json.dumps({"ok": True, "feed_id": args.feed_id, "page": args.page, "articles": len(rows)}, ensure_ascii=False))
        return 0
    except HTTPError as exc:
        message = exc.read().decode("utf-8", errors="replace")[:400]
        print(json.dumps({"ok": False, "status": exc.code, "stop": exc.code in {401, 429} or "WeReadError429" in message or "今日小黑屋" in message, "message": message}, ensure_ascii=False))
        return 3
    except (URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": type(exc).__name__}, ensure_ascii=False))
        return 1
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
