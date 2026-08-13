"""Core domain rules for 实践小搭.

The module deliberately keeps critical decisions deterministic: dates, eligibility,
expiry, review status and recommendation hard filters never depend on an LLM.
"""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Iterable
from urllib.parse import parse_qs, urlparse, urlunparse


DATE_RE = re.compile(r"(?:(20\d{2})[年./-])?(\d{1,2})[月./-](\d{1,2})日?")
DATE_RANGE_RE = re.compile(r"(?:(?P<sy>20\d{2})年)?(?P<sm>\d{1,2})月(?P<sd>\d{1,2})日?\s*(?:—|－|-|至|~)\s*(?:(?P<ey>20\d{2})年)?(?P<em>\d{1,2})月(?P<ed>\d{1,2})日?")
AMOUNT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*元")
RATIO_RE = re.compile(r"(\d{1,3})\s*%")

KNOWN_LOCATIONS = [
    "北京", "上海", "天津", "重庆", "河北", "河南", "山东", "山西", "陕西", "甘肃",
    "青海", "四川", "云南", "贵州", "广西", "广东", "福建", "浙江", "江苏", "安徽",
    "江西", "湖北", "湖南", "辽宁", "吉林", "黑龙江", "内蒙古", "宁夏", "新疆", "西藏",
    "海南", "香港", "澳门", "台湾", "线上",
]

KNOWN_DEPARTMENTS = [
    "建筑学院", "土木系", "水利系", "环境学院", "机械系", "精仪系", "能源与动力工程系",
    "车辆学院", "工业工程系", "电机系", "电子系", "计算机系", "自动化系", "集成电路学院",
    "航院", "工程物理系", "化工系", "材料学院", "数学系", "物理系", "化学系", "生命学院",
    "医学院", "经管学院", "公管学院", "人文学院", "社科学院", "法学院", "新闻学院",
    "美术学院", "新雅书院", "致理书院", "日新书院", "未央书院", "探微书院", "行健书院",
]

# 抽取器内部用英文字段名，但「待确认字段」是要直接给学生看的——
# 甩一串 eligibility、reimbursement 没人看得懂。
FIELD_LABELS = {
    "signup_deadline": "报名截止",
    "eligibility": "参与资格",
    "reimbursement": "经费与报销",
    "practice_dates": "实践时间",
    "location": "地点",
    "signup_method": "报名方式",
    "contact": "联系方式",
    "organizer": "主办方",
    "quota": "招募人数",
    "source_url": "原文链接",
    "required_materials": "报名材料",
}

GRADE_TERMS = ["本科生", "研究生", "硕士生", "博士生", "大一", "大二", "大三", "大四"]
IDENTITY_TERMS = ["清华学生", "清华师生", "学生党员", "共青团员", "志愿者", "留学生"]

THEME_KEYWORDS = {
    "乡村振兴": ["乡村振兴", "农村", "乡村", "产业振兴", "基层治理"],
    "教育": ["教育", "支教", "学习", "课程", "儿童"],
    "科技创新": ["科技", "人工智能", "AI", "数字化", "创新", "科普"],
    "生态环保": ["生态", "环保", "环境", "低碳", "生物多样性"],
    "公益志愿": ["公益", "志愿", "服务", "助老", "帮扶"],
    "文化传承": ["文化", "非遗", "文物", "传统", "乡土"],
    "公共治理": ["治理", "社区", "公共政策", "政务", "基层"],
    "健康医疗": ["医疗", "健康", "卫生", "养老", "心理"],
}

SOURCE_RELIABILITY = {
    "清华大学社会实践": 1.0,
    "乐学": 0.95,
    "管理员投稿": 0.85,
    "用户投稿": 0.65,
}


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def parse_iso_date(value: Any) -> date | None:
    if not value:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def parse_date_from_text(text: str, default_year: int) -> str | None:
    match = DATE_RE.search(text or "")
    if not match:
        return None
    year = int(match.group(1) or default_year)
    month, day = int(match.group(2)), int(match.group(3))
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def normalize_url(raw_url: str) -> str:
    if not raw_url:
        return ""
    try:
        parsed = urlparse(raw_url.strip())
        query = parse_qs(parsed.query, keep_blank_values=False)
        preserved: list[str] = []
        for key in ("sn", "__biz", "mid", "idx"):
            if key in query:
                preserved.append(f"{key}={query[key][0]}")
        return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, "", "&".join(preserved), ""))
    except Exception:
        return raw_url.strip()


def stable_id(*parts: str) -> str:
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:12]
    return f"pxd_{digest}"


def _find_line(lines: list[str], keywords: Iterable[str]) -> str:
    for line in lines:
        if any(keyword in line for keyword in keywords):
            return line.strip()
    return ""


# 中文句末标点。用于把挤成一整行的正文切回一句一句。
_SENTENCE_END = re.compile(r"(?<=[。；！？!?])")


def _clip_to_keyword_sentence(line: str, keywords: Iterable[str], *, limit: int) -> str:
    """从一整行里取出含关键词的那一句，并剥掉「标签：」前缀；太长则判为抽取失败。

    公众号正文被转发或被采集器压平之后经常丢掉换行，整段挤成一行。原来的做法
    是「砍掉第一个冒号之前的部分，剩下全要」，在这种输入上会把后面几句话一起
    吞进来——实测抽出过一个 60 字的「实践地点」，里面还夹着报名截止日期和一句
    欢迎报名。

    这种结果比抽不到更糟：抽不到会被标进 uncertain_fields 交给人工核验，而一个
    看起来有值的字段会被当成已确认，直接进入推荐和文案。所以这里宁可判失败——
    一个 60 字的「地点」显然不是地点。
    """
    if not line:
        return ""
    for sentence in _SENTENCE_END.split(line):
        if any(keyword in sentence for keyword in keywords):
            value = re.sub(r"^.*?[：:]", "", sentence).strip().strip("。；！？!?,，")
            return value if 0 < len(value) <= limit else ""
    # 关键词不在任何一句里（通常是整行没有句末标点），退回原有做法再判长度。
    value = re.sub(r"^.*?[：:]", "", line).strip()
    return value if 0 < len(value) <= limit else ""


def _find_section(lines: list[str], headings: Iterable[str], *, max_lines: int = 12) -> str:
    """Return a labeled text block when schedule/requirements follow a heading."""
    for index, line in enumerate(lines):
        if any(heading in line for heading in headings):
            return "\n".join(lines[index:index + max_lines]).strip()
    return ""


def _evidence(line: str, input_type: str) -> dict[str, str] | None:
    if not line:
        return None
    return {
        "quote": line[:360],
        "source_location": "导入文本",
        "extraction_method": "ocr" if input_type == "ocr_text" else "text",
    }


def _extract_dates(lines: list[str], today: date) -> tuple[str | None, str | None, str | None, dict[str, Any]]:
    deadline_line = _find_line(lines, ["报名截止", "截止时间", "报名截至", "申报截止", "报名截止至"])
    deadline = parse_date_from_text(deadline_line, today.year)

    practice_line = _find_line(lines, ["实践时间", "活动时间", "项目时间", "调研时间", "出发时间"])
    # Many volunteer notices use a standalone “时间安排” heading and place
    # the actual dates on subsequent lines.  Treat that labeled block as an
    # explicit schedule, rather than leaving dates unparsed.
    if not list(DATE_RE.finditer(practice_line)):
        practice_line = _find_section(lines, ["时间安排", "工作时间", "服务时间", "排班安排"])
    matches = list(DATE_RE.finditer(practice_line))
    practice_start = practice_end = None
    if matches:
        practice_start = parse_date_from_text(matches[0].group(0), today.year)
        practice_end = parse_date_from_text(matches[-1].group(0), today.year)
        if practice_start and practice_end and practice_end < practice_start:
            end_match = matches[-1]
            y = int(end_match.group(1) or today.year + 1)
            try:
                practice_end = date(y, int(end_match.group(2)), int(end_match.group(3))).isoformat()
            except ValueError:
                practice_end = None

    return deadline, practice_start, practice_end, {
        "signup_deadline": deadline_line,
        "practice_dates": practice_line,
    }


