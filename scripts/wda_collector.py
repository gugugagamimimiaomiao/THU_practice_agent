#!/usr/bin/env python3
"""把 wechat-download-api 已入库的文章导出成实践小搭的投稿 JSONL。

wechat-download-api（https://github.com/tmwgsicp/wechat-download-api）负责扫码
授权、订阅公众号、定时抓取正文并存进它自己的 SQLite（articles 表已经带
plain_content 和 content）。这个适配器只负责"读出来、转成 DATA_HANDOFF.md
里那六个字段"，不做任何字段抽取——截止日期、资格、经费仍然由 domain.py
在服务端解析。

两种读法，二选一：

    # 走 HTTP 接口（上游可以在别的机器 / Docker 里）
    python3 scripts/wda_collector.py --api http://127.0.0.1:5000 \
        --output data/exports/wda_batch.jsonl

    # 直接只读它的 SQLite（同机部署时更快，正文用 plain_content，最干净）
    python3 scripts/wda_collector.py --db /path/to/wechat-download-api/data/rss.db \
        --output data/exports/wda_batch.jsonl

游标存在 --state 指定的文件里（默认 data/wda_state.json），记录上一次导出到
的 publish_time。第二次跑就是增量，不会重复导出。想重跑一段时间用
--since 2026-08-01 覆盖游标。

导出完照旧先自查再投稿：

    python3 scripts/import_articles.py data/exports/wda_batch.jsonl --check
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone, timedelta
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CST = timezone(timedelta(hours=8))
IMAGE_RE = re.compile(r"""<img[^>]+(?:data-src|src)=["']([^"']+)["']""", re.IGNORECASE)
MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(\s*([^)\s]+)[^)]*\)")
BLOCK_TAGS = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "blockquote"}

# 正文短于这个长度时 import_articles.py 会打回；这里同样跳过，但计入统计，
# 让人看得见"抓到了但正文没抓全"和"根本没抓到"是两回事。
MIN_TEXT_LENGTH = 120


# ---------------------------------------------------------------- 小工具


class _TextExtractor(HTMLParser):
    """HTML → 纯文本，块级标签换行。DATA_HANDOFF 明确要求保留换行。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
        elif tag in BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self._parts.append(data)

    def text(self) -> str:
        raw = "".join(self._parts)
        # 只在 plain_content 缺失时兜底用。块级标签首尾各补一个换行会产生
        # 成片空行，这里压掉，输出与上游 plain_content 一致的"一行一段"。
        lines = [line.strip() for line in raw.splitlines()]
        return "\n".join(line for line in lines if line).strip()


def html_to_text(value: str) -> str:
    parser = _TextExtractor()
    parser.feed(value or "")
    return unescape(parser.text())


def unproxy(url: str) -> str:
    """把 http://本机:5000/api/image?url=xxx 还原成微信 CDN 原图地址。

    代理地址只在部署这台机器上能打开；投稿给服务器的必须是原始 URL，
    否则那边 OCR 拉图会 404，而且看不出是为什么。
    """
    if not url:
        return ""
    if "/api/image" not in url:
        return url
    query = parse_qs(urlparse(url).query)
    inner = (query.get("url") or [""])[0]
    return inner or url


def collect_images(html_text: str, limit: int = 24) -> list[str]:
    images: list[str] = []
    for raw in IMAGE_RE.findall(html_text or ""):
        url = unproxy(unescape(raw.strip()))
        if url.startswith("http") and url not in images:
            images.append(url)
        if len(images) >= limit:
            break
    return images


def markdown_to_text(markdown: str, title: str = "") -> tuple[str, list[str]]:
    """/api/feed/article/{id}.md 的正文 → 纯文本 + 图片列表。"""
    body = markdown
    if body.startswith("---"):
        end = body.find("\n---", 3)
        if end != -1:
            body = body[end + 4 :]

    # 上游会在正文前加一行 "# 标题"。标题我们单独有字段，留在正文里等于
    # 让抽取多看到一遍标题，去掉更接近"原文正文"。
    stripped = body.lstrip("\n")
    first, _, rest = stripped.partition("\n")
    if first.startswith("# ") and (not title or first[2:].strip() == title.strip()):
        body = rest

    images = [unproxy(url) for url in MD_IMAGE_RE.findall(body)]
    images = [url for url in dict.fromkeys(images) if url.startswith("http")]

    body = MD_IMAGE_RE.sub("", body)
    body = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", body)   # 链接只留文字
    body = re.sub(r"^#{1,6}\s*", "", body, flags=re.MULTILINE)
    body = re.sub(r"[*_`>]", "", body)
    body = re.sub(r"\n{3,}", "\n\n", body)
    lines = [line.strip() for line in body.splitlines()]
    out: list[str] = []
    for line in lines:
        if line or (out and out[-1]):
            out.append(line)
    return "\n".join(out).strip(), images[:24]


