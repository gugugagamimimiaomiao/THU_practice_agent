#!/usr/bin/env python3
"""Export a deduplicated batch of complete source articles for handoff."""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


def https_wechat_url(value: str) -> str:
    parts = urlsplit(value.strip())
    if parts.hostname not in {"mp.weixin.qq.com", "www.mp.weixin.qq.com"}:
        return value.strip()
    return urlunsplit(("https", "mp.weixin.qq.com", parts.path, parts.query, ""))


def excluded_source_urls(paths: list[Path]) -> set[str]:
    """Load URLs from prior JSON/JSONL handoffs so an increment is reproducible."""
    urls: set[str] = set()
    for path in paths:
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            continue
        decoder = json.JSONDecoder()
        records: list[dict] = []
        offset = 0
        while offset < len(text):
            while offset < len(text) and text[offset].isspace():
                offset += 1
            if offset >= len(text):
                break
            if text.startswith("//", offset):
                newline = text.find("\n", offset)
                offset = len(text) if newline < 0 else newline + 1
                continue
            payload, offset = decoder.raw_decode(text, offset)
            records.extend(payload if isinstance(payload, list) else [payload])
        for record in records:
            if isinstance(record, dict) and record.get("source_url"):
                urls.add(https_wechat_url(str(record["source_url"])))
    return urls


def export(database: Path, output: Path, limit: int, *, since: str = "",
           excluded_urls: set[str] | None = None) -> list[dict]:
    if since:
        date.fromisoformat(since)
    excluded_urls = excluded_urls or set()
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        """
        SELECT p.document, a.raw_text
        FROM projects AS p
        JOIN articles AS a ON a.id = json_extract(p.document, '$.article_id')
        WHERE COALESCE(json_extract(p.document, '$.demo_data'), 0) = 0
          AND p.status IN ('published', 'needs_review')
          AND (
              trim(COALESCE(p.signup_deadline, '')) = ''
              OR date(p.signup_deadline) >= date('now')
          )
          AND (
              trim(COALESCE(json_extract(p.document, '$.practice_end'), '')) = ''
              OR date(json_extract(p.document, '$.practice_end')) >= date('now')
          )
          AND length(trim(COALESCE(a.raw_text, ''))) >= 120
          AND trim(COALESCE(json_extract(p.document, '$.publish_date'), '')) != ''
          AND (? = '' OR date(json_extract(p.document, '$.publish_date')) >= date(?))
          AND trim(COALESCE(p.source_url, '')) != ''
        ORDER BY a.id DESC
        """,
        (since, since),
    ).fetchall()
    connection.close()

    records: list[dict] = []
    for row in rows:
        project = json.loads(row["document"])
        source_url = https_wechat_url(project["source_url"])
        if source_url in excluded_urls:
            continue
        record = {
            "source_account": project["source_account"],
            "source_url": source_url,
            "title": project["title"],
            "publish_date": project["publish_date"],
            "raw_text": row["raw_text"],
        }
        images = project.get("image_sources") or []
        if images:
            record["images"] = images
        records.append(record)
        if limit and len(records) >= limit:
            break

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )
    return records


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--since", default="", help="只导出此发布日期及之后的文章（YYYY-MM-DD）")
    parser.add_argument("--exclude", action="append", type=Path, default=[],
                        help="排除已交付 JSON/JSONL 中的 source_url；可重复传入")
    args = parser.parse_args()
    records = export(
        args.database,
        args.output,
        args.limit,
        since=args.since,
        excluded_urls=excluded_source_urls(args.exclude),
    )
    print(json.dumps({
        "output": str(args.output),
        "count": len(records),
        "titles": [record["title"] for record in records],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