def _extract_schedule_segments(text: str, default_year: int) -> list[dict[str, str]]:
    """Extract explicit date ranges while preserving multiple optional shifts."""
    segments: list[dict[str, str]] = []
    for match in DATE_RANGE_RE.finditer(text):
        try:
            start_year = int(match.group("sy") or default_year)
            start = date(start_year, int(match.group("sm")), int(match.group("sd"))).isoformat()
            end = date(int(match.group("ey") or start_year), int(match.group("em")), int(match.group("ed"))).isoformat()
        except ValueError:
            continue
        context = text[max(0, match.start() - 48): min(len(text), match.end() + 48)]
        period = "下午" if "下午" in context else "上午" if "上午" in context else ""
        label = ""
        prefix = text[max(0, match.start() - 100):match.start()].splitlines()
        if prefix:
            label = prefix[-1].strip(" ①②③④⑤、.：:")[:60]
        segment = {"start": start, "end": end, "period": period, "label": label}
        if segment not in segments:
            segments.append(segment)
    return segments


def _extract_title(lines: list[str], supplied: str) -> str:
    if supplied.strip():
        return supplied.strip()[:160]
    for line in lines:
        # 公众号标题常带【招募】【通知】这类前缀标签，先整段剥掉；
        # 只剥开头的一个短标签，避免误伤标题里本来就有的方括号内容。
        candidate = re.sub(r"^\s*[【\[][^】\]]{0,8}[】\]]\s*", "", line)
        candidate = re.sub(r"^[#【\[\s]+|[】\]\s]+$", "", candidate).strip()
        if 4 <= len(candidate) <= 80 and not any(x in candidate for x in ["来源：", "公众号：", "发布时间："]):
            return candidate
    return "未命名社会实践项目"


def _extract_themes(text: str) -> list[str]:
    tags = [theme for theme, words in THEME_KEYWORDS.items() if any(word.lower() in text.lower() for word in words)]
    return tags[:5] or ["综合实践"]


LOCATION_KEYWORDS = ["实践地点", "活动地点", "项目地点", "调研地点", "地点："]
# 地点写到"云南省大理白族自治州祥云县某某镇"已经很长了；超过这个长度基本是抽串行了。
MAX_LOCATION_DETAIL = 40
# 资格说明可以长一些（会列院系、年级、专业），但一整段正文肯定不是资格说明。
MAX_ELIGIBILITY_TEXT = 90


def _extract_location(lines: list[str], text: str) -> tuple[dict[str, str], str]:
    location_line = _find_line(lines, LOCATION_KEYWORDS)
    haystack = location_line or text[:1000]
    place = next((item for item in KNOWN_LOCATIONS if item in haystack), "")
    if "线上" in haystack and any(word in haystack for word in ["线下", "实地"]):
        mode = "hybrid"
    elif "线上" in haystack:
        mode = "online"
    elif location_line or place:
        mode = "offline"
    else:
        mode = "unknown"
    # 没有地点行时 detail 留空，不要拿正文里认出的省名（place）顶上。
    # 省名已经存在 province 字段里，塞进 detail 不增加任何信息，却会在合并时
    # 变成一个"非空的弱值"，把上一版抽到的"云南省大理州"顶掉——实测踩过。
    detail = _clip_to_keyword_sentence(location_line, LOCATION_KEYWORDS, limit=MAX_LOCATION_DETAIL)
    return {
        "country": "中国",
        "province": place if place not in {"线上", ""} else "",
        "city": "",
        "detail": detail,
        "mode": mode,
    }, location_line


# 明确的资格标签。这些词出现基本就意味着"这一行讲的是谁能报"。
ELIGIBILITY_LABELS = [
    "招募对象", "面向对象", "报名对象", "参与对象", "参加对象", "招收对象", "面向人群",
    "参与资格", "报名资格", "申请资格", "申请条件", "参与条件", "选拔条件",
    "招募要求", "报名要求", "报名条件", "招募范围",
]
# 宽松线索。只在没有明确标签时才用——招募通知的第一句几乎都是
# "……现面向全校招募……"这种套话，拿它当资格说明会把整句开场白写进项目卡。
ELIGIBILITY_LOOSE = ["面向全校", "全校学生", "全校师生", "仅限", "限本科", "限研究生"]
ELIGIBILITY_KEYWORDS = ELIGIBILITY_LABELS + ELIGIBILITY_LOOSE
_ELIGIBILITY_HEADING_ONLY = {"报名要求", "招募要求", "申请条件", "报名条件", "参与资格", "招募对象", "报名对象"}


def _extract_eligibility(lines: list[str]) -> tuple[dict[str, Any], str]:
    # 分两轮找：先找带明确标签的行，找不到再退回宽松线索。
    # 不能一轮找完——_find_line 返回的是第一个命中行，而开场白往往排在
    # 真正的「参与资格：」那一行前面，一轮下来永远是套话赢。
    line = _find_line(lines, ELIGIBILITY_LABELS) or _find_line(lines, ELIGIBILITY_LOOSE)
    if line in _ELIGIBILITY_HEADING_ONLY:
        line = _find_section(lines, [line])
    elif len(line) > MAX_ELIGIBILITY_TEXT:
        # 正文被压平成一整行时，这里会捞到整段。切回含关键词的那一句；
        # 切不出合理长度就当没抽到，交给人工核验，而不是把整段当成资格说明。
        line = _clip_to_keyword_sentence(line, ELIGIBILITY_KEYWORDS, limit=MAX_ELIGIBILITY_TEXT)
    explicit_no_restriction = any(term in line for term in ["面向全校", "全校学生", "全校师生"])
    departments = [dep for dep in KNOWN_DEPARTMENTS if dep in line]
    grades = [term for term in GRADE_TERMS if term in line]
    identities = [term for term in IDENTITY_TERMS if term in line]
    return {
        "departments": departments,
        "grades": grades,
        "majors": [],
        "identities": identities,
        "explicit_no_restriction": explicit_no_restriction and not departments,
        "restriction_text": line,
    }, line


def _extract_reimbursement(lines: list[str]) -> tuple[dict[str, Any], str]:
    line = _find_line(lines, ["报销", "补贴", "经费", "交通费", "食宿"])
    if not line:
        return {"has_reimbursement": None, "ratio": None, "amount": None, "text": ""}, ""
    negative = any(term in line for term in ["不报销", "无报销", "费用自理", "不提供补贴"])
    ratio_match = RATIO_RE.search(line)
    amount_match = AMOUNT_RE.search(line)
    return {
        "has_reimbursement": not negative,
        "ratio": int(ratio_match.group(1)) / 100 if ratio_match else (1.0 if "全额报销" in line else None),
        "amount": float(amount_match.group(1)) if amount_match else None,
        "text": line,
    }, line


