# 项目卡字段与审核规则

每个项目使用以下核心结构。缺失值使用 `null`、空字符串或空数组，不得猜测。

```json
{
  "id": "",
  "title": "",
  "source_account": "",
  "source_url": "",
  "publish_date": null,
  "organizer": "",
  "summary": "",
  "theme_tags": [],
  "practice_start": null,
  "practice_end": null,
  "signup_deadline": null,
  "location": {
    "country": "中国",
    "province": "",
    "city": "",
    "detail": "",
    "mode": "offline|online|hybrid|unknown"
  },
  "eligibility": {
    "departments": [],
    "grades": [],
    "majors": [],
    "identities": [],
    "explicit_no_restriction": false,
    "restriction_text": ""
  },
  "reimbursement": {
    "has_reimbursement": null,
    "ratio": null,
    "amount": null,
    "text": ""
  },
  "signup_method": "",
  "required_materials": [],
  "risk_notes": [],
  "uncertain_fields": [],
  "field_evidence": {},
  "confidence": 0.0,
  "status": "draft|needs_review|published|expired|rejected"
}
```

审核时执行以下规则：

- 截止日期、实践日期、资格和报销必须来自明确原文。
- OCR 产生的日期、金额或联系方式需要风险提示。
- “面向全校”只有在后文没有矛盾限制时才表示无院系限制。
- 只有二维码时，将报名方式标为“原文二维码报名”并保留原文链接。
- 截止日期早于当前日期时标记为 `expired`。
- 截止日期、资格、报销、来源或报名方式缺失/含糊时使用 `needs_review`。
- 正式推荐只使用 `published`；`needs_review` 只能作为潜在机会显示。

