#!/usr/bin/env python3
"""Push collected article text to the Xiaoda ingestion endpoint.

Usage:
    export XIAODA_INGEST_URL=http://8.217.145.109:8000/api/ingest
    export XIAODA_INGEST_KEY=<submission key>
    python3 scripts/push_article.py articles.jsonl
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

DEFAULT_URL = "http://8.217.145.109:8000/api/ingest"
REQUIRED = ("source_account", "source_url", "title", "publish_date", "raw_text")
RETRYABLE = {429, 500, 502, 503, 504}


class PushFailed(RuntimeError):
    """The server rejected a submission or all retries failed."""


def push_article(article: dict, *, correction: bool = False, url: str = "",
                 key: str = "", retries: int = 3, timeout: int = 30) -> dict:
    """Push one article and return the decoded server response."""
    url = url or os.getenv("XIAODA_INGEST_URL") or DEFAULT_URL
    key = key or os.getenv("XIAODA_INGEST_KEY", "")
    if not key:
        raise PushFailed("没有投稿密钥：设置环境变量 XIAODA_INGEST_KEY")
    missing = [field for field in REQUIRED if not str(article.get(field, "")).strip()]
    if missing:
        raise PushFailed(f"缺字段：{'、'.join(missing)}")

    request_body = {**article, "input_type": "copied_text"}
    if correction:
        request_body["correction"] = True
    body = json.dumps(request_body, ensure_ascii=False).encode("utf-8")
    opener = build_opener(ProxyHandler({}))

    last = ""
    for attempt in range(1, retries + 1):
        request = Request(url, data=body, headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json; charset=utf-8",
        })
        try:
            with opener.open(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:200]
            if exc.code not in RETRYABLE:
                raise PushFailed(f"HTTP {exc.code}：{detail}") from exc
            last = f"HTTP {exc.code}：{detail}"
        except URLError as exc:
            last = f"连不上：{exc.reason}"
        if attempt < retries:
            time.sleep(2 ** attempt)
    raise PushFailed(f"重试 {retries} 次仍未成功。{last}")


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    path = Path(sys.argv[1])
    if not path.is_file():
        print(f"找不到文件：{path}", file=sys.stderr)
        return 1

    ok = failed = 0
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("//"):
            continue
        try:
            record = json.loads(line)
            result = push_article(record, correction=bool(record.pop("correction", False)))
        except (PushFailed, json.JSONDecodeError) as exc:
            failed += 1
            print(f"  第 {number} 行  失败：{exc}")
            continue
        ok += 1
        status = result.get("status", "?")
        if result.get("corrected"):
            status = "corrected"
        title = (result.get("project") or {}).get("title", "")
        print(f"  第 {number} 行  {status:<16} {title[:30]}")
        time.sleep(0.3)

    print(f"\n成功 {ok} 条，失败 {failed} 条")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
