#!/usr/bin/env python3
"""wechat-download-api 体检：一条命令看清"现在到底能不能用"。

    python3 scripts/wda_check.py

它会依次看：服务在不在、登录态还剩几天、订阅了几个号、最近一轮轮询有没有
拿到带正文的文章、有没有被拉黑的号、日志里有没有频控（ret=200013）。最后给
一句结论和下一步该干什么。

只读，不改任何东西。默认找 http://127.0.0.1:5000 和 ../wechat-download-api，
不在默认位置就用 --api / --db 指。
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

CST = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parents[1]

OK = "[ok]"
WARN = "[!]"
BAD = "[x]"
INFO = "[-]"


def get_json(url: str, timeout: int = 10):
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "Practice-Xiaoda/1.0"})
    with urlopen(request, timeout=timeout) as response:
        return json.load(response)


def human_age(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 90:
        return f"{seconds} 秒前"
    if seconds < 5400:
        return f"{seconds // 60} 分钟前"
    if seconds < 86400 * 2:
        return f"{seconds // 3600} 小时前"
    return f"{seconds // 86400} 天前"


def stamp(value: int) -> str:
    if not value:
        return "从未"
    return datetime.fromtimestamp(int(value), CST).strftime("%Y-%m-%d %H:%M")


def find_install(explicit: str) -> Path | None:
    if explicit:
        path = Path(explicit).expanduser()
        return path if path.exists() else None
    for candidate in (
        ROOT.parent / "wechat-download-api",
        Path.home() / "wechat-download-api",
        ROOT / "wechat-download-api",
    ):
        if (candidate / "app.py").exists():
            return candidate
    return None


def find_db(explicit: str, install: Path | None) -> Path | None:
    if explicit:
        path = Path(explicit).expanduser()
        return path if path.exists() else None
    if install and (install / "data" / "rss.db").exists():
        return install / "data" / "rss.db"
    return None


def read_db(path: Path) -> dict:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        subscriptions = [dict(row) for row in connection.execute(
            "SELECT fakeid, nickname, last_poll, created_at FROM subscriptions ORDER BY nickname"
        )]
        per_account = {row["fakeid"]: dict(row) for row in connection.execute(
            """
            SELECT fakeid,
                   COUNT(*) AS total,
                   SUM(CASE WHEN content != '' THEN 1 ELSE 0 END) AS with_content,
                   MAX(publish_time) AS latest,
                   MAX(fetched_at) AS last_fetch
            FROM articles GROUP BY fakeid
            """
        )}
        totals = dict(connection.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN content != '' THEN 1 ELSE 0 END) AS with_content
            FROM articles
            """
        ).fetchone())
    finally:
        connection.close()
    return {"subscriptions": subscriptions, "per_account": per_account, "totals": totals}


