#!/usr/bin/env python3
"""WeWe RSS 现状速览：账号还能不能用、每个源多久没更新了。

    python3 scripts/wewe_state.py            # 看现状
    python3 scripts/wewe_state.py --json     # 给脚本用

只读 `data/wewe-rss.db`，不碰网络、不刷新任何东西。

为什么单独做一个：`accounts.status` 是这条链路的总开关——它一变成 0，
所有刷新都会静默地什么都拿不到，而界面上看不出来。每次动手之前先看一眼
这个数，比刷完之后猜"为什么没有新文章"省事得多。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = Path(os.getenv("WEWE_DB_PATH", str(ROOT / "data" / "wewe-rss.db")))


def stamp(value: int | None, unit_ms: bool = False) -> str:
    if not value:
        return "从未"
    seconds = int(value) / 1000 if unit_ms else int(value)
    return datetime.fromtimestamp(seconds).strftime("%Y-%m-%d %H:%M")


def days_ago(value: int | None) -> float | None:
    if not value:
        return None
    return (time.time() - int(value)) / 86400


def collect(db_path: Path) -> dict:
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        accounts = [
            {"id": row["id"], "name": row["name"], "status": row["status"], "updated_at": row["updated_at"]}
            for row in connection.execute("SELECT id, name, status, updated_at FROM accounts")
        ]
        feeds = [
            dict(row)
            for row in connection.execute(
                """
                SELECT f.id, f.mp_name, f.status, f.sync_time,
                       (SELECT COUNT(*) FROM articles a WHERE a.mp_id = f.id) AS articles,
                       (SELECT MAX(a.publish_time) FROM articles a WHERE a.mp_id = f.id) AS latest
                FROM feeds f
                ORDER BY latest DESC NULLS LAST
                """
            )
        ]
        total = connection.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    finally:
        connection.close()
    return {"accounts": accounts, "feeds": feeds, "total_articles": total}


def render(state: dict) -> int:
    """打印人看的版本，返回建议的退出码：0 能刷，1 得先重新扫码。"""
    print("=" * 62)
    print(f"WeWe RSS 现状  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 62)

    usable = False
    print("\n【微信读书账号】")
    if not state["accounts"]:
        print("  [x] 一个账号都没有 —— 需要先扫码添加")
    for account in state["accounts"]:
        if account["status"] == 1:
            usable = True
            print(f"  [ok] {account['name'] or account['id']}：status=1，可以刷新")
        else:
            print(f"  [x] {account['name'] or account['id']}：status={account['status']}，"
                  f"最后变动 {stamp(account['updated_at'], unit_ms=True)}")
            print("       这就是「刷了但一篇没有」的原因。必须重新扫码，"
                  "不要重试、不要换账号或 IP。")

    print(f"\n【订阅源】共 {len(state['feeds'])} 个，缓存文章 {state['total_articles']} 篇")
    for feed in state["feeds"]:
        age = days_ago(feed["latest"])
        if feed["articles"] == 0:
            mark, note = "[!]", "一篇都没有"
        elif age is not None and age > 7:
            mark, note = "[!]", f"最新文章是 {age:.0f} 天前"
        else:
            mark, note = "[ok]", f"最新 {stamp(feed['latest'])}"
        print(f"  {mark} {feed['mp_name']:<14} {feed['articles']:>3} 篇 | {note} | "
              f"上次同步 {stamp(feed['sync_time'])}")

    print("\n" + "=" * 62)
    if not usable:
        print("  结论：账号不可用，先重新扫码，刷新才有意义。")
        return 1
    stale = [f["mp_name"] for f in state["feeds"] if (days_ago(f["latest"]) or 999) > 7]
    if stale:
        print(f"  结论：账号可用；这些源超过 7 天没有新文章，值得刷一次：{'、'.join(stale[:6])}")
    else:
        print("  结论：账号可用，各源都不算陈旧。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=str(DEFAULT_DB), help="wewe-rss.db 路径")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()

    db_path = Path(args.db).expanduser()
    if not db_path.exists():
        print(f"找不到 {db_path}", file=sys.stderr)
        return 2

    state = collect(db_path)
    if args.json:
        print(json.dumps(state, ensure_ascii=False, indent=2))
        return 0
    return render(state)


if __name__ == "__main__":
    raise SystemExit(main())
