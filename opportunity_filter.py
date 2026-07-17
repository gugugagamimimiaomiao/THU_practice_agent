"""Deterministic gate that keeps non-recruitment content out of the opportunity DB."""

from __future__ import annotations

from typing import Any


RECRUITMENT_WORDS = ("招募", "报名", "志愿者", "招新", "选拔", "申请", "征集", "加入", "组队")
ACTION_WORDS = ("报名方式", "申请方式", "报名通道", "招募对象", "报名对象", "填写问卷", "扫描二维码", "扫码报名", "共招募", "招募计划", "名额")
TITLE_EXCLUDE_WORDS = ("结项", "成果展示", "行前预告", "体验纪实", "活动回顾", "行中推送", "侧记")
TERMINAL_EXCLUDE_WORDS = ("招募结束", "报名结束")


def candidate_decision(article: dict[str, Any], ocr_text: str = "") -> dict[str, Any]:
    """Decide whether an article can become a social-practice opportunity.

    Titles alone are insufficient: a recruitment call can sit in the body or a
    poster.  Conversely, a retrospective may thank volunteers without being
    open for registration.  The returned reasons are safe to persist in audit
    records and never contain credentials.
    """
    title = str(article.get("title") or "").strip()
    body = f"{article.get('content') or ''}\n{ocr_text}".strip()
    title_hits = [word for word in RECRUITMENT_WORDS if word in title]
    body_hits = [word for word in RECRUITMENT_WORDS if word in body]
    action_hits = [word for word in ACTION_WORDS if word in body]
    # A recruitment notice can mention a previous “结项” or an old activity
    # in its body.  Editorial/review labels are therefore title signals; only
    # an explicit closed-recruitment phrase may exclude from the full body.
    excluded = [word for word in TITLE_EXCLUDE_WORDS if word in title]
    excluded += [word for word in TERMINAL_EXCLUDE_WORDS if word in title or word in body]
    score = (3 if title_hits else 0) + (2 if body_hits else 0) + (2 if action_hits else 0)
    if "招募" in body and any(char.isdigit() for char in body):
        score += 1
    if excluded:
        score -= 8
    reasons: list[str] = []
    if title_hits:
        reasons.append(f"标题招募信号：{'、'.join(title_hits[:3])}")
    if body_hits:
        reasons.append(f"正文/OCR 招募信号：{'、'.join(body_hits[:3])}")
    if action_hits:
        reasons.append(f"行动信号：{'、'.join(action_hits[:3])}")
    if excluded:
        reasons.append(f"排除信号：{'、'.join(excluded[:3])}")
    # An explicit retrospective label with no signup action can be skipped
    # before image OCR.  If it contains a real signup flow, still OCR it for
    # audit, but never promote it automatically.
    return {
        "candidate": score >= 3 and not excluded,
        "score": score,
        "reasons": reasons,
        "hard_excluded": bool(excluded) and not bool(action_hits),
    }