def _extract_signup_method(text: str, lines: list[str]) -> str:
    signup_line = _find_line(lines, ["报名方式", "报名链接", "报名问卷", "扫码报名", "申请方式"])
    if "二维码" in signup_line or "扫码" in signup_line or "二维码" in text:
        return "原文二维码报名"
    url_match = re.search(r"https?://[^\s，。]+", signup_line or text)
    if url_match:
        return url_match.group(0).rstrip(")]）")
    email_match = re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", signup_line or text)
    if email_match:
        return f"发送材料至 {email_match.group(0)}"
    return signup_line[:160]


def _extract_materials(text: str) -> list[str]:
    mapping = {
        "报名表": ["报名表", "申请表"],
        "个人简历": ["简历", "个人履历"],
        "个人陈述": ["个人陈述", "申请理由"],
        "成绩单": ["成绩单"],
        "作品集": ["作品集"],
        "面试": ["面试", "答辩"],
    }
    return [name for name, keywords in mapping.items() if any(keyword in text for keyword in keywords)]


# 招募通知里常见的字段标签。用于把"标签独占一行、值在下一行"的排版合并起来。
_NOTICE_FIELD_LABELS = frozenset({
    "报名截止", "截止时间", "报名截至", "申报截止", "报名时间", "截止日期",
    "实践时间", "活动时间", "项目时间", "实践日期", "行程时间",
    "实践地点", "活动地点", "项目地点", "调研地点", "地点",
    "招募对象", "面向对象", "报名对象", "参与对象", "招募要求", "报名要求",
    "报名条件", "申请条件", "招募人数",
    "报名方式", "报名链接", "申请方式", "报名流程",
    "经费", "经费说明", "费用说明", "报销", "报销说明", "补贴",
    "主办单位", "组织单位", "承办单位", "项目方", "发起单位",
    "联系人", "联系方式", "咨询方式",
})

# 序号前缀：「一、」「（二）」「3.」等。必须带分隔符，否则会把「2026年…」的年份吃掉。
_NOTICE_ORDINAL = re.compile(r"^\s*(?:[（(]?[一二三四五六七八九十]{1,3}[)）]?[、.．]|\d{1,2}[、.．)])\s*")


def _normalize_notice_lines(lines: list[str]) -> list[str]:
    """把公众号排版整理成「标签：值」一行一条，便于后续按行抽取。

    两种排版此前会整片抽错：
    1. 标签独占一行、值在下一行 —— 这是最标准的通知写法：
       「五、报名截止」换行「2026年9月1日18:00」。抽取器只在同一行找值，
       结果截止日期抽不到，「二、实践地点」这行小标题反而被当成了地点。
    2. 要素挤在一行、用 | 或 ｜ 分隔 —— 微信里很常见，整行会被吞进某个字段。

    只重写"整行就是一个标签"的行，其余原样保留，尽量缩小影响面。
    """
    expanded: list[str] = []
    for line in lines:
        parts = [part.strip() for part in re.split(r"[|｜丨]", line) if part.strip()]
        expanded.extend(parts or [line])

    merged: list[str] = []
    index = 0
    while index < len(expanded):
        current = expanded[index]
        bare = _NOTICE_ORDINAL.sub("", current).strip().rstrip("：: ")
        if bare in _NOTICE_FIELD_LABELS and index + 1 < len(expanded):
            merged.append(f"{bare}：{expanded[index + 1].strip()}")
            index += 2
            continue
        merged.append(current)
        index += 1
    return merged


def extract_project(raw_text: str, metadata: dict[str, Any] | None = None, *, today: date | None = None) -> dict[str, Any]:
    """Extract a conservative project card from copied text or OCR text."""
    metadata = metadata or {}
    today = today or date.today()
    cleaned = raw_text.replace("\r\n", "\n").replace("\r", "\n").strip()
    lines = [re.sub(r"\s+", " ", line).strip() for line in cleaned.split("\n") if line.strip()]
    lines = _normalize_notice_lines(lines)
    input_type = metadata.get("input_type", "copied_text")
    title = _extract_title(lines, metadata.get("title", ""))
    source_account = metadata.get("source_account", "用户投稿").strip() or "用户投稿"
    source_url = normalize_url(metadata.get("source_url", ""))

    source_date = parse_iso_date(metadata.get("publish_date")) or today
    deadline, practice_start, practice_end, date_lines = _extract_dates(lines, source_date)
    schedule_segments = _extract_schedule_segments(cleaned, source_date.year)
    if schedule_segments:
        practice_start = min(segment["start"] for segment in schedule_segments)
        practice_end = max(segment["end"] for segment in schedule_segments)
    location, location_line = _extract_location(lines, cleaned)
    eligibility, eligibility_line = _extract_eligibility(lines)
    reimbursement, reimbursement_line = _extract_reimbursement(lines)
    organizer_line = _find_line(lines, ["主办单位", "组织单位", "项目方", "发起单位"])
    organizer = re.sub(r"^.*?[：:]", "", organizer_line).strip() if organizer_line else ""
    signup_method = _extract_signup_method(cleaned, lines)

    uncertain_fields: list[str] = []
    if not deadline:
        uncertain_fields.append("signup_deadline")
    if not practice_start:
        uncertain_fields.append("practice_dates")
    if not eligibility_line:
        uncertain_fields.append("eligibility")
    if reimbursement["has_reimbursement"] is None:
        uncertain_fields.append("reimbursement")
    if not signup_method:
        uncertain_fields.append("signup_method")
    if not source_url:
        uncertain_fields.append("source_url")

    demo_data = "演示" in cleaned or "演示" in title or urlparse(source_url).hostname == "example.invalid"
    risk_notes: list[str] = []
    if input_type == "ocr_text":
        risk_notes.append("内容来自OCR文本，请复核日期、金额和联系方式")
    if not source_url:
        risk_notes.append("缺少原文链接，发布前需补充来源")
    if demo_data:
        risk_notes.append("演示数据，不可作为真实报名依据")

    critical_present = 5 - sum(field in uncertain_fields for field in [
        "signup_deadline", "eligibility", "reimbursement", "signup_method", "source_url"
    ])
    confidence = round(min(0.98, 0.42 + critical_present * 0.09 + min(len(cleaned), 2500) / 25000), 2)
    status = "needs_review" if any(field in uncertain_fields for field in [
        "signup_deadline", "eligibility", "reimbursement", "signup_method", "source_url"
    ]) else "published"
    if deadline and parse_iso_date(deadline) and parse_iso_date(deadline) < today:
        status = "expired"

    evidence: dict[str, Any] = {}
    evidence_pairs = {
        "signup_deadline": date_lines["signup_deadline"],
        "practice_dates": date_lines["practice_dates"],
        "location": location_line,
        "eligibility": eligibility_line,
        "reimbursement": reimbursement_line,
        "organizer": organizer_line,
    }
    for key, line in evidence_pairs.items():
        item = _evidence(line, input_type)
        if item:
            evidence[key] = item

    created_at = now_iso()
    project_id = stable_id(source_url or title, organizer, deadline or "")
    return {
        "id": project_id,
        "title": title,
        "source_account": source_account,
        "source_url": source_url,
        "publish_date": metadata.get("publish_date") or None,
        "organizer": organizer,
        "summary": "".join(lines[1:4])[:260] if len(lines) > 1 else cleaned[:260],
        "theme_tags": _extract_themes(cleaned),
        "practice_start": practice_start,
        "practice_end": practice_end,
        "schedule_segments": schedule_segments,
        "signup_deadline": deadline,
        "location": location,
        "eligibility": eligibility,
        "reimbursement": reimbursement,
        "quota": None,
        "signup_method": signup_method,
        "contact": _find_line(lines, ["联系人", "联系方式", "咨询方式"])[:160],
        "required_materials": _extract_materials(cleaned),
        "risk_notes": risk_notes,
        "uncertain_fields": uncertain_fields,
        "field_evidence": evidence,
        "confidence": confidence,
        "status": status,
        "demo_data": demo_data,
        "created_at": created_at,
        "updated_at": created_at,
    }


