#!/usr/bin/env python3
"""Export complete WeWe articles not already present in the local article DB.

This deliberately exports all complete public articles, not only opportunity
titles. The receiving server can retain non-opportunity source material without
publishing it as a recommendation.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from urllib.parse import quote

from wewe_collector import extract_images, get_json, html_to_text, iso_date
from opportunity_filter import candidate_decision


def existing_urls(database: Path) -> set[str]:
    connection = sqlite3.connect(database)
    try:
        return {str(row[0]) for row in connection.execute("SELECT source_url FROM articles WHERE source_url IS NOT NULL AND source_url != ''")}
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--account", default="清华大学社会实践")
    parser.add_argument("--since", default="2026-01-01")
    parser.add_argument("--need", type=int, default=26)
    parser.add_argument("--database", type=Path, default=Path("data/practice_xiaoda.db"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--title-prefix", default="")
    parser.add_argument("--exclude-jsonl", type=Path, action="append", default=[])
    parser.add_argument("--correction", action="store_true", help="mark every exported row as an authoritative correction")
    args = parser.parse_args()

    feeds = get_json("/feeds")
    feed = next((row for row in feeds if row.get("name") == args.account or row.get("mpName") == args.account), None)
    if not feed:
        raise SystemExit(f"feed not found: {args.account}")
    feed_id = str(feed.get("id"))
    metadata = get_json(f"/feeds/{quote(feed_id)}.json", {"limit": 100, "page": 1, "mode": "metadata"})
    seen = existing_urls(args.database)
    for path in args.exclude_jsonl:
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict) and row.get("source_url"):
                seen.add(str(row["source_url"]).strip())
    records: list[dict[str, object]] = []
    metadata_items = sorted(
        (item for item in metadata.get("items", []) if isinstance(item, dict)),
        key=lambda item: str(item.get("date_modified") or ""),
        reverse=True,
    )
    for item in metadata_items:
        if len(records) >= args.need:
            break
        publish_date = iso_date(str(item.get("date_modified") or ""))
        link = str(item.get("url") or "").strip()
        title = str(item.get("title") or "").strip()
        if args.title_prefix and not title.startswith(args.title_prefix):
            continue
        if publish_date < args.since or not link.startswith("https://mp.weixin.qq.com/s/") or link in seen:
            continue
        if not candidate_decision({"title": title, "content": ""})["candidate"]:
            continue
        original_index = next(
            index for index, row in enumerate(metadata.get("items", []), 1)
            if row.get("id") == item.get("id")
        )
        full = get_json(f"/feeds/{quote(feed_id)}.json", {"limit": 1, "page": original_index, "mode": "fulltext"})
        match = (full.get("items") or [{}])[0]
        content_html = str(match.get("content_html") or "")
        content = html_to_text(content_html).strip()
        images = extract_images(content_html)
        if (not content and not images) or "获取全文失败" in content:
            continue
        record: dict[str, object] = {
            "source_account": args.account,
            "source_url": link,
            "title": title,
            "publish_date": publish_date,
            "raw_text": content,
        }
        if images:
            record["images"] = images
        if args.correction:
            record["correction"] = True
        records.append(record)
        seen.add(link)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in records), encoding="utf-8")
    print(json.dumps({"account": args.account, "count": len(records), "output": str(args.output), "needed": args.need}, ensure_ascii=False))
    return 0 if len(records) >= args.need else 1


if __name__ == "__main__":
    raise SystemExit(main())