def iso_date(timestamp: int) -> str:
    if not timestamp:
        return ""
    return datetime.fromtimestamp(int(timestamp), CST).date().isoformat()


def parse_since(value: str) -> int:
    value = str(value).strip()
    if not value:
        return 0
    if value.isdigit():
        return int(value)
    return int(datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=CST).timestamp())


def get_json(url: str, timeout: int = 60) -> Any:
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "Practice-Xiaoda/1.0"})
    with urlopen(request, timeout=timeout) as response:
        return json.load(response)


def get_text(url: str, timeout: int = 60) -> str:
    request = Request(url, headers={"User-Agent": "Practice-Xiaoda/1.0"})
    with urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", "replace")


# ---------------------------------------------------------------- 两种读法


def read_from_api(base: str, since: int, limit: int, page: int, pause: float) -> Iterable[dict[str, Any]]:
    """走 /api/feed/articles.json 游标分页 + /api/feed/article/{id}.md 取正文。"""
    base = base.rstrip("/")
    cursor = since
    produced = 0
    while produced < limit:
        payload = get_json(f"{base}/api/feed/articles.json?" + urlencode({"since": cursor, "limit": page}))
        rows = payload.get("articles") or []
        if not rows:
            return
        for row in rows:
            if produced >= limit:
                return
            record = {
                "id": row.get("id"),
                "nickname": row.get("nickname") or "",
                "title": row.get("title") or "",
                "link": row.get("link") or "",
                "publish_time": int(row.get("publish_time") or 0),
                "content_fetched": bool(row.get("content_fetched")),
                "raw_text": "",
                "images": [],
            }
            if record["content_fetched"]:
                try:
                    markdown = get_text(f"{base}/api/feed/article/{record['id']}.md")
                    record["raw_text"], record["images"] = markdown_to_text(markdown, record["title"])
                except Exception as error:  # noqa: BLE001 - 单篇失败不该中断整批
                    record["error"] = f"{type(error).__name__}: {error}"
            produced += 1
            yield record
            if pause:
                time.sleep(pause)
        next_cursor = int(payload.get("next_since") or cursor)
        if next_cursor <= cursor:
            return
        cursor = next_cursor


def parse_link_line(line: str) -> tuple[str, str] | None:
    """一行 → (公众号名, 链接)。公众号名可以没有。

    认三种写法，因为上游来源不一样：WeWe 导出的是 JSONL，手工整理的多半是
    裸链接，从表格里粘的常带一列公众号名。
    """
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if line.startswith("{"):
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            return None
        url = str(row.get("source_url") or row.get("url") or "").strip()
        return (str(row.get("source_account") or "").strip(), url) if url else None
    parts = line.replace("\t", " ").split()
    urls = [part for part in parts if part.startswith("http")]
    if not urls:
        return None
    url = urls[0]
    account = " ".join(part for part in parts if part != url).strip().strip(",，")
    return account, url


def post_article(base: str, url: str, timeout: int = 180) -> dict[str, Any]:
    payload = json.dumps({"url": url}).encode("utf-8")
    request = Request(
        f"{base.rstrip('/')}/api/article",
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "Practice-Xiaoda/1.0"},
        method="POST",
    )
    with urlopen(request, timeout=timeout) as response:
        return json.load(response)