def refresh_status(project: dict[str, Any], *, today: date | None = None) -> dict[str, Any]:
    today = today or date.today()
    result = deepcopy(project)
    deadline = parse_iso_date(result.get("signup_deadline"))
    if result.get("status") not in {"rejected"} and deadline and deadline < today:
        result["status"] = "expired"
    result["updated_at"] = now_iso()
    return result


def validate_project(project: dict[str, Any]) -> list[str]:
    errors = []
    if not project.get("title"):
        errors.append("title is required")
    if project.get("status") not in {"draft", "needs_review", "published", "expired", "rejected"}:
        errors.append("invalid status")
    for field in ("signup_deadline", "practice_start", "practice_end"):
        if project.get(field) and not parse_iso_date(project[field]):
            errors.append(f"{field} must be YYYY-MM-DD")
    confidence = project.get("confidence", 0)
    if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        errors.append("confidence must be between 0 and 1")
    return errors


def _as_set(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        return {item.strip() for item in re.split(r"[,，、]", value) if item.strip()}
    return {str(item).strip() for item in value if str(item).strip()}


def _date_overlap(start_a: date | None, end_a: date | None, start_b: date | None, end_b: date | None) -> bool:
    if not all([start_a, end_a, start_b, end_b]):
        return True
    return start_a <= end_b and start_b <= end_a


@dataclass
class MatchResult:
    project: dict[str, Any]
    score: float
    reasons: list[str]
    warnings: list[str]
    excluded_reasons: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "project": self.project,
            "score": round(self.score, 1),
            "reasons": self.reasons,
            "warnings": self.warnings,
            "excluded_reasons": self.excluded_reasons,
        }


def score_project(project: dict[str, Any], profile: dict[str, Any], *, today: date | None = None) -> MatchResult:
    today = today or date.today()
    excluded: list[str] = []
    warnings = list(project.get("risk_notes", []))
    reasons: list[str] = []

    if project.get("status") == "expired" or (parse_iso_date(project.get("signup_deadline")) and parse_iso_date(project.get("signup_deadline")) < today):
        excluded.append("报名已截止")

    eligibility = project.get("eligibility", {})
    department = str(profile.get("department", "")).strip()
    allowed_departments = _as_set(eligibility.get("departments"))
    if allowed_departments and department and department not in allowed_departments and not eligibility.get("explicit_no_restriction"):
        excluded.append(f"项目限定院系：{'、'.join(sorted(allowed_departments))}")

    grade = str(profile.get("grade", "")).strip()
    allowed_grades = _as_set(eligibility.get("grades"))
    if allowed_grades and grade and grade not in allowed_grades:
        broad_ok = ("本科生" in allowed_grades and grade.startswith("大")) or ("研究生" in allowed_grades and grade in {"硕士生", "博士生"})
        if not broad_ok:
            excluded.append(f"项目限定年级/培养层次：{'、'.join(sorted(allowed_grades))}")

    p_start, p_end = parse_iso_date(project.get("practice_start")), parse_iso_date(project.get("practice_end"))
    u_start, u_end = parse_iso_date(profile.get("available_start")), parse_iso_date(profile.get("available_end"))
    if p_start and p_end and u_start and u_end and not _date_overlap(p_start, p_end, u_start, u_end):
        excluded.append("实践日期与可用时间冲突")

    score = 0.0
    if p_start and p_end and u_start and u_end:
        if _date_overlap(p_start, p_end, u_start, u_end):
            score += 25
            reasons.append("实践时间与你的可用时间相符")
    else:
        score += 10
        warnings.append("实践日期信息不完整，需要确认")

    preferred_themes = _as_set(profile.get("themes"))
    project_themes = _as_set(project.get("theme_tags"))
    theme_overlap = preferred_themes & project_themes
    if theme_overlap:
        score += 25
        reasons.append(f"匹配主题偏好：{'、'.join(sorted(theme_overlap))}")
    elif not preferred_themes:
        score += 12

    preferred_locations = _as_set(profile.get("preferred_locations"))
    location = project.get("location", {})
    location_text = " ".join(str(location.get(k, "")) for k in ["province", "city", "detail", "mode"])
    matched_locations = {place for place in preferred_locations if place in location_text}
    if matched_locations:
        score += 15
        reasons.append(f"匹配地点偏好：{'、'.join(sorted(matched_locations))}")
    elif not preferred_locations:
        score += 8

    reimbursement_pref = profile.get("reimbursement_preference", "not_important")
    has_reimbursement = project.get("reimbursement", {}).get("has_reimbursement")
    if reimbursement_pref in {"required", "preferred"} and has_reimbursement is True:
        score += 10
        reasons.append("项目明确提供报销或补贴")
    elif reimbursement_pref == "required" and has_reimbursement is False:
        excluded.append("项目明确不提供报销，而你将报销设为必须")
    elif reimbursement_pref == "required" and has_reimbursement is None:
        warnings.append("经费信息未知，不满足“必须报销”的确认条件")
    else:
        score += 5

    reliability = SOURCE_RELIABILITY.get(project.get("source_account", ""), 0.72)
    score += reliability * 10
    confidence = float(project.get("confidence", 0) or 0)
    score += confidence * 15
    if confidence >= 0.85:
        reasons.append("项目信息较完整，关键字段有较高置信度")

    uncertain = project.get("uncertain_fields", [])
    score -= min(15, len(uncertain) * 3)
    if uncertain:
        warnings.append(f"待确认字段：{'、'.join(FIELD_LABELS.get(name, name) for name in uncertain)}")
    if project.get("demo_data") and not any("演示数据" in warning for warning in warnings):
        warnings.append("这是演示数据，不可作为真实报名依据")

    return MatchResult(project, max(0.0, min(100.0, score)), reasons, warnings, excluded)


def recommend_projects(projects: list[dict[str, Any]], profile: dict[str, Any], *, today: date | None = None) -> dict[str, Any]:
    eligible: list[dict[str, Any]] = []
    potential: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for project in projects:
        result = score_project(project, profile, today=today)
        item = result.to_dict()
        if result.excluded_reasons:
            excluded.append(item)
        elif project.get("status") == "published":
            eligible.append(item)
        else:
            potential.append(item)
    eligible.sort(key=lambda item: item["score"], reverse=True)
    potential.sort(key=lambda item: item["score"], reverse=True)
    excluded.sort(key=lambda item: item["score"], reverse=True)
    return {
        "eligible": eligible,
        "potential": potential,
        "excluded": excluded,
        "policy": "正式推荐仅包含 published 项目；needs_review 项目单列为潜在机会。",
    }


def project_snapshot(project: dict[str, Any]) -> str:
    location = project.get("location", {})
    reimbursement = project.get("reimbursement", {})
    return (
        f"项目：{project.get('title', '')}\n"
        f"主办方：{project.get('organizer') or '待确认'}\n"
        f"实践时间：{project.get('practice_start') or '待确认'} 至 {project.get('practice_end') or '待确认'}\n"
        f"地点：{location.get('detail') or location.get('province') or location.get('mode') or '待确认'}\n"
        f"报名截止：{project.get('signup_deadline') or '待确认'}\n"
        f"经费：{reimbursement.get('text') or '待确认'}\n"
        f"报名方式：{project.get('signup_method') or '待确认'}"
    )


