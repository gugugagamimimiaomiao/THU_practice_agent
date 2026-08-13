#!/usr/bin/env python3
"""Read articles collected by we-mp-rss through its local SQLite database.

This is intentionally a read-only adapter. we-mp-rss owns authentication,
subscriptions, and scheduled fetching; Practice Xiaoda owns classification,
OCR, deduplication, and project extraction.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sqlite3
import sys
from datetime import date, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


DEFAULT_DB = Path(os.getenv("WERSS_DB_PATH", "data/werss/db.db"))
OUTPUT_DIR = Path(os.getenv("WECHAT_DIGEST_OUTPUT_DIR", "output/social-practice-wechat-digest"))
IMAGE_RE = re.compile(r"<(?:img|source)\b[^>]*(?:data-src|src)=[\"']([^\"']+)", re.I)


class _TextExtractor(HTMLParser):
    BREAKS = {"br", "div", "p", "li", "section", "h1", "h2", "h3", "tr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.ignored = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript"}:
            self.ignored += 1
        elif not self.ignored and tag in self.BREAKS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"} and self.ignored:
            self.ignored -= 1
        elif not self.ignored and tag in self.BREAKS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.ignored:
            self.parts.append(data)

    def text(self) -> str:
        return "\n".join(line.strip() for line in "".join(self.parts).splitlines() if line.strip())


def html_to_text(value: str) -> str:
    parser = _TextExtractor()
    parser.feed(value or "")
    return html.unescape(parser.text())


def extract_images(value: str) -> list[str]:
    images = []
    for raw in IMAGE_RE.findall(value or ""):
        url = html.unescape(raw.strip())
        parsed = urlparse(url)
        if parsed.scheme in {"http", "https"} and parsed.hostname:
            images.append(url)
    return list(dict.fromkeys(images))


def _open_database(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError(f"we-mp-rss database not found: {path}")
    uri = f"file:{path.resolve()}?mode=ro"
    database = sqlite3.connect(uri, uri=True)
    database.row_factory = sqlite3.Row
    required = {"articles", "feeds"}
    tables = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not required.issubset(tables):
        database.close()
        raise ValueError("not a compatible we-mp-rss database")
    return database


def _publish_date(timestamp: Any) -> str:
    try:
        value = int(timestamp or 0)
        if value > 10_000_000_000:
            value //= 1000
        return datetime.fromtimestamp(value).astimezone().date().isoformat() if value else ""
    except (OSError, OverflowError, TypeError, ValueError):
        return ""


def collect_articles(path: Path, accounts: list[str], since: str, count: int) -> tuple[list[dict[str, Any]], list[str]]:
    database = _open_database(path)
    articles: list[dict[str, Any]] = []
    missing: list[str] = []
    since_timestamp = int(datetime.combine(date.fromisoformat(since), datetime.min.time()).timestamp())
    try:
        for account in dict.fromkeys(accounts):
            feeds = database.execute("SELECT id, mp_name FROM feeds WHERE mp_name = ? AND status != 0", (account,)).fetchall()
            if not feeds:
                missing.append(account)
                continue
            for feed in feeds:
                rows = database.execute(
                    """SELECT title, url, description, content, content_html, publish_time
                       FROM articles
                       WHERE mp_id = ? AND status = 1 AND publish_time >= ?
                       ORDER BY publish_time DESC LIMIT ?""",
                    (feed["id"], since_timestamp, max(1, count)),
                ).fetchall()
                for row in rows:
                    content_html = str(row["content_html"] or row["content"] or "")
                    content = html_to_text(content_html) or str(row["description"] or "").strip()
                    articles.append({
                        "query": account,
                        "account": str(feed["mp_name"] or account),
                        "title": str(row["title"] or "").strip(),
                        "link": str(row["url"] or "").strip(),
                        "publishDate": _publish_date(row["publish_time"]),
                        "content": content,
                        "images": extract_images(content_html),
                        "collector": "we-mp-rss",
                    })
    finally:
        database.close()
    unique: dict[str, dict[str, Any]] = {}
    for article in articles:
        key = article["link"] or f"{article['account']}|{article['title']}|{article['publishDate']}"
        unique[key] = article
    return list(unique.values()), missing


def _write_batch(articles: list[dict[str, Any]], missing: list[str], since: str, database: Path) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output = OUTPUT_DIR / f"articles_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    payload = {
        "collector": "we-mp-rss",
        "database": str(database),
        "since": since,
        "partial": False,
        "missing_accounts": missing,
        "articles": articles,
    }
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description="we-mp-rss read-only adapter")
    parser.add_argument("--database", type=Path, default=DEFAULT_DB)
    subparsers = parser.add_subparsers(dest="command", required=True)
    collect = subparsers.add_parser("collect")
    collect.add_argument("accounts", nargs="+")
    collect.add_argument("--since", default=(date.today()).isoformat())
    collect.add_argument("--count", type=int, default=12)
    collect.add_argument("--no-kb", action="store_true")
    args = parser.parse_args()
    try:
        articles, missing = collect_articles(args.database, args.accounts, args.since, args.count)
        output = _write_batch(articles, missing, args.since, args.database)
    except (FileNotFoundError, ValueError, sqlite3.Error) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps({"ok": True, "articles": len(articles), "missing_accounts": missing, "output": str(output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