def read_from_links(links_path: Path, base: str, pause: float, limit: int) -> Iterable[dict[str, Any]]:
    """逐条 POST /api/article 抓正文。

    这条路径不碰 appmsgpublish，因此不受 ret=200013 频控影响——列表接口被限
    的时候，只要能从别处拿到链接（WeWe、手工整理），这里照样能抓到全文和图。

    服务端自带三层限频：全局 10 次/分、单 IP 5 次/分、文章间隔 3 秒。撞上了
    它会返回中文的「请 N 秒后重试」，这里照它说的等，而不是把这一条丢掉。
    """
    base = base.rstrip("/")
    produced = 0
    for raw_line in links_path.read_text("utf-8", "replace").splitlines():
        if produced >= limit:
            return
        parsed = parse_link_line(raw_line)
        if not parsed:
            continue
        account, url = parsed

        payload: dict[str, Any] = {}
        error = ""
        for attempt in range(3):
            try:
                payload = post_article(base, url)
            except (URLError, HTTPError, OSError) as exc:
                error = f"{type(exc).__name__}: {exc}"
                break
            if payload.get("success"):
                error = ""
                break
            error = str(payload.get("error") or "未知错误")
            wait = re.search(r"(\d+)\s*秒后重试", error)
            if wait and attempt < 2:
                time.sleep(int(wait.group(1)) + 1)
                continue
            break

        data = payload.get("data") or {}
        text = str(data.get("plain_content") or "").strip()
        if not text and data.get("content"):
            text = html_to_text(str(data["content"]))
        images = [unproxy(str(item)) for item in (data.get("images") or []) if str(item).startswith("http")]

        publish_time = int(data.get("publish_time") or 0)
        if not publish_time and data.get("publish_time_str"):
            try:
                publish_time = int(
                    datetime.strptime(str(data["publish_time_str"]), "%Y-%m-%d %H:%M:%S")
                    .replace(tzinfo=CST).timestamp()
                )
            except ValueError:
                publish_time = 0

        produced += 1
        yield {
            "id": produced,
            # 没给公众号名时用 author：它是从文章页的 js_name / var nickname 抓的，
            # 多数公众号文章里就是公众号名本身。
            "nickname": account or str(data.get("author") or "").strip(),
            "title": str(data.get("title") or "").strip(),
            "link": url,
            "publish_time": publish_time,
            "content_fetched": bool(text or images),
            "raw_text": text,
            "images": images[:24],
            "error": error,
        }
        if pause:
            time.sleep(pause)