def _context_text(context: dict[str, Any], key: str, fallback: str) -> str:
    value = str(context.get(key) or "").strip()
    return value or fallback


def _compact_text(value: Any) -> str:
    """Make a student supplied form fragment readable without inventing facts.

    This deliberately performs only conservative spelling/punctuation cleanup.  It
    is not an LLM rewrite: a form answer must remain traceable to what the student
    actually supplied.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    replacements = {
        "志愿者者": "志愿者", "实践践": "实践", "调研研": "调研", "沟沟通": "沟通",
        "负责责": "负责", "资料料": "资料", "组织织": "组织", "联络络": "联络",
        "  ": " ", "；": "，", ";": "，", "。 。": "。",
    }
    for before, after in replacements.items():
        text = text.replace(before, after)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"[，、]{2,}", "、", text)
    return text.strip(" ，、；;")


def _theme_work_style(project: dict[str, Any]) -> tuple[str, str, str]:
    """Return concise, project-aware wording for application and outreach work."""
    corpus = " ".join([
        str(project.get("title") or ""), str(project.get("summary") or ""),
        " ".join(str(item) for item in project.get("theme_tags", []) or []),
    ])
    if any(term in corpus for term in ("教育", "儿童", "学校", "教师", "科普")):
        return ("教育服务与学习支持", "尊重学校安排和服务对象体验", "课堂协助、需求访谈与资料整理")
    if any(term in corpus for term in ("乡村", "农业", "村", "振兴")):
        return ("乡村发展与基层实践", "尊重当地节奏，以长期需求为导向", "产业/社区走访、观察记录与需求梳理")
    if any(term in corpus for term in ("生态", "环保", "环境", "低碳")):
        return ("生态保护与可持续发展", "遵守保护地和现场安全要求", "现场观察、数据记录与公众沟通")
    if any(term in corpus for term in ("医疗", "健康", "卫生", "养老", "心理")):
        return ("健康服务与公共卫生", "保护服务对象隐私，遵守专业边界", "资料整理、服务流程观察与健康宣教协助")
    if any(term in corpus for term in ("文化", "非遗", "文物", "文旅")):
        return ("文化传承与地方发展", "尊重地方文化与受访者表达", "资料整理、口述访谈与传播素材整理")
    if any(term in corpus for term in ("科技", "人工智能", "数字", "创新", "产业")):
        return ("科技创新与产业观察", "重视事实核验、数据边界和保密要求", "资料检索、案例梳理与访谈记录")
    return ("社会实践与公共服务", "尊重当地安排，注重事实核验", "资料检索、沟通协调、现场记录与成果整理")


def _location_area(project: dict[str, Any]) -> str:
    location = project.get("location") or {}
    detail = _compact_text(location.get("detail"))
    city = _compact_text(location.get("city"))
    province = _compact_text(location.get("province"))
    if detail:
        # “云南省大理州（线下）” reads more naturally as “大理州” in a list.
        detail = re.sub(r"（[^）]*）|\([^)]*\)", "", detail).strip()
        detail = re.sub(r"^(?:中国)?(?:[\u4e00-\u9fff]{2,4}省)", "", detail)
        return detail or city or province
    return city or province or "项目地"


def _site_id(name: str) -> str:
    return "site_" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:10]


def recommend_local_sites(project: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Create verifiable, project-aware local-site choices.

    Choices are deliberately institutions/point types that can be searched on an
    official site.  We never fabricate a contact person, phone number, address or
    claim that an organisation has agreed to receive the team.
    """
    context = context or {}
    area = _location_area(project)
    theme, principle, default_task = _theme_work_style(project)
    corpus = " ".join([
        str(project.get("title") or ""), str(project.get("summary") or ""),
        " ".join(str(item) for item in project.get("theme_tags", []) or []),
        _compact_text(context.get("partner_needs")),
    ])
    rows: list[tuple[str, str, str, list[str], str]] = []

    def add(name: str, category: str, value: str, topics: list[str], audience: str) -> None:
        rows.append((name, category, value, topics, audience))

    if any(term in corpus for term in ("教育", "儿童", "学校", "教师", "科普")):
        add(f"{area}教育行政部门", "教育主管部门", "了解区域教育资源、项目对接与政策边界", ["教育资源配置", "数字工具使用", "校地协作"], "部门/事业单位")
        add(f"{area}教师发展中心或教研机构", "教研与培训机构", "核验教师培训、课程资源和落地难点", ["教师培训", "资源适配", "课程实施"], "专业机构")
        add(f"{area}中心学校或项目方推荐学校", "学校", "观察真实教学/服务场景，优先以项目方预约为准", ["学生体验", "课堂协作", "家校沟通"], "学校")
        add(f"{area}青少年活动中心/科普场馆", "公共教育场馆", "了解校外学习、科普服务与参与机制", ["公共教育", "科普传播", "活动运营"], "公共服务机构")
    elif any(term in corpus for term in ("乡村", "农业", "村", "振兴")):
        add(f"{area}农业农村主管部门", "农业农村主管部门", "了解产业链、项目政策和可对接村镇", ["产业发展", "基层治理", "人才回流"], "部门/事业单位")
        add(f"{area}村民委员会或乡镇便民服务中心", "基层治理点位", "了解村级协作、公共服务与真实需求", ["村级治理", "公共服务", "参与式发展"], "基层组织")
        add(f"{area}示范合作社/家庭农场（由主管部门推荐）", "产业主体", "观察经营链条和市场/人才约束", ["产业组织", "品牌销售", "技术采用"], "企业/合作社")
        add(f"{area}农产品加工或产业园区运营方", "产业园区/企业", "了解加工、就业和产业配套", ["产业链协同", "就业带动", "园区服务"], "企业/园区")
    elif any(term in corpus for term in ("生态", "环保", "环境", "低碳")):
        add(f"{area}生态环境主管部门", "生态环境主管部门", "核验治理重点、公开监测资料与协作边界", ["环境治理", "公众参与", "监测与评估"], "部门/事业单位")
        add(f"{area}自然保护地/湿地公园管理机构", "保护地管理机构", "了解保护规则、社区协作与入场要求", ["生态保护", "社区共管", "环境教育"], "管理机构")
        add(f"{area}环保社会组织或志愿服务队", "社会组织", "了解公众行动和一线服务经验", ["环境教育", "志愿参与", "行为改变"], "社会组织")
    elif any(term in corpus for term in ("文化", "非遗", "文物", "文旅")):
        add(f"{area}文化和旅游主管部门", "文化主管部门", "了解地方文化资源、公共数据和参访规范", ["文化保护", "文旅协同", "公共文化服务"], "部门/事业单位")
        add(f"{area}博物馆/文化馆", "公共文化场馆", "了解展陈、教育和公共传播实践", ["文化传播", "公众教育", "地方记忆"], "场馆")
        add(f"{area}非遗传承人工作室或行业协会（由主管部门推荐）", "传承与行业主体", "从从业者视角了解技艺传承与市场环境", ["技艺传承", "青年参与", "市场转化"], "个人/协会")
    elif any(term in corpus for term in ("医疗", "健康", "卫生", "养老", "心理")):
        add(f"{area}卫生健康主管部门", "卫生健康主管部门", "了解服务体系、合规边界与公开资料", ["基层健康服务", "资源可及性", "服务协同"], "部门/事业单位")
        add(f"{area}社区卫生服务中心/乡镇卫生院", "基层服务机构", "观察服务流程；不得接触未授权个人健康信息", ["服务可及性", "健康宣教", "转诊协同"], "医疗卫生机构")
        add(f"{area}养老服务中心或公益服务机构", "社会服务机构", "了解服务对象需求和协作模式", ["老龄服务", "照护支持", "志愿协作"], "社会服务机构")
    elif any(term in corpus for term in ("科技", "人工智能", "数字", "创新", "产业")):
        add(f"{area}科技创新主管部门", "科技主管部门", "了解产业政策、创新载体与对接边界", ["创新生态", "技术转化", "人才服务"], "部门/事业单位")
        add(f"{area}高新技术产业园区/孵化器运营方", "园区/孵化器", "筛选适合走访的企业并了解产业服务", ["产业协同", "创业服务", "技术应用"], "园区/企业")
        add(f"{area}与项目议题相关企业（由园区推荐）", "企业", "了解具体应用场景、实际困难和合作条件", ["应用场景", "技术采用", "行业需求"], "企业")
        add(f"{area}科普场馆或公共创新空间", "公共创新空间", "了解公众教育、展示与参与机制", ["科技传播", "公众参与", "创新教育"], "场馆")
    else:
        add(f"{area}相关行业主管部门", "主管部门", "核验当地资源、公开资料与对接边界", ["项目背景", "资源协同", "政策执行"], "部门/事业单位")
        add(f"{area}公共服务中心或社区机构", "公共服务机构", "了解一线服务对象和实际需求", ["公共服务", "协作机制", "服务体验"], "公共服务机构")
        add(f"{area}与项目议题相关企业/基地（由主管部门推荐）", "企业/实践基地", "了解业务场景和可公开的实践经验", ["行业实践", "协作机制", "发展约束"], "企业/基地")

    # A user's verified information is more valuable than any generated lead.
    for index, raw in enumerate(re.split(r"[\n；;]+", _compact_text(context.get("local_info")))):
        name = raw.strip(" -—")
        if name:
            rows.insert(index, (name, "已提供线索", "用户填写；请确认是否为公开、可联络信息", [theme, "项目相关实践"], "以机构性质为准"))

    seen: set[str] = set()
    options: list[dict[str, Any]] = []
    for name, category, value, topics, audience in rows:
        if name in seen:
            continue
        seen.add(name)
        options.append({
            "id": _site_id(name), "name": name, "category": category, "value": value,
            "interview_topics": topics, "audience": audience,
            "contact_path": "机构官网“联系我们”/官方公众号/主管部门公开名录；联系前先核验名称、地址、开放与预约要求。",
        })
    return {"area": area, "theme": theme, "principle": principle, "default_task": default_task, "options": options[:8]}


