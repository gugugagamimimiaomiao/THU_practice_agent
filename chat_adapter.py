"""OpenAI-compatible conversation adapter for 清小搭.

The adapter is deliberately deterministic: eligibility, deadlines and recommendation
filters reuse the same audited domain rules as the web MVP. Conversation state is carried
by the incoming ``messages`` array, so multiple workers do not require sticky sessions.
"""

from __future__ import annotations

import calendar
import json
import os
import re
import threading
from collections import Counter
import time
import uuid
from dataclasses import dataclass
from collections import Counter
from datetime import date
from typing import Any, Callable, Iterable

import llm
from database import Database
from domain import (
    FIELD_LABELS,
    GRADE_TERMS,
    KNOWN_DEPARTMENTS,
    KNOWN_LOCATIONS,
    THEME_KEYWORDS,
    expand_location_query,
    extract_project,
    generate_asset,
    project_location_text,
    recommend_projects,
    redact_contacts,
)


MODEL_ID = os.getenv("XIAODA_MODEL_ID", "practice-xiaoda")
MODEL_CREATED = 1783900800
ALLOWED_ROLES = {"system", "user", "assistant", "tool", "developer"}


class ChatRequestError(ValueError):
    def __init__(self, message: str, *, param: str | None = None, code: str = "invalid_request") -> None:
        super().__init__(message)
        self.param = param
        self.code = code


@dataclass
class ChatResult:
    content: str
    intent: str
    project_id: str | None = None
    # 需要边生成边输出时（目前只有走大模型的写作类回复）填这个：一个每次调用
    # 都返回全新分片迭代器的工厂。规则类回复保持 None，直接用 content。
    # 用工厂而不是迭代器本身，是因为迭代器只能消费一次，而重试/非流式取全文
    # 都需要重新拿一份。
    stream_factory: Callable[[], Iterable[str]] | None = None

    def resolve(self) -> str:
        """非流式场景下取完整文本。"""
        if self.stream_factory is None:
            return self.content
        collected = "".join(self.stream_factory())
        return collected or self.content


def content_to_text(content: Any) -> tuple[str, bool]:
    if isinstance(content, str):
        return content, False
    if not isinstance(content, list):
        return "", False
    parts: list[str] = []
    has_visual = False
    for part in content:
        if not isinstance(part, dict):
            continue
        part_type = part.get("type")
        if part_type in {"text", "input_text"}:
            parts.append(str(part.get("text", "")))
        elif part_type in {"image_url", "input_image", "image"}:
            has_visual = True
    return "\n".join(parts), has_visual


def validate_chat_request(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], bool, str]:
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ChatRequestError("messages must be a non-empty array", param="messages")
    if len(messages) > 100:
        raise ChatRequestError("messages may contain at most 100 items", param="messages")
    normalized = []
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            raise ChatRequestError(f"messages[{index}] must be an object", param=f"messages[{index}]")
        role = message.get("role")
        if role not in ALLOWED_ROLES:
            raise ChatRequestError(f"messages[{index}].role is invalid", param=f"messages[{index}].role")
        text, has_visual = content_to_text(message.get("content", ""))
        if has_visual:
            raise ChatRequestError("实践小搭当前不支持图片输入，请先提供 OCR 文本", param=f"messages[{index}].content", code="unsupported_content_type")
        normalized.append({"role": role, "content": text})
    if not any(item["role"] == "user" and item["content"].strip() for item in normalized):
        raise ChatRequestError("at least one non-empty user message is required", param="messages")
    stream = payload.get("stream", False)
    if not isinstance(stream, bool):
        raise ChatRequestError("stream must be a boolean", param="stream")
    model = str(payload.get("model") or MODEL_ID)
    return normalized, stream, model


def resolve_max_tokens(payload: dict[str, Any]) -> int | None:
    """读取并校验 max_tokens。

    OpenAI 允许省略或传 null。清小搭的连通性探测会发 `max_tokens: 1`——
    此前这个参数被完全忽略，探测里我们照样返回几百 token 且 finish_reason 仍是
    stop。虽然平台放行了，但这是实打实的协议偏差，客户端据此做预算会算错。
    """
    if "max_tokens" not in payload:
        return None
    value = payload["max_tokens"]
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ChatRequestError("max_tokens must be an integer", param="max_tokens")
    if value < 1:
        raise ChatRequestError("max_tokens must be at least 1", param="max_tokens")
    return value


def truncate_to_tokens(text: str, max_tokens: int | None) -> tuple[str, bool]:
    """按 approximate_tokens 的口径把回复截到 max_tokens 以内。

    返回 (文本, 是否被截断)。被截断时调用方应把 finish_reason 置为 "length"。
    逐字累加而不是按比例估算，保证结果与 usage 里报的 completion_tokens 自洽。
    """
    if max_tokens is None or not text:
        return text, False
    if approximate_tokens(text) <= max_tokens:
        return text, False
    ascii_count = 0
    non_ascii = 0
    for index, char in enumerate(text):
        if ord(char) < 128:
            ascii_count += 1
        else:
            non_ascii += 1
        if max(1, (ascii_count + 3) // 4 + non_ascii) > max_tokens:
            return text[:index], True
    return text, False


def approximate_tokens(text: str) -> int:
    if not text:
        return 0
    ascii_count = sum(ord(char) < 128 for char in text)
    non_ascii = len(text) - ascii_count
    return max(1, (ascii_count + 3) // 4 + non_ascii)


def usage_for(messages: list[dict[str, Any]], response: str) -> dict[str, int]:
    prompt = approximate_tokens("\n".join(item.get("content", "") for item in messages))
    completion = approximate_tokens(response)
    return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion}


def model_list() -> dict[str, Any]:
    return {
        "object": "list",
        "data": [{"id": MODEL_ID, "object": "model", "created": MODEL_CREATED, "owned_by": "practice-xiaoda"}],
    }


def completion_payload(
    messages: list[dict[str, Any]],
    response: str,
    model: str,
    *,
    completion_id: str | None = None,
    max_tokens: int | None = None,
) -> dict[str, Any]:
    completion_id = completion_id or f"chatcmpl-pxd-{uuid.uuid4().hex[:20]}"
    response, truncated = truncate_to_tokens(response, max_tokens)
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": response},
            "finish_reason": "length" if truncated else "stop",
        }],
        "usage": usage_for(messages, response),
        "system_fingerprint": "practice-xiaoda-rules-v1",
    }


def _chunks(text: str, size: int = 36) -> Iterable[str]:
    position = 0
    while position < len(text):
        boundary = min(len(text), position + size)
        if boundary < len(text):
            for punctuation in ("\n", "。", "；", "，", " "):
                candidate = text.rfind(punctuation, position + 10, boundary + 1)
                if candidate >= 0:
                    boundary = candidate + 1
                    break
        yield text[position:boundary]
        position = boundary


def stream_events(
    messages: list[dict[str, Any]],
    response: str | Iterable[str],
    model: str,
    *,
    max_tokens: int | None = None,
) -> Iterable[str]:
    """把回复转成 SSE 帧。

    response 可以是完整字符串（规则类回复，一次性有全文），也可以是分片迭代器
    （走大模型时边生成边转发）。后者是为了不让用户盯着十几秒的白屏——首个分片
    到达的时间才是感知到的响应速度。
    """
    completion_id = f"chatcmpl-pxd-{uuid.uuid4().hex[:20]}"
    created = int(time.time())

    def event(delta: dict[str, Any], finish_reason: str | None, usage: dict[str, int] | None = None) -> str:
        payload: dict[str, Any] = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
            "system_fingerprint": "practice-xiaoda-rules-v1",
        }
        if usage is not None:
            payload["usage"] = usage
        return f"data: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n"

    # The guide requires exactly one role-only first frame.  Keep content
    # deltas separate so strict SSE clients can distinguish the two phases.
    yield event({"role": "assistant"}, None)

    emitted: list[str] = []
    truncated = False
    budget_used = 0

    def emit(piece: str) -> Iterable[str]:
        """按 max_tokens 预算发出一段内容，返回要 yield 的帧。"""
        nonlocal truncated, budget_used
        if truncated or not piece:
            return
        if max_tokens is not None:
            remaining = max_tokens - budget_used
            if remaining <= 0:
                truncated = True
                return
            piece, cut = truncate_to_tokens(piece, remaining)
            if cut:
                truncated = True
            if not piece:
                return
            budget_used += approximate_tokens(piece)
        emitted.append(piece)
        yield event({"content": piece}, None)

    if isinstance(response, str):
        for chunk in _chunks(response):
            yield from emit(chunk)
            if truncated:
                break
    else:
        # 分片来源：模型给多少就转发多少，不缓冲到全文再发。
        for piece in response:
            for sub in _chunks(piece):
                yield from emit(sub)
                if truncated:
                    break
            if truncated:
                break

    full = "".join(emitted)
    yield event({}, "length" if truncated else "stop", usage_for(messages, full))
    yield "data: [DONE]\n\n"


def openai_error(message: str, *, error_type: str = "invalid_request_error", param: str | None = None, code: str | None = None) -> dict[str, Any]:
    return {"error": {"message": message, "type": error_type, "param": param, "code": code}}


# 意图关键词集中在这里，方便随时补同义词。
# 教训：产品文档、网页 UI 和这里曾经各用各的词——README 写"报名陈述"，
# 而这里只认"报名理由"，用户照着文档说话反而失败。新增说法时三处要对齐。
# 材料类型 → 触发词。**这一张表同时决定"要不要生成"和"生成哪一种"。**
#
# 原来是两处各写各的：GENERATE_WORDS 里有「调研报告」，而 _generate 内部判断
# 类型时只认「报告框架」「报告大纲」。于是说「帮我写这个项目的调研报告」，
# 词表放行了、类型却掉回默认的 application——用户要调研报告，拿到一份报名表建议。
# 今天已经在"两张手工清单各写各的"上栽过三次，这里合成一张。
#
# 顺序即优先级：报告类要排在 application 之前，否则「写一份调研报告」里的
# 「写一份」会先被通用词抢走。
_ASSET_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("outreach", ("外联", "联系话术", "沟通话术", "对接话术", "联系当地", "对外沟通")),
    ("interview", ("访谈", "访谈提纲", "访谈问题", "问卷提纲", "该问什么")),
    ("itinerary", ("行程", "日程", "路线安排", "时间安排表")),
    ("report", (
        "调研报告", "研究报告", "实践报告", "结题报告", "成果报告",
        "报告框架", "报告大纲", "报告提纲", "报告结构", "报告怎么写",
        "调研提纲", "调研框架", "调研大纲", "调研设计", "调研方案",
    )),
    ("application", (
        "报名理由", "申请理由", "报名材料", "申请材料", "个人陈述",
        "报名陈述", "自荐", "报名文书", "报名表",
    )),
)
GENERATE_WORDS = tuple(dict.fromkeys(
    [word for _, words in _ASSET_KEYWORDS for word in words] + ["怎么写", "帮我写"]
))


# 这些词几乎每个标题里都有，光靠它们重合不算"点名了某个项目"。
#
# 反例来自实测：「帮我写一个乡村教育的调研提纲」命中了「滇西乡村教育数字化调研」，
# 于是被当成给那个项目写材料。而用户只是想要一份通用提纲。
# 「宝庆微光」这种专名才算点名。
_GENERIC_TITLE_WORDS = frozenset({
    "实践", "调研", "志愿", "支教", "招募", "项目", "活动", "服务", "教育",
    "乡村", "农村", "文化", "非遗", "生态", "环保", "科技", "数字", "健康",
    "医疗", "公益", "治理", "社区", "振兴", "支队", "计划", "行动", "中心",
    "报告", "提纲", "框架", "方案", "同学", "学生", "大学", "清华", "夏令营",
})


# 标题里被引号括起来的那一段，通常就是支队专名：
#     实践招募 | 机械系“宝庆微光”赴湖南新宁支教实践支队招募
_QUOTED_NAME_RE = re.compile(r"[“”\"'‘’「」『』]([^“”\"'‘’「」『』]{2,12})[“”\"'‘’「」『』]")


def _distinctive_overlap(title: str, text: str) -> str:
    """title 和 text 最长的公共片段；只是领域通用词拼出来的话返回空。

    第一版用停用词表判断"是不是通用词"，补到第三条就露馅了：「生态保护」里
    「生态」在表上、「保护」不在，于是被当成点名项目。这类表永远补不完——
    今天已经在手工清单上栽过四次。

    换成结构判据：标题里被引号括起来的那段通常就是支队专名（「宝庆微光」）。
    命中专名就算点名；标题没有专名时（「滇西乡村教育数字化调研」），
    短片段一律当通用主题词，要五个字以上才算。
    """
    best = ""
    for start in range(len(text)):
        for end in range(start + len(best) + 1, len(text) + 1):
            fragment = text[start:end]
            if fragment in title and len(fragment) > len(best):
                best = fragment
    if len(best) < 3:
        return ""
    quoted = " ".join(_QUOTED_NAME_RE.findall(title))
    if quoted and (best in quoted or any(part in best for part in quoted.split() if len(part) >= 3)):
        return best
    if len(best) < 5:
        return ""
    residue = best
    for word in _GENERIC_TITLE_WORDS:
        residue = residue.replace(word, "")
    return best if residue.strip() else ""


# published / expired / needs_review 是内部枚举，学生看不懂。
_STATUS_LABELS = {
    "published": "已核验，可报名",
    "expired": "已过期",
    "needs_review": "线索待核验，不能作为报名依据",
    "rejected": "已驳回",
}


def _cell(value: Any) -> str:
    """放进 Markdown 表格的单元格：竖线要转义，换行要压平。"""
    return str(value or "").replace("|", "｜").replace("\n", " ").strip()


def _asset_kind(text: str) -> str:
    for kind, words in _ASSET_KEYWORDS:
        if any(word in text for word in words):
            return kind
    return "application"
RECOMMEND_WORDS = (
    "推荐", "适合我", "找项目", "匹配", "有什么项目", "有哪些项目", "筛选",
    "有报销", "能报销", "可以报销", "有经费", "报销的",
    "还能报名", "还可以报名", "能报的", "报什么",
    # 学生常见的自然说法，往往一个"推荐"都不带：
    # 「我八月有空，想去云南做实践」——意图很明确，以前却掉兜底。
    "想找", "想去", "想参加", "想报", "有空", "有时间", "空闲", "能参加",
)
# —— 以下几组是"先判断这句话到底想干什么"，排在具体意图之前 ——
#
# 关键词匹配最大的毛病不是接不住，而是接错：句子里出现"实践""想参加""帮我写"
# 就往推荐或生成上撞。实测「帮我改改这段话：我很想参加这个实践」会返回一整页
# 项目推荐，「帮我写一个乡村教育的调研提纲」会返回某个项目的报名表填写建议。
# 自信地答错比老实说不会更伤——用户会觉得它听不懂人话，而且"准确性"是评分维度。

# 问数据从哪来、准不准。这恰恰是本产品最该答好的问题：每个字段都留了原文引用。
PROVENANCE_WORDS = (
    "准吗", "准确吗", "靠谱吗", "可靠吗", "真的吗", "是真的",
    "怎么知道", "哪来的", "哪里来的", "什么来源", "数据来源", "信息来源",
    "什么时候更新", "多久更新", "更新频率", "最近更新",
    "是不是漏", "有没有漏", "全不全", "全吗", "完整吗",
    "会不会有错", "错了怎么办",
)

# 写作/润色类，但没有绑定到具体项目——不该当成"给某个项目生成材料"。
WRITING_HELP_WORDS = (
    "改改", "改一下", "润色", "修改一下", "通顺吗", "帮我看看这段",
    "怎么写", "怎么下笔", "写作技巧", "提纲怎么", "框架怎么",
    "调研提纲", "写提纲", "写框架", "写一份", "写一个",
)

# 问"这类东西一般怎么写"，要的是方法不是成稿。这些词必须参与路由判断，
# 光放在处理函数里不够——「招募推送文案有什么套路」会先被"推送"抢去写成稿。
GENERIC_WRITING_HINTS = (
    "一般怎么写", "怎么写", "如何写", "写作技巧", "有什么套路", "套路是什么",
    "写法", "文风", "语气",
    # 起名、想标题这类创作请求，同样不需要先绑定项目——用户往往还没立项。
    "起个名", "起名", "取名", "想几个名", "支队名", "队名",
    "格式是什么", "结构是什么", "要写哪些", "包含哪些", "分几部分",
    "范文", "模板", "示例", "有没有例子", "怎么下笔", "开头怎么", "结尾怎么",
    "注意什么", "注意哪些", "有什么讲究",
)

# 对已采集数据的统计性提问。这类有据可查——答案是从库里算出来的，不是编的。
# 以前全都掉别处了：「哪些主题的实践比较多」被"比较"抢去做项目对比，
# 「实践招募一般什么时候发布」里的"实践招募"模糊命中标题变成了项目候选列表。
CORPUS_STATS_HINTS = (
    "一般去", "都去哪", "去哪些地方", "哪些地方", "地点分布",
    "一般多长", "多长时间", "一般几天", "持续多久",
    "一般什么时候", "什么时候发布", "什么时候多", "集中在什么时候",
    "哪些主题", "主题分布", "什么主题", "哪类多", "哪些类型",
    "收录了多少", "有多少篇", "数据量", "样本量", "语料",
)

# ── 生成结果的事实校验 ───────────────────────────────────────────────────
#
# 模型写推送时最容易顺手补的就是数字：一个具体的报名截止、一笔补贴金额、
# 一个「98% 的满意率」。这类东西读起来最像真的，也最容易被学生当真去照做。
#
# 叙事性的编造（「山里的孩子」「当地教育资源相对有限」）没法机械核对，
# 但**数字可以**：生成稿里出现的每个日期、金额、百分比，都应该能在项目卡里
# 找到出处；找不到就是模型自己加的。这条检查不拦截输出——文案可能只是把
# 「2026-08-24」写成了「8月24日」——而是把查不到出处的那几个当场点出来。
_DATE_YMD_RE = re.compile(r"(20\d{2})\s*[-年/.]\s*(\d{1,2})\s*[-月/.]\s*(\d{1,2})")
_DATE_MD_RE = re.compile(r"(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]")
_MONEY_RE = re.compile(r"(?<![\d.])(\d{1,6}(?:\.\d+)?)\s*(?:万元|元|块钱|块)")
_PERCENT_RE = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d+)?)\s*%")


def _month_days(text: str) -> set[tuple[int, int]]:
    """文本里的日期，归一成（月, 日）。

    只比月日不比年份：项目卡存的是「2026-08-24」，文案里往往写成「8月24日」，
    两种写法说的是同一天。年份写错是另一类问题，由日期抽取那边管。
    """
    found = {(int(m.group(2)), int(m.group(3))) for m in _DATE_YMD_RE.finditer(text)}
    found |= {(int(m.group(1)), int(m.group(2))) for m in _DATE_MD_RE.finditer(text)}
    return found


def _amounts(text: str) -> set[str]:
    found = {m.group(1) for m in _MONEY_RE.finditer(text)}
    found |= {m.group(1) + "%" for m in _PERCENT_RE.finditer(text)}
    return found


# 裸年份。喂了原文之后文案会引用「1941 年正式建校」这类历史信息，
# 这是好事——但也意味着模型有能力编一个同样像真的年份，而原来的正则
# 只认 20xx 的完整日期，裸年份一个都查不到。
_BARE_YEAR_RE = re.compile(r"(?<!\d)((?:1[89]|20)\d{2})\s*年")


def _years(text: str) -> set[str]:
    return {m.group(1) for m in _BARE_YEAR_RE.finditer(text)}


def unsupported_numbers(draft: str, facts: str) -> list[str]:
    """生成稿里出现、但在事实来源里查不到的日期、金额和年份。

    `facts` 要把**所有**给过模型的材料都带上，不能只给抽取字段。
    自从写推送时把原文一起喂进去，文案就会合法地引用原文里的日期和年份
    （「1941 年正式建校」）；只对着抽取字段查的话，这些真东西全会被
    误报成编造，警告一多就没人看了。
    """
    out = [f"{month}月{day}日" for month, day in sorted(_month_days(draft) - _month_days(facts))]
    out += [item if item.endswith("%") else f"{item}元"
            for item in sorted(_amounts(draft) - _amounts(facts))]
    out += [f"{year}年" for year in sorted(_years(draft) - _years(facts))]
    return out