def read_from_db(db_path: Path, since: int, limit: int) -> Iterable[dict[str, Any]]:
    """只读打开 rss.db，用 plain_content（最接近原文的纯文本）。"""
    uri = f"file:{db_path}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT a.id, a.title, a.link, a.author, a.content, a.plain_content,
                   a.publish_time, COALESCE(s.nickname, '') AS nickname
            FROM articles a
            LEFT JOIN subscriptions s ON s.fakeid = a.fakeid
            WHERE a.publish_time > ?
            ORDER BY a.publish_time ASC
            LIMIT ?
            """,
            (since, limit),
        ).fetchall()
    finally:
        connection.close()

    for row in rows:
        text = (row["plain_content"] or "").strip()
        if not text and row["content"]:
            text = html_to_text(row["content"])
        yield {
            "id": row["id"],
            "nickname": row["nickname"],
            "title": row["title"] or "",
            "link": row["link"] or "",
            "publish_time": int(row["publish_time"] or 0),
            "content_fetched": bool(row["content"]),
            "raw_text": text,
            "images": collect_images(row["content"] or ""),
        }


# ---------------------------------------------------------------- 过滤与输出


def load_filter():
    """标题预筛用仓库里那套共享规则；导入失败就不筛，不另写一套。"""
    try:
        from opportunity_filter import candidate_decision  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return None
    return lambda title, text: bool(candidate_decision({"title": title, "content": text})["candidate"])


def existing_urls(database: Path) -> set[str]:
    if not database.exists():
        return set()
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        return {
            str(row[0])
            for row in connection.execute(
                "SELECT source_url FROM articles WHERE source_url IS NOT NULL AND source_url != ''"
            )
        }
    except sqlite3.Error:
        return set()
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--api", default="", help="wechat-download-api 地址，如 http://127.0.0.1:5001")
    source.add_argument("--db", default="", help="它的 SQLite 路径，如 …/wechat-download-api/data/rss.db")
    source.add_argument("--links", default="", help="一份文章链接清单，逐条抓正文（不受列表接口频控影响）")
    parser.add_argument("--links-api", default="http://127.0.0.1:5001", help="--links 模式下的服务地址")
    parser.add_argument("--filter-titles", action="store_true", help="--links 模式下也做招募标题预筛（默认不筛）")
    parser.add_argument("--output", default="data/exports/wda_batch.jsonl", help="JSONL 输出路径")
    parser.add_argument("--state", default="data/wda_state.json", help="增量游标文件")
    parser.add_argument("--since", default="", help="覆盖游标：2026-08-01 或 unix 时间戳")
    parser.add_argument("--limit", type=int, default=200, help="本次最多导出多少篇")
    parser.add_argument("--page", type=int, default=100, help="API 模式每页条数（1-200）")
    parser.add_argument("--pause", type=float, default=0.0, help="API 模式每篇之间的间隔秒数（读本地库，一般不用）")
    parser.add_argument("--all-titles", action="store_true", help="不做招募标题预筛，全部导出")
    parser.add_argument("--dedupe-db", default="data/practice_xiaoda.db", help="按已入库 source_url 去重")
    parser.add_argument("--no-state", action="store_true", help="不读也不写游标文件")
    args = parser.parse_args()

    if not args.api and not args.db and not args.links:
        args.api = "http://127.0.0.1:5001"
    if args.links:
        # 链接是人挑好的，游标和标题预筛都不该再插一脚。
        args.no_state = True
        if args.pause == 0.0:
            args.pause = 13.0  # 服务端单 IP 限 5 次/分钟，13 秒一条刚好不撞

    state_path = ROOT / args.state if not Path(args.state).is_absolute() else Path(args.state)
    state: dict[str, Any] = {}
    if not args.no_state and state_path.exists():
        try:
            state = json.loads(state_path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            state = {}

    since = parse_since(args.since) if args.since else int(state.get("next_since") or 0)

    if args.links:
        links_path = Path(args.links).expanduser()
        if not links_path.is_absolute():
            links_path = (ROOT / args.links) if not links_path.exists() else links_path
        records = read_from_links(links_path, args.links_api, args.pause, args.limit)
        mode = f"links {links_path} → {args.links_api}"
    elif args.db:
        records = read_from_db(Path(args.db).expanduser(), since, args.limit)
        mode = f"sqlite {args.db}"
    else:
        records = read_from_api(args.api, since, args.limit, max(1, min(200, args.page)), args.pause)
        mode = f"api {args.api}"

    if args.links:
        title_filter = load_filter() if args.filter_titles else None
    else:
        title_filter = None if args.all_titles else load_filter()
    seen = existing_urls(Path(args.dedupe_db) if Path(args.dedupe_db).is_absolute() else ROOT / args.dedupe_db)

    output_path = Path(args.output) if Path(args.output).is_absolute() else ROOT / args.output
    output_path.parent.mkdir(parents=True, exist_ok=True)

    counts = {"scanned": 0, "written": 0, "no_content": 0, "too_short": 0, "filtered": 0, "duplicate": 0, "bad_url": 0}
    failures: list[str] = []
    highest = since
    with output_path.open("w", encoding="utf-8") as handle:
        for record in records:
            counts["scanned"] += 1
            highest = max(highest, record["publish_time"])

            url = record["link"]
            if not url.startswith("https://mp.weixin.qq.com/s"):
                counts["bad_url"] += 1
                continue
            if url in seen:
                counts["duplicate"] += 1
                continue
            if not record["content_fetched"]:
                counts["no_content"] += 1
                # 抓不到的原因必须看得见：触发验证、登录过期和文章被删要分开处理，
                # 混在一个「失败 N 条」里没法决定下一步。
                if record.get("error") and len(failures) < 5:
                    failures.append(f"{record['link'][-24:]}  {record['error'][:90]}")
                continue

            text = record["raw_text"].strip()
            if title_filter and not title_filter(record["title"], text):
                counts["filtered"] += 1
                continue
            # 图文型推送（正文都在图里）正文很短是正常的，只要有图就照样交给
            # 服务端 OCR；真正要拦的是"一张图都没有、正文也没抓到"。
            if len(text) < MIN_TEXT_LENGTH and not record["images"]:
                counts["too_short"] += 1
                continue

            payload = {
                "source_account": record["nickname"],
                "source_url": url,
                "title": record["title"],
                "publish_date": iso_date(record["publish_time"]),
                "raw_text": text,
            }
            if record["images"]:
                payload["images"] = record["images"]
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
            counts["written"] += 1
            seen.add(url)

    if not args.no_state:
        state["next_since"] = highest
        state["updated_at"] = datetime.now(CST).isoformat(timespec="seconds")
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", "utf-8")

    print(f"来源：{mode}")
    print(f"游标：{since} → {highest}")
    print(
        "扫描 {scanned} 篇 / 写出 {written} 篇（正文未抓到 {no_content}，"
        "正文过短且无图 {too_short}，标题预筛掉 {filtered}，已入库 {duplicate}，链接不合法 {bad_url}）".format(**counts)
    )
    if failures:
        print("抓不到正文的前几条：")
        for line in failures:
            print(f"  {line}")
    print(f"输出：{output_path}")
    if counts["written"]:
        print(f"下一步自查：python3 scripts/import_articles.py {output_path} --check")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
