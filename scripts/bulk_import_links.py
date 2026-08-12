#!/usr/bin/env python3
"""批量导入公众号文章链接。

用法：
    python3 scripts/bulk_import_links.py links.txt
    python3 scripts/bulk_import_links.py links.txt --dry-run      # 只看会做什么
    python3 scripts/bulk_import_links.py links.txt --delay 5      # 放慢一点
    python3 scripts/bulk_import_links.py links.txt --retry-failed # 只重试上次没抓到的

links.txt 每行一个 mp.weixin.qq.com 链接，空行和 # 开头的行会跳过。

为什么走链接而不是批量采集器：批量采集需要微信登录态（Token/Cookie），
凭证会过期、会触发验证码，而且那个上游采集器脚本本身不在这个仓库里。
单篇公开链接的抓取不需要凭证，失败也只影响一条，合规上也干净。

抓不到正文时不会编——链接会存成待补线索，你在微信里打开、复制正文，
再用网页端或对话粘贴进来即可。这条规则贯穿整个项目。
"""
from __future__ import annotations

import argparse
import json
import random
import sqlite3
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from collector_settings import credentials as collector_credentials  # noqa: E402
from database import Database  # noqa: E402
from wechat_ingest import import_wechat_link  # noqa: E402


def read_links(path: Path) -> list[str]:
    links: list[str] = []
    seen: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        url = line.strip()
        if not url or url.startswith("#"):
            continue
        # 同一批里贴重了很常见，先去个重，省得白抓
        if url in seen:
            continue
        seen.add(url)
        links.append(url)
    return links


def already_imported(db_path: str) -> dict[str, str]:
    """已经抓过的链接 -> 当时的状态。用于断点续跑。"""
    done: dict[str, str] = {}
    with sqlite3.connect(db_path) as con:
        for url, status in con.execute(
            "SELECT source_url, collector_status FROM articles WHERE source_url != ''"
        ):
            # 后写的覆盖先写的，保留最后一次的结果
            done[url] = status or ""
    return done


def main() -> int:
    parser = argparse.ArgumentParser(description="批量导入公众号链接")
    parser.add_argument("links", type=Path, help="链接列表文件，每行一个")
    parser.add_argument("--delay", type=float, default=3.0,
                        help="每条之间的间隔秒数，默认 3；抓得太快容易触发风控")
    parser.add_argument("--dry-run", action="store_true", help="只列出将要处理的链接，不实际抓取")
    parser.add_argument("--retry-failed", action="store_true",
                        help="只重试之前没抓到正文的链接")
    parser.add_argument("--limit", type=int, default=0, help="本次最多处理多少条，0 为不限")
    parser.add_argument("--report", type=Path, default=ROOT / "data" / "import_report.md",
                        help="人工复核清单的输出路径")
    args = parser.parse_args()

    if not args.links.is_file():
        print(f"找不到链接文件：{args.links}", file=sys.stderr)
        return 1

    database = Database()
    links = read_links(args.links)
    done = already_imported(str(database.path))

    pending: list[str] = []
    for url in links:
        status = done.get(url)
        if status is None:
            pending.append(url)
        elif args.retry_failed and status.startswith("fetch_"):
            pending.append(url)
    if args.limit:
        pending = pending[: args.limit]

    print(f"链接文件共 {len(links)} 条，其中 {len(links) - len(pending)} 条此前已处理过")
    print(f"本次将处理 {len(pending)} 条，间隔 {args.delay}s\n")
    if args.dry_run:
        for url in pending[:20]:
            print("  " + url)
        if len(pending) > 20:
            print(f"  …… 其余 {len(pending) - 20} 条")
        return 0
    if not pending:
        print("没有需要处理的链接。")
        return 0

    _, stored_cookie = collector_credentials()
    tally: Counter[str] = Counter()
    imported: list[dict] = []
    failed: list[tuple[str, str]] = []

    for index, url in enumerate(pending, 1):
        try:
            result = import_wechat_link(
                database, {"source_url": url, "input_type": "wechat_url"},
                fallback_cookie=stored_cookie,
            )
        except Exception as exc:  # 单条炸掉不该带走整批
            tally["exception"] += 1
            failed.append((url, f"{type(exc).__name__}: {exc}"))
            print(f"[{index}/{len(pending)}] 异常  {type(exc).__name__}  {url}")
            continue

        status = result.get("status", "?")
        tally[status] += 1
        if status in {"imported", "merged"} or result.get("project"):
            project = result.get("project") or {}
            imported.append(project)
            missing = "、".join(project.get("uncertain_fields", [])) or "无"
            print(f"[{index}/{len(pending)}] {status:<16} {project.get('title', '')[:30]}  待确认：{missing}")
        else:
            failed.append((url, result.get("truthfulness_note") or result.get("action_required", "")))
            print(f"[{index}/{len(pending)}] {status:<16} {url}")

        if index < len(pending):
            # 加一点随机抖动，避免形成固定节奏
            time.sleep(max(0.0, args.delay + random.uniform(-0.5, 0.5)))

    print("\n=== 汇总 ===")
    for status, count in tally.most_common():
        print(f"  {status}: {count}")

    write_report(args.report, imported, failed, tally)
    print(f"\n人工复核清单已写入：{args.report}")
    print("提示：导入的项目都是 needs_review，核验通过后才会进入正式推荐。")
    return 0


def write_report(path: Path, imported: list[dict], failed: list[tuple[str, str]], tally: Counter) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# 批量导入复核清单（{datetime.now():%Y-%m-%d %H:%M}）",
        "",
        "导入的项目一律是 `needs_review`，核验通过后才会进入正式推荐。",
        "**逐条打开原文核对报名截止和参与资格**——这两项写错的代价最大。",
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

    if failed:
        lines += ["", f"## 没抓到正文（{len(failed)} 条）", "",
                  "这些链接已存为待补线索。在微信里打开、复制正文，再从网页端或对话里粘贴导入即可。", ""]
        for url, reason in failed:
            lines += [f"- {url}", f"  - {reason}"]

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