def _selected_sites(context: dict[str, Any]) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    raw = context.get("selected_sites") or []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict) and _compact_text(item.get("name")):
                selected.append({
                    "name": _compact_text(item.get("name")),
                    "category": _compact_text(item.get("category")) or "实践点位",
                    "value": _compact_text(item.get("value")),
                    "audience": _compact_text(item.get("audience")),
                    "interview_topics": [str(topic) for topic in item.get("interview_topics", []) if str(topic).strip()],
                    "contact_path": _compact_text(item.get("contact_path")),
                })
    return selected[:8]


def _project_places(project: dict[str, Any], context: dict[str, Any]) -> list[str]:
    places: list[str] = []
    for site in _selected_sites(context):
        if site["name"] not in places:
            places.append(site["name"])
    explicit = str(context.get("place") or "").strip()
    if explicit:
        places.extend(item.strip() for item in re.split(r"[\n，、;；]", explicit) if item.strip())
    place_markers = ("社区", "街道", "学校", "基地", "园区", "企业", "工厂", "大厦", "中心", "车站", "村", "镇")
    for segment in project.get("schedule_segments", []) or []:
        label = str(segment.get("label") or "").strip()
        if label and any(marker in label for marker in place_markers) and not re.search(r"20\d{2}|\d{1,2}月\d{1,2}日", label) and label not in places:
            places.append(label)
    location = project.get("location", {})
    detail = str(location.get("detail") or location.get("province") or "").strip()
    if detail and detail not in places:
        places.append(detail)
    return places[:6] or ["[待确认的具体点位]"]


def _gateway_hint(area: str) -> str:
    known = {
        "大理": "优先核验大理站与大理凤仪机场的实时班次、进城交通和末班车；不要仅凭名称判断距离。",
        "北京": "优先按酒店片区核验北京南站/北京西站、首都机场/大兴机场的实际到达时间与末班交通。",
        "上海": "优先按酒店片区核验上海虹桥站/上海站、虹桥机场/浦东机场的实际到达时间与末班交通。",
        "昆明": "优先核验昆明南站/昆明站与长水机场的实时班次、机场大巴和地铁运营时间。",
        "西宁": "优先核验西宁站与曹家堡机场的实时班次及进城交通。",
    }
    for city, hint in known.items():
        if city in area:
            return hint
    return f"请在 12306、航司/机场官方渠道和地图中查询抵达{area}的高铁/飞机及最后一段进城交通；系统不会把非实时信息写成已确定班次。"


