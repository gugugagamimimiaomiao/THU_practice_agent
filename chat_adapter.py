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
    extract_project,
    generate_asset,
    recommend_projects,
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
GENERATE_WORDS = (
    "访谈", "外联", "联系话术", "沟通话术", "行程", "日程",
    "报告框架", "报告大纲", "调研报告",
    "报名理由", "申请理由", "报名材料", "申请材料", "个人陈述",
    "报名陈述", "自荐", "报名文书", "怎么写", "帮我写",
)
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

# 「比较」当副词用时不是要对比项目：「比较多」「比较难」「比较早」。
# 「哪些主题的实践比较多」实测被这个词抢走，变成了两个项目的对比表格。
_COMPARE_RE = re.compile(r"比较(?![多少好难易大小早晚快慢久短高低远近贵便])|对比|哪个好|区别|选哪个")

# 起名/拟标题。用正则而不是硬列词组：「帮我想个推送标题」里"想"和"标题"之间
# 隔着"个推送"，靠穷举字符串永远补不全。
_NAMING_RE = re.compile(r"(想|起|取|拟|来)[几个一二三四五六七八九十\s]*(个|些)?[^，。？！]{0,6}(名字|名称|标题|队名)")

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


def _month_span(text: str) -> tuple[str, str] | None:
    """把「八月」「8月」解析成一整个月的起止日期。

    这里原本写死成 2026-07/2026-08 两个月份。写死日期在这个项目里已经咬过
    两次（演示数据的截止日、推荐页默认可用时间），所以改成按当前年份推算，
    并且覆盖全部 12 个月：说到的月份如果今年已经整月过完，就理解为明年的该月。
    """
    month = None
    match = re.search(r"(?<!\d)(1[0-2]|[1-9])\s*月", text)
    if match:
        month = int(match.group(1))
    else:
        for name in sorted(_CN_MONTHS, key=len, reverse=True):
            if f"{name}月" in text:
                month = _CN_MONTHS[name]
                break
    if not month:
        return None
    today = date.today()
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
        if self._is_polish_request(latest):
            return self._polish_text(latest)
        # 对已采集数据的统计性提问。同样要排在项目匹配之前——「实践招募一般
        # 什么时候发布」里的"实践招募"会模糊命中一堆标题，变成项目候选列表。
        if any(word in latest for word in CORPUS_STATS_HINTS) and not self._mentions_project_exactly(latest):
            return self._corpus_stats(latest)
        # 「招募推送一般怎么写」问的是写法，不是要给某个项目出成稿。这类必须在
        # POST_WORDS / GENERATE_WORDS 之前拦下——否则"推送""帮我写"会先把它抢走。
        if (any(word in latest for word in WRITING_HELP_WORDS + GENERIC_WRITING_HINTS)
                or _NAMING_RE.search(latest)) and not self._mentions_project_exactly(latest):
            return self._writing_help(latest)
        if any(word in latest for word in NEGATION_WORDS):
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
        if any(word in latest for word in RECOMMEND_WORDS):
            return self._recommend(all_user_text)
        # 条件筛选：「有没有校内的志愿服务」「只看志愿服务」。必须排在项目匹配
        # 之前——否则句子里的"志愿服务"会模糊命中某个标题，变成查那一个项目。
        # 但用户完整点名某个项目时（「宝庆微光有吗」）仍然按查详情处理。
        if any(word in latest for word in FILTER_WORDS) and not self._mentions_project_exactly(latest):
            return self._recommend(all_user_text)
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
            return self._recommend(all_user_text)
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

    def _extract_profile(self, text: str) -> dict[str, Any]:
        profile: dict[str, Any] = {
            "department": "",
            "grade": "",
            "available_start": "",
            "available_end": "",
            "themes": [],
            "preferred_locations": [],
            "reimbursement_preference": "not_important",
        }
        profile["department"] = next((item for item in KNOWN_DEPARTMENTS if item in text), "")
        profile["grade"] = next((item for item in GRADE_TERMS if item in text), "")
        profile["themes"] = [theme for theme, words in THEME_KEYWORDS.items() if theme in text or any(word.lower() in text.lower() for word in words)]
        profile["preferred_locations"] = [item for item in KNOWN_LOCATIONS if item in text]
        if any(word in text for word in ["必须报销", "必须有报销", "必须有补贴", "只要有报销", "经费必须"]):
            profile["reimbursement_preference"] = "required"
        elif any(word in text for word in ["优先报销", "优先有补贴", "最好有报销", "偏好报销"]):
            profile["reimbursement_preference"] = "preferred"
        iso_dates = re.findall(r"20\d{2}-\d{1,2}-\d{1,2}", text)
        if len(iso_dates) >= 2:
            profile["available_start"], profile["available_end"] = iso_dates[:2]
        else:
            span = _month_span(text)
            if span:
                profile["available_start"], profile["available_end"] = span
        return profile

    def _recommend(self, text: str) -> ChatResult:
        profile = self._extract_profile(text)
        result = recommend_projects(self._projects(include_expired=True), profile)
        lines = ["## 正式推荐"]
        if not result["eligible"]:
            # 空结果最容易发生在换了真实数据、或全部项目都过了截止的时候。
            # 与其只说一句"没有"，不如说清楚是被什么条件挡住的、下一步怎么放宽。
            blockers: list[str] = []
            for item in result["excluded"][:6]:
                blockers.extend(item.get("excluded_reasons", []))
            top = "；".join(dict.fromkeys(blockers))[:120]
            lines.append("按你给的条件，暂时没有同时满足硬条件且已核验的项目。")
            if top:
                lines.append(f"\n主要卡在：{top}")
            lines.append(
                "\n可以试着放宽一个条件——比如换个时间段、去掉地点限制，"
                "或者说「还有哪些实践机会」看全部在招项目。"
            )
        for index, item in enumerate(result["eligible"][:5], 1):
            lines.extend(self._recommendation_card(index, item))
        if result["potential"]:
            lines.append("\n## 潜在机会（需先复核）")
            for item in result["potential"][:3]:
                project = item["project"]
                warnings = "；".join(item["warnings"][:2])
                lines.append(f"- **{project['title']}**：{warnings}")
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
            hint = "「帮我写第一个的报名理由」"
            if len(result["eligible"]) >= 2:
                hint = f"「比较前两个推荐项目」，或者{hint}"
            lines.append(
                f"\n接下来可以说：{hint}。"
                "想看某一个的详情和原文引用，说出标题里能区分的那几个字就行——地名或主题都可以。"
            )
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

    def _resolve_project(
        self, messages: list[dict[str, Any]], latest: str, *,
        latest_only: bool = False, loose: bool | None = None,
    ) -> dict[str, Any] | None:
        projects = self._projects(include_expired=True)
        direct = [project for project in projects if project["title"] in latest or project["id"] in latest]
        if direct:
            return direct[0]

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
        conversation = "\n".join(item["content"] for item in messages)
        mentioned = [project for project in projects if project["title"] in conversation or project["id"] in conversation]
        if mentioned:
            if any(word in latest for word in ["第二个", "第2个"]) and len(mentioned) >= 2:
                return mentioned[1]
            return mentioned[0]
        return None

    def _pick_one_of(self, candidates: list[dict[str, Any]], purpose: str) -> str:
        """有多个同样像的项目时，把候选摆出来让用户挑，而不是替他猜一个。"""
        lines = [f"有 {len(candidates)} 个项目都对得上，你要{purpose}哪一个？", ""]
        for project in candidates:
            deadline = project.get("signup_deadline") or "截止待确认"
            lines.append(f"- **{project['title']}**（{deadline}）")
        lines.append("\n把标题里能区分的那几个字告诉我就行，比如地名或主题。")
        return "\n".join(lines)

    def _generate(self, messages: list[dict[str, Any]], all_user_text: str, latest: str) -> ChatResult:
        kind = "application"
        if any(word in latest for word in ["外联", "联系话术", "沟通话术"]):
            kind = "outreach"
        elif "访谈" in latest:
            kind = "interview"
        elif any(word in latest for word in ["行程", "日程"]):
            kind = "itinerary"
        elif any(word in latest for word in ["报告框架", "报告大纲"]):
            kind = "report"
        project = self._resolve_project(messages, latest, loose=True)
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
        return ChatResult(result["content"] + warnings + hint, f"generate_{kind}", project["id"])

    _POST_SYSTEM_PROMPT = (
        "你在帮清华大学的学生给一次社会实践招募写公众号推送文案。\n"
        "硬性要求：\n"
        "1. 只使用【项目事实】里给出的信息。时间、地点、报名截止、参与资格、"
        "报销条件这些一个字都不能改，也不能补充里面没有的内容。\n"
        "2. 【项目事实】里标着「待确认」的字段，就在文中写成待定并提示读者以原文通知为准，"
        "绝对不要为了通顺而编一个具体值。\n"
        "3. 不要编造带队老师姓名、往届成果、报名人数、获奖情况这类没给你的细节。\n"
        "4. 面向清华在校生，语气真诚具体，别用空泛的口号堆砌。\n"
        "输出结构：一个标题（15 字以内）、一段引入、项目要点、报名方式与截止提醒。"
        "用 Markdown，总长 400-700 字。"
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
        user_prompt = f"【项目事实】\n{facts}\n\n请据此写推送文案。"

        def produce() -> Iterable[str]:
            yield header
            produced_any = False
            try:
                for piece in llm.stream(self._POST_SYSTEM_PROMPT, user_prompt):
                    produced_any = True
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
            f"报名方式：{project.get('signup_method') or '待确认'}",
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

        latest_import = ""
        for item in self.db.recent_activity(limit=20):
            if item.get("event_type") in {"ingest", "seed", "collector"}:
                latest_import = item.get("created_at", "")[:16].replace("T", " ")
                break

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

        lines = [f"{index}. **{project['title']}**（匹配度 {round(item['score'])}）"]
        if facts:
            lines.append(f"   - {'；'.join(facts)}")
        missing = [FIELD_LABELS.get(name, name) for name in project.get("uncertain_fields", [])
                   if name != "source_url"]
        if missing:
            lines.append(f"   - 原文未写明：{'、'.join(missing[:4])}——以原文为准")
        if project.get("source_url"):
            lines.append(f"   - 原文：{project['source_url']}")
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
        samples = corpus.search(draft[:200], genre=genre, limit=2) or corpus.search(draft[:200], limit=2)

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

        tail = ""
        if samples:
            tail = ("\n\n---\n\n参照了库里这几篇真实推文的写法："
                    + "、".join(f"《{s.title[:26]}》" for s in samples)
                    + "。里面的日期地点属于那些项目本身，没有被写进你的稿子。")
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
        samples = corpus.search(question, genre=genre, limit=3) or corpus.search(question, limit=3)

        if not samples:
            return (
                "我这边的语料库里还没有同类的真实推文可以参考——目前收录的都是"
                f"{'、'.join(corpus.genres()) or '（暂无）'}这几类。\n\n"
                "你可以把想模仿的那篇推文正文贴给我，我照着它的结构和语气来写；"
                "或者说出一个具体项目名，我按项目卡里的已核验信息给你出材料。"
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
                    return (
                        f"{body.strip()}\n\n---\n\n"
                        f"上面的写法建议参考了这几篇真实推文：\n{listing}\n\n"
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

    def _help_decide(self, messages: list[dict[str, Any]], all_user_text: str, latest: str) -> ChatResult:
        """「帮我参谋一下」「哪个更容易被选上」——用户要的是判断依据，不是清单。"""
        result = self._recommend(all_user_text)
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
        excluded_terms = []
        for term in KNOWN_LOCATIONS + list(THEME_KEYWORDS.keys()):
            for marker in ("不要", "不想", "别推荐", "除了"):
                if f"{marker}{term}" in latest:
                    excluded_terms.append(term)
                    break

        result = self._recommend(all_user_text)
        notes = ["已按你最新的说法重新筛了一遍。"]
        if excluded_terms:
            notes.append(f"我理解你想避开：{'、'.join(dict.fromkeys(excluded_terms))}。")
        notes.append(
            "如果我理解偏了，直接把完整条件再说一遍就行——比如"
            "「我大四，九月有空，想去西部，要有报销」。"
        )
        return ChatResult("\n".join(notes) + "\n\n" + result.content, "recommend_corrected", result.project_id)

    def _compare(self, messages: list[dict[str, Any]], text: str) -> ChatResult:
        projects = self._projects(include_expired=True)
        mentioned = [project for project in projects if project["title"] in text or project["id"] in text]
        if len(mentioned) < 2:
            profile = self._extract_profile(text)
            recommendation = recommend_projects(projects, profile)
            mentioned = [item["project"] for item in recommendation["eligible"][:2]]
        if len(mentioned) < 2:
            return ChatResult("目前没有两个已核验项目可供比较。请提供两个项目名称。", "compare_needs_projects")
        a, b = mentioned[:2]
        rows = [
            ("审核状态", a.get("status"), b.get("status")),
            ("报名截止", a.get("signup_deadline") or "待确认", b.get("signup_deadline") or "待确认"),
            ("实践时间", f"{a.get('practice_start') or '?'} 至 {a.get('practice_end') or '?'}", f"{b.get('practice_start') or '?'} 至 {b.get('practice_end') or '?'}"),
            ("地点", a.get("location", {}).get("detail") or "待确认", b.get("location", {}).get("detail") or "待确认"),
            ("资格", a.get("eligibility", {}).get("restriction_text") or "待确认", b.get("eligibility", {}).get("restriction_text") or "待确认"),
            ("经费", a.get("reimbursement", {}).get("text") or "待确认", b.get("reimbursement", {}).get("text") or "待确认"),
        ]
        table = [f"| 维度 | {a['title']} | {b['title']} |", "|---|---|---|"]
        table.extend(f"| {label} | {left} | {right} |" for label, left, right in rows)
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
            f"- 状态：{project.get('status')}",
        ]
        lines.extend(f"- {label}：{value}" for label, value in rows if value)
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
                lines.append(f"- {label}：「{quote}」{suffix}")

        if project.get("demo_data"):
            lines += ["", "> 这是演示数据，不能作为真实报名依据。"]
        if project.get("source_url"):
            lines.append(f"\n原文链接：{project['source_url']}")
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
