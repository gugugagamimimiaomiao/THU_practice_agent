#!/usr/bin/env python3
"""Dependency-free CLI client for the 实践小搭 MVP API."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def request(base_url: str, path: str, method: str = "GET", payload=None, api_key: str = ""):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = Request(
        f"{base_url.rstrip('/')}{path}", data=data, method=method,
        headers=headers,
    )
    try:
        with urlopen(req, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"API error {exc.code}: {body}")
    except URLError as exc:
        raise SystemExit(f"Cannot reach 实践小搭 at {base_url}: {exc.reason}")


def split_list(value: str):
    return [item.strip() for item in value.replace("，", ",").split(",") if item.strip()]


def main():
    parser = argparse.ArgumentParser(description="实践小搭 API 客户端")
    parser.add_argument("--base-url", default=os.getenv("PRACTICE_XIAODA_URL", "http://127.0.0.1:8765"))
    parser.add_argument("--api-key", default=os.getenv("PRACTICE_XIAODA_API_KEY", "dev-local-key"))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("health")

    projects = sub.add_parser("projects")
    projects.add_argument("--status", default="")
    projects.add_argument("--query", default="")

    ingest = sub.add_parser("ingest")
    ingest.add_argument("--input-type", choices=["copied_text", "ocr_text", "admin_notice", "wechat_url"], default="copied_text")
    ingest.add_argument("--source-account", default="用户投稿")
    ingest.add_argument("--source-url", default="")
    ingest.add_argument("--title", default="")
    ingest.add_argument("--text-file", type=Path)

    recommend = sub.add_parser("recommend")
    recommend.add_argument("--department", default="")
    recommend.add_argument("--grade", default="")
    recommend.add_argument("--start", default="")
    recommend.add_argument("--end", default="")
    recommend.add_argument("--themes", default="")
    recommend.add_argument("--locations", default="")
    recommend.add_argument("--reimbursement", choices=["required", "preferred", "not_important"], default="not_important")

    generate = sub.add_parser("generate")
    generate.add_argument("project_id")
    generate.add_argument("--kind", choices=["application", "outreach", "interview", "itinerary", "report"], default="application")
    generate.add_argument("--name", default="")
    generate.add_argument("--department", default="")
    generate.add_argument("--strengths", default="")
    generate.add_argument("--motivation", default="")

    chat = sub.add_parser("chat")
    chat.add_argument("message")
    chat.add_argument("--model", default="practice-xiaoda")
    chat.add_argument("--stream", action="store_true")

    args = parser.parse_args()
    if args.command == "health":
        result = request(args.base_url, "/health")
    elif args.command == "projects":
        query = urlencode({k: v for k, v in {"status": args.status, "q": args.query}.items() if v})
        result = request(args.base_url, f"/api/projects{('?' + query) if query else ''}", api_key=args.api_key)
    elif args.command == "ingest":
        raw_text = args.text_file.read_text(encoding="utf-8") if args.text_file else ""
        result = request(args.base_url, "/api/ingest", "POST", {
            "input_type": args.input_type, "source_account": args.source_account,
            "source_url": args.source_url, "title": args.title, "raw_text": raw_text,
        }, api_key=args.api_key)
    elif args.command == "recommend":
        result = request(args.base_url, "/api/recommend", "POST", {"profile": {
            "department": args.department, "grade": args.grade,
            "available_start": args.start, "available_end": args.end,
            "themes": split_list(args.themes), "preferred_locations": split_list(args.locations),
            "reimbursement_preference": args.reimbursement,
        }}, api_key=args.api_key)
    elif args.command == "generate":
        result = request(args.base_url, "/api/generate", "POST", {
            "project_id": args.project_id, "kind": args.kind,
            "context": {"name": args.name, "department": args.department, "strengths": args.strengths, "motivation": args.motivation},
        }, api_key=args.api_key)
    else:
        payload = {"model": args.model, "messages": [{"role": "user", "content": args.message}], "stream": args.stream}
        if args.stream:
            req = Request(
                f"{args.base_url.rstrip('/')}/v1/chat/completions",
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                method="POST",
                headers={"Content-Type": "application/json", "Accept": "text/event-stream", "Authorization": f"Bearer {args.api_key}"},
            )
            try:
                with urlopen(req, timeout=60) as response:
                    for raw_line in response:
                        print(raw_line.decode("utf-8"), end="")
                return
            except HTTPError as exc:
                raise SystemExit(f"API error {exc.code}: {exc.read().decode('utf-8', errors='replace')}")
            except URLError as exc:
                raise SystemExit(f"Cannot reach 实践小搭 at {args.base_url}: {exc.reason}")
        result = request(args.base_url, "/v1/chat/completions", "POST", payload, api_key=args.api_key)
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    print()


if __name__ == "__main__":
    main()
