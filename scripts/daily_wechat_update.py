#!/usr/bin/env python3
"""Collect trusted WeChat accounts and import likely social-practice notices.

Run this from cron, a cloud scheduled job, or a manually triggered admin worker.
It needs the upstream collector and its WECHAT_TOKEN/WECHAT_COOKIE credentials;
the web service itself never stores those secrets in SQLite or logs.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from database import Database  # noqa: E402
from domain import deep_merge, extract_project  # noqa: E402
from opportunity_filter import candidate_decision  # noqa: E402
from wechat_ingest import collector_credentials_present  # noqa: E402
from wechat_image_ocr import OCRResult, ocr_wechat_images  # noqa: E402


DEFAULT_ACCOUNTS = ("清华大学社会实践", "无限之声", "清华大学学生公益")
def report_progress(percent: int, stage: str, label: str, *, current: int | None = None, total: int | None = None) -> None:
    """Emit machine-readable, non-secret progress for the developer panel."""
    event: dict[str, Any] = {"event": "progress", "percent": max(0, min(100, int(percent))), "stage": stage, "label": label}
    if current is not None:
        event["current"] = current
    if total is not None:
        event["total"] = total
    print(json.dumps(event, ensure_ascii=False), flush=True)


def is_candidate(article: dict[str, Any]) -> bool:
    return bool(candidate_decision(article).get("candidate"))


def _article_images(article: dict[str, Any]) -> list[str]:
    return list(dict.fromkeys(str(url).strip() for url in article.get("images", []) if str(url).strip()))


def _update_ocr_stats(stats: dict[str, int], ocr: OCRResult, *, has_images: bool) -> None:
    stats["articles_processed"] = stats.get("articles_processed", 0) + 1
    if has_images:
        stats["articles_with_images"] = stats.get("articles_with_images", 0) + 1
    stats["attempted"] = stats.get("attempted", 0) + ocr.attempted
    stats["downloaded"] = stats.get("downloaded", 0) + ocr.downloaded
    stats["processed"] = stats.get("processed", 0) + ocr.processed
    stats["completed"] = stats.get("completed", 0) + ocr.completed
    stats["failed"] = stats.get("failed", 0) + ocr.failed


def import_article(database: Database, article: dict[str, Any], *, ocr: OCRResult | None = None) -> str:
    content = str(article.get("content") or "").strip()
    metadata = {
        "input_type": "wechat_batch",
        "source_account": str(article.get("account") or "公众号批量采集").strip(),
        "source_url": str(article.get("link") or "").strip(),
        "title": str(article.get("title") or "").strip(),
        "publish_date": str(article.get("publishDate") or "").strip(),
    }
    image_sources = _article_images(article)
    ocr = ocr or ocr_wechat_images(image_sources, cookie=os.getenv("WECHAT_COOKIE", ""))
    raw_text = f"{metadata['title']}\n{content}".strip() if metadata["title"] else content
    if ocr.text:
        raw_text += f"\n\n【公众号配图 OCR】\n{ocr.text}"
    if len(raw_text) < 20:
        return "skipped_no_content"
    article_id = database.insert_article({**metadata, "raw_text": raw_text, "collector_status": "batch_success"})
    project = extract_project(raw_text, metadata)
    project["article_id"] = article_id
    if image_sources:
        project["image_sources"] = image_sources
        project["image_ocr"] = {
            "attempted": ocr.attempted, "downloaded": ocr.downloaded, "processed": ocr.processed,
            "text_found": ocr.completed, "failed": ocr.failed, "details": ocr.details,
        }
        if not ocr.runtime_available:
            project["image_ocr_status"] = "runtime_unavailable"
            note = f"原文包含 {len(image_sources)} 张配图，但当前环境未安装 OCR 引擎；下次扫描会自动重试。"
        elif ocr.processed == ocr.attempted:
            project["image_ocr_status"] = "completed"
            note = f"已处理全部 {ocr.processed}/{ocr.attempted} 张原文配图；其中 {ocr.completed} 张识别到文字。日期、地点和资格仍需与原图复核。"
        else:
            project["image_ocr_status"] = "partial_or_failed"
            failure_hint = f"失败原因：{'；'.join(ocr.failures[:2])}。" if ocr.failures else ""
            note = f"原文配图已下载 {ocr.downloaded}/{ocr.attempted} 张、已处理 {ocr.processed}/{ocr.attempted} 张、识别到文字 {ocr.completed} 张，失败 {ocr.failed} 张；{failure_hint}下次扫描会自动重试失败图片。"
        project["risk_notes"] = list(dict.fromkeys(project.get("risk_notes", []) + [note]))
    else:
        project["image_sources"] = []
        project["image_ocr_status"] = "not_needed"
    duplicate = database.find_duplicate(project)
    merged = False
    if duplicate:
        # Re-scan is intentionally enrichment, not a skip.  Preserve manually
        # reviewed fields when the new scrape is poorer, but always take richer
        # OCR text/image state and retry incomplete image OCR on future scans.
        project["id"] = duplicate["id"]
        project["created_at"] = duplicate.get("created_at", project["created_at"])
        for field in ("practice_start", "practice_end", "signup_deadline", "signup_method", "organizer", "contact"):
            if not project.get(field) and duplicate.get(field):
                project[field] = duplicate[field]
        # A newly fetched body can be poorer than an earlier OCR/manual review.
        # Never replace a specific eligibility, location, or funding statement
        # with an empty or placeholder extraction such as “活动地点”.
        if not project.get("eligibility", {}).get("restriction_text") and duplicate.get("eligibility", {}).get("restriction_text"):
            project["eligibility"] = duplicate["eligibility"]
        new_location = project.get("location", {})
        old_location = duplicate.get("location", {})
        placeholder_locations = {"", "活动地点", "实践地点", "项目地点"}
        if str(new_location.get("detail") or "").strip() in placeholder_locations and str(old_location.get("detail") or "").strip() not in placeholder_locations:
            project["location"] = old_location
        new_reimbursement = project.get("reimbursement", {})
        old_reimbursement = duplicate.get("reimbursement", {})
        if new_reimbursement.get("has_reimbursement") is None and old_reimbursement.get("has_reimbursement") is not None:
            project["reimbursement"] = old_reimbursement
        if len(project.get("schedule_segments", [])) < len(duplicate.get("schedule_segments", [])):
            project["schedule_segments"] = duplicate["schedule_segments"]
        project["image_sources"] = list(dict.fromkeys(duplicate.get("image_sources", []) + project.get("image_sources", [])))
        old_status = duplicate.get("image_ocr_status", "")
        new_status = project.get("image_ocr_status", "")
        if old_status == "completed" and new_status != "completed":
            project["image_ocr_status"] = "completed"
        project["field_evidence"] = deep_merge(duplicate.get("field_evidence", {}), project.get("field_evidence", {}))
        for field, old_evidence in duplicate.get("field_evidence", {}).items():
            new_evidence = project["field_evidence"].get(field, {})
            if old_evidence.get("extraction_method") == "image_ocr_review" and new_evidence.get("extraction_method") != "image_ocr_review":
                project["field_evidence"][field] = old_evidence
        project["risk_notes"] = list(dict.fromkeys(duplicate.get("risk_notes", []) + project.get("risk_notes", [])))
        uncertain = set(duplicate.get("uncertain_fields", [])) | set(project.get("uncertain_fields", []))
        if project.get("practice_start") and project.get("practice_end"):
            uncertain.discard("practice_dates")
        if project.get("signup_deadline"):
            uncertain.discard("signup_deadline")
        if project.get("eligibility", {}).get("restriction_text"):
            uncertain.discard("eligibility")
        if project.get("reimbursement", {}).get("has_reimbursement") is not None:
            uncertain.discard("reimbursement")
        project["uncertain_fields"] = sorted(uncertain)
        merged = True
    database.upsert_project(project, note="每日公众号采集更新" if merged else "每日公众号采集导入")
    return "merged" if merged else "imported"


def _write_audit(audit_dir: Path, summary: dict[str, Any], records: list[dict[str, Any]]) -> str:
    """Persist a compact, credential-free explanation of every scan decision."""
    audit_dir.mkdir(parents=True, exist_ok=True)
    path = audit_dir / f"scan-{datetime.now().astimezone().strftime('%Y%m%dT%H%M%S%z')}.json"
    payload = {"generated_at": datetime.now().astimezone().isoformat(timespec="seconds"), "summary": summary, "articles": records}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path.name


def collect(args: argparse.Namespace) -> int:
    report_progress(3, "preparing", "正在检查采集配置")
    collector = Path(args.collector).expanduser()
    if not collector.is_file():
        print(json.dumps({"ok": False, "error": "WECHAT_COLLECTOR_PATH is missing or invalid"}, ensure_ascii=False))
        return 2
    if not collector_credentials_present():
        print(json.dumps({"ok": False, "error": "set WECHAT_TOKEN and WECHAT_COOKIE in the developer task"}, ensure_ascii=False))
        return 2
    python = os.getenv("WECHAT_COLLECTOR_PYTHON", sys.executable)
    with tempfile.TemporaryDirectory(prefix="practice-xiaoda-daily-") as output_dir:
        environment = {**os.environ, "WECHAT_DIGEST_OUTPUT_DIR": output_dir}
        command = [python, str(collector), "collect", *args.accounts, "--since", args.since, "--count", str(args.count), "--no-kb"]
        report_progress(12, "collecting", f"正在拉取 {len(args.accounts)} 个公众号的新文章")
        try:
            completed = subprocess.run(command, env=environment, timeout=args.timeout, capture_output=True, text=True, check=False)
        except (OSError, subprocess.TimeoutExpired):
            print(json.dumps({"ok": False, "error": "collector execution failed or timed out"}, ensure_ascii=False))
            return 1
        files = sorted(Path(output_dir).glob("articles_*.json"))
        if completed.returncode not in {0, 130} or not files:
            print(json.dumps({"ok": False, "error": "collector returned no usable article batch"}, ensure_ascii=False))
            return 1
        try:
            articles = json.loads(files[-1].read_text(encoding="utf-8")).get("articles", [])
        except (OSError, json.JSONDecodeError):
            print(json.dumps({"ok": False, "error": "collector output was not valid JSON"}, ensure_ascii=False))
            return 1

    database = Database(args.database)
    valid_articles = [article for article in articles if isinstance(article, dict)]
    summary: dict[str, Any] = {
        "seen": len(articles), "audited": len(valid_articles), "candidate": 0, "non_candidate": 0,
        "imported": 0, "merged": 0, "removed_non_opportunity": 0, "skipped_no_content": 0,
    }
    ocr_stats = {"articles_processed": 0, "articles_with_images": 0, "articles_skipped_by_rule": 0, "attempted": 0, "downloaded": 0, "processed": 0, "completed": 0, "failed": 0}
    audit_records: list[dict[str, Any]] = []
    for index, article in enumerate(valid_articles, start=1):
        title = str(article.get("title") or "未命名文章").strip()[:50]
        percent = 30 + int(62 * (index - 1) / max(1, len(valid_articles)))
        report_progress(percent, "ocr_and_classify", f"正在 OCR 与判断文章 {index}/{len(valid_articles)}：{title}", current=index, total=len(valid_articles))
        image_sources = _article_images(article)
        pre_decision = candidate_decision(article)
        # A clearly retrospective article (for example “行前预告” or
        # “活动回顾”) is audited and recorded but does not spend minutes OCRing
        # decorative galleries.  Every other article is OCRed before its final
        # decision so recruitment text hidden in a poster cannot be missed.
        if pre_decision["hard_excluded"]:
            ocr = OCRResult("", 0, 0, 0, True)
            ocr_stats["articles_processed"] += 1
            ocr_stats["articles_skipped_by_rule"] += 1
            skipped_visual = True
        else:
            ocr = ocr_wechat_images(image_sources, cookie=os.getenv("WECHAT_COOKIE", ""))
            _update_ocr_stats(ocr_stats, ocr, has_images=bool(image_sources))
            skipped_visual = False
        decision = candidate_decision(article, ocr.text)
        audit_record = {
            "title": title, "source_url": str(article.get("link") or ""), "images": len(image_sources),
            "candidate": decision["candidate"], "candidate_score": decision["score"], "decision_reasons": decision["reasons"],
            "ocr": {"skipped_by_rule": skipped_visual, "attempted": ocr.attempted, "downloaded": ocr.downloaded, "processed": ocr.processed, "text_found": ocr.completed, "failed": ocr.failed, "failures": ocr.failures[:5]},
        }
        audit_records.append(audit_record)
        if not decision["candidate"]:
            summary["non_candidate"] += 1
            if decision["hard_excluded"]:
                summary["removed_non_opportunity"] += database.delete_projects_by_source(
                    str(article.get("link") or ""), note="每日采集识别为行前预告/纪实/回顾",
                )
            continue
        summary["candidate"] += 1
        outcome = import_article(database, article, ocr=ocr)
        summary[outcome] = summary.get(outcome, 0) + 1
    summary["ocr"] = ocr_stats
    summary["audit_file"] = _write_audit(Path(args.audit_dir), summary, audit_records)
    database.log("daily_wechat_update", "每日公众号采集完成", summary)
    report_progress(98, "finalizing", "正在写入机会库并保存扫描审计", current=len(valid_articles), total=len(valid_articles))
    print(json.dumps({"ok": True, "since": args.since, "accounts": args.accounts, **summary}, ensure_ascii=False))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="每日抓取可信公众号中的社会实践招募线索")
    parser.add_argument("--collector", default=os.getenv("WECHAT_COLLECTOR_PATH", ""))
    parser.add_argument("--database", default=os.getenv("PRACTICE_XIAODA_DB", str(ROOT / "data" / "practice_xiaoda.db")))
    parser.add_argument("--since", default=(date.today() - timedelta(days=2)).isoformat())
    parser.add_argument("--count", type=int, default=int(os.getenv("WECHAT_DAILY_COUNT", "12")))
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--accounts", nargs="+", default=list(DEFAULT_ACCOUNTS))
    parser.add_argument("--audit-dir", default=str(ROOT / "data" / "collector_audits"))
    return collect(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
