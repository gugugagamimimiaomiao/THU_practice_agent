#!/usr/bin/env python3
"""导入外部采集器交付的文章正文（JSONL）。

    python3 scripts/import_articles.py articles.jsonl --check   # 只校验格式，不写库
    python3 scripts/import_articles.py articles.jsonl           # 真导入
    python3 scripts/import_articles.py articles.jsonl --limit 20

交付格式见 DATA_HANDOFF.md。一行一篇文章，六个字段：

    {"source_account": "...", "source_url": "https://mp.weixin.qq.com/s/...",
     "title": "...", "publish_date": "2026-08-05", "raw_text": "正文全文……",
     "images": ["https://mmbiz.qpic.cn/..."]}

为什么只要原文、不要对方替我们解析好的字段：抽取规则（哪天截止、谁能报、
报销多少、拿不准的字段怎么标）是这个项目的核心逻辑，写在 domain.py 里，
有测试覆盖，而且刻意保守——拿不准就标 uncertain 而不是猜。如果让采集方
各自解析一遍，我们收到的就是别人的猜测，而且看不出哪些是猜的。原文是
唯一能回查、能重跑、能追责的东西。

--check 是给采集方自查用的：跑通了再发过来，省得来回。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from database import Database  # noqa: E402
from wechat_ingest import import_article_text, validate_wechat_url  # noqa: E402

REQUIRED = ("source_account", "source_url", "title", "publish_date", "raw_text")
# 少于这个长度的“正文”基本是抓到了摘要、验证页或图片占位，不是真正文。
# 宁可打回去重抓，也不要让半截正文进库——项目卡会缺字段，而缺的原因看不出来。
MIN_TEXT_LENGTH = 120


def check_record(index: int, record: Any) -> list[str]:
    """返回这一条的所有问题；空列表表示可用。"""
    problems: list[str] = []
    if not isinstance(record, dict):
        return [f"第 {index} 行不是 JSON 对象"]

    for field in REQUIRED:
        if field not in record:
            problems.append(f"缺字段 {field}")
        elif not str(record[field]).strip():
            problems.append(f"{field} 是空的")

    url = str(record.get("source_url", "")).strip()
    if url:
        ok, message = validate_wechat_url(url)
        if not ok:
            problems.append(f"source_url 不合法：{message}")

    date_text = str(record.get("publish_date", "")).strip()
    if date_text:
        try:
            datetime.strptime(date_text, "%Y-%m-%d")
        except ValueError:
            problems.append(f"publish_date 要写成 2026-08-05 这样的格式，收到的是「{date_text}」")

    text = str(record.get("raw_text", "")).strip()
    if text and len(text) < MIN_TEXT_LENGTH:
        problems.append(f"raw_text 只有 {len(text)} 字，像是摘要或验证页而不是正文")
    if "请在微信客户端打开链接" in text or "环境异常" in text:
        problems.append("raw_text 疑似验证页而不是文章正文")

    images = record.get("images")
    if images is not None and not isinstance(images, list):
        problems.append("images 要是数组；没有图片就不写这个字段")
    return problems


def read_records(path: Path) -> tuple[list[tuple[int, dict]], list[str]]:
    """返回 (可解析的记录, 解析失败的说明)。"""
    records: list[tuple[int, dict]] = []
    broken: list[str] = []
    for index, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("//"):
            continue
        try:
            records.append((index, json.loads(stripped)))
        except json.JSONDecodeError as exc:
            # 单行坏掉不该让整个文件不可用——报出行号，其余照常处理。
            broken.append(f"第 {index} 行 JSON 解析失败：{exc.msg}（第 {exc.colno} 字符）")
    return records, broken


def already_imported(db_path: str) -> set[str]:
    done: set[str] = set()
    with sqlite3.connect(db_path) as con:
        for (url,) in con.execute("SELECT source_url FROM articles WHERE source_url IS NOT NULL AND source_url != ''"):
            done.add(url)
    return done


def import_metadata(record: dict[str, Any]) -> dict[str, Any]:
    """Build ingestion metadata without dropping optional image evidence."""
    return {
        "input_type": "collector_file",
        "source_account": str(record["source_account"]).strip(),
        "source_url": str(record["source_url"]).strip(),
        "title": str(record["title"]).strip(),
        "publish_date": str(record["publish_date"]).strip(),
        "images": [str(url).strip() for url in record.get("images", []) if str(url).strip()],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="导入采集器交付的文章正文")
    parser.add_argument("path", type=Path, help="JSONL 文件")
    parser.add_argument("--check", action="store_true", help="只校验格式，不写数据库")
    parser.add_argument("--limit", type=int, default=0, help="本次最多导入多少条，0 为不限")
    parser.add_argument("--report", type=Path, default=ROOT / "data" / "import_report.md")
    parser.add_argument("--allow-short", action="store_true",
                        help="放行过短正文（默认拦截，只在确认对方就是给了短通知时用）")
    parser.add_argument("--review-all", action="store_true",
                        help="强制全部进人工核验队列，不让任何一条直接进正式推荐")
    parser.add_argument("--corpus-only", action="store_true",
                        help="整批只作写作语料：保存文章供写作参考，不做项目抽取、不进推荐。"
                             "用于回采历史实践总结、纪实、志愿故事")
    args = parser.parse_args()

    if not args.path.is_file():
        print(f"找不到文件：{args.path}", file=sys.stderr)
        return 1

    records, broken = read_records(args.path)
    print(f"文件共 {len(records) + len(broken)} 行，其中 {len(records)} 行能解析\n")
    for message in broken:
        print(f"  ✗ {message}")

    problems: dict[int, list[str]] = {}
    for index, record in records:
        found = check_record(index, record)
        if args.allow_short:
            found = [p for p in found if "像是摘要或验证页" not in p]
        if found:
            problems[index] = found

    if problems:
        print(f"\n有 {len(problems)} 行有问题：")
        for index in sorted(problems)[:30]:
            print(f"  第 {index} 行：" + "；".join(problems[index]))
        if len(problems) > 30:
            print(f"  …… 其余 {len(problems) - 30} 行同样有问题")

    usable = [(i, r) for i, r in records if i not in problems]
    print(f"\n可用 {len(usable)} 条，需要修 {len(problems) + len(broken)} 条")

    if args.check:
        print("\n（--check 模式，没有写数据库）")
        return 1 if problems or broken else 0
    if not usable:
        print("没有可导入的记录。")
        return 1

    database = Database()
    done = already_imported(str(database.path))
    pending = [(i, r) for i, r in usable if str(r["source_url"]).strip() not in done]
    skipped = len(usable) - len(pending)
    if args.limit:
        pending = pending[: args.limit]
    print(f"其中 {skipped} 条此前已导入过，本次处理 {len(pending)} 条\n")
    if not pending:
        # 这里必须直接返回。继续走下去会用一份空结果覆盖掉上次的复核清单——
        # 而复核清单正是人工核验的工作台，重跑一次就没了很难受。
        print("没有新内容，复核清单保持上次的结果不变。")
        return 0

    tally: Counter[str] = Counter()
    imported: list[dict] = []
    rejected: list[tuple[str, str]] = []

    for position, (line_no, record) in enumerate(pending, 1):
        metadata = import_metadata(record)
        try:
            result = import_article_text(
                Database(), metadata, str(record["raw_text"]),
                collector_status="collector_file",
                log_channel="ingest",
                origin_label="采集文件导入",
                corpus_only=args.corpus_only or bool(record.get("corpus_only")),
            )
        except Exception as exc:  # 单条炸掉不该带走整批
            tally["exception"] += 1
            rejected.append((metadata["title"] or metadata["source_url"], f"{type(exc).__name__}: {exc}"))
            print(f"[{position}/{len(pending)}] 异常  第 {line_no} 行  {type(exc).__name__}")
            continue

        status = result["status"]
        tally[status] += 1
        if status == "corpus_only":
            print(f"[{position}/{len(pending)}] 已入语料  {metadata['title'][:30]}")
            continue
        if status == "imported":
            project = result["project"]
            if args.review_all and project["status"] == "published":
                # 五项关键字段都抽到了就会自动发布。抽得"干净"不等于抽得对——
                # 今天就修过三个抽出自信错值的 bug。头几批外部数据建议全过一遍人工。
                project["status"] = "needs_review"
                project = database.upsert_project(project, note="按 --review-all 强制进人工核验")
            imported.append(project)
            missing = "、".join(project.get("uncertain_fields", [])) or "无"
            mark = "待核验" if project["status"] == "needs_review" else "已发布"
            print(f"[{position}/{len(pending)}] {mark}  {project['title'][:26]}  待确认：{missing}")
        else:
            reason = "；".join(result.get("decision_reasons", [])) or result.get("action_required", "")
            rejected.append((metadata["title"], reason))
            print(f"[{position}/{len(pending)}] {status:<16} {metadata['title'][:28]}")

    print("\n=== 汇总 ===")
    for status, count in tally.most_common():
        print(f"  {status}: {count}")

    published = [p for p in imported if p["status"] == "published"]
    reviewing = [p for p in imported if p["status"] == "needs_review"]
    write_report(args.report, imported, rejected, tally, problems, broken)
    print(f"\n  其中已直接发布 {len(published)} 条，待人工核验 {len(reviewing)} 条")
    if published:
        # 这里必须说准。五项关键字段都抽到就会自动发布——"抽得干净"不等于"抽得对"。
        print("  已发布的会立刻出现在推荐里。抽取干净不等于抽得对，")
        print("  建议至少抽查报名截止和参与资格；想全部先过人工用 --review-all。")
    print(f"\n复核清单：{args.report}")
    return 0


def write_report(path: Path, imported: list[dict], rejected: list[tuple[str, str]],
                 tally: Counter, problems: dict[int, list[str]], broken: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    auto = sum(1 for p in imported if p["status"] == "published")
    lines = [
        f"# 采集文件导入复核清单（{datetime.now():%Y-%m-%d %H:%M}）",
        "",
        "**逐条打开原文核对报名截止和参与资格**——这两项写错的代价最大：",
        "截止日期错了会让人错过报名，资格写错会让人白准备一场。",
        "",
        f"五项关键字段都抽到的会自动进入正式推荐，本次有 **{auto}** 条属于这种情况；"
        "其余进人工核验队列。抽得干净不等于抽得对，自动发布的那些同样建议抽查。",
        "想让全部先过人工，导入时加 `--review-all`。",
        "",
        "## 汇总",
        "",
    ]
    for status, count in tally.most_common():
        lines.append(f"- {status}: {count}")

    if imported:
        lines += ["", f"## 待核验项目（{len(imported)} 条）", ""]
        for project in imported:
            missing = "、".join(project.get("uncertain_fields", [])) or "无"
            lines += [
                f"### {project.get('title', '(无标题)')}",
                "",
                f"- 报名截止：{project.get('signup_deadline') or '**待确认**'}",
                f"- 实践时间：{project.get('practice_start') or '待确认'} 至 {project.get('practice_end') or '待确认'}",
                f"- 地点：{(project.get('location') or {}).get('detail') or '待确认'}",
                f"- 参与资格：{(project.get('eligibility') or {}).get('restriction_text') or '待确认'}",
                f"- 经费：{(project.get('reimbursement') or {}).get('text') or '待确认'}",
                f"- 待确认字段：{missing}",
                f"- 置信度：{round(float(project.get('confidence') or 0) * 100)}%",
                f"- 原文：{project.get('source_url') or '（无链接）'}",
                "",
            ]

    if rejected:
        lines += ["", f"## 未进入机会库（{len(rejected)} 条）", "",
                  "多为实践纪实、活动回顾、行前预告——有实践内容但没有可报名的行动信号。",
                  "如果其中有确实在招募的，说明判断规则漏了，把标题反馈回来即可。", ""]
        for title, reason in rejected:
            lines += [f"- {title or '(无标题)'}", f"  - {reason}"]

    if problems or broken:
        lines += ["", "## 需要采集方修的（未导入）", ""]
        for message in broken:
            lines.append(f"- {message}")
        for index in sorted(problems):
            lines.append(f"- 第 {index} 行：" + "；".join(problems[index]))

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
