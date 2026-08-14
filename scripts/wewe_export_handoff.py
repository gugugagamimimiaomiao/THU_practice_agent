#!/usr/bin/env python3
"""Export complete WeWe articles not already present in the local article DB.

This deliberately exports all complete public articles, not only opportunity
titles. The receiving server can retain non-opportunity source material without
publishing it as a recommendation.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from wewe_collector import extract_images, get_json, html_to_text, iso_date
from domain import extract_project
from opportunity_filter import candidate_decision


CORPUS_INCLUDE = re.compile(
    r"(?:实践|志愿|支队|调研).*(?:总结|纪实|回顾|侧记|心得|感悟|成果)"
    r"|(?:总结|纪实|回顾|侧记|心得|感悟|成果).*(?:实践|志愿|支队|调研)"
)
CORPUS_EXCLUDE = re.compile(r"招募|报名|招新|纳新|通知|预告|公示")


def existing_urls(database: Path) -> set[str]:
    connection = sqlite3.connect(database)
    try:
        return {str(row[0]) for row in connection.execute("SELECT source_url FROM articles WHERE source_url IS NOT NULL AND source_url != ''")}
    finally:
        connection.close()


def is_current_opportunity(record: dict[str, object]) -> bool:
    """Reject demo and expired records after the full body has been fetched."""
    project = extract_project(
        str(record.get("raw_text") or ""),
        {
            "input_type": "wechat_batch",
            "source_account": str(record.get("source_account") or ""),
            "source_url": str(record.get("source_url") or ""),
            "title": str(record.get("title") or ""),
            "publish_date": str(record.get("publish_date") or ""),
        },
    )
    return not project.get("demo_data") and project.get("status") != "expired"


def is_corpus_article(title: str) -> bool:
    """High-precision historical writing material, never a live opportunity."""
    value = str(title).strip()
    return bool(CORPUS_INCLUDE.search(value)) and not CORPUS_EXCLUDE.search(value)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--account", action="append", default=[])
    parser.add_argument("--since", default="2026-01-01")
    parser.add_argument("--need", type=int, default=26)
    parser.add_argument("--mode", choices=("current", "corpus"), default="current")
    parser.add_argument("--delay", type=float, default=1.0,
                        help="seconds between full-text requests; use a low rate for historical backfill")
    parser.add_argument("--database", type=Path, default=Path("data/practice_xiaoda.db"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--title-prefix", default="")
    parser.add_argument("--exclude-jsonl", type=Path, action="append", default=[])
    parser.add_argument("--correction", action="store_true", help="mark every exported row as an authoritative correction")
    args = parser.parse_args()

    feeds = get_json("/feeds")
    accounts = args.account or ["清华大学社会实践"]
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
    for account in accounts:
        feed = next((row for row in feeds if row.get("name") == account or row.get("mpName") == account), None)
        if not feed:
            raise SystemExit(f"feed not found: {account}")
        feed_id = str(feed.get("id"))
        metadata = get_json(f"/feeds/{quote(feed_id)}.json", {"limit": 100, "page": 1, "mode": "metadata"})
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
            if args.mode == "corpus":
                if not is_corpus_article(title):
                    continue
            elif not candidate_decision({"title": title, "content": ""})["candidate"]:
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
                "source_account": account,
                "source_url": link,
                "title": title,
                "publish_date": publish_date,
                "raw_text": content,
            }
            if images:
                record["images"] = images
            if args.mode == "corpus":
                record["corpus_only"] = True
            if args.correction:
                record["correction"] = True
            if args.mode == "current" and not is_current_opportunity(record):
                continue
            records.append(record)
            seen.add(link)
            if args.delay > 0:
                time.sleep(args.delay)
        if len(records) >= args.need:
            break
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in records), encoding="utf-8")
    print(json.dumps({"accounts": accounts, "mode": args.mode, "count": len(records),
                      "output": str(args.output), "needed": args.need}, ensure_ascii=False))
    return 0 if len(records) >= args.need else 1


if __name__ == "__main__":
    raise SystemExit(main())