def generate_asset(project: dict[str, Any], kind: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Generate safe, editable action drafts without inventing user facts."""
    context = context or {}
    name = _compact_text(context.get("name")) or "[姓名]"
    department = _compact_text(context.get("department")) or "[院系]"
    strengths = _context_text(context, "strengths", "尚未提供具体经历；提交前补充一项真实的学习、组织或服务经历")
    ideal_role = _context_text(context, "ideal_role", "资料整理、沟通协调、访谈记录或现场执行中与团队最匹配的一项")
    motivation = _context_text(context, "motivation", "希望在真实场景中理解项目议题，并完成明确的团队分工")
    purpose = _context_text(context, "purpose", motivation)
    contribution = _context_text(context, "contribution", "完成资料检索、现场记录、访谈协助、进度同步与成果整理等具体工作")
    goal = _context_text(context, "goal", "确认实践项目、当地资源与合作方式")
    local_info = _context_text(context, "local_info", "[尚未提供已核验的当地机构或联系人]")
    practice_gains = _context_text(context, "practice_gains", "[请在实践后补充：对议题的理解、方法能力、协作体验、个人反思与下一步行动]")
    theme, work_principle, default_task = _theme_work_style(project)
    local_recommendations = recommend_local_sites(project, context)
    selected_sites = _selected_sites(context)
    hotel = _compact_text(context.get("hotel"))
    departure = _compact_text(context.get("departure"))
    route_evidence = _compact_text(context.get("route_evidence"))
    snapshot = project_snapshot(project)
    title = project.get("title", "该项目")
    warnings = []
    if project.get("uncertain_fields"):
        warnings.append(f"生成前仍需核实：{'、'.join(project['uncertain_fields'])}")
    if project.get("demo_data"):
        warnings.append("当前项目为演示数据，请替换为真实项目后再对外使用")

    if kind == "application":
        content = f"""# {title}｜报名表填写建议

> 按表格栏目输出的简洁填写版；只保留用户已给出的真实信息。提交前请逐项核对。

| 报名表栏目 | 可直接填写内容 |
|---|---|
| 姓名/院系 | {name} / {department} |
| 相关经历与优势 | {_compact_text(strengths)}；具备{default_task}所需的基础能力。 |
| 意向岗位 | {_compact_text(ideal_role)}；可根据团队安排补位，优先完成可交付的具体任务。 |
| 报名目的 | {_compact_text(purpose)}；关注{theme}，{work_principle}。 |
| 我能做什么 | {_compact_text(contribution)}；行前完成资料检索与问题梳理，现场做好记录、授权与进度同步，后期参与证据整理。 |
| 与目的地/对象相关的承诺 | 面向{_location_area(project)}的实践安排，{work_principle}；不将未经核实的信息写入结论。 |

## 表格有“其他说明”时可补充
- 工作方式：按预约与安全要求推进；及时反馈进度；尊重当地合作方与服务对象。
- 自检：经历、岗位、目的、可承担任务均来自本人真实情况；删除空泛的“热爱、吃苦、全能”等表述。

---
项目核对信息：
{snapshot}
"""
    elif kind == "outreach":
        options = selected_sites or local_recommendations["options"]
        choice_rows = "".join(
            f"| {site['name']} | {site['category']} | {site['value'] or '根据项目目标确认可合作内容'} | {site['contact_path'] or '官网/官方公众号/公开名录'} |\n"
            for site in options
        )
        letter_blocks = "\n\n".join(
            f"### 致 {site['name']}（{site['audience'] or site['category']}）\n"
            f"**主题：** 清华大学“{title}”实践团队关于{theme}调研/参访的沟通请求\n\n"
            f"尊敬的负责人：\n\n"
            f"您好！我们是清华大学“{title}”实践团队，拟在{_location_area(project)}围绕{theme}开展学习调研。"
            f"了解到贵单位可能与“{site['value'] or default_task}”相关，现希望在不影响正常工作的前提下，申请一次[30–60 分钟访谈 / 参访交流 / 公开资料交流]。\n\n"
            f"我们拟重点了解：{'、'.join(site['interview_topics']) or '与项目议题相关的实践经验'}。团队会提前提供人员名单、问题提纲和时间备选；"
            f"未经书面同意，不记录或公开非公开信息、个人信息及受限场所影像。若方便，烦请告知合适的对接窗口、预约流程与可交流时间。\n\n"
            f"此致\n敬礼！\n\n清华大学“{title}”实践团队\n联系人：{name}（{department}）\n电话/邮箱：[填入已核验联系方式]"
            for site in options[:4]
        )
        content = f"""# {title} 当地外联方案

## 一、选点与外联原则
- 项目地：{_location_area(project)}；实践议题：{theme}；本次目标：{goal}。
- 已选/推荐点位：{'、'.join(site['name'] for site in options)}。
- 措辞原则：面向政府/学校先说明调研边界、预约与合规；面向企业/基地突出“了解实践经验”，不暗示商业合作或索要内部资料；{work_principle}。

## 二、可选地点与联系核验
| 推荐地点 | 性质 | 适合的外联目标 | 联系方式获取方式 |
|---|---|---|---|
{choice_rows}

已知当地信息（仅使用已核实内容）：
{local_info}

> 不编造企业名称、邮箱、手机号、地址或传真。生成的“推荐地点”是待核验线索，不代表已建立合作关系。

## 三、可直接改写发送的外联邮件/正式沟通函
{letter_blocks}

## 四、发送前清单
1. 用官网、官方公众号或主管部门公开名录核验机构全称与预约方式；
2. 按不同机构分别发送，不群发、不催促；
3. 补齐联系人、团队人数、可选时间、指导教师/指导单位（如对方要求）；
4. 对方确认前，不把点位写入已确定行程。
"""
    elif kind == "interview":
        places = _project_places(project, context)
        selected_by_name = {site["name"]: site for site in selected_sites}
        place_blocks = "\n\n".join(
            (lambda site: f"### 点位：{place}\n"
                f"- 适配实践主题：{'、'.join(site.get('interview_topics') or [theme])}\n"
                f"- 建议访谈对象：{site.get('audience') or '负责业务、服务或协作的工作人员'}\n"
                f"- 建议时长：30–45 分钟；先说明用途并征得记录授权。\n\n"
                f"1. 围绕{'、'.join(site.get('interview_topics') or [theme])}，贵单位当前最需要解决的具体问题是什么？\n"
                f"2. 在{place}，与服务对象/合作方协作的实际流程如何？哪个环节最容易卡住？\n"
                f"3. 能否用一个可公开的案例，说明已有做法、效果和仍存在的限制？\n"
                f"4. 对学生实践团队而言，哪些观察、参访或资料获取方式既有价值又不增加贵单位负担？\n"
                f"5. 哪些公开数据、文件或其他受访者可帮助我们交叉核验今天的理解？")
            (selected_by_name.get(place, {"interview_topics": [theme], "audience": "工作人员"}))
            for place in places
        )
        content = f"""# {title} 地点适配访谈提纲

## 访谈开场
说明调研目的、预计时长、记录方式与资料用途；征得受访者对记录、录音和引用的明确同意。

## 通用问题（所有点位均可用）
1. 您与本项目/议题的关系是什么？当前最需要解决的问题是什么？
2. 哪些群体最受影响？关键参与者分别承担什么角色？
3. 可公开分享的做法、效果和限制分别是什么？
4. 如果只能优先改进一件事，您会选择什么？

## 基于已选外联地点的专属追问
{place_blocks}

## 访谈后检查
- 区分事实、个人观点和研究者解释；
- 核对专有名词、时间、数字和引用授权；
- 删除与研究问题无关的个人敏感信息。
"""
    elif kind == "itinerary":
        if not hotel:
            raise ValueError("路线任务必须填写酒店位置；请填写酒店名称或详细地址后再生成")
        if not selected_sites:
            raise ValueError("路线任务必须先在“当地外联”中勾选至少一个已核验/拟去地点")
        start = project.get("practice_start") or "D1"
        end = project.get("practice_end") or "Dn"
        places = _project_places(project, context)
        route_points = " → ".join([hotel, *places, hotel])
        route_rows = "".join(
            f"| {place} | 从{hotel}出发，观察现场、完成预约访谈或资料收集 | 地址/联系人/预约/拍摄与录音授权/实时交通 | 点位观察记录、联系人状态、证据编号 |\n"
            for place in places
        )
        transport_section = route_evidence or "尚未获得实时地图结果。请在左侧点击“查询实时交通”，或从高德/百度地图复制首选公共交通、驾车和步行时间后重新生成；系统不会猜测地铁线路、班次或航班。"
        content = f"""# {title} 行程与路线任务方案

项目计划窗口：{start} 至 {end}

## 一、酒店、点位与出行顺序
- 酒店/集合点：{hotel}
- 进城出发地：{departure or '[填写出发城市/机场/车站]'}
- 已选点位：{'、'.join(places)}
- 建议顺序：{route_points}
- 排序原则：同一片区连续安排；上午优先预约访谈，下午安排开放度更高的参访/观察；每天预留 30–45 分钟缓冲。

## 二、实时交通核验与方式选择
{transport_section}

### 城际到达便利性（不等同于实时班次）
{_gateway_hint(_location_area(project))}

### 选择规则
1. 同一片区、步行 ≤ 1 公里：优先步行；
2. 有直达公交/地铁且总耗时与打车差距不大：优先公共交通；
3. 携带设备、多人同行、首末班受限或预约时间刚性：优先合规网约车/出租车，并预留堵车时间；
4. 高铁/飞机只用于进出项目地：以 12306、航司/机场当日实时信息为准，至少准备一个备选班次。

| 路线节点 | 到达后任务 | 需要提前确认 | 当日交付 |
|---|---|---|---|
{route_rows}

| 阶段 | 主要任务 | 负责人 | 交付物 | 风险检查 |
|---|---|---|---|---|
| 行前准备 | 核验每个点位、酒店与交通；完成联系确认、访谈预约 | [成员] | 背景简报、联系人表、交通截图 | 资格、保险、交通 |
| D1 抵达与对齐 | 安全说明、项目方会议、调整问题清单 | [成员] | 当日纪要 | 录音/拍摄授权 |
| D2–D3 实地调研 | 访谈、观察、资料收集 | [成员] | 访谈记录、观察日志 | 数据备份、匿名化 |
| D4 交叉核验 | 补访、验证关键数字和矛盾信息 | [成员] | 证据矩阵 | 避免单一来源结论 |
| D5 阶段反馈 | 向项目方反馈初步发现 | [成员] | 反馈纪要 | 不提前承诺结论 |
| 返程后一周 | 编码、分析、报告初稿 | [成员] | 报告 v1 | 引用、事实核查 |

每日收尾固定完成：文件命名、双份备份、授权状态标注、次日预约确认和安全报备。
"""
    elif kind == "report":
        content = f"""# {title} 调研报告框架

## 摘要
用 300–500 字说明研究问题、方法、核心发现和建议。仅在证据充分后填写结论。

## 1. 问题提出与研究背景
- 项目所处的政策、地区或行业背景；
- 研究问题及其现实价值；
- 关键概念和研究边界。

## 2. 研究设计
- 样本与受访者选择；
- 访谈、观察、问卷或二手资料方法；
- 时间、地点与实施过程；
- 研究伦理、局限性和数据处理方式。

## 3. 主要发现
按“发现—证据—解释—反例/边界”组织每一节，不把单个受访者观点外推为总体事实。

## 4. 机制分析
分析参与主体、资源约束、协作流程与制度环境之间的关系。

## 5. 对策建议
分别给出短期可执行建议和中长期制度建议，注明实施主体、成本、前提和评估指标。

## 6. 结论与反思
回答研究问题，说明尚未解决的问题及后续研究方向。

## 7. 实践收获与个人反思
基于实践收获输入，区分“我实际学到了什么”“哪些判断被现场修正”“下一步如何改进”，不把感想写成未经证实的事实。

本次实践收获输入：
{practice_gains}

## 附录
访谈提纲、知情同意说明、样本概况、编码表和来源清单。

### 证据矩阵建议
| 编号 | 结论 | 证据来源 | 是否交叉验证 | 可否公开 |
|---|---|---|---|---|
| F01 | [待填写] | 访谈/观察/文件 | 是/否 | 是/匿名/否 |
"""
    else:
        raise ValueError("kind must be application, outreach, interview, itinerary, or report")

    return {"kind": kind, "title": title, "content": content.strip(), "warnings": warnings, "generated_at": now_iso()}


def deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


PLACEHOLDER_LOCATIONS = {"", "活动地点", "实践地点", "项目地点"}
_CARRY_FORWARD_FIELDS = ("practice_start", "practice_end", "signup_deadline", "signup_method", "organizer", "contact")


def _eligibility_richness(project: dict[str, Any]) -> int:
    """这一版的参与资格里，抽出了多少条真正能用来筛人的条件。

    「面向全校」也算一条——它是明确的"不限"，而不是没抽到。真正的零分是
    那种抽了一段话、但里面没有任何院系/年级/身份/不限信号的情况。
    """
    eligibility = project.get("eligibility") or {}
    score = (len(eligibility.get("departments", []))
             + len(eligibility.get("grades", []))
             + len(eligibility.get("identities", [])))
    if eligibility.get("explicit_no_restriction"):
        score += 1
    return score


def merge_project_versions(duplicate: dict[str, Any], project: dict[str, Any]) -> dict[str, Any]:
    """同一个项目再次被导入时，合并新旧两版，就地修改并返回 project。

    再次导入是**补充**，不是覆盖，也不是跳过。

    这条规则来自一个具体的坑：同一篇招募推送常被多个号转发，转发版往往是
    截断的；采集器重跑时也可能因为风控只拿到半截正文。如果一律"后来者覆盖"，
    一条本来字段齐全的项目会被一条只有标题和截止日期的稀疏版本清空——而且
    清空之后看不出发生过什么，只会显示成"这个项目信息不全"。

    所以：新版本有值就用新版本，新版本为空而旧版本有值就留旧的。地点还要额外
    防一手，"活动地点"这种抽取失败的占位串不能覆盖掉"云南省大理州"。
    人工核验过的 OCR 结论（image_ocr_review）也不能被自动抽取覆盖。
    """
    project["id"] = duplicate["id"]
    project["created_at"] = duplicate.get("created_at", project.get("created_at"))

    for field in _CARRY_FORWARD_FIELDS:
        if not project.get(field) and duplicate.get(field):
            project[field] = duplicate[field]

    if _eligibility_richness(project) < _eligibility_richness(duplicate):
        # 不只看"是不是空的"。招募通知的开场白里几乎都有「现面向全校招募…」，
        # 排版丢失时会被当成资格说明抽出来——非空，但只是一句套话。若拿它去
        # 覆盖上一版真正那行「参与资格：仅限计算机系大二至大四本科生」，硬过滤
        # 就会放行本来没资格的人。所以比的是抽出了多少可用的结构化条件。
        project["eligibility"] = duplicate["eligibility"]

    new_detail = str((project.get("location") or {}).get("detail") or "").strip()
    old_detail = str((duplicate.get("location") or {}).get("detail") or "").strip()
    if new_detail in PLACEHOLDER_LOCATIONS and old_detail not in PLACEHOLDER_LOCATIONS:
        project["location"] = duplicate["location"]

    if (project.get("reimbursement", {}).get("has_reimbursement") is None
            and duplicate.get("reimbursement", {}).get("has_reimbursement") is not None):
        project["reimbursement"] = duplicate["reimbursement"]

    if len(project.get("schedule_segments", [])) < len(duplicate.get("schedule_segments", [])):
        project["schedule_segments"] = duplicate["schedule_segments"]

    project["image_sources"] = list(dict.fromkeys(duplicate.get("image_sources", []) + project.get("image_sources", [])))
    if duplicate.get("image_ocr_status") == "completed" and project.get("image_ocr_status") != "completed":
        project["image_ocr_status"] = "completed"

    project["field_evidence"] = deep_merge(duplicate.get("field_evidence", {}), project.get("field_evidence", {}))
    for field, old_evidence in duplicate.get("field_evidence", {}).items():
        if (old_evidence.get("extraction_method") == "image_ocr_review"
                and project["field_evidence"].get(field, {}).get("extraction_method") != "image_ocr_review"):
            project["field_evidence"][field] = old_evidence

    project["risk_notes"] = list(dict.fromkeys(duplicate.get("risk_notes", []) + project.get("risk_notes", [])))

    # 两版的存疑字段先合并，再把这一版真的补上了的划掉。
    uncertain = set(duplicate.get("uncertain_fields", [])) | set(project.get("uncertain_fields", []))
    if project.get("practice_start") and project.get("practice_end"):
        uncertain.discard("practice_dates")
    if project.get("signup_deadline"):
        uncertain.discard("signup_deadline")
    if project.get("eligibility", {}).get("restriction_text"):
        uncertain.discard("eligibility")
    if project.get("reimbursement", {}).get("has_reimbursement") is not None:
        uncertain.discard("reimbursement")
    if project.get("signup_method"):
        uncertain.discard("signup_method")
    project["uncertain_fields"] = sorted(uncertain)
    return project


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
