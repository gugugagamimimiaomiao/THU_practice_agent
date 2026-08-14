#!/usr/bin/env python3
"""Build and optionally import a truthful historical WeChat opportunity snapshot.

An opportunity qualifies only when its article has an explicit signup deadline
that was not earlier than the publication date. Importing never revives expired
projects: today's status is still computed by the normal ingestion pipeline.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from database import Database  # noqa: E402
from domain import extract_project, parse_iso_date  # noqa: E402
from opportunity_filter import candidate_decision  # noqa: E402
from wechat_ingest import import_article_text  # noqa: E402


UPDATE_MARKERS = re.compile(r"(?:活动)?启动|倒计时\d+天|报名中|火热报名中", re.I)
TITLE_NOISE = re.compile(r"[^0-9a-zA-Z\u4e00-\u9fff]+")


def normalized_opportunity_title(title: str) -> str:
    value = UPDATE_MARKERS.sub("", str(title))
    return TITLE_NOISE.sub("", value).lower()


def load_articles(paths: list[Path]) -> list[dict[str, Any]]:
    articles: dict[str, dict[str, Any]] = {}
    for path in paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict) and isinstance(payload.get("articles"), list):
            rows = payload["articles"]
        elif isinstance(payload, dict) and payload.get("link"):
            rows = [payload]
        else:
            rows = []
        for article in rows:
            if not isinstance(article, dict):
                continue
            url = str(article.get("link") or "").strip()
            if url:
                articles[url] = article
    return list(articles.values())


def historical_candidate(article: dict[str, Any]) -> dict[str, Any] | None:
    published = parse_iso_date(article.get("publishDate"))
    if not published or not candidate_decision(article)["candidate"]:
        return None
    metadata = {
        "input_type": "wechat_historical_backfill",
        "source_account": str(article.get("account") or "").strip(),
        "source_url": str(article.get("link") or "").strip(),
        "title": str(article.get("title") or "").strip(),
        "publish_date": published.isoformat(),
    }
    raw_text = "\n".join(part for part in (metadata["title"], str(article.get("content") or "").strip()) if part)
    project = extract_project(raw_text, metadata, today=published)
    deadline = parse_iso_date(project.get("signup_deadline"))
    practice_end = parse_iso_date(project.get("practice_end"))
    if not deadline or deadline < published or (practice_end and practice_end < published):
        return None
    return {
        "article": article,
        "metadata": {**metadata, "images": list(article.get("images") or [])},
        "raw_text": raw_text,
        "project": project,
        "historical_valid_at": published.isoformat(),
        "historical_signup_deadline": deadline.isoformat(),
    }


def deduplicate(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for record in records:
        project = record["project"]
        key = (normalized_opportunity_title(project["title"]), record["historical_signup_deadline"])
        groups.setdefault(key, []).append(record)

    selected: list[dict[str, Any]] = []
    for group in groups.values():
        group.sort(key=lambda item: (len(item["raw_text"]), item["historical_valid_at"]), reverse=True)
        primary = group[0]
        primary["alternate_source_urls"] = [
            item["metadata"]["source_url"] for item in group[1:] if item["metadata"]["source_url"]
        ]
        selected.append(primary)
    return sorted(selected, key=lambda item: (item["historical_valid_at"], item["project"]["title"]), reverse=True)


def import_records(database: Database, records: list[dict[str, Any]]) -> dict[str, int]:
    outcomes = {"imported": 0, "not_opportunity": 0}
    for record in records:
        result = import_article_text(
            database,
            record["metadata"],
            record["raw_text"],
            collector_status="historical_wewe_snapshot",
            log_channel="historical_wechat_snapshot",
            origin_label="历史公众号快照",
        )
        if result["status"] != "imported":
            outcomes["not_opportunity"] += 1
            continue
        project = result["project"]
        project.update({
            "historical_snapshot": True,
            "historical_valid_at": record["historical_valid_at"],
            "historical_validity_basis": "explicit_signup_deadline",
            "historical_signup_deadline": record["historical_signup_deadline"],
            "alternate_source_urls": record.get("alternate_source_urls", []),
        })
        database.upsert_project(project, note="记录文章发布时仍可报名的历史证据", log_activity=False)
        outcomes["imported"] += 1
    return outcomes


def snapshot_payload(records: list[dict[str, Any]], *, source_articles: int) -> dict[str, Any]:
    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "definition": "文章发布当日存在明确报名截止日，且截止日不早于发布日期；不代表今天仍可报名。",
        "source_articles": source_articles,
        "historically_valid_unique_opportunities": len(records),
        "opportunities": [
            {
                "title": record["project"]["title"],
                "source_account": record["metadata"]["source_account"],
                "source_url": record["metadata"]["source_url"],
                "alternate_source_urls": record.get("alternate_source_urls", []),
                "publish_date": record["historical_valid_at"],
                "signup_deadline": record["historical_signup_deadline"],
                "practice_start": record["project"].get("practice_start"),
                "practice_end": record["project"].get("practice_end"),
                "deadline_evidence": record["project"].get("field_evidence", {}).get("signup_deadline", {}),
            }
            for record in records
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="生成公众号历史有效机会快照")
    parser.add_argument("batches", nargs="+", type=Path, help="WeWe collector 输出的 articles_*.json")
    parser.add_argument("--output", type=Path, default=ROOT / "data" / "exports" / "historical_wechat_snapshot.json")
    parser.add_argument("--database", type=Path, default=ROOT / "data" / "practice_xiaoda.db")
    parser.add_argument("--import-database", action="store_true", help="导入本地库；项目今天已过期时仍保持 expired")
    args = parser.parse_args()

    articles = load_articles(args.batches)
    candidates = [record for article in articles if (record := historical_candidate(article))]
    records = deduplicate(candidates)
    payload = snapshot_payload(records, source_articles=len(articles))
    if args.import_database:
        payload["database_import"] = import_records(Database(args.database), records)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "ok": True,
        "source_articles": len(articles),
        "historically_valid_links": len(candidates),
        "historically_valid_unique_opportunities": len(records),
        "output": str(args.output),
        "database_import": payload.get("database_import"),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