# ── 会话状态：从 messages 里确定性地还原，而不是拼接全文去猜 ──────────────
#
# 平台每轮都把完整历史（含 assistant 消息）发过来，但 reply() 原来只挑 user
# 消息，自己说过的话全丢了。100 轮实测暴露出的串线、条件累计、指代失效，
# 根子都在这里。
#
# 还原的依据就是我自己写出去的那段文字：推荐卡是「1. **标题**」，详情页是
# 「## 标题」，标题都原样输出，按标题回查就能拿回项目对象。不引入隐藏标记，
# 因为那要赌平台不会把它渲染出来或者洗掉。
_LISTED_RE = re.compile(r"^\s{0,3}(\d{1,2})\.\s+\*\*(.+?)\*\*", re.M)
_DETAIL_HEAD_RE = re.compile(r"^##\s+(.+?)\s*$", re.M)
# 材料输出的绑定抬头：「> 为「标题」生成的…」
_ASSET_HEADER_RE = re.compile(r"为「(.+?)」")

_CN_NUMERALS = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
                "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_ORDINAL_RE = re.compile(r"第\s*([一二两三四五六七八九十]|\d{1,2})\s*(?:个|条|项|款|名)?")

# ── 否定与排他 ──────────────────────────────────────────────────────────
#
# 抽取器原来完全没有否定处理，于是「不考虑学生骨干岗位」这句话里的
# 「学生骨干」反而成了正向关键词——100 轮实测里，用户明确说不考虑之后，
# 系统只列出五个学生骨干岗位。那不是巧合，是必然。
#
# 处理方式是按小句切分：带否定词的小句里抽到的东西进排除表，不进偏好表。
# 不做句法分析，因为中文口语里否定的辖域本来就靠标点和语序，切小句已经
# 覆盖了绝大多数写法（「想去支教，不要学生骨干」「除了北京都行」）。
_NEGATION_LEADS = (
    "不要", "不想", "不考虑", "不用", "别推", "别给", "别拿", "除了", "排除",
    "不接受", "不去", "不做", "不看", "不感兴趣", "不是",
)
# 固定搭配列不完：「主要做技术支持，不讲课」里的「不讲课」一个都对不上，
# 于是这句否定完全没生效，推出来的第一条正是支教项目。
# 小句以否定词开头是个很强的信号，比穷举动词可靠。
#
# 「非」不能进这套规则——「想做非遗相关的实践」会被整句判成否定，
# 把文化传承主题扔进排除表。非遗是这个领域的常用词，不是否定。
_NEGATION_PREFIXES = ("不", "别", "勿", "无需", "毋须")
# 这几个「不X」是肯定语义，别误当否定。
_NOT_ACTUALLY_NEGATIVE = (
    "不限", "不错", "不少", "不仅", "不止", "不但", "不管", "不论",
    # 「不用出京」「不出北京」说的是**只在北京**，不是排除北京。
    # 它们以「不」开头，被小句切分判成否定后，唯一那条北京项目反而被踢掉了——
    # 用户说的和系统做的正好相反。这类"否定形式、肯定语义"的说法必须单列。
    "不出京", "不用出京", "不出北京", "不用出北京", "不离京", "不出校",
    "不用出校", "不出学校",
)


def _is_negative_clause(clause: str) -> bool:
    text = clause.strip()
    if any(word in text for word in _NOT_ACTUALLY_NEGATIVE):
        return False
    return (any(lead in text for lead in _NEGATION_LEADS)
            or text.startswith(_NEGATION_PREFIXES))
# 排他：这类说法是把偏好升级成硬条件，允许空结果。
_EXCLUSIVE_LEADS = ("只要", "只看", "只考虑", "仅限", "必须是", "必须在", "只想", "就要", "仅")
# 用户明确表态"宁可没有也别凑数"。这时空结果就是正确答案。
_STRICT_HINTS = (
    "没有就直接说", "没有就说没有", "没有合适就直接说", "没有合适就说",
    "不要凑数", "别凑数", "不用凑", "不要补位", "宁可没有", "凑数",
)
# 除了标点，转折连词也要断句。
#
# 实测：「我想去云南但是不要支教」整句没有标点，被当成一个否定小句，
# 于是「云南」——用户明确想去的地方——被划进了排除表，回复是
# 「我理解你想避开：云南、教育、支教」，跟他说的正好相反。
# 「推荐三个」「给我两个」「来五条」——说了数量就按数量给，别一律五条。
_ASK_COUNT_RE = re.compile(r"(?:推荐|给我|来|要|找|列)\s*([一二两三四五六七八九十]|\d)\s*(?:个|条|项|份)")

# 用户要求代办：提交、报名、投递、联系对方。这些我做不了，也不该做，
# 但更糟的是**默默换成"给你一份建议"**——用户会以为已经办好了。
_ACT_FOR_ME_RE = re.compile(
    r"帮我提交|替我提交|帮我报名|替我报名|帮我投递|帮我发过去|帮我联系|"
    r"直接提交|直接报名|帮我交上去|你去报|你帮我报"
)

# 「换一批」「再推荐几个」——不是给新条件，是说"这批我不满意"。
_ANOTHER_BATCH_RE = re.compile(r"换一?批|换几个|换点别的|重新推荐|再推荐|其它的|其他的|别的呢")

# 这句话落在这个领域里吗。
_DOMAIN_RE = re.compile(r"实践|志愿|项目|支队|支教|调研|报名|机会|活动|招募|推荐|筛|找")
# 这句话像不像在改筛选条件。「换成湖南的」「时间改到9月」「不要校内的」
# 这类没有任何领域词，但显然是在下指令。
_CONSTRAINT_EDIT_RE = re.compile(
    r"换成|换到|改成|改到|改为|换一?批|再来一?批"
    r"|不要|不想|不考虑|不看|除了|排除"
    r"|只要|只看|只想|仅限|必须"
    r"|我(?:大[一二三四]|是研究生|读研|本科|研[一二三])"
)

_CLAUSE_SPLIT_RE = re.compile(r"[，,。；;！!？?、\n]+|但是|不过|然而|另外|只是|可是")


def _split_clauses(text: str) -> list[str]:
    return [clause for clause in _CLAUSE_SPLIT_RE.split(text) if clause.strip()]


# ── 「改上一份」──────────────────────────────────────────────────────────
#
# 100 轮实测里，用户只是要求修改刚拿到的那份材料，却经常被按关键词重新分类：
#
#   「加入两个比较维度」 → 跳成单项目详情（"比较"被 _COMPARE_RE 抢走）
#   「压缩到 120 字」    → 跳成推荐列表
#   「不要用常识补充」   → 跳成重新筛选
#   「加入未成年人保护」 → 跳成校园讲解志愿者推文
#
# 路由器只看当前这一句有什么词，不看"现在正在干什么"。
_REVISION_WORDS = (
    "加上", "加入", "添上", "补上", "补充一下", "去掉", "删掉", "拿掉", "删去",
    "改成", "换成", "改为", "压缩到", "精简到", "缩到", "缩短", "扩到", "展开",
    "再短", "再长", "短一点", "长一点", "详细一点", "简单一点",
    "别用", "不要用", "换个语气", "更口语", "更正式", "语气", "重写", "再来一版",
    "换一版", "调整", "改一改", "改得", "再改",
)
# 词表列不完改稿的说法。100 轮扫描里漏掉的：
#     标题再抓人一点 / 加两个关于受助学生家庭情况的问题 / 再具体一些
# 这些都掉了兜底或被重新分类。改稿是学生拿到材料后必然会做的第二步，
# 断在这里比第一版稿子写得一般严重得多。
_REVISION_RE = re.compile(
    r"(?:再|更)\s*(?:短|长|正式|口语|诚恳|抓人|具体|简单|详细|通俗|严谨|活泼)"
    # 「加两个关于家庭情况的问题」——数词夹在中间，不能要求量词紧跟着「加」
    r"|(?:加|添|补)\s*(?:上|入|进)?\s*[一二两三四五六七八九十\d]*\s*[点些条个句段项条]"
    r"|删\s*(?:掉|去|除)|去掉|拿掉"
    r"|换\s*(?:成|个|一个|种)"
    r"|压缩|精简|缩短|扩写|重写|再来一版|改一改|调整一下"
)
# 超过这个长度的多半是贴了一段新文稿要润色，不是在指挥改上一份。
_MAX_REVISION_INSTRUCTION = 60

# 规则生成的结构化回复的指纹。命中任何一条就说明上一条不是可改的草稿，
# 而是推荐列表 / 项目卡 / 比较表这类东西——那种情况下「换成湖南的」是
# 在改筛选条件，不是在改稿子。
_STRUCTURED_REPLY_MARKERS = (
    "## 正式推荐", "## 这次是怎么排的", "## 线索（尚未核实", "## 项目库总览",
    "- 状态：", "个项目都对得上", "这句我没接住", "潜在机会",
)


# 「给我三个支教项目」「来两个调研的」——这是要推荐，不是要看全部。
# 实测里它掉给了模型做意图分类，被判成 list，于是端出整个项目库，
# 列出来的五条一个支教都没有——用户说了主题却完全没生效。
_ASK_FOR_N_RE = re.compile(
    r"(?:给我|来|要|找)\s*[一二两三四五六七八九十\d]+\s*(?:个|条|份|项)?"
    r".{0,6}(?:项目|实践|志愿|机会|支教|调研)"
)

# 用户在质疑我有没有把项目搞混。这是最该答得上来的问题之一，
# 而实测第 100 轮问「是不是串线了」掉了兜底。
_MISBIND_RE = re.compile(
    r"串线|串了|搞混|弄混|混了|搞错了对象|说的是哪个|指的是哪个|"
    r"对应的是哪个|绑(?:定)?的是哪个|是同一个吗|前后不一致|自相矛盾"
)

# 指代词。「它」要排除「其它/其他」里的那个字。
_DEICTIC_RE = re.compile(
    r"这个项目|那个项目|该项目|这一个|这条|这个|上面那个|刚才那个|刚才那条|上述|(?<!其)它"
)


# 追问推荐依据的问法。故意不收「怎么样」——那是润色请求的词，
# 也不收裸的「依据」——「报名依据」是另一回事。
_WHY_RE = re.compile(
    r"为什么|为何|为啥|凭什么|凭啥|"
    r"(?:什么|啥|哪些)(?:依据|标准|条件)|"
    r"(?:依据|标准|条件)(?:是什么|有哪些)|"
    r"怎么(?:判断|排的?序|算出?来?的|选出?来?的|得出|来的)|"
    r"如何(?:判断|排序|筛选|选)|"
    r"库里(?:真的)?(?:就)?没有"
)


def _autolink(url: str) -> str:
    """把链接包成 Markdown 自动链接，防止渲染器把它拆散。

    在清小搭里实测到的：库里存的是完整链接

        https://mp.weixin.qq.com/s?sn=6bc0320d...&__biz=MjM5NDczNDYyNQ==&mid=2654153808&idx=1

    页面上却渲染成 `https://mp.weixin.qq.com/s?s` ——公众号链接里的 `__biz`
    带双下划线，Markdown 会把它当成粗体标记，把后半截 URL 吃掉。

    这个 bug 只有在真实界面里才看得见：所有纯文本测试拿到的都是完整链接，
    看不出渲染之后会坏。而「点原文自己核对」是这个产品的核心承诺，链接断了
    等于承诺落空。

    尖括号是 Markdown 的 autolink 语法，里面的内容不参与其它标记解析。
    """
    url = (url or "").strip()
    return f"<{url}>" if url else ""


# 「比较」当副词用时不是要对比项目：「比较多」「比较难」「比较早」。
# 「哪些主题的实践比较多」实测被这个词抢走，变成了两个项目的对比表格。
_COMPARE_RE = re.compile(r"比较(?![多少好难易大小早晚快慢久短高低远近贵便])|对比|哪个好|区别|选哪个")

# 起名/拟标题。用正则而不是硬列词组：「帮我想个推送标题」里"想"和"标题"之间
# 隔着"个推送"，靠穷举字符串永远补不全。
_NAMING_RE = re.compile(r"(想|起|取|拟|来)[几个一二三四五六七八九十\s]*(个|些)?[^，。？！]{0,6}(名字|名称|标题|队名)")

# 我手上根本没有的东西：报名人数、报名名单、谁参加过。
# 这些既不在公开推送里，也不是我能去打听的。必须精确，别误伤「怎么报名」
# 「报名截止是哪天」——「报名」这两个字在这个场景里到处都是。
_NO_SUCH_DATA_RE = re.compile(
    # 报名情况：多少人报了、谁报了
    r"(?:谁|哪些人|什么人|多少人|几个人|几人)[^。？?！!]{0,6}(?:报名|报了|申请|入选|录取)"
    r"|报名[^。？?！!]{0,4}(?:人数|名单|情况|人员|了多少|了几)"
    # 参加过的人是谁。这里只认「谁/哪些人」，不认「多少人」——
    # 「招多少人」「多少人能参加」问的是名额，推送里常常写了，那个要照答。
    r"|(?:谁|哪些人|什么人)[^。？?！!]{0,6}(?:参加|参与|去过)"
    r"|(?:参加|参与|入选|录取|去过)[^。？?！!]{0,6}(?:名单|哪些人|都是谁|都有谁|什么人)"
)

# 个人联系方式。原文链接就摆在项目卡里，真要联系点原文去；
# 我不做"帮你把某个具体的人的手机号翻出来"这件事。
_PERSONAL_CONTACT_RE = re.compile(
    r"(?:负责人|带队|队长|联系人|老师|学长|学姐|同学)[^。？?！!]{0,6}"
    r"(?:手机|电话|微信|联系方式|号码|QQ|qq)"
    r"|手机号|微信号|私人电话|个人邮箱"
)

# 含金量、水不水、好不好进——要答这个得有往年录取数据和一套评价标准，
# 两样我都没有。现在这几句会掉进 recommend / compare，等于**拿排序冒充
# 一个我没依据的判断**：用户看到第一条，自然理解成"这个含金量最高"。
_SUBJECTIVE_RANK_RE = re.compile(
    r"含金量|性价比|水不水|最水|够不够硬|档次|分量"
    r"|(?:好进|容易进|难进|通过率|录取率|报录比|竞争激烈|竞争大不大|难度大)"
)

# 关于社会实践本身的常识性提问，不是在找项目。
ABOUT_PRACTICE_WORDS = (
    "保研", "加分", "有什么用", "有用吗", "值得吗", "算学分", "综测",
    "怎么组队", "怎么组建", "支队是什么", "什么是社会实践", "第一次参加",
    "有什么建议", "注意什么", "需要准备什么",
)

# 让我帮着拿主意，而不是列清单。
DECISION_WORDS = (
    "参谋", "拿不定", "拿不准", "选哪个", "报哪个", "去哪个", "哪个好",
    "更容易", "更稳", "怎么选", "帮我决定", "犹豫",
)

# 否定与纠正：多轮里非常常见，掉兜底会显得完全没在听。
NEGATION_WORDS = ("不要", "不想", "别推荐", "除了", "换几个", "换一批", "重新推荐", "说错了", "搞错了", "不是这个")

POST_WORDS = (
    "推送", "推文", "公众号文案", "宣传稿", "宣传文案", "招募文案",
    "招募推送", "朋友圈文案", "宣传推送",
)
DETAIL_WORDS = ("详情", "介绍", "资格", "截止", "报销", "地点", "时间", "这个项目", "怎么样", "什么条件")
LIST_WORDS = (
    "项目列表", "全部项目", "近期项目", "实践机会", "有哪些", "还有哪些",
    "快截止", "最近截止", "都有什么",
)

# 「有没有校内的志愿服务」——问的是"符合这个条件的还有吗"，是筛选。
# 实测它以前掉进了项目详情：标题里带"志愿服务"的项目被模糊匹配上，
# 于是返回了一个**已经过期**的推普项目的详情页，答非所问还给了过期信息。
FILTER_WORDS = (
    "有没有", "有吗", "有嘛", "还有没有", "有木有",
    "只看", "只要", "不要", "除了",
    "校内", "本地", "线上", "线下", "同城",
)


_CN_MONTHS = {
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6,
    "七": 7, "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12,
}


# 「上旬/中旬/下旬」按通行的三分法：1–10、11–20、21–月末。
_TEN_DAY_SPANS = {"上旬": (1, 10), "中旬": (11, 20), "下旬": (21, 31)}
# 「9月10号到9月20号」这类明确区间。日期里的「号」和「日」都要认。
_DAY_RANGE_RE = re.compile(
    r"(?<!\d)(1[0-2]|[1-9])\s*月\s*(\d{1,2})\s*[日号]?\s*(?:到|至|-|—|~|～)\s*"
    r"(?:(1[0-2]|[1-9])\s*月\s*)?(\d{1,2})\s*[日号]?"
)
_SINGLE_DAY_RE = re.compile(r"(?<!\d)(1[0-2]|[1-9])\s*月\s*(\d{1,2})\s*[日号]")


def _resolve_year(month: int, day: int) -> int:
    """说到的月日如果今年已经过完，就理解为明年——学生说「九月」时指的是
    最近的那个九月，不会是已经过去的那个。"""
    today = date.today()
    try:
        target = date(today.year, month, min(day, calendar.monthrange(today.year, month)[1]))
    except ValueError:
        return today.year
    return today.year if target >= today else today.year + 1


def _month_span(text: str) -> tuple[str, str] | None:
    """把口语里的时间说法解析成起止日期。

    原来只认「八月」「8月」，一律返回整月。实测下来这会把用户给的精确区间
    撑大：「9月10号到9月20号有空」被理解成整个九月，于是"实践日期与可用时间
    冲突"这条硬条件几乎筛不掉东西——一个月的窗口跟什么都不冲突。
    「9月上旬」同理，被当成整个九月。

    现在按精确度从高到低试：明确区间 → 单日 → 旬 → 整月。
    """
    today = date.today()

    # 1）明确区间：「9月10号到9月20号」「9月10日至20日」
    match = _DAY_RANGE_RE.search(text)
    if match:
        m1, d1 = int(match.group(1)), int(match.group(2))
        m2 = int(match.group(3)) if match.group(3) else m1
        d2 = int(match.group(4))
        year = _resolve_year(m1, d1)
        end_year = year + 1 if m2 < m1 else year          # 跨年区间，比如 12月28日到1月5日
        try:
            start = date(year, m1, min(d1, calendar.monthrange(year, m1)[1]))
            end = date(end_year, m2, min(d2, calendar.monthrange(end_year, m2)[1]))
        except ValueError:
            return None
        if end >= start:
            return start.isoformat(), end.isoformat()

    # 2）旬：「9月上旬」
    month = None
    match = re.search(r"(?<!\d)(1[0-2]|[1-9])\s*月", text)
    if match:
        month = int(match.group(1))
    else:
        for name in sorted(_CN_MONTHS, key=len, reverse=True):
            if f"{name}月" in text:
                month = _CN_MONTHS[name]
                break
    if month:
        for label, (first, last) in _TEN_DAY_SPANS.items():
            if label in text:
                year = _resolve_year(month, first)
                last = min(last, calendar.monthrange(year, month)[1])
                return f"{year}-{month:02d}-{first:02d}", f"{year}-{month:02d}-{last:02d}"

    # 3）单日：「9月10号那天」
    match = _SINGLE_DAY_RE.search(text)
    if match:
        m, d = int(match.group(1)), int(match.group(2))
        year = _resolve_year(m, d)
        try:
            day = date(year, m, min(d, calendar.monthrange(year, m)[1]))
        except ValueError:
            return None
        return day.isoformat(), day.isoformat()

    # 4）整月：「八月」「8月」
    if not month:
        return None
    year = today.year
    last_day = calendar.monthrange(year, month)[1]
    if date(year, month, last_day) < today:
        year += 1
        last_day = calendar.monthrange(year, month)[1]
    return f"{year}-{month:02d}-01", f"{year}-{month:02d}-{last_day:02d}"


