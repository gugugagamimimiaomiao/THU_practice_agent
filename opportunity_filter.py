"""Deterministic gate that keeps non-recruitment content out of the opportunity DB."""

from __future__ import annotations

from typing import Any


RECRUITMENT_WORDS = ("招募", "报名", "志愿者", "招新", "选拔", "申请", "征集", "加入", "组队")
TITLE_OVERRIDE_WORDS = ("招募", "报名", "招新", "志愿者")
ACTION_WORDS = (
    "报名方式", "申请方式", "报名通道", "报名链接", "报名表", "报名截止", "截止日期",
    "招募对象", "报名对象", "参与对象", "招募要求", "报名要求",
    "填写问卷", "扫描二维码", "扫码报名", "长按识别", "问卷星", "wjx.cn",
    "共招募", "招募计划", "招募人数", "名额", "拟招",
)
# 体裁标签。出现在标题里基本就给这篇文章定了性：它在介绍、总结、说明某件事，
# 而不是在招人。真实数据里这几类占了相当比例。
GENRE_EXCLUDE_WORDS = (
    "资源推介", "基地推介", "基地介绍", "风采展示", "资源介绍",
    "实践日报", "工作简报", "实践简报",
    "实践总结", "实践纪实", "经验分享", "心得体会",
    "选课说明", "学习说明", "培训说明", "课程说明",
    "圆满结束", "顺利举行", "圆满举行", "成功举办",
)
TITLE_EXCLUDE_WORDS = (
    "结项", "成果展示", "行前预告", "体验纪实", "活动回顾", "行中推送", "侧记",
    *GENRE_EXCLUDE_WORDS,
)
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
    title_overrides = [word for word in TITLE_OVERRIDE_WORDS if word in title]
    body_hits = [word for word in RECRUITMENT_WORDS if word in body]
    action_hits = [word for word in ACTION_WORDS if word in body]
    # A recruitment notice can mention a previous “结项” or an old activity
    # in its body.  Editorial/review labels are therefore title signals; only
    # an explicit closed-recruitment phrase may exclude from the full body.
    genre_excluded = [word for word in TITLE_EXCLUDE_WORDS if word in title]
    terminal_excluded = [word for word in TERMINAL_EXCLUDE_WORDS if word in title or word in body]
    excluded = genre_excluded + terminal_excluded
    score = (3 if title_hits else 0) + (2 if body_hits else 0) + (2 if action_hits else 0)
    if "招募" in body and any(char.isdigit() for char in body):
        score += 1
    # The receiving server explicitly wants real calls for applicants even
    # when an editorial prefix also looks like a recap or resource feature.
    # Example: “实践基地进清华｜...项目，招募启动！”.  The override is title
    # only: an old recap merely mentioning recruitment in its body stays out.
    effective_excluded = terminal_excluded + (genre_excluded if not title_overrides else [])
    if effective_excluded:
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
    # 一个能报名的机会，总得说清楚"怎么报"：要么标题就在喊招募，要么正文里
    # 有报名方式、招募对象、名额、二维码之类的行动线索。两者至少占一个。
    #
    # 这条是拿真实数据补上的。原来只看总分，而"正文里出现过招募词"就 +2、
    # "正文里有数字"再 +1，正好卡在 3 分门槛上——于是 28 篇「XX基地资源推介」
    # 里有 24 篇这样过了线，生成了假的项目卡。它们标题没有招募词、正文没有任何
    # 报名线索，只是在介绍基地时提了一句往年有多少支队招募过队员。
    #
    # 换句话说：正文里飘过一次"招募"，说明这篇文章**提到**了招募，不说明它
    # **本身**在招募。剩下 4 篇被拦下纯属侥幸——那 4 篇正文里恰好没出现这两个字。
    actionable = bool(title_hits or action_hits)
    if not actionable:
        reasons.append("正文提到招募，但没有标题信号，也没有任何报名方式/对象/名额线索")
    if excluded and title_overrides:
        reasons.append(f"标题强招募信号覆盖排除：{'、'.join(title_overrides[:3])}")
    # An explicit retrospective label with no signup action can be skipped
    # before image OCR.  If it contains a real signup flow, still OCR it for
    # audit, but never promote it automatically.
    return {
        "candidate": score >= 3 and actionable and not effective_excluded,
        "score": score,
        "reasons": reasons,
        "hard_excluded": bool(effective_excluded) and not bool(action_hits),
    }
