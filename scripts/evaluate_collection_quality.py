#!/usr/bin/env python3
"""Evaluate OCR field recovery and recruitment classification on labeled samples.

The input contains no credentials or image bytes. Each JSONL row describes the
article before OCR, the OCR text captured by the pipeline, and reviewer labels.
This makes quality checks reproducible without committing private WeChat data.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from domain import extract_project  # noqa: E402
from opportunity_filter import candidate_decision  # noqa: E402


FIELDS = ("practice_start", "practice_end", "signup_deadline", "signup_method", "organizer", "contact")


def read_samples(path: Path) -> list[dict[str, Any]]:
    samples = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"第 {line_number} 行不是有效 JSON：{exc}") from exc
        if not isinstance(item, dict):
            raise ValueError(f"第 {line_number} 行必须是 JSON 对象")
        samples.append(item)
    return samples


def project_fields(article: dict[str, Any], ocr_text: str) -> dict[str, Any]:
    raw_text = "\n".join(part for part in (str(article.get("title") or ""), str(article.get("content") or ""), ocr_text) if part)
    project = extract_project(raw_text, {
        "input_type": "quality_evaluation",
        "source_account": str(article.get("account") or "质量评估"),
        "source_url": str(article.get("link") or ""),
        "title": str(article.get("title") or ""),
        "publish_date": str(article.get("publishDate") or ""),
    })
    fields = {field: project.get(field) for field in FIELDS}
    fields.update({
        "location": project.get("location", {}).get("detail") or "",
        "eligibility": project.get("eligibility", {}).get("restriction_text") or "",
        "reimbursement": project.get("reimbursement", {}).get("text") or "",
    })
    return fields


def evaluate(samples: list[dict[str, Any]]) -> dict[str, Any]:
    tp = fp = tn = fn = 0
    expected_fields = correct_fields = recovered_fields = 0
    records = []
    for index, sample in enumerate(samples, 1):
        article = sample.get("article") if isinstance(sample.get("article"), dict) else {}
        ocr_text = str(sample.get("ocr_text") or "")
        expected_candidate = bool(sample.get("expected_candidate"))
        decision = candidate_decision(article, ocr_text)
        predicted_candidate = bool(decision["candidate"])
        if expected_candidate and predicted_candidate:
            tp += 1
        elif not expected_candidate and predicted_candidate:
            fp += 1
        elif not expected_candidate and not predicted_candidate:
            tn += 1
        else:
            fn += 1

        before = project_fields(article, "")
        after = project_fields(article, ocr_text)
        expected = sample.get("expected_fields") if isinstance(sample.get("expected_fields"), dict) else {}
        field_diffs = {}
        for field, value in expected.items():
            if field not in after or value in {None, ""}:
                continue
            expected_fields += 1
            actual = after.get(field)
            correct = str(actual or "").strip() == str(value).strip()
            recovered = not str(before.get(field) or "").strip() and bool(str(actual or "").strip())
            correct_fields += int(correct)
            recovered_fields += int(correct and recovered)
            field_diffs[field] = {"expected": value, "before_ocr": before.get(field), "after_ocr": actual, "correct": correct, "recovered": recovered}
        records.append({
            "sample": sample.get("id") or index,
            "source_url": article.get("link") or "",
            "expected_candidate": expected_candidate,
            "predicted_candidate": predicted_candidate,
            "decision_reasons": decision["reasons"],
            "fields": field_diffs,
        })

    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    return {
        "samples": len(samples),
        "classification": {
            "true_positive": tp, "false_positive": fp, "true_negative": tn, "false_negative": fn,
            "precision": round(precision, 4), "recall": round(recall, 4),
        },
        "ocr_fields": {
            "expected": expected_fields,
            "correct": correct_fields,
            "recovered_from_images": recovered_fields,
            "accuracy": round(correct_fields / expected_fields, 4) if expected_fields else 0.0,
            "recovery_rate": round(recovered_fields / expected_fields, 4) if expected_fields else 0.0,
        },
        "records": records,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="评估公众号采集分类和 OCR 字段补全质量")
    parser.add_argument("samples", type=Path, help="人工标注 JSONL")
    parser.add_argument("--output", type=Path, default=ROOT / "data" / "collection_quality_report.json")
    args = parser.parse_args()
    samples = read_samples(args.samples)
    if not samples:
        print("没有可评估样本", file=sys.stderr)
        return 2
    report = evaluate(samples)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": True, "output": str(args.output), **{k: v for k, v in report.items() if k != "records"}}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