def scan_logs(install: Path) -> dict:
    """数日志里的风控信号。只看最近 4000 行，别把整个日志读进内存。"""
    findings = {"freq_control": 0, "api_error": 0, "verification": 0, "samples": []}
    log_files = sorted(install.glob("logs/*.log")) + sorted(install.glob("*.log"))
    for log_file in log_files[-3:]:
        try:
            lines = log_file.read_text("utf-8", "replace").splitlines()[-4000:]
        except OSError:
            continue
        for line in lines:
            if "WeChat API error" in line:
                findings["api_error"] += 1
                if re.search(r"ret=200013|freq control", line):
                    findings["freq_control"] += 1
                if len(findings["samples"]) < 3:
                    findings["samples"].append(line.strip()[:160])
            elif "Verification triggered" in line:
                findings["verification"] += 1
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--api", default="http://127.0.0.1:5000", help="服务地址")
    parser.add_argument("--install", default="", help="wechat-download-api 目录（默认自动找）")
    parser.add_argument("--db", default="", help="rss.db 路径（默认自动找）")
    args = parser.parse_args()

    base = args.api.rstrip("/")
    install = find_install(args.install)
    database = find_db(args.db, install)
    verdict: list[str] = []
    now = time.time()

    print("=" * 60)
    print(f"wechat-download-api 体检  {datetime.now(CST).strftime('%Y-%m-%d %H:%M')}")
    print("=" * 60)

    # 1. 服务
    print("\n【服务】")
    health = None
    try:
        health = get_json(f"{base}/api/health")
        proxies = health.get("proxy_pool") or {}
        print(f"  {OK} {base} 在跑，HTTP 引擎：{health.get('http_engine', '?')}")
        print(f"  {INFO} 代理池：{'已启用 %s 个' % proxies.get('total') if proxies.get('enabled') else '未启用（直连）'}")
    except (URLError, HTTPError, OSError) as error:
        print(f"  {BAD} 连不上 {base}：{error}")
        print(f"  {INFO} 先去 wechat-download-api 目录双击「启动.command」，或跑 bash start.sh")
        return 1

    # 2. 登录态
    print("\n【登录态】")
    logged_in = False
    try:
        status = get_json(f"{base}/api/admin/status")
        logged_in = bool(status.get("loggedIn"))
        if not logged_in:
            print(f"  {BAD} 未登录：{status.get('status', '')}")
            print(f"  {INFO} 打开 {base}/login.html，用公众号管理员微信扫码")
            verdict.append("先扫码登录，其余检查都要等它")
        else:
            expire = int(status.get("expireTime") or 0)
            left = expire - now if expire else 0
            nickname = status.get("nickname") or status.get("account") or ""
            print(f"  {OK} 已登录：{nickname}")
            if expire:
                mark = OK if left > 86400 else WARN
                print(f"  {mark} 凭证到期：{stamp(expire)}（还剩 {left / 3600:.1f} 小时）")
                if 0 < left <= 86400:
                    verdict.append("登录态 24 小时内过期，抽空重新扫一次码")
                elif left <= 0:
                    verdict.append("登录态已过期，重新扫码后所有采集才会恢复")
    except (URLError, HTTPError, OSError) as error:
        print(f"  {WARN} 读不到登录状态：{error}")

    # 3. 订阅与入库
    print("\n【订阅与入库】")
    if not database:
        print(f"  {WARN} 没找到 rss.db，用 --db 指一下路径")
        data = None
    else:
        print(f"  {INFO} 数据库：{database}")
        data = read_db(database)
        subscriptions = data["subscriptions"]
        if not subscriptions:
            print(f"  {WARN} 一个公众号都没订阅")
            print(f"  {INFO} 打开 {base}/rss.html 搜名称添加，第一次只加一个号")
            verdict.append("先订阅一个公众号，观察一轮再加")
        else:
            print(f"  {INFO} 已订阅 {len(subscriptions)} 个号：")
            silent = []
            for row in subscriptions:
                counts = data["per_account"].get(row["fakeid"], {})
                total = counts.get("total") or 0
                with_content = counts.get("with_content") or 0
                latest = counts.get("latest") or 0
                poll_age = now - (row["last_poll"] or 0) if row["last_poll"] else None
                mark = OK if with_content else (WARN if total else BAD)
                print(f"    {mark} {row['nickname'] or row['fakeid'][:10]:<16} "
                      f"文章 {total} 篇 / 有正文 {with_content} 篇 / 最新 {stamp(latest)} / "
                      f"上次轮询 {human_age(poll_age) if poll_age is not None else '从未'}")
                if not total and (row["last_poll"] or 0):
                    silent.append(row["nickname"] or row["fakeid"][:10])
            totals = data["totals"]
            print(f"  {INFO} 合计 {totals.get('total') or 0} 篇，其中 {totals.get('with_content') or 0} 篇有正文")
            if silent:
                verdict.append(f"这些号轮询过但一篇没拿到：{'、'.join(silent)}——大概率是列表接口被频控，见上面【日志】")

    # 4. 黑名单
    print("\n【黑名单】")
    try:
        blacklist = (get_json(f"{base}/api/admin/blacklist") or {}).get("blacklist") or []
        active = [row for row in blacklist if row.get("is_active")]
        if not active:
            print(f"  {OK} 没有被拉黑的公众号")
        else:
            for row in active:
                print(f"  {WARN} {row.get('nickname') or row.get('fakeid')}：{row.get('reason')} "
                      f"（验证码触发 {row.get('verification_count', 0)} 次）")
            verdict.append("有号被自动拉黑了，去 /blacklist.html 看原因，别急着解封")
    except (URLError, HTTPError, OSError) as error:
        print(f"  {WARN} 读不到黑名单：{error}")

    # 5. 日志里的风控信号
    print("\n【日志】")
    if not install:
        print(f"  {WARN} 没找到安装目录，跳过日志检查（用 --install 指）")
    else:
        findings = scan_logs(install)
        if not (install / "logs").exists() and not list(install.glob("*.log")):
            print(f"  {INFO} 还没有日志文件；用「启动.command」起服务会写到 logs/service.log")
        elif findings["freq_control"]:
            print(f"  {BAD} 命中频控 ret=200013 共 {findings['freq_control']} 次")
            for line in findings["samples"]:
                print(f"       {line}")
            verdict.append("已经撞上频控：当天停止加号和手动刷新，WeWe 那条链路先别停")
        elif findings["api_error"]:
            print(f"  {WARN} 有 {findings['api_error']} 条微信接口报错（非频控）")
            for line in findings["samples"]:
                print(f"       {line}")
        else:
            print(f"  {OK} 没有微信接口报错记录")
        if findings["verification"]:
            print(f"  {WARN} 触发图形验证 {findings['verification']} 次（同一个号累计 8 次会被自动拉黑）")

    # 6. 结论
    print("\n" + "=" * 60)
    print("结论")
    print("=" * 60)
    if logged_in and data and (data["totals"].get("with_content") or 0) > 0 and not verdict:
        print(f"  {OK} 这条链路是通的：已经有带正文的文章入库。")
        print("  下一步：")
        print("    python3 scripts/wda_collector.py --db %s --output data/exports/wda_batch.jsonl"
              % (database or "…/data/rss.db"))
        print("    python3 scripts/import_articles.py data/exports/wda_batch.jsonl --check")
    elif not verdict:
        print(f"  {INFO} 还没有可判断的数据，等一轮轮询（默认 1 小时）再跑一次这个脚本。")
    else:
        for line in verdict:
            print(f"  {WARN} {line}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
