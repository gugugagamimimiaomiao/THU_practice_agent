---
name: practice-xiaoda
description: Import, verify, match, compare, and act on Tsinghua social-practice opportunities through the 实践小搭 API. Use when handling WeChat recruitment links, copied notices, screenshot OCR text, administrator submissions, social-practice project cards, deadline or eligibility checks, reimbursement filtering, personalized practice recommendations, application drafts, outreach scripts, interview guides, itineraries, or research-report outlines.
---

# 实践小搭

Use the bundled API client to turn social-practice notices into evidence-backed project cards and guide a student from opportunity discovery through preparation.

## Workflow

1. Check the service with `python3 scripts/client.py health`.
2. Identify the input as a WeChat URL, copied full text, OCR text, or administrator notice.
3. Import the input with `scripts/client.py ingest`. A public `mp.weixin.qq.com` URL can be fetched automatically; if it returns `fetch_failed`, request copied text or OCR and never claim the article was read.
4. Inspect `uncertain_fields`, `risk_notes`, `field_evidence`, and `status` before using the project.
5. Recommend only `published` projects as formal matches. Keep `needs_review` projects in a clearly labeled potential section.
6. Apply deadline, date-conflict, department, grade, and identity restrictions as hard filters. Never override them with semantic similarity.
7. Generate application, outreach, interview, itinerary, or report drafts only after selecting a project. Preserve placeholders instead of inventing user experience.
8. Require user confirmation before any external submission, email, or message.

## Commands

```bash
python3 scripts/client.py health
python3 scripts/client.py projects --status published
python3 scripts/client.py ingest --input-type copied_text --source-account "清华大学社会实践" --source-url "URL" --text-file notice.txt
python3 scripts/client.py recommend --department "社科学院" --start 2026-08-01 --end 2026-08-25 --themes "乡村振兴,公共治理"
python3 scripts/client.py generate PROJECT_ID --kind interview
```

Use `PRACTICE_XIAODA_URL` to override the default `http://127.0.0.1:8765` service URL.
Use `PRACTICE_XIAODA_API_KEY` (or `--api-key`) for Bearer authentication.

For 清小搭 standard-protocol integration, the public base URL must end at `/v1`.
The service exposes `GET /v1/models` and `POST /v1/chat/completions`; `chat --stream`
prints the SSE frames and the final `data: [DONE]` sentinel.

## References

- Read `references/project-card-schema.md` before importing, reviewing, or explaining project fields.
- Read `references/api.md` when constructing direct API calls or integrating another agent.

## Reliability Rules

- Preserve the source account and source URL.
- Do not infer deadlines, eligibility, or reimbursement without explicit evidence.
- Mark OCR-derived critical values and missing fields for review.
- Hide expired projects from formal recommendations while retaining them for history.
- Keep dynamic opportunities in the structured project database; keep static research and writing guidance in a separate RAG knowledge base.
- Treat demo records marked `demo_data: true` as product examples, never as real application opportunities.
