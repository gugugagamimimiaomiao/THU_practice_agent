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


# ── 是不是社会实践/志愿服务 ────────────────────────────────────────────
#
# 上面那套判据问的是「这是不是一篇招募」。它管不了另一个维度：**招的是什么**。
# 「【代表队招新】体育集结号——男篮篇」命中「招新」，一路放行进了机会库。
#
# 实测 73 条真实项目，只有 28 条是学生要找的社会实践/志愿服务；20 条是体育
# 代表队，11 条是学生组织招新（进宣传部当骨干），其余是学科竞赛、留学讲座、
# 迎新晚会主持人、教学助教岗。近一半的库存跟这个产品要解决的问题无关。
#
# **不删，只分类。** 三个理由：
#   1. 误判会静默丢掉真机会，而删除不可逆——分类错了至少还能翻出来看
#   2. 采集是合作者一条条攒的，不该因为我判错就扔掉
#   3. 分类结果写进项目卡，出了错能一条命令捞回来
#
# 判据从 73 条真实标题长出来，不是我拍的；每一类后面括号里是它在库里对应
# 的实例。改这张表之前先跑 tests/test_opportunity_kind.py，那里有全部 73 条
# 的人工标注当基准。

# 明确指向社会实践/志愿服务。这些词一出现，基本就定性了。
PRACTICE_WORDS = (
    "社会实践", "实践招募", "实践支队", "实践支队员", "暑期实践", "暑假实践",
    "寒假实践", "实践项目", "实践基地", "支教", "调研支队", "志愿服务",
    "志愿者招募", "志愿招募", "志愿者", "支队招募", "支队员招募",
)

# 校内活动，不是社会实践。分组是为了让注释能贴着实例走。
_CAMPUS_SPORTS = ("代表队招新", "体育集结号", "男足", "女足", "男篮", "女篮",
                  "男排", "女排", "体育助教", "队招新")           # 20 条体育队 + 助教岗
_CAMPUS_CONTEST = ("设计大赛", "竞赛", "开拓杯", "征名", "主题征集")   # 红石大赛、开拓杯、学生节
_CAMPUS_EVENT = ("分享会", "交流会报名", "讲座", "晚会", "舞会",
                 "主持人招募", "节目征集")                        # 留学分享会、迎新晚会
# 学生组织招新——加入部门当骨干，不是去做实践。这一类最容易有争议：
# 「校团委志愿中心学生骨干招募」带着"志愿"两个字，但它招的是组织成员。
# 判成 campus 是我的判断，不是铁律；要改就改这一组。
_CAMPUS_ORG = ("学生骨干招募", "骨干招募", "组长招募", "副组长", "组员招募",
               "部长招新", "部门介绍", "研团招新", "转岗", "学生组织", "分会")

CAMPUS_WORDS = _CAMPUS_SPORTS + _CAMPUS_CONTEST + _CAMPUS_EVENT + _CAMPUS_ORG

# 校级实践/公益号发的东西，默认就是实践——它们的定位就是这个。
# 院系号和书院号不算：它们什么都发，从体育队到部门招新。
PRACTICE_ACCOUNTS = ("清华大学社会实践", "清华大学学生公益", "清华大学乡村振兴工作站")


def opportunity_kind(title: str, source_account: str = "") -> str:
    """这条机会是社会实践/志愿服务（practice），还是校内活动（campus）。

    校内活动照常入库，只是不进正式推荐——学生找实践时不该看到体育队招新。

    顺序是有讲究的：**先看否定信号**。「校团委学习实践部组长招募」里有
    「实践」，「志愿中心学生骨干招募」里有「志愿」，但它们招的是部门成员。
    反过来先看肯定信号的话，这两类会被误判成实践。
    """
    text = f"{title} {source_account}"
    if any(word in text for word in CAMPUS_WORDS):
        return "campus"
    if any(word in text for word in PRACTICE_WORDS):
        return "practice"
    if source_account in PRACTICE_ACCOUNTS:
        return "practice"
    # **认不出来的默认当实践**，不默认排除。
    #
    # 两种兜底的错法不对称：判成 campus 会把一条真机会静默地挡在推荐之外，
    # 学生永远不知道它存在；判成 practice 最多是让一条杂物混进列表，用户
    # 一眼就看见，还能反馈给我们。看得见的错比看不见的错好收拾。
    #
    # 实测这个方向也不吃亏：73 条里认不出的那些，绝大多数已经被上面的
    # campus 词表接住了。
    return "practice"


def _terminal_exclusions(title: str, body: str) -> list[str]:
    """Return explicit closed-recruitment signals, ignoring future sequencing.

    Notices often say ``赛程将在报名结束后安排`` while registration is still
    open.  That phrase describes what happens later; it is not evidence that
    signup has already closed.  A terminal phrase in the title remains an
    unconditional exclusion, while the body form is ignored only when it is
    immediately followed by ``后``.
    """
    hits: list[str] = []
    for word in TERMINAL_EXCLUDE_WORDS:
        if word in title:
            hits.append(word)
            continue
        searchable_body = body.replace(f"{word}后", "")
        if word in searchable_body:
            hits.append(word)
    return hits


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
    terminal_excluded = _terminal_exclusions(title, body)
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
