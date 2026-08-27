#!/usr/bin/env python3
"""Collect public WeChat articles from a local WeWe RSS instance.

WeWe RSS uses a WeChat Reading account and a relay for discovery.  This
adapter never reads or prints that token: it consumes only the local public
JSON Feed endpoints and emits the existing daily collector contract.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from opportunity_filter import candidate_decision  # noqa: E402


BASE_URL = os.getenv("WEWE_BASE_URL", "http://127.0.0.1:4000").rstrip("/")
OUTPUT_DIR = Path(os.getenv("WECHAT_DIGEST_OUTPUT_DIR", "output/social-practice-wechat-digest"))
IMAGE_RE = re.compile(r"<(?:img|source)\b[^>]*(?:data-src|src)=[\"']([^\"']+)", re.I)


class TextExtractor(HTMLParser):
    breaks = {"br", "div", "p", "li", "section", "h1", "h2", "h3", "tr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.ignored = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript", "svg"}:
            self.ignored += 1
        elif not self.ignored and tag in self.breaks:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg"} and self.ignored:
            self.ignored -= 1
        elif not self.ignored and tag in self.breaks:
            self.parts.append("\n")

    def handle_data(self, value: str) -> None:
        if not self.ignored:
            self.parts.append(value)

    def text(self) -> str:
        lines = (re.sub(r"\s+", " ", line).strip() for line in "".join(self.parts).splitlines())
        return "\n".join(dict.fromkeys(line for line in lines if line))


def html_to_text(value: str) -> str:
    parser = TextExtractor()
    parser.feed(value or "")
    return html.unescape(parser.text())


def extract_images(value: str, limit: int = 24) -> list[str]:
    images: list[str] = []
    for raw in IMAGE_RE.findall(value or ""):
        url = html.unescape(raw.strip())
        if url.startswith("http") and url not in images:
            images.append(url)
        if len(images) >= limit:
            break
    return images


def get_json(path: str, params: dict[str, Any] | None = None, timeout: int = 120) -> Any:
    url = f"{BASE_URL}{path}"
    if params:
        url += "?" + urlencode(params)
    request = Request(url, headers={"Accept": "application/feed+json, application/json", "User-Agent": "Practice-Xiaoda/1.0"})
    with urlopen(request, timeout=timeout) as response:
        return json.load(response)


def iso_date(value: str) -> str:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone().date().isoformat()
    except (AttributeError, ValueError):
        return ""


def discover_feeds() -> dict[str, str]:
    rows = get_json("/feeds")
    return {str(row.get("mpName") or row.get("name") or ""): str(row.get("id") or "") for row in rows if isinstance(row, dict)}


def likely_candidate(title: str) -> bool:
    return bool(candidate_decision({"title": title, "content": ""})["candidate"])


def collect_articles(accounts: list[str], since: str, count: int) -> tuple[list[dict[str, Any]], list[str], list[dict[str, str]]]:
    feeds = discover_feeds()
    missing = [account for account in accounts if account not in feeds]
    failures: list[dict[str, str]] = []
    articles: list[dict[str, Any]] = []
    for account in accounts:
        feed_id = feeds.get(account)
        if not feed_id:
            continue
        metadata = get_json(f"/feeds/{quote(feed_id)}.json", {"limit": max(count * 3, 100), "page": 1, "mode": "metadata"})
        original_items = [item for item in metadata.get("items", []) if isinstance(item, dict)]
        metadata_items = sorted(
            original_items,
            key=lambda item: str(item.get("date_modified") or ""),
            reverse=True,
        )
        candidates = [
            item for item in metadata_items
            if iso_date(str(item.get("date_modified") or "")) >= since
            and likely_candidate(str(item.get("title") or ""))
        ][:count]
        for item in candidates:
            try:
                original_index = next(i for i, row in enumerate(original_items, 1) if row.get("id") == item.get("id"))
                full = get_json(f"/feeds/{quote(feed_id)}.json", {"limit": 1, "page": original_index, "mode": "fulltext"})
                match = (full.get("items") or [{}])[0]
                content_html = str(match.get("content_html") or "")
                content = html_to_text(content_html)
                if not content or "获取全文失败" in content:
                    raise ValueError("public full text unavailable")
                articles.append({
                    "query": account, "account": account,
                    "title": str(item.get("title") or "").strip(),
                    "link": str(item.get("url") or "").strip(),
                    "publishDate": iso_date(str(item.get("date_modified") or "")),
                    "content": content,
                    "images": extract_images(content_html),
                    "collector": "wewe-rss",
                })
            except (HTTPError, URLError, TimeoutError, ValueError, StopIteration) as exc:
                failures.append({"title": str(item.get("title") or ""), "error": type(exc).__name__})
    unique = {article["link"]: article for article in articles if article["link"]}
    return list(unique.values()), missing, failures


def main() -> int:
    parser = argparse.ArgumentParser(description="WeWe RSS public-feed adapter")
    sub = parser.add_subparsers(dest="command", required=True)
    collect = sub.add_parser("collect")
    collect.add_argument("accounts", nargs="+")
    collect.add_argument("--since", default=date.today().isoformat())
    collect.add_argument("--count", type=int, default=12)
    collect.add_argument("--no-kb", action="store_true")
    args = parser.parse_args()
    try:
        articles, missing, failures = collect_articles(args.accounts, args.since, max(1, args.count))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": f"WeWe RSS unavailable: {type(exc).__name__}"}), file=sys.stderr)
        return 2
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output = OUTPUT_DIR / f"articles_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    output.write_text(json.dumps({"collector": "wewe-rss", "since": args.since,
        "partial": bool(failures or missing), "missing_accounts": missing,
        "failures": failures, "articles": articles}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": True, "articles": len(articles), "missing_accounts": missing,
        "failures": len(failures), "output": str(output)}, ensure_ascii=False))
    # A no-op is a healthy completed scan: there may simply be no new
    # recruitment posts. Treating it as a failure made the daily job report a
    # broken update and discarded its otherwise useful audit batch.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
