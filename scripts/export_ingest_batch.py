#!/usr/bin/env python3
"""Export a deduplicated batch of complete source articles for handoff."""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


def https_wechat_url(value: str) -> str:
    parts = urlsplit(value.strip())
    if parts.hostname not in {"mp.weixin.qq.com", "www.mp.weixin.qq.com"}:
        return value.strip()
    return urlunsplit(("https", "mp.weixin.qq.com", parts.path, parts.query, ""))


def export(database: Path, output: Path, limit: int) -> list[dict]:
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        """
        SELECT p.document, a.raw_text
        FROM projects AS p
        JOIN articles AS a ON a.id = json_extract(p.document, '$.article_id')
        WHERE COALESCE(json_extract(p.document, '$.demo_data'), 0) = 0
          AND length(trim(COALESCE(a.raw_text, ''))) >= 120
          AND trim(COALESCE(json_extract(p.document, '$.publish_date'), '')) != ''
          AND trim(COALESCE(p.source_url, '')) != ''
        ORDER BY a.id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    connection.close()

    records: list[dict] = []
    for row in rows:
        project = json.loads(row["document"])
        record = {
            "source_account": project["source_account"],
            "source_url": https_wechat_url(project["source_url"]),
            "title": project["title"],
            "publish_date": project["publish_date"],
            "raw_text": row["raw_text"],
        }
        images = project.get("image_sources") or []
        if images:
            record["images"] = images
        records.append(record)

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
    args = parser.parse_args()
    records = export(args.database, args.output, args.limit)
    print(json.dumps({
        "output": str(args.output),
        "count": len(records),
        "titles": [record["title"] for record in records],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
