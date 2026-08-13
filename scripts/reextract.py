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
import os
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from database import Database  # noqa: E402
from domain import extract_project  # noqa: E402
from opportunity_filter import candidate_decision  # noqa: E402

# 拿出来比对的字段。都是会直接影响"能不能报、报不报得上"的。
WATCHED = (
    "signup_deadline", "practice_start", "practice_end",
    "organizer", "signup_method", "status",
)


def run_pending_ocr(database: Database, *, apply: bool) -> int:
    """识别 image_ocr_status=pending 的项目配图，把文字并入正文后重抽。

    投稿接口刻意不做同步 OCR——下载加识别几十秒，会把采集方的推送拖到超时。
    所以入库时只存图片 URL、状态标 pending，由这一趟补上。

    没装 tesseract 时不报错也不假装成功：状态改成 runtime_unavailable，
    图片 URL 保留，装好引擎再跑一次就行。
    """
    from wechat_image_ocr import ocr_wechat_images  # 只有这条路用得到，放函数里导入

    pending = []
    for summary in database.list_projects(include_expired=True):
        project = database.get_project(summary["id"])
        if project and project.get("image_ocr_status") == "pending" and project.get("image_sources"):
            pending.append(project)
    print(f"待识别配图的项目 {len(pending)} 条\n")
    if not pending:
        return 0

    done = 0
    for project in pending:
        images = project["image_sources"]
        result = ocr_wechat_images(images, cookie=os.getenv("WECHAT_COOKIE", ""))
        if not result.runtime_available:
            print(f"── 未装 OCR 引擎，跳过并保留图片 URL：{project['title'][:36]}")
            project["image_ocr_status"] = "runtime_unavailable"
            if apply:
                database.upsert_project(project, note="缺少 OCR 引擎，配图待后续识别")
            continue

        print(f"── {project['title'][:36]}")
        print(f"     {len(images)} 张图，识别到文字 {result.completed} 张，失败 {result.failed} 张")
        if not result.text.strip():
            project["image_ocr_status"] = "partial_or_failed"
            if apply:
                database.upsert_project(project, note="配图未识别出文字")
            continue

        article = _latest_article(database, project.get("source_url", ""))
        base_text = article["raw_text"] if article else project.get("title", "")
        merged_text = f"{base_text}\n\n【公众号配图 OCR】\n{result.text}"
        fresh = extract_project(merged_text, {
            "title": project.get("title", ""),
            "source_account": project.get("source_account", ""),
            "source_url": project.get("source_url", ""),
            "input_type": "ocr_text",
        })
        fresh["id"] = project["id"]
        fresh["created_at"] = project.get("created_at", fresh["created_at"])
        fresh["image_sources"] = images
        fresh["image_ocr_status"] = "completed"
        fresh["risk_notes"] = list(dict.fromkeys(
            fresh.get("risk_notes", []) + ["关键字段来自配图 OCR，发布前请与原图核对日期、金额和联系方式"]))
        for field in ("signup_deadline", "practice_start", "location", "eligibility"):
            before, after = describe(project, field), describe(fresh, field)
            if before != after:
                print(f"     {field:<16} {before[:34]}  →  {after[:34]}")
        if apply:
            database.upsert_project(fresh, note="配图 OCR 后重抽")
        done += 1

    print()
    print(f"完成 {done} 条" + ("" if apply else "（预演，没有写库；确认后加 --apply）"))
    return 0


def _latest_article(database: Database, source_url: str) -> dict | None:
    if not source_url:
        return None
    with database.connect() as db:
        row = db.execute(
            "SELECT raw_text FROM articles WHERE source_url=? ORDER BY id DESC LIMIT 1", (source_url,)
        ).fetchone()
    return dict(row) if row else None


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
    parser.add_argument("--ocr", action="store_true",
                        help="先识别待处理的原文配图，把识别文字并入正文再重抽")
    args = parser.parse_args()

    if args.ocr:
        return run_pending_ocr(Database(), apply=args.apply)

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
    retired: list[str] = []
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

        # 分类规则也可能改。改严之后，之前放行的内容会变成"根本不该是项目卡"，
        # 光重抽字段没用——那张卡还挂在机会库里冒充可报名项目，必须撤下来。
        # 原文留在 articles 表里做审计，不删。
        if not candidate_decision({"title": project.get("title", ""),
                                   "content": article["raw_text"]})["candidate"]:
            retired.append(project["title"])
            print(f"── 撤下（现已判为非招募内容）  {project['title'][:46]}")
            if args.apply:
                database.delete_projects_by_source(url, note="分类规则更新后判定为非招募内容，撤出机会库")
            print()
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
    print(f"字段有变化 {changed} 条；撤下 {len(retired)} 条（现已判为非招募内容）")
    print(f"跳过 {skipped_no_text} 条（找不到存档原文）、"
          f"{skipped_status} 条（状态不在 {args.only_status} 内）")
    print(f"状态分布  重抽前 {dict(tally_before)}")
    print(f"          重抽后 {dict(tally_after)}")
    if not args.apply:
        print("\n（预演模式，没有写数据库。确认无误后加 --apply）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
