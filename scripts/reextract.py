#!/usr/bin/env python3
"""用当前的抽取规则，把已入库的原文重新抽一遍。

    python3 scripts/reextract.py                 # 预演，只打印会改什么，不写库
    python3 scripts/reextract.py --apply         # 真的写回去
    python3 scripts/reextract.py --apply --only-status needs_review

为什么需要它：抽取规则会一直改——每次拿到新一批真实数据，都会发现之前
没考虑到的写法。规则改了，之前入库的项目卡还停在旧结果上。让采集方把
几十篇重推一遍既麻烦又要等人，而原文我们本来就一份不落地存着（articles
表），重抽是本地就能做完的事。

默认只碰 needs_review 和 expired——这两种还没人拍板。published 和 rejected
可能已经有人工核验过了，重抽会把人的判断洗掉，要动得显式指定。

预演是默认行为。抽取规则的改动经常有意外的连带影响（今天就遇到过：给地点
加了个看似无害的兜底，结果在合并时把更准确的值顶掉了），先看清楚再落库。
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from database import Database  # noqa: E402
from domain import extract_project  # noqa: E402

# 拿出来比对的字段。都是会直接影响"能不能报、报不报得上"的。
WATCHED = (
    "signup_deadline", "practice_start", "practice_end",
    "organizer", "signup_method", "status",
)


def describe(project: dict, field: str) -> str:
    if field == "eligibility":
        return (project.get("eligibility") or {}).get("restriction_text") or "(空)"
    if field == "location":
        return (project.get("location") or {}).get("detail") or "(空)"
    return str(project.get(field) or "(空)")


def main() -> int:
    parser = argparse.ArgumentParser(description="用当前规则重抽已入库的原文")
    parser.add_argument("--apply", action="store_true", help="真的写回数据库（默认只预演）")
    parser.add_argument("--only-status", default="needs_review,expired",
                        help="只重抽这些状态的项目，逗号分隔；传 all 表示不限")
    args = parser.parse_args()

    database = Database()
    allowed = None if args.only_status == "all" else {
        s.strip() for s in args.only_status.split(",") if s.strip()
    }

    # 每个链接保留最后一份原文——订正推送会留下多条，最后一条才是最新的。
    latest: dict[str, dict] = {}
    with database.connect() as db:
        for row in db.execute(
            "SELECT source_url, source_account, title, raw_text, input_type "
            "FROM articles WHERE source_url != '' AND raw_text IS NOT NULL ORDER BY id"
        ):
            latest[row["source_url"]] = dict(row)

    projects = [p for p in database.list_projects(include_expired=True) if not p.get("demo_data")]
    print(f"库里非演示项目 {len(projects)} 条，存档原文 {len(latest)} 篇\n")

    changed = 0
    skipped_no_text = 0
    skipped_status = 0
    tally_before: Counter[str] = Counter()
    tally_after: Counter[str] = Counter()

    for summary in projects:
        project = database.get_project(summary["id"])
        if not project:
            continue
        url = project.get("source_url") or ""
        article = latest.get(url)
        if not article:
            skipped_no_text += 1
            continue
        if allowed is not None and project.get("status") not in allowed:
            skipped_status += 1
            continue

        fresh = extract_project(article["raw_text"], {
            "title": article["title"],
            "source_account": article["source_account"],
            "source_url": url,
            "input_type": article["input_type"] or "copied_text",
        })
        diffs = [(f, describe(project, f), describe(fresh, f))
                 for f in WATCHED + ("eligibility", "location")
                 if describe(project, f) != describe(fresh, f)]
        tally_before[project["status"]] += 1
        tally_after[fresh["status"]] += 1
        if not diffs:
            continue

        changed += 1
        print(f"── {project['title'][:44]}")
        for field, old, new in diffs:
            print(f"     {field:<16} {old[:42]}")
            print(f"     {'':<16}   → {new[:42]}")

        if args.apply:
            # 保留身份、创建时间和原文归档指针；其余以重抽结果为准。
            fresh["id"] = project["id"]
            fresh["created_at"] = project.get("created_at", fresh["created_at"])
            fresh["article_id"] = project.get("article_id", fresh.get("article_id"))
            database.upsert_project(fresh, note="抽取规则更新后重抽")
        print()

    print("=" * 66)
    print(f"有变化 {changed} 条；跳过 {skipped_no_text} 条（找不到存档原文）、"
          f"{skipped_status} 条（状态不在 {args.only_status} 内）")
    print(f"状态分布  重抽前 {dict(tally_before)}")
    print(f"          重抽后 {dict(tally_after)}")
    if not args.apply:
        print("\n（预演模式，没有写数据库。确认无误后加 --apply）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
