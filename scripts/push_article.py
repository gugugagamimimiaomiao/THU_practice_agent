#!/usr/bin/env python3
"""爬到一篇就推一篇——给负责采集的同学直接抄进爬虫里。

命令行自测：

    export XIAODA_INGEST_URL=http://8.217.145.109:8000/api/ingest
    export XIAODA_INGEST_KEY=<给你的投稿密钥>
    python3 scripts/push_article.py 样例.jsonl

写进自己的爬虫里（把这个文件放到爬虫目录，或直接复制 push_article 函数）：

    from push_article import push_article
    push_article({
        "source_account": "清华大学学生社会实践",
        "source_url": "https://mp.weixin.qq.com/s/xxx",
        "title": "……",
        "publish_date": "2026-08-05",
        "raw_text": "正文全文……",
    })

只用标准库，不装任何包。失败会抛异常并带上服务端的说明，不会静默吞掉。

投稿密钥只能往里送文章，不能改项目、不能导出项目库——万一写进了公开仓库
或者聊天记录，损失止于"多了一些待核验的文章"。即便如此也别往公开处贴。
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Iterator
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

DEFAULT_URL = "http://8.217.145.109:8000/api/ingest"
REQUIRED = ("source_account", "source_url", "title", "publish_date", "raw_text")
RETRYABLE = {429, 500, 502, 503, 504}


class PushFailed(RuntimeError):
    """服务端明确拒绝，或重试若干次仍未成功。"""


def iter_json_records(text: str) -> Iterator[tuple[int, dict]]:
    """Read JSONL, pretty-printed JSON objects, or a JSON array.

    The returned number is the source line where each record starts so CLI
    errors remain useful even when an object spans multiple lines.
    """
    decoder = json.JSONDecoder()
    offset = 0
    length = len(text)
    while offset < length:
        while offset < length and text[offset].isspace():
            offset += 1
        if offset >= length:
            break
        if text.startswith("//", offset):
            newline = text.find("\n", offset)
            offset = length if newline < 0 else newline + 1
            continue
        line = text.count("\n", 0, offset) + 1
        value, end = decoder.raw_decode(text, offset)
        values = value if isinstance(value, list) else [value]
        for record in values:
            if not isinstance(record, dict):
                raise json.JSONDecodeError("article record must be an object", text, offset)
            yield line, record
        offset = end


def push_article(article: dict, *, correction: bool = False, url: str = "",
                 key: str = "", retries: int = 3, timeout: int = 30) -> dict:
    """推送一篇文章，返回服务端的结果。

    返回里的 status 有三种：
      imported        —— 已生成项目卡
      not_opportunity —— 存了原文，但判定不是招募（纪实/回顾/行前预告）
      needs_text      —— 只收到链接没有正文，存成了待补线索
    后两种都不是错误，不用重试，也不用改内容再发一遍。

    correction=True 用于订正一篇已经推过的文章：主办方改了通知（延期、名额
    调整、资格放宽），或者你发现之前那版正文抓漏了。

    平时重复推送同一条是安全的——按 source_url 认成同一个项目，只补不覆盖，
    不会重复出现在推荐里。但正因为"只补不覆盖"，订正后的内容如果比原来少
    （比如资格从"仅限计算机系"改成"面向全校"），默认会被挡回去。这时候要
    带上 correction=True，表示以这一版为准。
    """
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
    # 不走系统代理。本机代理常把内网/裸 IP 一起劫走，表现成莫名其妙的 502。
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
                # 401 密钥不对、422 内容不合规——重试多少次都一样，直接报出来。
                raise PushFailed(f"HTTP {exc.code}：{detail}") from exc
            last = f"HTTP {exc.code}：{detail}"
        except URLError as exc:
            last = f"连不上：{exc.reason}"
        if attempt < retries:
            time.sleep(2 ** attempt)  # 1 次失败后等 2s、4s，别把服务打死
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
    try:
        records = list(iter_json_records(path.read_text(encoding="utf-8")))
    except json.JSONDecodeError as exc:
        print(f"JSON 格式错误（第 {exc.lineno} 行）：{exc.msg}", file=sys.stderr)
        return 1
    for number, record in records:
        try:
            # 文件里也可以写 "correction": true，逐条标记哪些是订正。
            result = push_article(record, correction=bool(record.pop("correction", False)))
        except PushFailed as exc:
            failed += 1
            print(f"  第 {number} 行  失败：{exc}")
            continue
        ok += 1
        status = result.get("status", "?")
        if result.get("corrected"):
            status = "corrected"
        title = (result.get("project") or {}).get("title", "")
        print(f"  第 {number} 行  {status:<16} {title[:30]}")
        time.sleep(0.3)  # 别把服务刷满，评审期间它同时在给用户服务

    print(f"\n成功 {ok} 条，失败 {failed} 条")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
