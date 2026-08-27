#!/usr/bin/env python3
"""逐个公众号让 WeWe 去拉新文章，账号之间留足间隔。

WeWe 的「更新」按钮走的是 tRPC 的 feed.refreshArticles。网页上一个个点太累，
但脚本连着点又会踩到微信读书的频控——实测每个公众号之间至少要隔 2 分钟。
所以这里按固定间隔逐个触发，并在每个号刷完后报一句拿到了几篇新的。

    python3 scripts/wewe_refresh_feeds.py                    # 全部订阅号，间隔 120 秒
    python3 scripts/wewe_refresh_feeds.py --interval 180     # 更保守
    python3 scripts/wewe_refresh_feeds.py --only 清华大学学生会,清华紫荆之声
    python3 scripts/wewe_refresh_feeds.py --dry-run          # 只列出会刷哪些、要多久

连续失败两次就停——多半是被限流了，继续刷只会把限制拖长。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "wewe-rss.db"


def get_json(url: str, timeout: int = 30):
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "Practice-Xiaoda/1.0"})
    with urlopen(request, timeout=timeout) as response:
        return json.load(response)


def post_json(url: str, payload: dict, timeout: int = 180):
    body = json.dumps(payload).encode("utf-8")
    request = Request(
        url, data=body,
        headers={"Content-Type": "application/json", "User-Agent": "Practice-Xiaoda/1.0"},
        method="POST",
    )
    with urlopen(request, timeout=timeout) as response:
        text = response.read().decode("utf-8", "replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"raw": text[:200]}


def list_feeds(base: str) -> list[dict]:
    rows = get_json(f"{base}/feeds")
    if isinstance(rows, dict):
        rows = rows.get("items") or rows.get("list") or []
    feeds = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        feed_id = str(row.get("id") or "").strip()
        name = str(row.get("mpName") or row.get("name") or "").strip()
        if feed_id:
            feeds.append({"id": feed_id, "name": name or feed_id})
    return feeds


def article_counts(database: Path) -> dict[str, int]:
    """每个 feed 现在有多少篇。数据库不在就返回空，不影响刷新本身。"""
    if not database.exists():
        return {}
    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    except sqlite3.Error:
        return {}
    try:
        return {str(row[0]): int(row[1])
                for row in connection.execute("SELECT mp_id, COUNT(*) FROM articles GROUP BY mp_id")}
    except sqlite3.Error:
        return {}
    finally:
        connection.close()


def human_minutes(seconds: float) -> str:
    minutes = int(seconds // 60)
    return f"{minutes} 分钟" if minutes else f"{int(seconds)} 秒"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", default=os.getenv("WEWE_BASE_URL", "http://127.0.0.1:4000"))
    parser.add_argument("--interval", type=float, default=float(os.getenv("WEWE_REFRESH_INTERVAL", "120")),
                        help="每个公众号之间的间隔秒数，默认 120（实测的下限）")
    parser.add_argument("--only", default="", help="只刷这些公众号，逗号分隔")
    parser.add_argument("--limit", type=int, default=0, help="最多刷几个，0 表示不限")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="WeWe 的 SQLite，用来数新增篇数")
    parser.add_argument("--dry-run", action="store_true", help="只列出会刷哪些、大概多久")
    args = parser.parse_args()

    base = args.base.rstrip("/")
    if args.interval < 120:
        print(f"注意：间隔 {args.interval:.0f} 秒低于实测下限 120 秒，容易触发频控。", file=sys.stderr)

    try:
        feeds = list_feeds(base)
    except (URLError, HTTPError, OSError) as error:
        print(f"连不上 WeWe（{base}）：{error}", file=sys.stderr)
        return 2
    if not feeds:
        print("一个订阅号都没有。", file=sys.stderr)
        return 2

    wanted = [name.strip() for name in args.only.split(",") if name.strip()]
    if wanted:
        missing = [name for name in wanted if name not in {f["name"] for f in feeds}]
        if missing:
            print(f"这些名字在订阅列表里找不到：{'、'.join(missing)}", file=sys.stderr)
        feeds = [f for f in feeds if f["name"] in wanted]
    if args.limit > 0:
        feeds = feeds[: args.limit]
    if not feeds:
        print("筛完一个都不剩。", file=sys.stderr)
        return 2

    total_wait = args.interval * max(0, len(feeds) - 1)
    print(f"要刷 {len(feeds)} 个公众号，间隔 {args.interval:.0f} 秒，"
          f"预计 {human_minutes(total_wait)}（不算抓取本身的时间）")
    for feed in feeds:
        print(f"  · {feed['name']}")
    if args.dry_run:
        return 0
    print()

    database = Path(args.db).expanduser()
    before = article_counts(database)
    results: list[dict] = []
    consecutive_failures = 0

    for index, feed in enumerate(feeds, 1):
        label = f"[{index}/{len(feeds)}] {feed['name']}"
        started = time.time()
        try:
            post_json(f"{base}/trpc/feed.refreshArticles", {"mpId": feed["id"]})
            error = ""
        except (URLError, HTTPError, OSError) as exc:
            error = f"{type(exc).__name__}: {exc}"

        after = article_counts(database)
        gained = (after.get(feed["id"], 0)) - (before.get(feed["id"], 0))
        before = after

        if error:
            consecutive_failures += 1
            print(f"{label}  失败：{error[:90]}")
            results.append({"name": feed["name"], "ok": False, "error": error, "gained": 0})
            if consecutive_failures >= 2:
                print("\n连续两个都失败了，停。多半是被限流——今天别再刷了，"
                      "继续只会把限制拖长。", file=sys.stderr)
                break
        else:
            consecutive_failures = 0
            spent = time.time() - started
            print(f"{label}  新增 {gained} 篇（用时 {spent:.0f} 秒）")
            results.append({"name": feed["name"], "ok": True, "gained": gained})

        if index < len(feeds):
            time.sleep(args.interval)

    total_gained = sum(row.get("gained", 0) for row in results)
    failed = [row["name"] for row in results if not row.get("ok")]
    print()
    print(json.dumps({
        "refreshed": len([row for row in results if row.get("ok")]),
        "planned": len(feeds),
        "gained": total_gained,
        "failed": failed,
    }, ensure_ascii=False))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
