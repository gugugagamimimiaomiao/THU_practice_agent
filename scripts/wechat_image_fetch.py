#!/usr/bin/env python3
"""Small requests-based fallback for public WeChat image CDN downloads.

This runs under the collector virtualenv when available.  It intentionally
accepts only official mmbiz hosts and never receives a Cookie argument.
"""

from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import urlparse


ALLOWED_HOSTS = {"mmbiz.qpic.cn", "mmbiz.qlogo.cn"}
MAX_BYTES = 8 * 1024 * 1024


def main() -> int:
    if len(sys.argv) != 3:
        return 2
    url, destination = sys.argv[1], Path(sys.argv[2])
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
        return 2
    try:
        import requests
        response = requests.get(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; PracticeXiaoda/1.0)",
                "Referer": "https://mp.weixin.qq.com/",
                "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
            },
            timeout=(8, 15),
            stream=True,
        )
        response.raise_for_status()
        size = 0
        with destination.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=64 * 1024):
                size += len(chunk)
                if size > MAX_BYTES:
                    destination.unlink(missing_ok=True)
                    return 3
                handle.write(chunk)
        if not size:
            return 3
        return 0
    except Exception:
        destination.unlink(missing_ok=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