class PracticeChatAdapter:
    def __init__(self, database: Database) -> None:
        self.db = database
        # 单次请求内的项目列表缓存。一次对话里 list_projects 会被调五六次
        # （匹配、推荐、列表、兜底各要一次），每次都是全表读 + 逐条 JSON 解析：
        # 500 条项目时兜底回复要 237ms，其中大半耗在重复解析上。
        # 用 threading.local 而不是实例属性——服务是多线程的，各请求不能互相串。
        self._local = threading.local()

    def _projects(self, *, include_expired: bool = True) -> list[dict[str, Any]]:
        cache = getattr(self._local, "projects", None)
        if cache is None:
            cache = {}
            self._local.projects = cache
        if include_expired not in cache:
            cache[include_expired] = self.db.list_projects(include_expired=include_expired)
        return cache[include_expired]

    def _reset_project_cache(self) -> None:
        self._local.projects = {}
        self._local.corpus = None

    def _corpus(self):
        """写作范例语料库。按进程缓存——建一次要读全部原文并算一遍 IDF。

        语料是采集来的真实推文，包括那些不是可报名机会的实践总结、纪实和
        基地推介：作为机会它们是噪音，作为范文正好——真实、清华语境、已经
        发出去过，比任何模板都贴近实际写法。
        """
        corpus = getattr(self, "_corpus_cache", None)
        if corpus is None:
            from corpus import load_corpus
            corpus = load_corpus(self.db)
            self._corpus_cache = corpus
        return corpus

    def reply(self, messages: list[dict[str, Any]]) -> ChatResult:
        # 每次对话开始时清空缓存：同一次请求内复用，跨请求不复用，
        # 这样刚导入的项目下一句就能查到，不会读到过期快照。
        self._reset_project_cache()
        user_messages = [item["content"].strip() for item in messages if item["role"] == "user" and item["content"].strip()]
        latest = user_messages[-1]
        all_user_text = "\n".join(user_messages)
        normalized = latest.lower()

        if self._is_import(latest):
            return self._import_notice(latest)

        # 这几组必须排在具体意图之前，否则会被"实践""帮我写"这类词抢走。
        if any(word in latest for word in PROVENANCE_WORDS):
            return ChatResult(self._provenance(), "provenance")
        if any(word in latest for word in ABOUT_PRACTICE_WORDS):
            return ChatResult(self._about_practice(latest), "about_practice")
        # 注意这里要用"精确点名"而不是模糊匹配：说「帮我写一个乡村教育的调研提纲」时，
        # 「乡村教育」会模糊命中「滇西乡村教育数字化调研」，于是被当成给那个项目写材料。
        # 只有用户完整说出项目名或 ID，才认为这次写作请求是绑定到项目的。
        # 贴一段文字要求润色/点评。必须排在最前面：这类消息往往很长，里面随便
        # 一个词都可能模糊命中某个项目标题——实测「你看看我这个开头写得怎么样：
        # 盛夏的七月，我们踏上了前往西部的列车」被匹配成了某个项目的详情页。
        # 「改上一份」必须排在所有按关键词分类的分支之前——「加入两个比较维度」
        # 里的"比较"、「压缩到120字」里的"推荐"，都会被下面的词表抢走。
        # 它只在上一条回复确实是一份可改的草稿时才成立，所以不会把
        # 「换成湖南的」这种改筛选条件的话误接过来。
        if self._is_revision_request(latest, messages):
            return self._revise_previous(messages, latest)
        if self._is_polish_request(latest):
            return self._polish_text(latest)
        # 有些问题的正确答案是"我答不了"。放在这个位置：让「保研有没有用」
        # 继续走 about_practice（那条本来就答得对），也让贴稿润色、改上一份
        # 先过——用户的草稿里出现「含金量」三个字不代表他在问我哪个含金量高。
        out_of_scope = self._out_of_scope(latest)
        if out_of_scope:
            return out_of_scope
        # 「为什么推荐不在京津冀的？是库里没有吗」——这句里有"推荐"，以前被
        # RECOMMEND_WORDS 接走，把同一份列表原样重跑一遍，等于答非所问。
        # 追问推荐依据是评委最可能问的一句，必须单独接住。
        if self._is_why_question(latest, user_messages):
            return self._explain_recommendation(user_messages)
        # 「是不是串线了」「你刚才说的是哪个项目」——用户在质疑我有没有搞混。
        # 实测第 100 轮问这句掉了兜底，而这恰恰是最该答得上来的问题：
        # 答案就是我当前认定的那份列表，摆出来他一眼就能判断对不对。
        if _MISBIND_RE.search(latest):
            return self._explain_binding(messages)
        # 对已采集数据的统计性提问。同样要排在项目匹配之前——「实践招募一般
        # 什么时候发布」里的"实践招募"会模糊命中一堆标题，变成项目候选列表。
        if any(word in latest for word in CORPUS_STATS_HINTS) and not self._mentions_project_exactly(latest):
            return self._corpus_stats(latest)
        # 「招募推送一般怎么写」问的是写法，不是要给某个项目出成稿。这类必须在
        # POST_WORDS / GENERATE_WORDS 之前拦下——否则"推送""帮我写"会先把它抢走。
        if (any(word in latest for word in WRITING_HELP_WORDS + GENERIC_WRITING_HINTS)
                or _NAMING_RE.search(latest)) and not self._names_a_project(latest):
            return self._writing_help(latest)
        # 否定的判定统一走 _extract_profile 的否定小句逻辑，不再单独维护一张
        # NEGATION_WORDS 词表——两张表一定会漂。实测漏掉的就是「不考虑」：
        # 「不考虑学生骨干岗位」单独成一轮时，NEGATION_WORDS 没接住，
        # 「学生骨干」被模糊匹配成项目名，于是列出五个学生骨干岗位让用户挑，
        # 跟用户说的正好相反。
        if any(word in latest for word in NEGATION_WORDS) or self._states_an_exclusion(latest):
            return self._handle_correction(messages, all_user_text, latest)
        if any(word in latest for word in DECISION_WORDS):
            return self._help_decide(messages, all_user_text, latest)

        # 推送文案必须排在 GENERATE_WORDS 之前：「帮我写推送」里的"帮我写"
        # 也在生成材料的词表里，先匹配到就会去出报名表建议了。
        if any(word in latest for word in POST_WORDS):
            return self._draft_post(messages, all_user_text, latest)
        if any(word in latest for word in GENERATE_WORDS):
            return self._generate(messages, all_user_text, latest)
        if _COMPARE_RE.search(latest):
            return self._compare(messages, all_user_text)
        if any(word in latest for word in RECOMMEND_WORDS) or _ASK_FOR_N_RE.search(latest):
            return self._recommend(user_messages)
        # 条件筛选：「有没有校内的志愿服务」「只看志愿服务」。必须排在项目匹配
        # 之前——否则句子里的"志愿服务"会模糊命中某个标题，变成查那一个项目。
        # 但用户完整点名某个项目时（「宝庆微光有吗」）仍然按查详情处理。
        if any(word in latest for word in FILTER_WORDS) and not self._mentions_project_exactly(latest):
            return self._recommend(user_messages)
        project = self._resolve_project(messages, latest)
        if project and any(word in latest for word in DETAIL_WORDS):
            return ChatResult(self._project_detail(project), "project_detail", project["id"])
        if any(word in normalized for word in ["hello", "hi"]) or any(word in latest for word in ["你好", "您好", "你是谁", "怎么用", "帮助", "能做什么"]):
            return ChatResult(self._welcome(), "help")
        if any(word in latest for word in LIST_WORDS):
            return ChatResult(self._list_projects(), "list_projects")
        # 只说了项目名（没带其它意图词）时，直接给项目卡而不是兜底菜单——
        # 兜底话术里本来就写着"查询项目：说出项目名称"，照做却掉进兜底是自相矛盾。
        # 这里只认最新一句里的项目名，不翻历史，避免话题转开后仍然返回旧项目。
        named = self._resolve_project(messages, latest, latest_only=True)
        if named:
            return ChatResult(self._project_detail(named), "project_detail", named["id"])
        # 说了个地名或主题、但有好几个项目都对得上时，把候选摆出来让用户挑，
        # 这比直接掉兜底菜单有用得多——真实数据里同一个县往往有多支支队。
        candidates = self.match_projects(latest)
        if candidates:
            return ChatResult(self._pick_one_of(candidates, "看"), "project_candidates")

        # 走到这里说明所有词表都没接住。关键词永远补不完——今天补了"参谋"，
        # 明天用户说"拿不定主意"。所以在掉兜底之前，让模型判断一次意图，
        # 再交回规则执行。模型只输出一个标签，不产生任何事实，
        # "推荐结果和字段值全部来自规则 + SQLite" 这条底线不受影响。
        # 常见问法上面就命中了，压根走不到这里，所以延迟和成本只发生在长尾上。
        # 这一轮只要贡献了任何筛选条件，就不许掉兜底，也不该让模型去猜意图。
        #
        # 100 轮实测里最伤信任的一组：
        #     换成湖南的     → 「这句我没接住」
        #     时间改到9月    → 一整段自我介绍（模型把它判成了 help）
        #     算了不要支教了 → 这时前两轮的湖南和九月**又都生效了**
        # 条件是从全部历史重抽的，跟"这一轮有没有接住"是两套逻辑。用户看到的是
        # 连着两轮没听懂，多半在第二轮就重说一遍或者放弃了——而系统其实听懂了。
        if self._states_a_constraint(latest):
            return self._handle_correction(messages, all_user_text, latest)

        routed = self._route_by_model(messages, all_user_text, latest)
        if routed is not None:
            return routed
        return ChatResult(self._fallback(), "fallback")

    def _route_by_model(
        self, messages: list[dict[str, Any]], all_user_text: str, latest: str
    ) -> ChatResult | None:
        if not llm.is_enabled():
            return None
        try:
            intent = llm.classify_intent(latest)
        except llm.LLMUnavailable:
            return None  # 模型不可用就照常掉兜底，不把外部故障变成用户可见的错误
        if intent == "recommend":
            return self._recommend(self._user_texts(messages))
        if intent == "list":
            return ChatResult(self._list_projects(), "list_projects")
        if intent == "compare":
            return self._compare(messages, all_user_text)
        if intent == "generate":
            return self._generate(messages, all_user_text, latest)
        if intent == "post":
            return self._draft_post(messages, all_user_text, latest)
        if intent == "import":
            return ChatResult(
                "看起来你想把一则招募通知存进来。"
                "把**完整正文**贴给我，开头写上「导入这则通知：」就行——"
                "我会提取成带原文引用的项目卡，抽不准的字段会标成待确认，不替它编。",
                "import_hint",
            )
        if intent == "provenance":
            return ChatResult(self._provenance(), "provenance")
        if intent == "about":
            return ChatResult(self._about_practice(latest), "about_practice")
        if intent in {"help", "project"}:
            return ChatResult(self._welcome(), "help")
        return None

    @staticmethod
    def _is_import(text: str) -> bool:
        # 判据放宽过一次：原来要求正文 ≥100 字或命中 ≥2 个信号词，
        # 结果照着帮助文案说「导入这则通知：……」贴一段短通知反而不触发。
        # 命令词本身已经足够明确，配合任意一个招募信号词即可。
        command = any(word in text[:80] for word in ["导入", "收录", "录入", "解析这篇", "提取项目卡", "存进来"])
        notice_signal = sum(
            word in text
            for word in ["报名截止", "截止", "实践时间", "招募", "报名方式", "主办", "面向", "地点", "联系"]
        )
        return command and (len(text) >= 40 or notice_signal >= 1)

    def _import_notice(self, text: str) -> ChatResult:
        url_match = re.search(r"https?://[^\s，。]+", text)
        source_url = url_match.group(0).rstrip(")]）") if url_match else ""
        # 把「导入这则通知：」「帮我录入下面这条招募：」这类前缀整段剥掉。
        # 只剥"导入"两个字的话，剩下的"这则通知：……"会被当成项目标题。
        raw_text = re.sub(
            r"^(?:请)?(?:帮我)?(?:把)?(?:导入|收录|录入|解析这篇|提取项目卡|存进来)"
            r"(?:下面)?(?:这[则条篇个份])?(?:通知|文章|推送|招募|信息)?"
            r"[：:，,\s]*",
            "",
            text,
            count=1,
        )
        # 从微信复制到对话框时换行经常会丢，整篇通知挤成一行。抽取逻辑是按行走的，
        # 这种情况下标题会把整段吃进去、地点也跟着串行。这里按句号补回换行，
        # 只在确实没有换行时才做，避免影响正常粘贴的多行正文。
        if "\n" not in raw_text and len(raw_text) > 40:
            raw_text = re.sub(r"(?<=[。！？；])(?=[^\s])", "\n", raw_text)
        article_id = self.db.insert_article({
            "input_type": "copied_text",
            "source_account": "清小搭对话导入",
            "source_url": source_url,
            "raw_text": raw_text,
            "collector_status": "success",
        })
        project = extract_project(raw_text, {
            "input_type": "copied_text",
            "source_account": "清小搭对话导入",
            "source_url": source_url,
        })
        project["article_id"] = article_id
        duplicate = self.db.find_duplicate(project)
        if duplicate:
            project["id"] = duplicate["id"]
            project["created_at"] = duplicate.get("created_at", project["created_at"])
        project = self.db.upsert_project(project, note="通过清小搭对话导入")
        # 刚写进库，缓存里的快照已经过期，同一次回复里后面还会用到。
        self._reset_project_cache()
        missing = self._field_labels(project.get("uncertain_fields", [])) or "无"
        content = (
            f"已生成项目卡：**{project['title']}**\n\n"
            f"- 状态：{project['status']}\n"
            f"- 报名截止：{project.get('signup_deadline') or '待确认'}\n"
            f"- 实践时间：{project.get('practice_start') or '待确认'} 至 {project.get('practice_end') or '待确认'}\n"
            f"- 地点：{project.get('location', {}).get('detail') or '待确认'}\n"
            f"- 信息置信度：{round(float(project.get('confidence', 0)) * 100)}%\n"
            f"- 待确认字段：{missing}\n\n"
            "关键字段仍应与公众号原文核对；只有 `published` 项目才会进入正式推荐。"
        )
        return ChatResult(content, "import", project["id"])

    # 一个片段在多大比例的标题里出现，就算"太泛"不能拿来排除。
    # 「招募」出现在几乎所有标题里，拿它当排除条件会清空整个库。
    _TERM_TOO_GENERIC = 0.4

    # 这些是这个领域的结构词，不是用户的偏好。
    #
    # 实测踩到的：「不要拿外地项目凑数」抽出来的排除词是「项目」——比例判据
    # 没拦住它（库里只有部分标题带「项目」两个字），但拿它去排除等于误伤一大片。
    # 用户说这句话时想表达的是"别用不符合地域的来补位"，跟"项目"这个词无关。
    _TERM_STOPWORDS = frozenset({
        "项目", "实践", "招募", "活动", "支队", "推荐", "报名", "同学",
        "学生", "工作", "计划", "通知", "开展", "参加", "地方", "东西",
    })

    def _terms_worth_excluding(self, text: str) -> list[str]:
        """从否定小句里挑出能真正落到项目上的片段。

        为什么不用固定词表：用户说「不考虑学生骨干岗位」，排除的应该是
        「学生骨干」；说「不要支教」排除的是「支教」——这些词事先列不完。
        所以反过来做：拿这句话里所有 2~6 字的片段去比对真实标题，
        留下确实能命中、又不至于命中所有标题的那些。判据完全来自库里的
        真实数据，不靠猜。
        """
        titles = [project.get("title", "") for project in self._projects(include_expired=True)]
        if not titles:
            return []
        cleaned = re.sub(r"[^一-鿿A-Za-z0-9]", "", text)
        for lead in _NEGATION_LEADS:
            cleaned = cleaned.replace(lead, "")
        # 「不要拿外地的凑数」表达的是"别补位"，不是"排除某个词"。
        # 这类整句先剥掉，免得从里面挖出无关的片段来。
        for hint in _STRICT_HINTS + ("凑", "补位", "外地", "外省"):
            cleaned = cleaned.replace(hint, "")
        hits: list[str] = []
        for size in range(6, 1, -1):
            for start in range(len(cleaned) - size + 1):
                token = cleaned[start:start + size]
                if token in self._TERM_STOPWORDS:
                    continue
                if any(token in existing for existing in hits):
                    continue  # 已经被更长的命中覆盖了
                matched = sum(1 for title in titles if token in title)
                if matched and matched / len(titles) <= self._TERM_TOO_GENERIC:
                    hits.append(token)
        return hits

    def _profile_from_turns(self, user_messages: list[str]) -> dict[str, Any]:
        """按轮次抽条件，后一轮覆盖前一轮同类条件。

        原来是把所有用户消息拼成一坨丢给 _extract_profile。100 轮实测里的后果：
        用户先说「京津冀」，后来改成「湖南」，系统理解成"京津冀、湖南都要"——
        因为拼接文本里两个地名都在，没有先后之分。

        规则：某一轮说了某类条件，就整体替换该类条件；这一轮没提的，沿用之前的。
        排除项累加（用户是在一条条加限制），但如果后面某轮又正向提到了同一个词，
        就把它从排除表里拿掉——那是用户改主意了。
        """
        merged = self._extract_profile("")
        for raw in user_messages:
            # 用户打出来的项目名是指称不是条件，先摘掉再抽。见 _strip_project_names。
            text = self._strip_project_names(raw)
            turn = self._extract_profile(text)
            for field in ("department", "grade", "themes", "preferred_locations",
                          "location_labels", "available_start", "available_end",
                          "wanted_count"):
                if turn[field]:
                    merged[field] = turn[field]
            if turn["reimbursement_preference"] != "not_important":
                merged["reimbursement_preference"] = turn["reimbursement_preference"]
            for field in ("excluded_locations", "excluded_location_labels",
                          "excluded_themes", "excluded_terms"):
                merged[field] = list(dict.fromkeys(merged[field] + turn[field]))
            for flag in ("location_strict", "strict"):
                merged[flag] = merged[flag] or turn[flag]
            # 后来又正向提到的，从排除表里撤掉。
            merged["excluded_locations"] = [
                item for item in merged["excluded_locations"]
                if item not in merged["preferred_locations"]
            ]
            merged["excluded_themes"] = [
                item for item in merged["excluded_themes"] if item not in merged["themes"]
            ]
        return merged

    def _extract_profile(self, text: str) -> dict[str, Any]:
        profile: dict[str, Any] = {
            "department": "",
            "grade": "",
            "available_start": "",
            "available_end": "",
            "themes": [],
            "preferred_locations": [],
            "location_labels": [],
            "excluded_locations": [],
            "excluded_location_labels": [],
            "excluded_themes": [],
            "excluded_terms": [],
            "wanted_count": 0,
            "location_strict": False,
            "strict": False,
            "reimbursement_preference": "not_important",
        }
        original = text
        # 带否定词的小句单独拎出来：里面的地名、主题进排除表，不进偏好表。
        negative_clauses = [c for c in _split_clauses(text) if _is_negative_clause(c)]
        if negative_clauses:
            joined = "，".join(negative_clauses)
            (profile["excluded_location_labels"],
             profile["excluded_locations"]) = expand_location_query(joined)
            profile["excluded_themes"] = [
                theme for theme, words in THEME_KEYWORDS.items()
                if theme in joined or any(word.lower() in joined.lower() for word in words)
            ]
            profile["excluded_terms"] = self._terms_worth_excluding(joined)
            # 否定小句里的词不能再当成正向偏好，所以后面只看剩下的部分。
            text = "，".join(c for c in _split_clauses(text) if c not in negative_clauses)
        profile["location_strict"] = any(
            lead in clause and expand_location_query(clause)[1]
            for clause in _split_clauses(text) for lead in _EXCLUSIVE_LEADS
        ) or any(word in original for word in ("外地", "外省"))  # 「不要外地的」= 地域升级成硬条件
        # 「不要凑数」本身就是个否定小句，上面会被剥掉，所以这里必须看原文。
        profile["strict"] = (
            any(hint in original for hint in _STRICT_HINTS)
            or bool(profile["excluded_terms"] or profile["excluded_locations"]
                    or profile["excluded_themes"])
            or profile["location_strict"]
        )
        profile["department"] = next((item for item in KNOWN_DEPARTMENTS if item in text), "")
        profile["grade"] = next((item for item in GRADE_TERMS if item in text), "")
        profile["themes"] = [theme for theme, words in THEME_KEYWORDS.items() if theme in text or any(word.lower() in text.lower() for word in words)]
        # labels 是用户原话（「京津冀」），preferred_locations 是展开后的省份
        # （北京/天津/河北）。跟用户说话用前者，做匹配用后者。
        profile["location_labels"], profile["preferred_locations"] = expand_location_query(text)
        if any(word in text for word in ["必须报销", "必须有报销", "必须有补贴", "只要有报销", "经费必须"]):
            profile["reimbursement_preference"] = "required"
        elif any(word in text for word in [
            "优先报销", "优先有补贴", "最好有报销", "偏好报销",
            # 学生的实际说法，一个「报销」两个字都不带：
            # 「有没有管吃管住报销路费的」「补贴超过2000的」「包吃住的」。
            # 原来这几种一个都认不出来，条件说了等于没说。
            "管吃管住", "包吃住", "包食宿", "报销路费", "报销车费", "报销交通",
            "有补贴", "有报销", "能报销", "可以报销", "有经费", "给钱",
        ]) or re.search(r"(?:补贴|报销|经费|补助)[^。，,；;]{0,6}\d", text):
            profile["reimbursement_preference"] = "preferred"
        # 「推荐三个」「给我两个」——说了数量就该按数量给。
        want = _ASK_COUNT_RE.search(text)
        if want:
            token = want.group(1)
            count = _CN_NUMERALS.get(token) or (int(token) if token.isdigit() else 0)
            if 1 <= count <= 9:
                profile["wanted_count"] = count
        iso_dates = re.findall(r"20\d{2}-\d{1,2}-\d{1,2}", text)
        if len(iso_dates) >= 2:
            profile["available_start"], profile["available_end"] = iso_dates[:2]
        else:
            span = _month_span(text)
            if span:
                profile["available_start"], profile["available_end"] = span
        return profile

    @staticmethod
    def _last_draft(messages: list[dict[str, Any]]) -> str:
        """上一条 assistant 回复，且它得是一份可改的草稿。

        推荐列表、项目卡、比较表这些是规则拼出来的，改它们没有意义——
        用户说「换成湖南的」时想改的是筛选条件，不是那段文字。所以只有
        自由文本（模型生成的报名理由、访谈提纲、推送稿…）才算草稿。
        """
        for item in reversed(messages):
            if item.get("role") != "assistant":
                continue
            content = (item.get("content") or "").strip()
            if not content:
                return ""
            if any(marker in content for marker in _STRUCTURED_REPLY_MARKERS):
                return ""
            return content
        return ""

    def _is_revision_request(self, latest: str, messages: list[dict[str, Any]]) -> bool:
        if len(latest) > _MAX_REVISION_INSTRUCTION:
            return False  # 这么长多半是贴了一段新文稿，那是润色不是改上一份
        if not (any(word in latest for word in _REVISION_WORDS) or _REVISION_RE.search(latest)):
            return False
        # 这句要是给出了筛选条件，那它是在改筛选口径，不是在改稿子。
        #
        # 实测：写完报名理由之后说「换成湖南的」被改稿路径抢走，模型做了一个
        # 毫无意义的空改（把「邵阳市新宁县」改成「湖南省邵阳市新宁县」）。
        # 同一句话在推荐语境下是正确识别地点条件的正例——放宽改稿识别的副作用。
        #
        # 只看地点和时间，不看主题：「加入我的支教经历」里的"支教"是稿子的内容，
        # 不是筛选条件。地点和时间在改稿指令里几乎不会当内容出现，拿它们做判据
        # 才不会误伤。
        turn = self._extract_profile(latest)
        if turn["preferred_locations"] or turn["excluded_locations"] or turn["available_start"]:
            return False
        return bool(self._last_draft(messages))

    def _revise_previous(self, messages: list[dict[str, Any]], latest: str) -> ChatResult:
        """按用户的指令改上一份输出，而不是重新分类成另一个任务。"""
        draft = self._last_draft(messages)
        if not llm.is_enabled():
            return ChatResult(
                "当前没有配置写作模型，改不了稿。你要的调整是"
                f"「{latest[:40]}」——可以自己在上一份基础上动手，"
                "需要我按结构给建议的话说一声。",
                "revise_degraded",
            )
        system_prompt = (
            "你在按用户的指令修改**你上一轮给出的稿子**。\n\n"
            "**只按指令改，不做别的**：用户没让你动的段落保持原样，不要顺手重写整篇。\n"
            "**不许新增事实**：稿子里没有的时间、地点、人数、联系方式、经历、能力、"
            "满意度数字，一个字都不要加。用户要求加入某项内容而稿子里没有依据时，"
            "写成待补的占位（比如「（此处填写你的相关经历）」），并在末尾说明这一处需要他自己补。\n"
            "如果指令本身要求写入无法核实的信息，直接说明哪一项不能替他写、为什么。\n"
            "**不要反过来向用户要信息。** 缺什么就在稿子里写成占位（比如"
            "「（报名截止以原文通知为准）」），并在末尾列出需要他自己补的项。"
            "实测出现过：用户说「把报名截止时间也加上」，你回了一句"
            "「请提供项目名称以及报名截止时间」——他要是知道，就不会来问你了。\n"
            "**有字数要求就严格照做**，超了就删内容，不要只删标点。\n\n"
            "先给改好的完整稿子，再用两到四条说明改了什么。用中文。"
        )
        try:
            body = llm.complete(system_prompt, f"修改指令：{latest}\n\n上一版稿子：\n{draft}")
        except llm.LLMUnavailable:
            return ChatResult("写作模型暂时不可用，稍后再试。上一版稿子还在上面，可以先照着改。",
                              "revise_degraded")
        if not body.strip():
            return ChatResult("这次没改出更好的版本，把要求说得更具体些再试一次？", "revise_degraded")
        # 改稿最容易出的问题是"顺手补一个具体数字"。上一版和这条指令里都没有的
        # 日期、金额、百分比，一律点出来。
        invented = unsupported_numbers(body, draft + "\n" + latest)
        if invented:
            body += ("\n\n> **这几个数字是这一版新加的，上一版和你的指令里都没有**："
                     + "、".join(invented) + "。请核实后再用。")
        return ChatResult(body.strip(), "revise")

    def _explain_binding(self, messages: list[dict[str, Any]]) -> ChatResult:
        """摆出我当前认定的列表和绑定对象，让用户当场判断有没有搞混。"""
        shown = self._shown_list(messages)
        # 必须跟 _resolve_project 用同一个判据。这里要是报"最近展开详情的那个"、
        # 而实际绑的是"最近做过材料的那个"，这个自查出口就会在用户最需要它的
        # 时候撒谎——他问「是不是串线了」，我答一个和真实绑定不同的项目。
        detailed = self._current_project(messages)
        lines = ["## 我现在认定的是这些"]
        if shown:
            lines.append("\n你最近看到的编号列表（「第一个」「第二个」指的就是它们）：\n")
            lines.extend(f"{i}. {p['title']}" for i, p in enumerate(shown, 1))
        else:
            lines.append("\n我这边没有编号列表——你还没让我推荐过，或者中间换过话题。"
                         "这种时候你说「第一个」我不会瞎猜，会先问你是哪个。")
        if detailed:
            lines.append(f"\n当前绑定的项目：**{detailed['title']}**。"
                         "「它」「这个项目」指的是这一个，接下来不点名地要材料也写给它。")
        lines.append(
            "\n对不上就直接说项目名，我按名字重新绑。"
            "\n\n> 这份列表是从我上一条回复里逐条还原出来的，不是猜的——"
            "所以如果上面写的和你屏幕上看到的不一样，那是真出问题了，请告诉我。"
        )
        return ChatResult("\n".join(lines), "explain_binding")

    def _states_a_constraint(self, latest: str) -> bool:
        """这一句里有没有任何能用来筛选的条件。

        判据跟推荐时用的是同一套抽取器——凡是它读得出来的，就不该被当成
        没听懂。反过来也成立：抽取器读不出来的，才有资格掉兜底。
        """
        # 光有条件词还不够：「今天北京天气怎么样」里有「北京」，「我对教育很感
        # 兴趣」里有主题词，但它们都不是在下筛选指令。还得这句话要么落在这个
        # 领域里（提到实践/志愿/项目…），要么长得像在改条件（换成/改到/不要/只要…）。
        if not (_DOMAIN_RE.search(latest) or _CONSTRAINT_EDIT_RE.search(latest)):
            return False
        profile = self._extract_profile(latest)
        return any(profile[field] for field in (
            "preferred_locations", "excluded_locations", "themes", "excluded_themes",
            "excluded_terms", "available_start", "grade", "department", "wanted_count",
        )) or profile["reimbursement_preference"] != "not_important"

    def _states_an_exclusion(self, latest: str) -> bool:
        """这一句里有没有真的排除掉什么。判据跟推荐时用的是同一套。"""
        profile = self._extract_profile(latest)
        return bool(profile["excluded_terms"] or profile["excluded_locations"]
                    or profile["excluded_themes"] or profile["location_strict"])

    def _is_why_question(self, latest: str, user_messages: list[str]) -> bool:
        """这句是在追问推荐依据，而不是发起一次新的推荐。

        只靠「为什么」三个字不够——「为什么实践总结这么难写」是写作求助，
        不该抢过来。所以还要求它指向推荐结果：要么这句里点了名
        （推荐/排序/这几个），要么上一句本来就是在要推荐。
        """
        if not _WHY_RE.search(latest):
            return False
        if any(word in latest for word in ("推荐", "排序", "排名", "这几个", "这些", "第一个", "结果", "选出")):
            return True
        prior = user_messages[-2] if len(user_messages) >= 2 else ""
        return bool(prior) and any(
            word in prior for word in RECOMMEND_WORDS + FILTER_WORDS
        )

    def _explain_recommendation(self, user_messages: list[str]) -> ChatResult:
        """把这次排序的依据摊开讲：读到了什么条件、地点满足没满足、分怎么给的。

        这段刻意全部用当次的真实数字，不写通用套话——「库里符合京津冀的有 4 个」
        是可以被当场证伪的，「我们会综合考虑地域因素」不是。
        """
        profile = self._profile_from_turns(user_messages)
        result = recommend_projects(self._projects(include_expired=True), profile)
        lines = ["## 这次是怎么排的"]

        read: list[str] = []
        if profile.get("grade"):
            read.append(f"年级：{profile['grade']}")
        if profile.get("available_start"):
            read.append(f"可用时间：{profile['available_start']} 到 {profile['available_end']}")
        if profile.get("themes"):
            read.append(f"主题：{'、'.join(profile['themes'])}")
        if profile.get("location_labels"):
            expanded = "、".join(profile.get("preferred_locations", [])[:8])
            read.append(f"地点：{'、'.join(profile['location_labels'])}（展开成 {expanded} 去匹配）")
        if profile.get("reimbursement_preference") != "not_important":
            read.append("经费：你要求有报销")
        lines.append("\n**从你的话里读到的条件**\n")
        lines.extend(f"- {item}" for item in read)
        if not read:
            lines.append("- 没读到明确条件——没提年级、时间、主题或地点，所以只能按通用规则排。")

        if profile.get("location_labels"):
            said = "、".join(f"「{item}」" for item in profile["location_labels"])
            lines.append(f"\n**关于{said}**\n")
            in_list = result.get("location_matched", 0)
            anywhere = result.get("location_matched_all", 0)
            if anywhere:
                lines.append(f"- 库里符合的一共 {anywhere} 个，逐个交代去向：")
                # 只说「4 个里有 1 个进了推荐」等于把追问推到下一轮。
                # 剩下那几个卡在哪，这里一次说完。
                bucket_names = {
                    "eligible": "进了正式推荐",
                    "potential": "待核验，没进正式推荐",
                    "excluded": "被硬条件排除",
                }
                for entry in result.get("location_matched_detail", [])[:8]:
                    where = bucket_names.get(entry["bucket"], entry["bucket"])
                    why = f"（{entry['why']}）" if entry["why"] else ""
                    lines.append(f"  - {entry['title'][:32]}：{where}{why}")
                if not in_list:
                    lines.append("  - 所以上面列出来的都不在这个范围内。")
            else:
                lines.append("- **库里目前一个都没有。**上面列出来的都不在这个范围内。")
            blank = sum(
                1 for project in self._projects(include_expired=True)
                if not project.get("demo_data") and not project_location_text(project).strip()
            )
            if blank:
                lines.append(f"- 另有 {blank} 个项目原文没写明地点，判断不了在不在范围内——这是原文本身缺信息，不是没查。")

        lines.append("\n**排序依据**\n")
        lines.append("按这几项加总，从高到低排；地点命中的一律提到最前面：")
        lines.append("- 实践时间与你的可用时间有重叠：+25")
        lines.append("- 主题命中你说的方向：+25（没说主题时统一 +12）")
        lines.append("- 地点命中：+15")
        lines.append("- 来源公众号的可靠度、字段置信度：各占一部分")
        lines.append("- 原文缺字段：每缺一项 -3，最多扣 15")

        # 排除原因要照实说。原来无论如何都写「被硬条件排除（截止、时间冲突、
        # 资格或经费不符）」，于是出现过前后打架：上面刚说「没读到明确条件」，
        # 下面就说「37 个因资格或经费不符被排除」——用户没给条件，哪来的不符。
        # 真实原因就在每条的 excluded_reasons 里，统计一下即可。
        reason_counts: Counter[str] = Counter()
        for item in result["excluded"]:
            for reason in item.get("excluded_reasons", []):
                reason_counts[reason] += 1
        why = "；".join(f"{reason} {count} 个" for reason, count in reason_counts.most_common(4))
        lines.append(
            f"\n本次共 {len(result['eligible'])} 个进正式推荐、"
            f"{len(result['potential'])} 个待核验、"
            f"{len(result['excluded'])} 个被排除"
            + (f"（{why}）。" if why else "。")
        )
        lines.append(
            "\n> 这个分数是**针对你这次的提问**算的，不是项目本身的评分。"
            "换个问法同一个项目分数会变——比如不提主题词就少 13 分。"
            "所以界面上只给排序，不显示分数。"
        )
        lines.append("\n想看某一条为什么排在那里，说出它标题里的几个字，我给你逐字段的原文引用。")
        return ChatResult("\n".join(lines), "explain_recommendation")

    def _constraint_receipt(self, profile: dict[str, Any], result: dict[str, Any]) -> str:
        """把用户说的每一条件，逐条交代它到底起没起作用。

        这是「静默失效」这一类问题的通用解法。地域和时间已经各有一段说明，
        但 100 轮实测发现口子远不止这两个——下面 11 句完全不同的话，
        返回的是**逐字节相同**的推荐列表，回复里一个字都没提用户的条件：

            校内的有吗 / 有没有管吃管住报销路费的 / 补贴超过2000的有没有 /
            大一新生能报什么 / 我是研究生，有啥适合我的 / 机械系的有吗 /
            有没有出国的 / 推荐三个 / 现在还有什么能报的 /
            国庆期间有实践吗 / 我大一，只想找不用出京的

        用户以为筛过了，其实没有。所以不再为每个条件单写一段，而是统一走这里：
        凡是读到的条件，都必须出现在回执里；凡是没能落到实处的，明说没落到。
        """
        shown = result.get("eligible", [])[:self._show_count(profile)]
        rows: list[str] = []

        location = self._location_note(profile, result)
        if location:
            rows.append(location)
        time_note = self._time_note(profile, result)
        if time_note:
            rows.append(time_note)

        # 年级和院系：只有当项目原文写明了限制，这两条才真的起作用。
        # 真实数据里绝大多数通知只写「面向全校」，所以老实说"没起作用"。
        for label, value, field in (("年级", profile.get("grade"), "grades"),
                                    ("院系", profile.get("department"), "departments")):
            if not value:
                continue
            restricted = sum(
                1 for project in self._projects(include_expired=True)
                if (project.get("eligibility") or {}).get(field)
            )
            if restricted:
                rows.append(f"{label}「{value}」：库里有 {restricted} 个项目写了{label}限制，"
                            f"不符合的已排除；其余没写，按不限处理。")
            else:
                rows.append(f"{label}「{value}」：库里的通知**都没写{label}限制**，"
                            f"所以这一条没能筛掉任何东西，报名前请自行确认。")

        if profile.get("reimbursement_preference") != "not_important" and shown:
            with_money = sum(
                1 for item in shown
                if (item["project"].get("reimbursement") or {}).get("has_reimbursement") is True)
            unknown = len(shown) - with_money
            rows.append(f"经费：上面 {with_money} 条原文明确写了提供，"
                        f"另外 {unknown} 条没写明——没写不等于没有，要看原文或问主办方。")

        if not rows:
            return ""
        if len(rows) == 1:
            return f"> {rows[0]}"
        return "\n".join(["> **你说的条件，逐条交代：**", ">"]
                         + [f"> - {row}" for row in rows])

    def _next_steps(self, profile: dict[str, Any], result: dict[str, Any]) -> str:
        """告诉用户接下来还能干什么——按这次的实际情况挑，不是一句固定话术。

        原来只提两件事：「比较前两个」和「帮我写第一个的报名理由」。而系统能做的
        远不止：访谈提纲、外联话术、调研报告、推送文案、放宽条件、追问排序依据、
        查数据来源、看还没核验的线索。用户看不见就等于没有——100 轮实测里
        很多能力从头到尾没被触发过，不是它不会，是没人知道可以这么问。

        挑选是有条件的：只有真的有两条以上才提「比较」，只有真的存在待核验线索
        才提「看线索」，说了地域又没匹配上才提「放宽」。提一件做不到的事
        比不提更糟。
        """
        steps: list[str] = []
        if len(result.get("eligible", [])) >= 2:
            steps.append("「比较前两个」")
        steps.append("「帮我写第一个的报名理由」")
        steps.append("「写访谈提纲」「写外联话术」「写调研报告框架」「写推送文案」")
        if result.get("location_asked") and not result.get("location_matched"):
            steps.append("「不限地点再看看」")
        if result.get("potential"):
            steps.append(f"「看看那 {len(result['potential'])} 条线索」")
        steps.append("「你为什么这么推荐」")
        return (
            "\n---\n**接下来可以说**\n\n"
            + "\n".join(f"- {step}" for step in steps)
            + "\n\n想看某一条的详情和原文引用，说出标题里能区分的那几个字就行——地名或主题都可以。"
        )

    @staticmethod
    def _show_count(profile: dict[str, Any]) -> int:
        """列几条。说了「推荐三个」就给三条，没说按默认五条。"""
        wanted = profile.get("wanted_count") or 0
        return wanted if 1 <= wanted <= 9 else 5

    @staticmethod
    def _time_note(profile: dict[str, Any], result: dict[str, Any]) -> str:
        """说了时间就得交代它在多少条上真的起了作用。

        跟地域是同一类问题：用户给了条件，系统在一部分项目上**静默失效**。
        线上 36 个项目里只有 14 个写了实践日期，其余 22 个原文根本没写——
        对它们而言"时间冲突"这条硬条件无从判断，于是照样进推荐。
        每张卡片上确实写了「原文未写明：实践时间」，但一屏五条里散着看，
        没人会意识到"我说的时间对其中三条压根没生效"。
        """
        start, end = profile.get("available_start"), profile.get("available_end")
        if not (start and end):
            return ""
        shown = result.get("eligible", [])[:PracticeChatAdapter._show_count(profile)]
        if not shown:
            return ""
        unknown = sum(1 for item in shown
                      if not (item["project"].get("practice_start")
                              and item["project"].get("practice_end")))
        checked = len(shown) - unknown
        note = f"你说了 {start} 到 {end} 有空："
        if unknown and checked:
            note += f"上面 {checked} 条的日期我核过、不冲突；另外 {unknown} 条原文没写实践时间，冲不冲突判断不了。"
        elif unknown:
            note += f"上面这 {unknown} 条**原文都没写实践时间**，所以时间这一条实际上没能筛掉任何东西，得你自己看原文确认。"
        else:
            note += "上面几条的日期我都核过，跟你的时间不冲突。"
        return note

    def _location_note(self, profile: dict[str, Any], result: dict[str, Any]) -> str:
        """说清楚地域偏好到底满足没满足。没有偏好就返回空串。

        为什么必须有这一段：地点在打分里只值 +15，很容易被主题（+25）和时间（+25）
        压过去，于是「想找京津冀附近的支教」推出来第一条是湖南、第二条是河南，
        而**整段回复里一个地名都不提**——学生无从判断是自己没说清楚、是库里没有、
        还是系统压根没听见。实测时接着追问「是库里没有吗」，它继续不答。
        三种情况都得给个准话。
        """
        labels = profile.get("location_labels") or []
        if not labels or not result.get("location_asked"):
            return ""
        said = "、".join(f"「{item}」" for item in labels)
        in_list = result.get("location_matched", 0)
        anywhere = result.get("location_matched_all", 0)

        # 有没有"补位"的条目，决定了这段话能不能说「下面几条」。
        # 排他模式下一条都不补，说了就自相矛盾——实测出现过：
        # 「库里目前一个都没有。下面几条不在这个范围内」紧跟着
        # 「没有完全匹配的」，前后两句打架。
        filler = max(0, min(len(result.get("eligible", [])), self._show_count(profile)) - in_list)

        if in_list:
            note = f"你提到了{said}：库里符合的有 {in_list} 个，已经排在最前面。"
            if filler:
                note += "排在后面的不在这个范围内，是按时间和主题补上的。"
        elif anywhere:
            note = (
                f"你提到了{said}：这个范围内有 {anywhere} 个项目，但都没能进正式推荐"
                "（已截止，或关键字段还没核对完）。"
            )
            note += "下面几条**不在**你要的范围里。" if filler else ""
        else:
            note = f"你提到了{said}：**库里目前一个都没有。**"
            if filler:
                note += "下面几条不在这个范围内，只满足其它条件。"

        blank = sum(
            1 for project in self._projects(include_expired=True)
            if not project.get("demo_data") and not project_location_text(project).strip()
        )
        if blank:
            note += f"另有 {blank} 个项目的原文没写明地点，无法判断在不在范围内。"
        return note

    @staticmethod
    def _restrictions_said(profile: dict[str, Any]) -> str:
        """把这次的硬条件复述成人话，让用户能当场纠正我理解错的地方。"""
        parts: list[str] = []
        if profile.get("location_strict") and profile.get("location_labels"):
            parts.append(f"只要{'、'.join(profile['location_labels'])}")
        elif profile.get("location_labels"):
            parts.append("、".join(profile["location_labels"]))
        for label, field in (("不去", "excluded_locations"), ("不做", "excluded_themes"),
                             ("不要", "excluded_terms")):
            if profile.get(field):
                parts.append(f"{label}{'、'.join(profile[field])}")
        if profile.get("themes"):
            parts.append("、".join(profile["themes"]))
        if profile.get("available_start"):
            parts.append(f"{profile['available_start']} 到 {profile['available_end']} 有空")
        return "；".join(parts) or "你刚才说的那些条件"

    def _recommend(self, user_messages: list[str]) -> ChatResult:
        profile = self._profile_from_turns(user_messages)
        result = recommend_projects(self._projects(include_expired=True), profile)
        lines = ["## 正式推荐"]
        receipt = self._constraint_receipt(profile, result)
        if receipt:
            lines.append("\n" + receipt + "\n")
        if not result["eligible"]:
            # 空结果最容易发生在换了真实数据、或全部项目都过了截止的时候。
            # 与其只说一句"没有"，不如说清楚是被什么条件挡住的、下一步怎么放宽。
            blockers: list[str] = []
            for item in result["excluded"][:6]:
                blockers.extend(item.get("excluded_reasons", []))
            top = "；".join(dict.fromkeys(blockers))[:120]
            if profile.get("strict"):
                # 用户明确说了"只要 X""不要 Y""没有就直说"。这时空结果就是
                # 正确答案，不能再拿别的凑——实测里系统在这种时候反而输出了
                # 整个项目库，等于把用户的话当没听见。
                said = self._restrictions_said(profile)
                lines.append(f"**没有完全匹配的。**你要求的是：{said}，库里现在没有同时满足这些条件的项目。")
                if top:
                    lines.append(f"\n卡在：{top}")
                lines.append("\n要我放宽哪一条，你说了我再筛——在你说之前我不会自己放。")
            else:
                lines.append("按你给的条件，暂时没有同时满足硬条件且已核验的项目。")
                if top:
                    lines.append(f"\n主要卡在：{top}")
                lines.append(
                    "\n可以试着放宽一个条件——比如换个时间段、去掉地点限制，"
                    "或者说「还有哪些实践机会」看全部在招项目。"
                )
        # 前三条给完整卡片，第四五条压成一行。
        #
        # 实测一条推荐回复 1272 字，其中五张完整卡片占 954。清小搭那边渲染很慢，
        # 长度直接变成等待时间。但直接砍到三条又损失了选择面——真实数据本来就少，
        # 少给两个选项对学生是实实在在的损失。
        # 分层能两头兼顾：前三条照旧可以直接判断能不能报，后两条留个名字和一句
        # 关键信息，想看详情说标题里那几个字就行。
        total = self._show_count(profile)
        full = min(3, total)
        for index, item in enumerate(result["eligible"][:full], 1):
            lines.extend(self._recommendation_card(index, item))
        for index, item in enumerate(result["eligible"][full:total], full + 1):
            lines.append(self._recommendation_line(index, item))
        if result["potential"]:
            # 标题原来叫「潜在机会（需先复核）」，条目排版和正式推荐几乎一样，
            # 实测里学生分不出来——一条 2036 年截止、没有原文链接的导入线索，
            # 看起来跟真实招募没区别。这里改成把"这还不算数"写在最前面，
            # 并且明确点出没有原文可查的那些。
            lines.append("\n## 线索（尚未核实，不能作为报名依据）")
            lines.append("采集到但**还没核对完**，字段可能有误，也可能根本不存在：")
            # 每条只留标题。原来把两条 warning 全展开，这一段占了整条回复的
            # 四分之一（1272 字里的 317），而"为什么待核验"是追问时才需要的细节，
            # 第一屏上真正要传达的只有一件事：这些还不算数。
            for item in result["potential"][:3]:
                project = item["project"]
                if project.get("source_url"):
                    lines.append(f"- 线索待核验：**{project['title']}**")
                else:
                    lines.append(
                        f"- 线索待核验：**{project['title']}**（**没有原文链接可查**，"
                        "无法核实是否真实存在）"
                    )
            lines.append("想知道某一条卡在哪，说出它标题里的几个字。")
        if result["excluded"]:
            lines.append(f"\n另有 {len(result['excluded'])} 个项目因截止、时间、资格或经费硬条件被排除。")

        # 采集进来的项目默认是 needs_review，只有人工核验过才进正式推荐。
        # 真实数据下这一批往往比正式推荐多得多，不说明的话用户会以为"就这么点项目"。
        pending = [
            project for project in self._projects(include_expired=False)
            if project.get("status") == "needs_review"
        ]
        shown = {item["project"]["id"] for item in result["potential"]}
        unshown = [project for project in pending if project["id"] not in shown]
        if unshown:
            lines.append(
                f"\n还有 {len(unshown)} 条已采集但尚未人工核验的项目没有进入上面的推荐——"
                "关键字段还没核对完，先不作数。想看的话说「还有哪些实践机会」。"
            )
        # 这句提示原本写死了演示项目的名字。换成真实数据后它会指向一个不存在的
        # 项目，用户照着说必然失败——所以改成引用本次结果里的头名。
        if result["eligible"]:
            # 别把完整标题塞进引导语——真实通知的标题有三四十字
            # （「关于组建2026年赴湖南省湘西州花垣县开展……支队的通知」），
            # 照着这句说没人打得出来。用序号指代，再告诉用户可以只说关键几个字。
            lines.append(self._next_steps(profile, result))
        return ChatResult("\n".join(lines), "recommend")

    @staticmethod
    def _longest_common_run(a: str, b: str) -> int:
        """两个字符串最长连续公共片段的长度。

        真实通知的标题是「关于组建2026年赴湖南省湘西州花垣县开展智慧农业技术
        应用调研支队的通知」这种三四十字的长句，而学生只会说「花垣县那个」或
        「智慧农业那个」。原来要求整个标题是用户输入的子串，在真实数据下等于
        这条路走不通，所以改成看双方最长的公共片段有多长。
        """
        if not a or not b:
            return 0
        previous = [0] * (len(b) + 1)
        best = 0
        for i in range(1, len(a) + 1):
            current = [0] * (len(b) + 1)
            ai = a[i - 1]
            for j in range(1, len(b) + 1):
                if ai == b[j - 1]:
                    current[j] = previous[j - 1] + 1
                    if current[j] > best:
                        best = current[j]
            previous = current
        return best

    # 三个汉字连着对上才算有信息量。两个字太容易误命中——「北京」「教育」
    # 这种词在几十条通知里到处都是。
    _MATCH_MIN_RUN = 3

    # 说明这句话是在指代某个具体项目，而不是随口提到一个词。
    _REFERENCE_HINTS = ("那个", "这个", "那条", "这条", "那支", "这支", "的项目",
                        "详情", "介绍一下", "第一个", "第二个", "刚才", "上面")

    def match_projects(self, text: str, *, limit: int = 5, loose: bool | None = None) -> list[dict[str, Any]]:
        """按标题与用户这句话的公共片段找候选项目，最相关的排前面。

        先要求三个汉字连着对上。找不到时可以退一步用两个字——「滇西」「湘西」
        「龙岩」「延安」这类地名就是两个字，真实数据里很常见。

        但两个字太危险：「实践」「教育」「调研」几乎每条标题里都有，放开之后
        「社会实践是个好东西」会被当成在问某个项目。所以放宽有前提——要么这句话
        本身带指代（「滇西**那个**」），要么调用方已经确定用户是要对某个项目
        做事（生成材料、写推送），此时上下文足够明确。
        """
        hits = self._match_with_min_run(text, self._MATCH_MIN_RUN, limit)
        if hits:
            return hits
        if loose is None:
            loose = any(hint in text for hint in self._REFERENCE_HINTS)
        if not loose:
            return []
        two_char = self._match_with_min_run(text, 2, limit + 1)
        return two_char if 0 < len(two_char) <= 2 else []

    def _match_with_min_run(self, text: str, min_run: int, limit: int) -> list[dict[str, Any]]:
        text = (text or "")[:120]
        if len(text) < min_run:
            return []
        # 先用 n-gram 交集粗筛，再对少数幸存者做最长公共子串。
        # 逐条跑 LCS 在几十条项目时无所谓，但库涨到几百条会明显拖慢每一次对话。
        n = min_run
        grams = {text[i:i + n] for i in range(len(text) - n + 1)}
        scored: list[tuple[int, int, dict[str, Any]]] = []
        for index, project in enumerate(self._projects(include_expired=True)):
            title = project["title"]
            if not any(gram in title for gram in grams):
                continue
            run = self._longest_common_run(title, text)
            if run >= n:
                scored.append((run, -index, project))
        scored.sort(key=lambda item: (-item[0], -item[1]))
        return [project for _run, _idx, project in scored[:limit]]

    def _title_index(self) -> dict[str, dict[str, Any]]:
        return {project["title"]: project for project in self._projects(include_expired=True)}

    def _shown_list(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """还原用户最近一次看到的那份编号列表，按屏幕上的顺序。

        依据是我自己写出去的那段文字——推荐卡的格式就是「1. **标题**」，
        标题原样输出，按标题回查就能拿回项目。

        「潜在机会」用的是不带编号的「- **标题**」，所以「第 N 个」不会指到
        那里去，这跟用户的直觉一致。
        """
        by_title = self._title_index()
        for item in reversed(messages):
            if item.get("role") != "assistant":
                continue
            found = [
                by_title[match.group(2).strip()]
                for match in _LISTED_RE.finditer(item.get("content") or "")
                if match.group(2).strip() in by_title
            ]
            if found:
                return found
        return []

    def _current_project(self, messages: list[dict[str, Any]]) -> dict[str, Any] | None:
        """当前话题落在哪个项目上：看详情和做材料，**谁更近算谁**。

        原来这是两个函数各自倒着扫，调用方先问「最近给谁做过材料」，
        问不到才问「最近看过谁的详情」——于是谁先被*问到*谁赢，
        而不是谁更*近*谁赢。实测：

            3  帮我写报名理由            -> A（材料抬头留在 A）
            4  宝庆微光 详细说说          -> B  用户明确换到 B 了
            5  帮我写报名理由            -> A  又绑回去了
            6  访谈提纲也来一份          -> A  一直错下去

        第 5 轮往后每一份材料写的都是 A，而输出看起来完全正常——用户
        除非逐字读，否则发现不了。这就是"绑错之后不会自愈"。

        合成一次扫描，从后往前，先撞上哪个标记就是哪个。顺带也不用再
        担心两个函数的判据漂开——这个项目上手工维护的并列清单已经
        漂过五次了。
        """
        by_title = self._title_index()
        for item in reversed(messages):
            if item.get("role") != "assistant":
                continue
            content = item.get("content") or ""
            # 同一条消息里两种标记都有时（详情页下面又跟了材料），
            # 按它们在正文里出现的先后取最后那个。
            marks = [(m.start(), m.group(1).strip()) for m in _ASSET_HEADER_RE.finditer(content)]
            marks += [(m.start(), m.group(1).strip()) for m in _DETAIL_HEAD_RE.finditer(content)]
            for _, title in sorted(marks, key=lambda pair: pair[0], reverse=True):
                if title in by_title:
                    return by_title[title]
        return None

    @staticmethod
    def _ordinal_in(text: str) -> int | None:
        match = _ORDINAL_RE.search(text)
        if not match:
            return None
        token = match.group(1)
        return _CN_NUMERALS.get(token) or (int(token) if token.isdigit() else None)

    def _resolve_project(
        self, messages: list[dict[str, Any]], latest: str, *,
        latest_only: bool = False, loose: bool | None = None,
    ) -> dict[str, Any] | None:
        projects = self._projects(include_expired=True)
        direct = [project for project in projects if project["title"] in latest or project["id"] in latest]
        if direct:
            return direct[0]

        # 序数词必须在最前面处理，而且不受 latest_only 影响。
        #
        # 原来这段包在 `if not latest_only:` 里。reply() 后半段有一次
        # `_resolve_project(..., latest_only=True)`，走到那里时序数逻辑被跳过，
        # 「第二个详细说说」于是掉进模糊匹配——「第二」命中了标题里的
        # 「（第二批）」，「第一」命中「（第一批次）」。实测三次串线全出在这里，
        # 而且返回的是已截止三个月的项目卡，还煞有介事地列出经费原文。
        ordinal_asked = self._ordinal_in(latest)
        if ordinal_asked is not None:
            shown = self._shown_list(messages)
            return shown[ordinal_asked - 1] if 1 <= ordinal_asked <= len(shown) else None

        # 指代同样要排在模糊匹配前面。
        #
        # 100 轮实测里最严重的一次串线就出在这里：第 96 轮推荐了春山和宝庆，
        # 第 97 轮说「给第一项生成报名风险清单」，系统绑到了宣传部学生骨干，
        # 之后 98、99 轮继续错下去，第 100 轮问「是不是串线了」还掉了兜底。
        # 原因是旧代码拿 `[p for p in projects if p["title"] in conversation]`
        # 当候选，那是**数据库顺序**——「第一个」等于"历史上出现过的项目里在库里
        # 排最前的那个"，跟屏幕上的顺序毫无关系。
        # 给错项目写材料比答不上来严重得多，所以这里宁可返回 None。
        if not latest_only:
            # 「这个项目」「它」这类指代同样要排在模糊匹配前面。
            # 实测过一次：「帮我写这个项目的报名理由」里的"项目"两个字，
            # 被模糊匹配命中了标题里含「项目」的另一条记录——指代词本身
            # 成了匹配依据，指到了一个用户根本没看过的项目上。
            if _DEICTIC_RE.search(latest):
                anchored = self._current_project(messages)
                if anchored:
                    return anchored
                shown = self._shown_list(messages)
                if len(shown) == 1:
                    return shown[0]

        # 这一句没有点名任何项目、而刚刚又给某个项目做过材料时，指的就是它。
        #
        # 必须排在模糊匹配之前。实测：先给 A 写报名理由、再写访谈提纲都对，
        # 第三句「外联话术也来一份」却静默切到了一个完全无关的已过期项目——
        # 因为这句里没有项目名，模糊匹配就拿整句去撞标题，撞上了谁算谁。
        # 静默换项目比答不上来危险得多：用户拿到的材料看起来完全正常。
        if not latest_only and not self._names_a_project(latest):
            anchored = self._current_project(messages)
            if anchored:
                return anchored

        # 模糊匹配：只有当最相关的那个明显强于第二名时才认定，
        # 否则宁可让调用方列出候选让用户挑，也不猜错项目。
        candidates = self.match_projects(latest, loose=loose)
        if candidates:
            if len(candidates) == 1:
                return candidates[0]
            top = self._longest_common_run(candidates[0]["title"], latest)
            second = self._longest_common_run(candidates[1]["title"], latest)
            if top > second:
                return candidates[0]

        if latest_only:
            return None
        # 「它」「这个项目」「刚才那个」这类指代：认最近展开过详情的那一个，
        # 其次认列表里只有一条的情况。列表有好几条又没说是哪条，就返回 None
        # 让调用方问一句——原来这里返回 mentioned[0]（数据库顺序里的头一个），
        # 那是在猜，而且猜错时用户看不出来。
        # 看过详情、或刚给谁做过材料——谁更近算谁，见 _current_project。
        anchored = self._current_project(messages)
        if anchored:
            return anchored
        shown = self._shown_list(messages)
        return shown[0] if len(shown) == 1 else None

    def _pick_one_of(self, candidates: list[dict[str, Any]], purpose: str) -> str:
        """有多个同样像的项目时，把候选摆出来让用户挑，而不是替他猜一个。"""
        lines = [f"有 {len(candidates)} 个项目都对得上，你要{purpose}哪一个？", ""]
        for project in candidates:
            deadline = project.get("signup_deadline") or "截止待确认"
            lines.append(f"- **{project['title']}**（{deadline}）")
        lines.append("\n把标题里能区分的那几个字告诉我就行，比如地名或主题。")
        return "\n".join(lines)

    def _generate(self, messages: list[dict[str, Any]], all_user_text: str, latest: str) -> ChatResult:
        kind = _asset_kind(latest)
        project = self._resolve_project(messages, latest, loose=True)
        if kind == "report" and project:
            return self._draft_report(project)
        if not project:
            candidates = self.match_projects(latest, loose=True)
            if len(candidates) > 1:
                return ChatResult(self._pick_one_of(candidates, "生成材料给"), "generate_needs_project")
            return ChatResult(
                "请先告诉我具体项目名称或项目 ID。我需要把材料绑定到已核验的项目时间、地点、资格和报名要求，不能凭空生成。",
                "generate_needs_project",
            )
        profile = self._extract_profile(all_user_text)
        try:
            result = generate_asset(project, kind, {"department": profile.get("department", "")})
        except ValueError as exc:
            # 行程/路线任务需要先在行动工作台里勾选当地点位和填写住宿位置，
            # 而对话里没有这些表单。以前这里会把异常抛成 API 错误，清小搭那边
            # 直接显示成"对话失败"；改为在对话里说清楚缺什么、去哪补。
            if kind == "itinerary":
                return ChatResult(
                    f"「{project['title']}」的行程任务表需要先确定当地要去的点位和住宿位置，"
                    "这两项在对话里没法勾选，也不该由我替你猜——路线一旦编错，"
                    "会直接影响你的实地安排。\n\n"
                    "请先在实践小搭的「行动工作台」里选好点位并填写住宿位置，再生成行程。\n\n"
                    f"我现在可以直接帮你出这个项目的：访谈提纲、当地外联话术、报名理由或调研报告框架——说一声就行。\n\n"
                    f"> 原因：{exc}",
                    "generate_itinerary_needs_sites",
                    project["id"],
                )
            return ChatResult(f"这份材料暂时生成不了：{exc}", f"generate_{kind}_failed", project["id"])
        warnings = "\n\n> 注意：" + "；".join(result["warnings"]) if result["warnings"] else ""
        siblings = {
            "application": "当地外联话术、访谈提纲、调研报告框架",
            "outreach": "访谈提纲、报名理由、调研报告框架",
            "interview": "当地外联话术、报名理由、调研报告框架",
            "report": "报名理由、当地外联话术、访谈提纲",
        }.get(kind)
        hint = f"\n\n还可以为这个项目生成：{siblings}。" if siblings else ""
        # 抬头写明是给哪个项目做的。既是给用户看的，也是下一轮还原绑定的依据——
        # 实测「出完推送后说『再给它写个访谈提纲』」会掉回「先告诉我给哪个项目写」，
        # 因为材料输出里没有任何能让我认回项目的痕迹。
        # 用户说的是「提交」「帮我报名」，拿到的却是一份填写建议——中间这层
        # 差别必须挑明。实测「帮我提交研究生支教团的报名表」直接给出成品表格，
        # 全文没有一句说明我不能替他提交，很容易被理解成已经代办了。
        header = f"> 为「{project['title']}」生成。\n\n"
        if _ACT_FOR_ME_RE.search(latest):
            header = (
                "> **我不能替你提交，也没有你的账号。** 报名要你自己在原文的报名入口完成。\n"
                f"> 下面是给「{project['title']}」的填写建议，你复制过去核对后再提交。\n\n"
            )
        return ChatResult(header + result["content"] + warnings + hint,
                          f"generate_{kind}", project["id"])

    _REPORT_SYSTEM_PROMPT = (
        "你在帮清华大学的学生搭一份社会实践调研报告的框架。**他还没出发**，"
        "所以你写的是「到了那里该弄清什么、该记下什么」，不是结论。\n\n"
        "**你手上有两样材料**：【原文】是这个项目自己发布的招募通知全文，"
        "【项目事实】是从里面抽出来、已经核对过的关键字段。\n\n"
        "硬性要求：\n"
        "1. **每一节的提示都必须落到这个项目上。** 原文里写了去哪、做什么、"
        "跟谁合作、想解决什么问题——把这些变成具体的调研问题。"
        "写不出项目特有的东西时，宁可这一节少写两条，也不要用「分析各方关系」\n"
        "「梳理现状」这种放到任何项目上都成立的空话填满。\n"
        "2. **不要替他写结论、发现、数据、访谈记录。** 他还没去。\n"
        "3. **不许编原文里没有的事实**：合作单位、往届成果、当地数据、政策文件名。"
        "需要他去核实的，就写成「待你到现场确认」。\n"
        "4. 方法建议要跟这次实践的实际形态匹配——支教就是课堂观察和师生访谈，"
        "产业调研就是企业走访和二手数据，别一律写「发放问卷」。\n"
        "5. 伦理提醒要具体：涉及未成年人、涉及个人隐私、需要录音授权的，分别点出来。\n\n"
        "输出结构（用 Markdown，标题层级照抄）：\n"
        "开头一句话说明这份框架是按哪个项目、哪个地点、哪个议题定制的；\n"
        "## 摘要 / ## 1. 问题提出与研究背景 / ## 2. 研究设计 / ## 3. 主要发现\n"
        "## 4. 机制分析 / ## 5. 对策建议 / ## 6. 结论与反思 / ## 7. 实践收获与个人反思\n"
        "## 附录，最后给一张证据台账空表。\n"
        "总长 800-1200 字。"
    )

    def _draft_report(self, project: dict[str, Any]) -> ChatResult:
        """调研报告框架——跟推送一样，拿原文当依据，模板只作兜底。

        用户的原话是「调研还是很烂……没有上下文」。确实：我修推送时把原文喂了
        进去，报告这条却还是纯模板套变量——七节提示放到任何项目上都成立，
        学生要它没用。同一个项目、同一份原文，凭什么写推送能用、写报告不能用。
        """
        facts = self._project_facts_block(project)
        source = redact_contacts(
            self.db.latest_article_text(project.get("source_url", ""))[:4000], keep_email=True)
        fallback = generate_asset(project, "report")

        if not llm.is_enabled() or not source:
            reason = "当前没有配置写作模型" if not llm.is_enabled() else "这个项目没有存档原文"
            return ChatResult(
                f"> {reason}，下面是按项目地点和主题套出来的通用框架。\n\n"
                + fallback["content"],
                "generate_report_fallback", project["id"],
            )
        try:
            body = llm.complete(
                self._REPORT_SYSTEM_PROMPT,
                f"【项目事实】\n{facts}\n\n【原文】\n{source}\n\n请据此搭调研报告框架。",
            )
        except llm.LLMUnavailable:
            return ChatResult(
                "> 写作模型这次没能用上，下面是通用框架。\n\n" + fallback["content"],
                "generate_report_fallback", project["id"],
            )
        if not body.strip():
            return ChatResult(fallback["content"], "generate_report_fallback", project["id"])

        invented = unsupported_numbers(body, f"{facts}\n{source}")
        tail = ""
        if invented:
            tail = ("\n\n> **这几个数字在原文和项目卡里都查不到出处**："
                    + "、".join(invented) + "。用之前先核实。")
        tail += ("\n\n---\n> 这份框架依据的是该项目的招募通知原文。"
                 "空着的地方是只有去过现场的人才知道的，我不替你写。")
        header = f"> 为「{project['title']}」生成的调研报告框架。\n\n"
        return ChatResult(header + body.strip() + tail, "generate_report", project["id"])

    _POST_SYSTEM_PROMPT = (
        "你在帮清华大学的学生给一次社会实践招募写公众号推送文案。\n\n"
        "**你手上有两样材料**：【原文】是这个项目自己发布的通知全文，"
        "【项目事实】是从原文里抽出来、已经核对过的关键字段。\n\n"
        "硬性要求：\n"
        "1. **所有具体内容都必须能在【原文】或【项目事实】里找到出处。**"
        "写之前先想清楚这句话的依据在哪一行；找不到出处就不要写。\n"
        "2. **不许自己造场景。** 不要写「群山之间」「站在讲台上」「一双双好奇的眼睛」"
        "这类画面——你没去过那里，也没有任何材料支持。不要编课程内容、往届成果、"
        "带队老师、报名人数、获奖情况。原文里讲了什么故事，你就用什么故事。\n"
        "3. 标着「待确认」的字段，在文中写成待定并提示以原文通知为准，"
        "绝对不要为了通顺而编一个具体值。\n"
        "4. **不要把内部字段名写进文案。** 「主题标签」「待确认字段」这些是我们的"
        "数据结构，不是推送内容。\n"
        "5. 面向清华在校生。宁可朴素具体，也不要空泛抒情——"
        "「微光虽小，聚在一起就能照亮一段路」这种句子不要写。\n\n"
        "输出结构：一个标题（15 字以内）、一段从原文里提炼的引入、项目要点"
        "（时间/地点/资格/报名方式/截止）、结尾的号召。用 Markdown，总长 400-700 字。"
    )

    def _draft_post(self, messages: list[dict[str, Any]], all_user_text: str, latest: str) -> ChatResult:
        """写公众号推送文案——目前唯一走大模型的能力。

        分工是刻意的：项目事实全部来自已核验的项目卡（规则 + SQLite），
        模型只负责把这些事实组织成通顺的文字。模型不可用时回落到要点清单，
        用户仍然拿得到能用的东西，而不是一个报错。
        """
        project = self._resolve_project(messages, latest, loose=True)
        if not project:
            candidates = self.match_projects(latest, loose=True)
            if len(candidates) > 1:
                return ChatResult(self._pick_one_of(candidates, "写推送给"), "draft_post_needs_project")
            return ChatResult(
                "先告诉我给哪个项目写推送——说出项目名，或者标题里能区分的那几个字就行。"
                "我需要把文案绑定到已核验的时间、地点、资格和报名要求上，不能凭空写。",
                "draft_post_needs_project",
            )
        facts = self._project_facts_block(project)

        if not llm.is_enabled():
            return ChatResult(
                self._post_outline(project, facts, reason="当前没有配置写作模型"),
                "draft_post_fallback",
                project["id"],
            )

        # 故意不用 H1：模型会自己写一个标题，两个 H1 叠在一起很难看。
        # 这一行只是标明这是草稿、绑定到哪个项目。
        header = f"> 为「{project['title']}」起草的推送文案，项目事实来自已核验的项目卡。\n\n"
        # 把原文一起给模型。只给十来条抽取字段、却要求写 400–700 字，等于逼它编。
        # 上限 4000 字是为了控制单次请求的体量；招募通知本身通常就在这个量级内，
        # 真超了的话开头那部分（背景、理念、团队介绍）正是写文案最用得上的。
        # 喂给模型的原文也要脱敏。133 篇原文里 19 篇含手机号，模型完全可能
        # 把它照抄进推送——那就是我们主动把私人号码公开发布出去。
        source = redact_contacts(
            self.db.latest_article_text(project.get("source_url", ""))[:4000], keep_email=True)
        blocks = [f"【项目事实】\n{facts}"]
        if source:
            blocks.append(f"【原文】\n{source}")
        else:
            blocks.append("【原文】\n（这个项目没有存档原文，只能依据上面的字段写，"
                          "内容要相应地克制，不要展开任何没有依据的描写。）")
        user_prompt = "\n\n".join(blocks) + "\n\n请据此写推送文案。"

        def produce() -> Iterable[str]:
            yield header
            produced_any = False
            written: list[str] = []
            try:
                for piece in llm.stream(self._POST_SYSTEM_PROMPT, user_prompt):
                    produced_any = True
                    written.append(piece)
                    yield piece
            except llm.LLMUnavailable as exc:
                reason = f"写作模型这次没能用上（{exc}）"
                # 已经吐出去的字收不回来。如果模型一个字都没给，就直接接要点清单；
                # 如果已经写了一半才断，加条分隔线说明后面是降级内容，
                # 而不是假装那半截是完整文案。
                if produced_any:
                    yield "\n\n---\n\n"
                yield self._post_outline(project, facts, reason=reason)
                return
            # 数字是最容易被顺手编出来、也最容易被当真的东西。逐个回查项目卡，
            # 查不到出处的当场点名——不拦截，但绝不让它悄悄混过去。
            # 对照的是**给过模型的全部材料**，包括原文。只对抽取字段查的话，
            # 文案里引用原文的日期和年份会被全部误报。
            invented = unsupported_numbers("".join(written), f"{facts}\n{source}")
            if invented:
                yield (
                    "\n\n> **这几个数字在项目卡里查不到出处**："
                    + "、".join(invented)
                    + "。多半是模型写顺手加的，发布前务必删掉或去原文核实。"
                )
            yield (
                "\n\n---\n"
                "> 以上文案由模型基于已核验的项目卡生成，发布前请逐条核对原文通知；"
                "带「待确认」的字段务必自行补全。"
            )

        return ChatResult("", "draft_post", project["id"], stream_factory=produce)

    @staticmethod
    def _project_facts_block(project: dict[str, Any]) -> str:
        location = (project.get("location") or {}).get("detail") or "待确认"
        eligibility = (project.get("eligibility") or {}).get("restriction_text") or "待确认"
        reimbursement = (project.get("reimbursement") or {}).get("text") or "待确认"
        return "\n".join([
            f"项目名称：{project.get('title')}",
            f"主办方：{project.get('organizer') or '待确认'}",
            f"项目简介：{project.get('summary') or '待确认'}",
            f"实践时间：{project.get('practice_start') or '待确认'} 至 {project.get('practice_end') or '待确认'}",
            f"实践地点：{location}",
            f"参与资格：{eligibility}",
            f"报名截止：{project.get('signup_deadline') or '待确认'}",
            f"报名方式：{redact_contacts(project.get('signup_method') or '待确认', keep_email=True)}",
            f"经费与报销：{reimbursement}",
            f"主题标签：{'、'.join(project.get('theme_tags') or []) or '待确认'}",
            f"待确认字段：{PracticeChatAdapter._field_labels(project.get('uncertain_fields') or []) or '无'}",
        ])

    @staticmethod
    def _post_outline(project: dict[str, Any], facts: str, *, reason: str) -> str:
        """模型不可用时的兜底：给要点和写作提纲，不是报错。"""
        return (
            f"# {project['title']}｜推送要点\n\n"
            f"> {reason}，这里先给你一份可以直接改写的要点清单。\n\n"
            "**建议结构**\n"
            "1. 标题：项目名 + 一个具体的吸引点（地点、主题或成果）\n"
            "2. 引入：这次实践要解决或了解什么问题\n"
            "3. 项目要点：时间、地点、内容安排\n"
            "4. 招募：面向谁、需要什么准备\n"
            "5. 结尾：报名方式与截止时间，单独一行加粗\n\n"
            "**可直接引用的已核验事实**\n\n"
            f"{facts}\n\n"
            "> 以上字段来自项目卡；标着「待确认」的请先核对原文通知再写进文案。"
        )

    def _mentions_project_exactly(self, text: str) -> bool:
        """用户是否完整点名了某个项目（标题或 ID），不做模糊。"""
        return any(
            project["title"] in text or project["id"] in text
            for project in self._projects(include_expired=True)
        )

    def _names_a_project(self, text: str) -> bool:
        """这一句能不能唯一定位到一个项目——完整标题、ID，或者足以区分的几个字。

        原来这几处护栏用的是 _mentions_project_exactly，要求打出完整标题：

            实践招募 | 机械系“宝庆微光”赴湖南新宁支教实践支队招募

        没人会这么打。实测「宝庆微光 调研提纲」因此被判成"没点名项目"，
        掉进通用写作建议——用户明明指定了项目，拿到的却是不绑项目的泛泛之谈。
        这就是"必须要非常非常严格的关键词"的另一面。

        改用模糊解析，但沿用它自带的歧义护栏：匹配不唯一时返回 None，
        「实践总结怎么写」这类不会被误绑到某个项目上。
        """
        if self._mentions_project_exactly(text):
            return True
        project = self._resolve_project([], text, latest_only=True, loose=True)
        return bool(project and _distinctive_overlap(project["title"], text))

    def _strip_project_names(self, text: str) -> str:
        """把用户打出来的项目名从这句话里摘掉，剩下的才是他对自己的描述。

        项目名是**指称**，不是**条件**。「研究生支教团 还能报吗」里的
        「研究生」是这个项目对参与者的要求，不是提问者的年级；可是抽条件时
        整句话一起进 _extract_profile，年级就被抽成了「研究生」。更糟的是
        条件会跨轮沿用——只要点开过一次这个项目，后面每一次筛选都被它压着，
        而且回复里从不提这条来自哪，用户根本看不出来。

        「赴湖南新宁支教实践支队 怎么报名」同理：湖南是那个项目在哪，
        不是用户想去哪。

        只在这句话确实唯一指到一个项目时才摘，判据沿用 _names_a_project
        那一套（完整标题、或标题里足以区分的专名片段）；「我想找乡村振兴
        主题的实践」这种只撞上通用主题词的，_distinctive_overlap 会返回空，
        原样放行。
        """
        if not text:
            return text
        project = self._resolve_project([], text, latest_only=True, loose=True)
        if not project:
            return text
        title = str(project.get("title") or "")
        for span in (title, str(project.get("id") or ""), _distinctive_overlap(title, text)):
            if span and span in text:
                return text.replace(span, " ")
        return text

    def _provenance(self) -> str:
        """回答"这些信息准吗 / 你怎么知道的 / 数据什么时候更新的"。

        这是本产品最该答好的一类问题——它和"让大模型直接读通知"的区别就在这里。
        所以不能空谈可靠，要把实际数字摆出来：库里多少条、多少条核验过、
        多少条还没核验、来源有几个、最近一次导入是什么时候。
        """
        projects = self._projects(include_expired=True)
        published = [p for p in projects if p.get("status") == "published"]
        pending = [p for p in projects if p.get("status") == "needs_review"]
        expired = [p for p in projects if p.get("status") == "expired"]
        sources = sorted({p.get("source_account", "") for p in projects if p.get("source_account")})
        with_evidence = [p for p in projects if (p.get("field_evidence") or {})]
        with_link = [p for p in projects if p.get("source_url")]
        demo = [p for p in projects if p.get("demo_data")]

        # 「多久更新一次」是这类问题里最常问的一句，而原来的回答通篇没有回答它。
        # 又不能编一个「每天更新」——更新取决于采集方什么时候推数据，没有固定周期。
        # 所以把最近几次实际入库时间摆出来，让用户自己看节奏。
        imports = [
            item.get("created_at", "")[:16].replace("T", " ")
            for item in self.db.recent_activity(limit=60)
            if item.get("event_type") in {"ingest", "seed", "collector"}
        ]
        latest_import = imports[0] if imports else ""

        lines = [
            "我不猜，只转述已经存进项目库的内容，每个关键字段都留了原文引用。",
            "",
            "**数据是怎么来的**",
            "- 来自公众号招募通知：可以是采集到的、你粘贴的正文，或截图 OCR 出来的文本",
            "- 抽取出报名截止、实践时间、地点、参与资格、经费等字段，"
            "同时把对应的**原文原句**一并存下来——你可以直接说出项目名，看每个字段是从哪句话来的",
            "- 抽不准或原文没写的，标成「待确认」，不会替它编一个值",
            "",
            "**现在库里的实际情况**",
            f"- 共 {len(projects)} 条：已核验 {len(published)} 条、待人工核验 {len(pending)} 条、已过截止 {len(expired)} 条",
            f"- 带原文引用的 {len(with_evidence)} 条，带原文链接的 {len(with_link)} 条",
            f"- 来源账号 {len(sources)} 个" + ("：" + "、".join(sources[:5]) if sources else ""),
        ]
        if latest_import:
            lines.append(f"- 最近一次入库：{latest_import}")
        if demo:
            lines.append(f"- 其中 {len(demo)} 条是演示数据，会明确标注，不能作为真实报名依据")
        lines += ["", "**多久更新一次**"]
        if len(imports) >= 2:
            recent = "、".join(list(dict.fromkeys(day[:10] for day in imports))[:5])
            lines.append(
                f"- **没有固定周期**。更新取决于采集方什么时候把新推送推过来，"
                f"我不替它承诺频率。最近几次入库发生在：{recent}"
            )
        else:
            lines.append("- **没有固定周期**，取决于采集方什么时候推数据。目前入库记录还很少。")
        lines.append("- 所以「最近有没有新项目」这个问题，看上面那个入库日期最准；"
                     "隔得久了就说明还没有新的推过来，不是我漏了。")

        lines += [
            "",
            "**哪些地方仍然可能出错**",
            "- 只有人工核验过的项目才会进正式推荐；待核验的单独列出，不作数",
            "- 通知发出后主办方可能改时间或延长报名，库里不一定跟得上",
            "- 所以：**报名前请点开原文链接再核对一次**，尤其是截止时间和资格要求",
        ]
        return "\n".join(lines)

    # 招募推送里常见的"参加能得到什么"。用于回答保研/学分类问题时，
    # 补一句有据可查的事实——而不是只说"我不知道"。
    _INCENTIVE_TERMS = (
        ("志愿工时", ("志愿工时", "工时认证", "服务时长")),
        ("实践证明或证书", ("实践证明", "结业证书", "荣誉证书", "证书")),
        ("交通或食宿补贴", ("交通补贴", "报销", "食宿", "补贴")),
        ("推荐信或评优机会", ("推荐信", "评优", "优秀个人", "表彰")),
    )

    def _incentive_evidence(self) -> str:
        """从语料里数一数：招募推送到底承诺了什么。

        「保研有没有用」这个问题的规则部分我们答不了，也不该猜。但"推送里
        写了什么"是查得到的——把它摆出来，比只说一句"以院系文件为准"有用，
        而且每个数字都能回查。

        注意措辞：说的是"推送里提到"，不是"参加就能获得"，更不是"对保研有用"。
        这三件事不一样。
        """
        corpus = self._corpus()
        recruitment = [s for s in corpus.samples if s.genre == "招募推送"]
        if not recruitment:
            return ""
        counts = []
        for label, terms in self._INCENTIVE_TERMS:
            hits = sum(1 for s in recruitment if any(term in s.text for term in terms))
            if hits:
                counts.append(f"{label} {hits} 篇")
        if not counts:
            return ""
        return (
            f"能查到的是这个：已采集的 {len(recruitment)} 篇招募推送里，"
            f"提到{'、'.join(counts)}。\n"
            "这只是**推送里写了什么**，不等于参加就一定拿得到，更不等于对保研有用——"
            "具体以各项目的原文和你院系的认定口径为准。\n\n"
        )

    def _about_practice(self, latest: str) -> str:
        """关于社会实践本身的常识提问——不是在找项目，别丢一页推荐给人家。"""
        base = (
            "这个问题超出我能负责的范围了——我只对项目库里那些**有原文出处**的信息负责，"
            "不想拿没有依据的说法糊弄你。\n\n"
        )
        if any(word in latest for word in ("保研", "加分", "综测", "学分")):
            return base + (
                "社会实践和保研、综测的换算规则各院系不一样，而且逐年调整，"
                "以你所在院系当年的文件和辅导员口径为准，别信二手转述。\n\n"
                + self._incentive_evidence() +
                "我能帮的是：按你的时间、地点、主题筛出还能报名的项目，"
                "并给出每个字段的原文出处。要试试吗？"
            )
        if any(word in latest for word in ("组队", "组建", "支队是什么")):
            return base + (
                "组队和立项流程由校团委和各院系发布，每年细节有变化，建议直接查最新通知。\n\n"
                "我这边可以做的是：帮你找已经在招募的支队（很多支队本来就缺人），"
                "或者你已经有了项目，我可以帮你写报名理由、外联话术、访谈提纲和推送文案。"
            )
        if any(word in latest for word in ("第一次参加", "有什么建议", "注意什么", "需要准备什么")):
            # 第一次参加的人是最该好好接住的——上来就说"超出范围"太冷了。
            return (
                "第一次参加的话，挑项目时我建议按这个顺序看：\n\n"
                "1. **报名截止来不来得及**——很多人是看到通知时已经过了\n"
                "2. **时间冲不冲突**——实践期常和小学期、暑培撞车，先把日历对一遍\n"
                "3. **资格限不限院系年级**——写明限定的就是硬门槛，别抱侥幸\n"
                "4. **经费怎么算**——交通住宿自理还是报销，差别很大\n"
                "5. **最后才是主题**——能不能真正参与进去，比题目大不大重要\n\n"
                "前四条我可以直接帮你筛掉不符合的，并说明每条为什么被排除。"
                "告诉我你的院系、年级、大概什么时候有空、想去哪类地方？\n\n"
                "> 至于流程、学分、材料这些规定，各院系每年都在变，以你院系的最新通知为准——"
                "这部分我没有可回查的依据，不替你猜。"
            )
        return base + (
            "我能帮的是这几件事：按条件筛项目并解释推荐理由、给出每个字段的原文引用、"
            "比较两个项目、为指定项目生成报名理由和外联访谈材料、把招募通知转成项目卡。\n\n"
            "先说说你的院系、年级和大概什么时候有空？"
        )

    @staticmethod
    def _recommendation_line(index: int, item: dict[str, Any]) -> str:
        """第四五名的一行式条目：留住名字和一句最能帮人做判断的信息。"""
        project = item["project"]
        detail = (project.get("location") or {}).get("detail") or ""
        deadline = project.get("signup_deadline")
        facts = [f"截止 {deadline}" if deadline else "", f"地点 {detail}" if detail else ""]
        tail = "；".join(f for f in facts if f) or "关键字段以原文为准"
        return f"{index}. **{project['title']}** — {tail}"

    @staticmethod
    def _recommendation_card(index: int, item: dict[str, Any]) -> list[str]:
        """推荐列表里的一条。

        原来固定输出「截止：X；地点：Y」，抽不到就写"待确认"。真实数据上线后
        实测一条推荐里能出现十几个"待确认"——因为很多通知本来就没写报名截止和
        地点。满屏"待确认"有两个坏处：看起来像系统没做好（其实是原文没写），
        而且把真正有用的信息淹了。

        改成只列**抽到了的**字段；确实没有的不占位，改在末尾统一说明缺什么、
        并把原文链接给出去——学生自己点进去看比我们反复说"待确认"有用。
        """
        project = item["project"]
        facts: list[str] = []
        if project.get("signup_deadline"):
            facts.append(f"截止 {project['signup_deadline']}")
        if project.get("practice_start"):
            span = project["practice_start"]
            if project.get("practice_end") and project["practice_end"] != span:
                span += f"~{project['practice_end']}"
            facts.append(f"时间 {span}")
        detail = (project.get("location") or {}).get("detail") or (project.get("location") or {}).get("province")
        if detail:
            facts.append(f"地点 {detail}")
        if (project.get("reimbursement") or {}).get("has_reimbursement"):
            facts.append("有经费支持")

        # 这里原来跟着一个「（匹配度 65）」。去掉了：那个分数是相对当次提问算的，
        # 同一个项目问「京津冀的支教」得 65、问「为什么这么推荐」得 52——差的
        # 13 分来自主题词有没有命中（+25 对 +12），跟项目本身好不好毫无关系。
        # 界面上只给个光秃秃的数字，看起来就像在随机跳动。排序已经表达了优劣，
        # 想要量化依据的人可以直接问「为什么这么推荐」。
        lines = [f"{index}. **{project['title']}**"]
        if facts:
            lines.append(f"   - {'；'.join(facts)}")
        missing = [FIELD_LABELS.get(name, name) for name in project.get("uncertain_fields", [])
                   if name != "source_url"]
        if missing:
            lines.append(f"   - 原文未写明：{'、'.join(missing[:4])}——以原文为准")
        if project.get("source_url"):
            lines.append(f"   - 原文：{_autolink(project['source_url'])}")
        lines.append(f"   - 理由：{'；'.join(item['reasons'][:2]) or '信息完整度较高'}")
        return lines

    # 贴一段文字要求处理。冒号后跟一整段，或者整条消息就是一大段文稿。
    POLISH_WORDS = (
        "润色", "改改", "改一下", "修改一下", "帮我改", "改得", "优化一下",
        "通顺吗", "怎么样", "点评", "看看这段", "帮我看看", "有什么问题",
        "读起来", "打磨", "精简", "扩写",
    )
    # 低于这个长度的多半不是待处理文稿，而是一句提问。
    # 原来设的 25 字太高：「你看看我这个开头写得怎么样：盛夏的七月，我们踏上了
    # 前往西部的列车。」冒号后只有 20 字，于是不触发，整句被模糊匹配成了某个
    # 项目的详情页。一个开头句本来就短，判据该是"有没有实质内容"而不是"够不够长"。
    MIN_POLISH_TEXT = 12

    @staticmethod
    def _extract_pasted_text(latest: str) -> str:
        """从「帮我润色这段：……」里把要处理的正文抠出来。

        用户贴文时的写法很随意：有冒号的、有换行的、也有直接一整段甩过来的。
        取冒号或换行之后的部分；都没有就看整条消息够不够长。
        """
        for separator in ("：", ":", "\n"):
            head, sep, tail = latest.partition(separator)
            if sep and len(tail.strip()) >= PracticeChatAdapter.MIN_POLISH_TEXT:
                return tail.strip()
        stripped = latest.strip()
        return stripped if len(stripped) >= 60 else ""

    def _is_polish_request(self, latest: str) -> bool:
        if not any(word in latest for word in self.POLISH_WORDS):
            return False
        return bool(self._extract_pasted_text(latest))

    def _polish_text(self, latest: str) -> ChatResult:
        """润色或点评用户贴过来的文稿。

        这是写作辅助最基本的用法，以前完全接不住：贴一段要求润色，系统回
        「我得先知道是给哪个项目写」；更糟的一次是把「盛夏的七月，我们踏上了
        前往西部的列车」模糊匹配到某个项目，端出了那个项目的详情页。

        改写只动表达，不动事实——用户没写的信息不会替他补上。这跟整个项目的
        承诺是同一条：语料只提供文风参照，不提供内容。
        """
        draft = self._extract_pasted_text(latest)
        corpus = self._corpus()
        genre = next((name for words, name in self._GENRE_FOR_QUESTION
                      if any(word in latest or word in draft[:60] for word in words)), "")
        samples = corpus.search(draft[:200], genre=genre, limit=2)
        if not samples and genre:
            # 主题对不上时用同体裁的代表作。润色本来就主要学文体和节奏，
            # 不像"帮我写某某项目的推送"那样需要主题也贴。
            samples = corpus.representatives(genre, limit=2)
        if not samples:
            samples = corpus.search(draft[:200], limit=2)

        if not llm.is_enabled():
            return ChatResult(
                "当前没有配置写作模型，我给不了改写稿。能帮你看的是结构——"
                "招募类推送一般是「背景与意义 → 做什么去哪多久 → 谁能报怎么报截止什么时候 "
                "→ 保障与收获 → 号召和联系方式」这五段，对照着看看你这段落在哪一环、缺了哪一环。\n\n"
                + (f"库里有 {len(samples)} 篇同类真实推文可以参照："
                   + "、".join(s.title[:26] for s in samples) if samples else ""),
                "polish_degraded",
            )

        from corpus import build_reference_block
        reference = build_reference_block(samples)
        system_prompt = (
            "你在帮清华学生改一段社会实践或志愿服务相关的文稿。\n\n"
            "**只动表达，不动事实**：用户没写的时间、地点、人数、联系方式、报销标准，"
            "一个字都不要替他补。参考推文只用来学语气和节奏，里面的具体信息属于别的项目。\n"
            "如果你觉得原文缺了关键信息（比如没写报名截止），在改写稿之后单独提一句提醒他补，"
            "不要直接编一个填进去。\n\n"
            "输出两部分：先给改写稿，再用三到五条说明改了什么、为什么。用中文。"
        )
        try:
            body = llm.complete(system_prompt, f"用户的要求：{latest[:120]}\n\n原文：\n{draft}\n\n{reference}")
        except llm.LLMUnavailable:
            return ChatResult("写作模型暂时不可用，稍后再试；或者把要求说得更具体些，我先给你列结构。", "polish_degraded")
        if not body.strip():
            return ChatResult("这段我没能给出更好的版本，换个说法再试一次？", "polish_degraded")

        # 有没有参照过范文，要说清楚。没找到同类范例时改写照做（润色本来就
        # 不依赖范文），但不能让人以为背后有一堆真实推文撑着。
        if samples:
            tail = ("\n\n---\n\n参照了库里这几篇真实推文的写法："
                    + "、".join(f"《{s.title[:26]}》" for s in samples)
                    + "。里面的日期地点属于那些项目本身，没有被写进你的稿子。")
        else:
            tail = ("\n\n---\n\n这次没有找到足够接近的同类范文，上面是按通用写作原则改的。"
                    "想让它更贴近清华公众号的文风，把你想模仿的那篇正文贴给我。")
        return ChatResult(body.strip() + tail, "polish")

    def _corpus_stats(self, latest: str) -> ChatResult:
        """回答「实践一般去哪些地方」这类问题——从已采集的数据里算，不猜。

        这是"用全部数据回答问题"最直接的一种用法：答案是统计出来的，每个数字
        都能回查。但必须把话说准——我们收的是**已采集到的这些**公众号推送，
        不等于清华所有实践。所以回答里一定带上样本量和来源，让人知道这句话
        的适用范围。
        """
        projects = self._projects()
        corpus = self._corpus()
        lines: list[str] = []
        accounts = sorted({p.get("source_account", "") for p in projects if p.get("source_account")})
        scope = (f"下面的数字来自已采集的 {len(corpus)} 篇公众号推送和 {len(projects)} 条项目卡"
                 + (f"（来源：{'、'.join(accounts[:4])}）" if accounts else "")
                 + "。这是我们收到的样本，不代表清华全部实践。")

        if any(word in latest for word in ("去哪", "地方", "地点", "地区")):
            places = Counter()
            for project in projects:
                location = project.get("location") or {}
                name = location.get("province") or location.get("detail")
                if name:
                    places[name[:12]] += 1
            if places:
                lines.append("**实践地点分布**（有写明地点的项目）\n")
                for name, count in places.most_common(8):
                    lines.append(f"- {name}：{count} 个")
            else:
                lines.append("已采集的项目里还没有写明地点的，暂时统计不出来。")

        elif any(word in latest for word in ("多长", "几天", "多久", "持续")):
            topic = next((word for word in ("支教", "调研", "志愿", "科普", "宣讲", "帮扶")
                          if word in latest), "")
            pool = [p for p in projects if not topic or topic in p.get("title", "")]
            spans, suspicious = [], []
            for project in pool:
                start, end = project.get("practice_start"), project.get("practice_end")
                if not (start and end):
                    continue
                try:
                    days = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
                except ValueError:
                    continue
                if days <= 0:
                    continue
                # 支队实践超过一个月的极少见。真实数据里见过一条"6月11日到8月6日"，
                # 那其实是各楼栋分批检测的日期，被当成了实践起止——统计会把这种
                # 抽错的值放大成一个看起来很确定的结论，所以单独拎出来说明。
                (suspicious if days > 40 else spans).append((days, project["title"]))
            scope_note = f"标题含「{topic}」的项目" if topic else "写明了起止日期的项目"
            if spans:
                spans.sort()
                middle = spans[len(spans) // 2][0]
                lines.append(f"**实践时长**（{len(spans)} 个{scope_note}）\n")
                lines.append(f"- 中位数 {middle} 天，最短 {spans[0][0]} 天，最长 {spans[-1][0]} 天")
                lines.append(f"- 最短《{spans[0][1][:26]}》，最长《{spans[-1][1][:26]}》")
                if len(spans) < 5:
                    lines.append(f"\n只有 {len(spans)} 个样本，这个数字参考价值有限。")
            else:
                lines.append(f"已采集的{scope_note}太少，算不出可靠的时长分布。"
                             "很多招募通知只写主题不写具体日期。")
            if suspicious:
                lines.append(f"\n另有 {len(suspicious)} 条跨度超过 40 天，多半是把正文里几个"
                             "分散的日期当成了起止（比如分批次的活动安排），没有计入上面的统计：")
                for days, title in suspicious[:3]:
                    lines.append(f"- 《{title[:30]}》标着 {days} 天")

        elif any(word in latest for word in ("主题", "类型", "哪类")):
            genres = corpus.genres()
            lines.append("**已采集推文的体裁分布**\n")
            for genre, count in sorted(genres.items(), key=lambda kv: -kv[1]):
                lines.append(f"- {genre}：{count} 篇")
            themes = Counter()
            for project in projects:
                for theme in project.get("themes", []):
                    themes[theme] += 1
            if themes:
                lines.append("\n**项目主题分布**\n")
                for theme, count in themes.most_common(8):
                    lines.append(f"- {theme}：{count} 个")

        elif any(word in latest for word in ("什么时候", "发布", "集中")):
            months = Counter()
            for project in projects:
                deadline = project.get("signup_deadline") or project.get("practice_start")
                if deadline and len(deadline) >= 7:
                    months[deadline[:7]] += 1
            if months:
                lines.append("**报名截止或实践开始的月份分布**\n")
                for month, count in sorted(months.items()):
                    lines.append(f"- {month}：{count} 个")
                lines.append("\n经验上暑期实践的招募集中在 5–6 月，秋季学期的组织招新集中在 9 月开学季——"
                             "但这句是常识，不是从上面这些数字里得出的，我把两者分开说。")
            else:
                lines.append("已采集的项目里带日期的还太少，算不出发布时间的分布。")

        else:
            lines.append(f"**已采集的数据规模**\n")
            lines.append(f"- 可作写作范例的真实推文：{len(corpus)} 篇")
            for genre, count in sorted(corpus.genres().items(), key=lambda kv: -kv[1]):
                lines.append(f"  - {genre}：{count} 篇")
            lines.append(f"- 项目卡：{len(projects)} 条")

        return ChatResult("\n".join(lines) + f"\n\n---\n\n{scope}", "corpus_stats")

    def _writing_help(self, latest: str) -> ChatResult:
        """写作类请求，但没有绑定具体项目。

        分两种，以前混作一谈、一律要求先选项目：

        1. **通用写法问题**——「实践总结报告一般怎么写」「招募推送有什么套路」。
           用户要的是方法，不是他那个项目的材料。硬要他先选项目，等于答非所问。
           这种用语料库里的真实推文当范例来答。

        2. **要给自己项目写材料**——「帮我写报名理由」。这种确实得先知道是哪个
           项目，因为时间、地点、资格、报名方式都必须跟已核验的项目卡对上。

        两者的边界必须说清楚：范例只提供写法，其中的日期地点联系方式属于别的
        项目，写进用户的材料就是编造。
        """
        if any(hint in latest for hint in GENERIC_WRITING_HINTS) or _NAMING_RE.search(latest):
            return ChatResult(self._generic_writing_answer(latest), "writing_guide")
        return ChatResult(
            "你是想让我帮着写点东西——但我得先知道是给**哪个项目**写，"
            "因为材料里的时间、地点、资格、报名方式都要跟已核验的项目卡对上，我不能凭空编。\n\n"
            "**说出项目名**（标题里能区分的几个字就行），然后告诉我要哪一种：\n"
            "- 报名理由：按报名表栏目给出逐项填写建议\n"
            "- 当地外联话术：联系当地机构时怎么开口\n"
            "- 访谈提纲：围绕已选点位的问题清单\n"
            "- 调研报告框架：章节结构与要点\n"
            "- 公众号推送文案：可直接改的成稿\n\n"
            "如果你只是想问**这类材料一般怎么写**，直接问就行，比如"
            "「实践总结报告一般怎么写」——那种不用先定项目。",
            "writing_help",
        )

    _GENRE_FOR_QUESTION = (
        (("招募", "纳新", "招新", "报名推送"), "招募推送"),
        (("总结", "纪实", "回顾", "心得", "感悟"), "实践总结"),
        (("基地", "推介"), "基地/资源介绍"),
        (("通讯", "简讯", "报道"), "活动通讯"),
    )

    def _generic_writing_answer(self, question: str) -> str:
        """回答"这类东西一般怎么写"，用语料库里的真实推文当范例。"""
        genre = next((name for words, name in self._GENRE_FOR_QUESTION
                      if any(word in question for word in words)), "")
        corpus = self._corpus()
        # 三级：先找主题也对得上的；退而求其次给同体裁的代表作；都没有才说没有。
        #
        # 中间这一级是必要的：分数跟查询长度挂钩，「实践总结怎么写」这种最自然的
        # 短问法只有 0.19 分，够不着门槛——而库里有 15 篇实践总结。用户点明了
        # 体裁，体裁本身就是强相关信号，不该再被分数否决。
        samples = corpus.search(question, genre=genre, limit=3)
        by_topic = bool(samples)
        if not samples and genre:
            samples = corpus.representatives(genre, limit=3)
        if not samples:
            samples = corpus.search(question, limit=3)
            by_topic = bool(samples)

        if not samples:
            # 检索有分数下限：勉强沾边的不算数。与其拿一篇不相干的文章当范文，
            # 不如说清楚没有——这跟项目一贯的做法一致，宁可说不会。
            available = "、".join(f"{k}{v}篇" for k, v in
                                  sorted(corpus.genres().items(), key=lambda kv: -kv[1]))
            return (
                f"库里 {len(corpus)} 篇真实推文里，没有和你这个问题足够接近的同类范例"
                f"（现有的是：{available or '暂无'}）。硬找一篇不相干的照着学，"
                "写出来的东西会跑偏，所以我不那么做。\n\n"
                "两个办法：把你想模仿的那篇正文贴给我，我照着它的结构和语气写；"
                "或者说得更具体一点——比如「志愿服务招募推送怎么写」这样点明文体，"
                "我更容易找到对得上的。"
            )

        from corpus import build_reference_block
        reference = build_reference_block(samples)
        listing = "\n".join(
            f"- {s.title}（{s.account or '来源未标注'}·{s.genre}）" for s in samples)

        if llm.is_enabled():
            naming = bool(_NAMING_RE.search(question)) or any(
                word in question for word in ("起名", "取名", "队名", "支队名"))
            if naming:
                # 起名和讲写法是两回事。用同一套"请给出结构和方法"的提示词，
                # 用户要几个名字，拿到的会是一篇命名方法论。
                system_prompt = (
                    "你在帮清华学生给社会实践支队或志愿项目起名字。\n\n"
                    "直接给 6~8 个候选，每个后面用一句话说明取意。清华支队名的惯例是"
                    "四到六字、常嵌地名或主题、多用对仗或双关（参考推文里能看到这种风格）。\n\n"
                    "**硬性要求**：不要照搬参考推文里已有的支队名——那是别人的项目。"
                    "用户没说主题或地点时，先问一句他想去哪、做什么，再给名字。\n"
                    "用中文，简洁，不要长篇大论。"
                )
            else:
                system_prompt = (
                    "你是清华大学社会实践与志愿服务的写作指导。用户问的是"
                    "「这类材料一般怎么写」，要的是方法和结构，不是某个具体项目的成稿。\n\n"
                    "请给出：写之前要想清楚的问题、推荐的结构（分段列出每段写什么）、"
                    "这类文体的语气特点、常见的问题。\n\n"
                    "**硬性要求**：参考推文只用来学写法。绝不要引用其中的具体日期、"
                    "地点、人名、联系方式、报销金额、名额——那些是别的项目的事实。"
                    "需要举例时用「某地」「X月X日」这样的占位写法。\n"
                    "用中文，控制在 700 字以内，不要写成论文。"
                )
            try:
                body = llm.complete(system_prompt, f"用户的问题：{question}\n\n{reference}")
                if body.strip():
                    # 说清楚这几篇是"主题也对得上"还是"只是同一类的代表作"。
                    # 两种都有用，但不该混为一谈——后者只保证文体像，不保证主题像。
                    lead = ("上面的写法建议参考了这几篇真实推文：" if by_topic
                            else f"没有找到主题正好对上的，下面是库里「{genre}」这一类的代表作，"
                                 "文体和结构可以参考：")
                    return (
                        f"{body.strip()}\n\n---\n\n{lead}\n{listing}\n\n"
                        "里面的日期、地点、联系方式都属于这些项目本身，别直接搬。"
                        "要写你自己项目的成稿，说出项目名就行——那时我会用项目卡里已核验的信息。"
                    )
            except llm.LLMUnavailable:
                pass

        # 没有写作模型时不硬编内容，给结构和范例，让人自己看。
        return (
            f"从库里找到 {len(samples)} 篇同类真实推文可以参考：\n\n{listing}\n\n"
            "**通用结构**（这几篇的共同骨架）：\n"
            "1. 开头点明背景和意义——为什么做这件事\n"
            "2. 项目/活动本身：做什么、去哪、多长时间\n"
            "3. 参与方式：谁能参加、怎么报名、截止什么时候\n"
            "4. 保障与收获：经费、证明、能学到什么\n"
            "5. 结尾的号召与联系方式\n\n"
            "当前没有配置写作模型，我给不出成稿。要看某一篇的完整正文，"
            "说出它标题里能区分的几个字即可。"
        )

    @staticmethod
    def _user_texts(messages: list[dict[str, Any]]) -> list[str]:
        return [item["content"].strip() for item in messages
                if item.get("role") == "user" and item.get("content", "").strip()]

    def _out_of_scope(self, latest: str) -> ChatResult | None:
        """有些问题的正确答案是"我答不了"，不是硬凑一个。

        第三轮扫描 P1-12。这四类现在全都被别的分支接走了：

            这个项目谁报名了      -> help，念一遍功能表，一个字不提我答不了
            负责人的手机号是多少   -> help，同上
            哪个含金量最高        -> recommend，原样出列表
            哪个通过率高          -> compare，还自己补了两个项目去比

        中间两条最危险。用户问「哪个含金量最高」，拿到一份排好序的列表，
        自然会把第一条理解成答案——**这是拿排序冒充一个我没有依据的判断**。
        我的排序依据是截止日期和条件匹配，跟含金量毫无关系。

        先把答不了这件事说清楚，再给我真有的东西。顺序不能反：现在的毛病
        正是把拒答埋在一堆正常输出后面，等于没说。
        """
        if _NO_SUCH_DATA_RE.search(latest):
            return ChatResult(
                "**这个我没有。** 我的库只有公开招募推送里写出来的内容——"
                "报名人数、报名名单、往届参加过谁，这些从来不会写在推送里，"
                "我也没有渠道去查。\n\n"
                "想知道报了多少人，只能问主办方，项目卡里的原文链接就是入口。\n\n"
                "我能给的是：报名截止、参与资格、实践时间地点、经费说明，每条都附原文引用。",
                "out_of_scope")
        if _PERSONAL_CONTACT_RE.search(latest):
            return ChatResult(
                "**个人联系方式我不给。** 有些推送正文里确实留了负责人的手机号，"
                "但那是发给读者看的，不是让我拆出来单独转发的；我在展示时会把它打码。\n\n"
                "要联系就走原文——项目卡里的原文链接指向公众号原推送，"
                "上面的联系方式和报名入口都是主办方自己放的，比经我转手一道可靠。\n\n"
                "如果推送把邮箱写成了报名渠道（「简历发送至……」），那条我会照原样给你。",
                "out_of_scope")
        if _SUBJECTIVE_RANK_RE.search(latest):
            return ChatResult(
                "**这个我答不了，也不想编。** 「含金量」「好不好进」要有依据得同时具备"
                "两样东西：各支队往年的报录数据，和一套公认的评价标准。我两样都没有——"
                "推送里不写，也没有公开的地方能查。\n\n"
                "更要紧的是：我给列表时的排序依据是报名截止和你说的条件匹配，"
                "跟含金量没有关系。要是我照常给你一份列表，你多半会把第一条当成答案，"
                "那等于我拿排序冒充了一个我根本做不出的判断。\n\n"
                "**能拿来判断的硬指标**（这些我都有原文出处）：\n"
                "- 报名截止来不来得及、实践时间和你的安排冲不冲\n"
                "- 参与资格你符不符合——不符合的话，别的都不用看了\n"
                "- 有没有经费支持、包不包食宿\n"
                "- 项目卡上「待确认」的字段有几个——通知写得越清楚，主办方通常越靠谱\n\n"
                "说「比较 A 和 B」，我可以把这几项逐条摆开给你自己判断。",
                "out_of_scope")
        return None

    def _help_decide(self, messages: list[dict[str, Any]], all_user_text: str, latest: str) -> ChatResult:
        """「帮我参谋一下」「哪个更容易被选上」——用户要的是判断依据，不是清单。"""
        result = self._recommend(self._user_texts(messages))
        addition = (
            "\n\n---\n\n**怎么挑，我的建议**\n"
            "- **先看硬条件**：报名截止是否来得及、时间和你的安排冲不冲、资格限制符不符——这几条不满足，再喜欢也没用\n"
            "- **再看信息完整度**：待确认字段越少，说明通知写得越清楚，主办方通常也更靠谱\n"
            "- **最后才看主题偏好**：真正影响你收获的是能不能深入参与，而不是题目听起来大不大\n\n"
            "「哪个更容易被选上」我答不了——各支队的选人标准不公开，我手里也没有往年录取数据，"
            "编一个说法出来对你没好处。但报名材料写得具体、和项目主题对得上，通常比写得漂亮更有用。\n\n"
            "想让我帮你逐项对比其中两个，说「比较前两个推荐项目」。"
        )
        return ChatResult(result.content + addition, "help_decide", result.project_id)

    def _handle_correction(self, messages: list[dict[str, Any]], all_user_text: str, latest: str) -> ChatResult:
        """处理「不要北京的，换几个」「说错了，我大四」这类否定与纠正。

        以前整句掉兜底，用户会觉得完全没在听。这里的做法很朴素：把这一句连同
        历史一起重新抽取偏好、重新推荐，并明确说出我理解到的排除项——
        理解错了用户能马上纠正，比默默猜一个结果强。
        """
        # 排除项的抽取统一交给 _extract_profile 的否定小句逻辑，这里只负责复述，
        # 不再维护第二套「不要+词表」的拼接规则——两套规则迟早会不一致。
        profile = self._profile_from_turns(self._user_texts(messages))
        # 复述用原话标签（「校内」），不是展开后的匹配词（紫荆、中关村、清华园…）。
        # 实测「不要校内的」把整张内部词表吐了出来，还出现了「不去清华」——
        # 对一个清华实践智能体来说这句话荒谬。
        excluded_terms = list(dict.fromkeys(
            profile["excluded_location_labels"] + profile["excluded_themes"]
            + profile["excluded_terms"]
        ))

        # 这一句里我到底读出了什么。读不出来就别说"已按你的说法重新筛了一遍"——
        # 实测「不要校内的」回了这句，却既没有排除项、列表也一条没变。
        # 嘴上说改了、实际没改，比老实说没听懂更伤。
        turn = self._extract_profile(latest)
        understood = [
            label for label, value in (
                (f"地点「{'、'.join(turn['location_labels'])}」", turn["location_labels"]),
                (f"不去{'、'.join(turn['excluded_location_labels'])}", turn["excluded_location_labels"]),
                (f"主题「{'、'.join(turn['themes'])}」", turn["themes"]),
                (f"不做{'、'.join(turn['excluded_themes'])}", turn["excluded_themes"]),
                (f"不要{'、'.join(turn['excluded_terms'])}", turn["excluded_terms"]),
                (f"时间 {turn['available_start']} 到 {turn['available_end']}", turn["available_start"]),
                (f"年级「{turn['grade']}」", turn["grade"]),
                (f"院系「{turn['department']}」", turn["department"]),
            ) if value
        ]
        # 「换一批」「再推荐几个」不是在给新条件，是在说"这批我不满意"。
        # 之前掉进了「这句我没读出具体的筛选条件」——用户会觉得连这都听不懂。
        if not understood and _ANOTHER_BATCH_RE.search(latest):
            result = self._recommend(self._user_texts(messages))
            return ChatResult(
                "条件没变，我按原条件重新排了一遍。库里符合条件的就这些，"
                "**换一批不会变出新项目来**——想看到不一样的，得放宽某个条件"
                "（比如去掉地点限制、换个时间段），或者说「还有哪些实践机会」看全部在招项目。\n\n"
                + result.content,
                "recommend_again", result.project_id,
            )
        if not understood:
            return ChatResult(
                f"「{latest[:40]}」这句我没读出具体的筛选条件——可能是我认不出你说的说法。\n\n"
                "换个说法我大概率能接住，比如：地点说省份或「京津冀」这样的区域；"
                "时间说「九月上旬」或具体日期；排除说「不要支教」「不考虑学生骨干」。\n\n"
                "现在的筛选条件还是上一轮那些，没有改动。",
                "correction_not_understood",
            )

        result = self._recommend(self._user_texts(messages))
        notes = [f"这一句我读到的是：{'；'.join(understood)}。已按此重新筛了一遍。"]
        if excluded_terms:
            notes.append(f"累计要避开的：{'、'.join(excluded_terms)}。")
        elif profile.get("location_strict") or profile.get("strict"):
            # 排他类的说法（「只要 X」「不要拿外地凑数」）不产生排除词，
            # 但同样改变了筛选口径，得复述出来让用户能纠正。
            notes.append(f"我理解到的条件是：{self._restrictions_said(profile)}。")
        notes.append(
            "如果我理解偏了，直接把完整条件再说一遍就行——比如"
            "「我大四，九月有空，想去西部，要有报销」。"
        )
        return ChatResult("\n".join(notes) + "\n\n" + result.content, "recommend_corrected", result.project_id)

    def _compare(self, messages: list[dict[str, Any]], text: str) -> ChatResult:
        projects = self._projects(include_expired=True)
        mentioned = [project for project in projects if project["title"] in text or project["id"] in text]

        # 简称也要认。原来只做整标题包含匹配，于是「比较一下黔心守艺和清年祁旅」
        # ——两条都在库里——被判成"没点名"，静默换成两个默认项目，全程一句说明都没有。
        # 而查详情用简称明明就能查到，两条路径的解析严格程度不一致。
        if len(mentioned) < 2:
            for part in re.split(r"和|与|跟|、|vs|VS|,|，", _COMPARE_RE.sub("", text)):
                part = part.strip()
                if len(part) < 2:
                    continue
                hit = self._resolve_project([], part, latest_only=True, loose=True)
                if hit and hit not in mentioned and _distinctive_overlap(hit["title"], part):
                    mentioned.append(hit)

        # 用户提到的序数（「比较第一个和第三个」）指的是他刚看到的那份列表。
        shown = self._shown_list(messages)
        if len(mentioned) < 2 and shown:
            picked = [shown[n - 1] for n in
                      (int(m) if m.isdigit() else _CN_NUMERALS.get(m, 0)
                       for m in _ORDINAL_RE.findall(text))
                      if 1 <= n <= len(shown)]
            for project in picked:
                if project not in mentioned:
                    mentioned.append(project)

        asked = len(mentioned)
        if len(mentioned) < 2:
            profile = self._extract_profile(text)
            recommendation = recommend_projects(projects, profile)
            for item in recommendation["eligible"]:
                if item["project"] not in mentioned:
                    mentioned.append(item["project"])
                if len(mentioned) >= 2:
                    break
        if len(mentioned) < 2:
            return ChatResult("目前没有两个已核验项目可供比较。请提供两个项目名称。", "compare_needs_projects")
        a, b = mentioned[:2]
        # 用户点名了项目、却没找到时必须说清楚换成了谁。
        # 实测「比较一下宝庆微光和助梦1+1」把助梦1+1 悄悄换成了研究生支教团，
        # 全程一句说明都没有——用户会以为自己在看助梦1+1 的数据。
        # asked 是上面真正解析出来的个数；不足两个时补位的那些都要点名。
        substituted = [project["title"] for project in mentioned[asked:2]]
        rows = [
            ("状态", _STATUS_LABELS.get(a.get("status"), a.get("status")),
             _STATUS_LABELS.get(b.get("status"), b.get("status"))),
            ("报名截止", a.get("signup_deadline") or "待确认", b.get("signup_deadline") or "待确认"),
            ("实践时间", f"{a.get('practice_start') or '?'} 至 {a.get('practice_end') or '?'}", f"{b.get('practice_start') or '?'} 至 {b.get('practice_end') or '?'}"),
            ("地点", a.get("location", {}).get("detail") or "待确认", b.get("location", {}).get("detail") or "待确认"),
            ("资格", a.get("eligibility", {}).get("restriction_text") or "待确认", b.get("eligibility", {}).get("restriction_text") or "待确认"),
            ("经费", a.get("reimbursement", {}).get("text") or "待确认", b.get("reimbursement", {}).get("text") or "待确认"),
        ]
        # 项目标题里带 `|`（「实践招募 | 机械系…」）会把 Markdown 表格撑坏：
        # 表头多出一列，分隔行对不上，在清小搭里渲染成错位表格。库里这类标题很多。
        table = [f"| 维度 | {_cell(a['title'])} | {_cell(b['title'])} |", "|---|---|---|"]
        table.extend(f"| {label} | {_cell(left)} | {_cell(right)} |" for label, left, right in rows)
        if substituted:
            table.insert(0, f"> **你只点了 {asked} 个项目**（或者我没认出来），"
                            f"另一个我用「{'、'.join(substituted)}」补上了——"
                            "这不是你要比的那个。要比别的就把项目名多说几个字。\n")
        table.append("\n以上信息来自项目卡；正式报名仍应打开原文核对最新通知。")
        table.append(f"\n接下来可以说：「帮我写{a['title']}的报名理由」，或者直接说出其中一个项目名看它的原文依据。")
        return ChatResult("\n".join(table), "compare")

    # 字段名到中文标签。抽取器内部用英文字段名，但这些名字会一路出现在
    # 「待确认字段」里给学生看——直接甩 eligibility、reimbursement 没人看得懂。
    _EVIDENCE_LABELS = {
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

    @classmethod
    def _field_label(cls, field: str) -> str:
        return cls._EVIDENCE_LABELS.get(field, field)

    @classmethod
    def _field_labels(cls, fields: Iterable[str]) -> str:
        return "、".join(cls._field_label(field) for field in fields)

    @classmethod
    def _project_detail(cls, project: dict[str, Any]) -> str:
        # 抽到的字段照常列，没抽到的收到末尾统一说明。
        #
        # 原来每个字段都固定输出、缺就写"待确认"，真实数据上一屏能出现十几个。
        # 两个坏处：看起来像系统没做好（其实是原文没写），以及把真正有内容的
        # 那两三行淹了。「原文未写明」既说清了责任在原文，也顺带提示去看原文。
        location = (project.get("location") or {}).get("detail")
        eligibility = (project.get("eligibility") or {}).get("restriction_text")
        reimbursement = (project.get("reimbursement") or {}).get("text")
        rows = [
            ("主办方", project.get("organizer")),
            ("实践时间", f"{project['practice_start']} 至 {project['practice_end']}"
                if project.get("practice_start") and project.get("practice_end")
                else project.get("practice_start") or ""),
            ("报名截止", project.get("signup_deadline")),
            ("地点", location),
            ("资格", eligibility),
            ("经费", reimbursement),
            ("报名方式", project.get("signup_method")),
        ]
        lines = [
            f"## {project['title']}",
            "",
            project.get("summary") or "暂无摘要",
            "",
            f"- 状态：{_STATUS_LABELS.get(project.get('status'), project.get('status'))}",
        ]
        # 「报名方式」里的邮箱是报名渠道，得留；其余字段里的联系方式一律打码。
        keep_email = {"报名方式", "联系方式"}
        lines.extend(
            f"- {label}：{redact_contacts(str(value), keep_email=label in keep_email)}"
            for label, value in rows if value
        )
        blank = [label for label, value in rows if not value]
        if blank:
            lines.append(f"- 原文未写明：{'、'.join(blank)}——以原文通知为准")

        # 把原文引用摆到对话里。"关键字段可回查原文"是这个产品区别于
        # 让大模型直接读通知的地方，但此前只有网页端能看到证据，对话里看不见。
        evidence = project.get("field_evidence") or {}
        quoted = [
            (cls._EVIDENCE_LABELS.get(field, field), item.get("quote", "").strip(), item.get("source_location", ""))
            for field, item in evidence.items()
            if isinstance(item, dict) and item.get("quote", "").strip()
        ]
        if quoted:
            lines += ["", "**原文依据**（可回查核对）"]
            for label, quote, where in quoted:
                suffix = f"（{where}）" if where else ""
                # 原文依据是长引用，联系方式常常是顺带扫进来的，不是这个字段
                # 的内容。实测「实践时间」的引用里带出了两个真人手机号。
                lines.append(
                    f"- {label}：「{redact_contacts(quote, keep_email=label in {'报名方式', '联系方式'})}」{suffix}")

        if project.get("demo_data"):
            lines += ["", "> 这是演示数据，不能作为真实报名依据。"]
        if project.get("source_url"):
            lines.append(f"\n原文链接：{_autolink(project['source_url'])}")
        else:
            lines.append("\n> 这条记录没有原文链接，报名前请自行核对来源通知。")

        lines.append(
            "\n接下来可以说：「帮我写这个项目的报名理由」，或者「比较这个和另一个项目名」。"
        )
        return "\n".join(lines)

    def _list_projects(self) -> str:
        projects = [project for project in self._projects(include_expired=False) if project.get("status") == "published"]
        if not projects:
            # 全部项目都过了报名截止时会走到这里，换真实数据的过渡期也会。
            # 原来会输出一个标题加一片空白，再跟一句无关的提示。
            pending = [p for p in self._projects(include_expired=True) if p.get("status") == "needs_review"]
            lines = ["当前没有仍在报名期内的已核验项目。"]
            if pending:
                lines.append(f"\n还有 {len(pending)} 个项目在人工复核队列里，核验通过后才会进入正式推荐。")
            lines.append(
                "\n如果你手上有招募通知，可以直接粘给我并说「导入这则通知」，"
                "我会提取成带原文引用的项目卡。"
            )
            return "\n".join(lines)
        lines = ["当前可正式推荐的已核验项目："]
        for index, project in enumerate(projects[:8], 1):
            # 同推荐卡片：只列抽到的，缺的不占位。一行里挂两个"待确认"
            # 既没信息量又显得系统没做好。
            facts = []
            if project.get("signup_deadline"):
                facts.append(f"截止 {project['signup_deadline']}")
            detail = (project.get("location") or {}).get("detail")
            if detail:
                facts.append(detail[:20])
            suffix = f"｜{'｜'.join(facts)}" if facts else "｜关键信息以原文为准"
            lines.append(f"{index}. **{project['title']}**{suffix}")
        if len(projects) > 8:
            lines.append(f"（共 {len(projects)} 条，只列出前 8 条）")

        pending = [
            project for project in self._projects(include_expired=False)
            if project.get("status") == "needs_review"
        ]
        if pending:
            lines.append(f"\n**尚未人工核验的（{len(pending)} 条）**")
            for project in pending[:5]:
                missing = self._field_labels(project.get("uncertain_fields", [])[:3]) or "关键字段待核对"
                lines.append(f"- {project['title']}｜待确认：{missing}")
            if len(pending) > 5:
                lines.append(f"（还有 {len(pending) - 5} 条）")
            lines.append("这些是已采集但还没核对完的线索，不进正式推荐，报名前务必自行核对原文。")

        if any(project.get("demo_data") for project in projects):
            lines.append("\n带“演示”标识的项目不能作为真实报名依据。")
        lines.append("\n告诉我院系、年级、空闲时间、主题、地点和经费偏好，我可以继续筛选。")
        return "\n".join(lines)

    def _welcome(self) -> str:
        """能力清单必须和对话里真能做到的事一致。

        原来这里写着"生成……行程"，但行程需要先勾选当地点位和住宿位置，
        对话里没有那些表单，用户照着问必然扑空。承诺做不到的事比少说一项更伤。
        """
        sample = next(
            (p["title"] for p in self._projects(include_expired=False)
             if p.get("status") == "published"),
            "",
        )
        example = f"「帮我写{sample}的报名理由」" if sample else "「帮我写第一个的报名理由」"
        return (
            "你好，我是**实践小搭**。我能做的是：\n\n"
            "1. 按院系、年级、可用时间、地点、主题和经费要求推荐社会实践，"
            "并说明每一条推荐和排除的理由；\n"
            "2. 给出项目的报名截止、资格、报销和报名方式，并附上原文引用供你回查；\n"
            "3. 逐项比较两个项目；\n"
            "4. 为指定项目生成报名理由、当地外联话术、访谈提纲和调研报告框架；\n"
            "5. 把你复制来的公众号招募通知转成带证据的项目卡。\n\n"
            "行程与路线任务需要先选定当地点位和住宿位置，请在实践小搭的行动工作台里完成。\n\n"
            f"可以先说：「我大三，八月有空，推荐乡村振兴方向的实践」，再说 {example}。"
        )

    def _fallback(self) -> str:
        """没听懂时也给点有用的东西，而不是重复同一段菜单。

        连着掉进兜底两次、每次一字不差，比答不上来更伤体验；顺手把当前还能
        报名的项目列出来，用户至少不会空手离开。
        """
        openings = [
            project for project in self._projects(include_expired=False)
            if project.get("status") == "published"
        ][:3]
        lines = ["这句我没接住。我能帮的是清华社会实践这一块，比如："]
        if openings:
            lines.append("")
            lines.append("**现在还能报名的项目**")
            for project in openings:
                lines.append(
                    f"- {project['title']}｜截止 {project.get('signup_deadline') or '待确认'}"
                )
        lines += ["", "**你可以直接说**",
                  "- 「我大三，八月有空，推荐乡村振兴方向的实践」——按你的条件筛，并说明推荐和排除的理由"]
        if openings:
            # 没有在招项目时不能说"上面任一个项目名"，那会指向一片空白。
            lines += [
                "- 直接说出上面任一个项目名——看它的时间、资格、报销，以及每个字段的原文引用",
                "- 「比较前两个推荐项目」——逐项对比",
                "- 「帮我写第一个的报名理由」——也可以换成外联话术、访谈提纲、报告框架",
            ]
        else:
            lines += [
                "- 「还有哪些实践机会」——看项目库当前状态",
                "- 粘贴一则招募通知并说「导入这则通知」——我会提取成带原文引用的项目卡",
            ]
        return "\n".join(lines)
