#!/usr/bin/env python3
"""Small dependency-free load test for the OpenAI-compatible endpoint."""

from __future__ import annotations

import argparse
import json
import statistics
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def one_request(url: str, key: str, timeout: float) -> tuple[bool, float, int, str]:
    body = json.dumps({"messages": [{"role": "user", "content": "推荐实践"}], "stream": False}, ensure_ascii=False).encode("utf-8")
    request = Request(url, data=body, method="POST", headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    started = time.monotonic()
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read())
            ok = response.status == 200 and bool(payload.get("choices"))
            return ok, time.monotonic() - started, response.status, ""
    except HTTPError as exc:
        return False, time.monotonic() - started, exc.code, "http_error"
    except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        return False, time.monotonic() - started, 0, type(exc).__name__


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, int(len(ordered) * fraction) - 1))]


def main() -> int:
    parser = argparse.ArgumentParser(description="压测实践小搭对话接口")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000/v1")
    parser.add_argument("--api-key", required=True)
    parser.add_argument("--requests", type=int, default=100)
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--timeout", type=float, default=20)
    args = parser.parse_args()
    url = args.base_url.rstrip("/") + "/chat/completions"
    started = time.monotonic()
    results = []
    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        futures = [pool.submit(one_request, url, args.api_key, args.timeout) for _ in range(max(1, args.requests))]
        for future in as_completed(futures):
            results.append(future.result())
    elapsed = time.monotonic() - started
    successes = [latency for ok, latency, _, _ in results if ok]
    failures: dict[str, int] = {}
    for ok, _, status, reason in results:
        if ok:
            continue
        key = str(status) if status else reason
        failures[key] = failures.get(key, 0) + 1
    report = {
        "requests": len(results), "concurrency": args.concurrency,
        "success": len(successes), "failed": len(results) - len(successes),
        "success_rate": round(len(successes) / len(results), 4),
        "requests_per_second": round(len(results) / elapsed, 2),
        "latency_ms": {
            "mean": round(statistics.mean(successes) * 1000, 2) if successes else 0,
            "p50": round(percentile(successes, 0.50) * 1000, 2),
            "p95": round(percentile(successes, 0.95) * 1000, 2),
            "max": round(max(successes) * 1000, 2) if successes else 0,
        },
        "failures": failures,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["success_rate"] >= 0.99 else 1


if __name__ == "__main__":
    raise SystemExit(main())
