"""OpenAI-compatible conversation adapter for 清小搭.

The adapter is deliberately deterministic: eligibility, deadlines and recommendation
filters reuse the same audited domain rules as the web MVP. Conversation state is carried
by the incoming ``messages`` array, so multiple workers do not require sticky sessions.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any, Iterable

from database import Database
from domain import (
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


def completion_payload(messages: list[dict[str, Any]], response: str, model: str, *, completion_id: str | None = None) -> dict[str, Any]:
    completion_id = completion_id or f"chatcmpl-pxd-{uuid.uuid4().hex[:20]}"
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": response}, "finish_reason": "stop"}],
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


def stream_events(messages: list[dict[str, Any]], response: str, model: str) -> Iterable[str]:
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
    for chunk in _chunks(response):
        yield event({"content": chunk}, None)
    yield event({}, "stop", usage_for(messages, response))
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
)
DETAIL_WORDS = ("详情", "介绍", "资格", "截止", "报销", "地点", "时间", "这个项目", "怎么样", "什么条件")
LIST_WORDS = (
    "项目列表", "全部项目", "近期项目", "实践机会", "有哪些", "还有哪些",
    "快截止", "最近截止", "都有什么",
)


class PracticeChatAdapter:
    def __init__(self, database: Database) -> None:
        self.db = database

    def reply(self, messages: list[dict[str, Any]]) -> ChatResult:
        user_messages = [item["content"].strip() for item in messages if item["role"] == "user" and item["content"].strip()]
        latest = user_messages[-1]
        all_user_text = "\n".join(user_messages)
        normalized = latest.lower()

        if self._is_import(latest):
            return self._import_notice(latest)
        if any(word in latest for word in GENERATE_WORDS):
            return self._generate(messages, all_user_text, latest)
        if any(word in latest for word in ["比较", "对比", "哪个好", "区别", "选哪个"]):
            return self._compare(messages, all_user_text)
        if any(word in latest for word in RECOMMEND_WORDS):
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
        return ChatResult(self._fallback(), "fallback")

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
        missing = "、".join(project.get("uncertain_fields", [])) or "无"
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
        elif "八月" in text or "8月" in text:
            profile["available_start"], profile["available_end"] = "2026-08-01", "2026-08-31"
        elif "七月" in text or "7月" in text:
            profile["available_start"], profile["available_end"] = "2026-07-01", "2026-07-31"
        return profile

    def _recommend(self, text: str) -> ChatResult:
        profile = self._extract_profile(text)
        result = recommend_projects(self.db.list_projects(include_expired=True), profile)
        lines = ["## 正式推荐"]
        if not result["eligible"]:
            lines.append("暂时没有同时满足硬条件且已核验的项目。")
        for index, item in enumerate(result["eligible"][:5], 1):
            project = item["project"]
            reasons = "；".join(item["reasons"][:3]) or "信息完整度较高"
            lines.extend([
                f"{index}. **{project['title']}**（匹配度 {round(item['score'])}）",
                f"   - 截止：{project.get('signup_deadline') or '待确认'}；地点：{project.get('location', {}).get('detail') or '待确认'}",
                f"   - 理由：{reasons}",
                f"   - 项目 ID：`{project['id']}`",
            ])
        if result["potential"]:
            lines.append("\n## 潜在机会（需先复核）")
            for item in result["potential"][:3]:
                project = item["project"]
                warnings = "；".join(item["warnings"][:2])
                lines.append(f"- **{project['title']}**：{warnings}")
        if result["excluded"]:
            lines.append(f"\n另有 {len(result['excluded'])} 个项目因截止、时间、资格或经费硬条件被排除。")
        lines.append("\n你可以继续说“比较前两个项目”或“为滇西乡村教育数字化调研生成报名理由”。")
        return ChatResult("\n".join(lines), "recommend")

    def _resolve_project(
        self, messages: list[dict[str, Any]], latest: str, *, latest_only: bool = False
    ) -> dict[str, Any] | None:
        projects = self.db.list_projects(include_expired=True)
        direct = [project for project in projects if project["title"] in latest or project["id"] in latest]
        if direct:
            return direct[0]
        if latest_only:
            return None
        conversation = "\n".join(item["content"] for item in messages)
        mentioned = [project for project in projects if project["title"] in conversation or project["id"] in conversation]
        if mentioned:
            if any(word in latest for word in ["第二个", "第2个"]) and len(mentioned) >= 2:
                return mentioned[1]
            return mentioned[0]
        return None

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
        project = self._resolve_project(messages, latest)
        if not project:
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
        return ChatResult(result["content"] + warnings, f"generate_{kind}", project["id"])

    def _compare(self, messages: list[dict[str, Any]], text: str) -> ChatResult:
        projects = self.db.list_projects(include_expired=True)
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
        return ChatResult("\n".join(table), "compare")

    @staticmethod
    def _project_detail(project: dict[str, Any]) -> str:
        return (
            f"## {project['title']}\n\n"
            f"{project.get('summary') or '暂无摘要'}\n\n"
            f"- 状态：{project.get('status')}\n"
            f"- 主办方：{project.get('organizer') or '待确认'}\n"
            f"- 实践时间：{project.get('practice_start') or '待确认'} 至 {project.get('practice_end') or '待确认'}\n"
            f"- 报名截止：{project.get('signup_deadline') or '待确认'}\n"
            f"- 地点：{project.get('location', {}).get('detail') or '待确认'}\n"
            f"- 资格：{project.get('eligibility', {}).get('restriction_text') or '待确认'}\n"
            f"- 经费：{project.get('reimbursement', {}).get('text') or '待确认'}\n"
            f"- 报名方式：{project.get('signup_method') or '待确认'}\n"
            f"- 待确认字段：{'、'.join(project.get('uncertain_fields', [])) or '无'}"
        )

    def _list_projects(self) -> str:
        projects = [project for project in self.db.list_projects(include_expired=False) if project.get("status") == "published"]
        lines = ["当前可正式推荐的已核验项目："]
        for index, project in enumerate(projects[:8], 1):
            lines.append(f"{index}. **{project['title']}**｜截止 {project.get('signup_deadline') or '待确认'}｜{project.get('location', {}).get('detail') or '地点待确认'}")
        lines.append("\n演示项目均有明确标识，不能作为真实报名依据。告诉我院系、年级、空闲时间、主题、地点和经费偏好，我可以继续筛选。")
        return "\n".join(lines)

    @staticmethod
    def _welcome() -> str:
        return (
            "你好，我是**实践小搭**。我可以帮助你：\n\n"
            "1. 按院系、年级、时间、地点、主题和经费要求推荐社会实践；\n"
            "2. 核对报名截止、参与资格和报销信息；\n"
            "3. 比较多个项目；\n"
            "4. 生成报名陈述、外联话术、访谈提纲、行程和报告框架；\n"
            "5. 把复制的公众号通知转换为带证据的项目卡。\n\n"
            "你可以说：‘我是社科学院大三学生，八月有空，推荐有报销的乡村教育实践。’"
        )

    def _fallback(self) -> str:
        """没听懂时也给点有用的东西，而不是重复同一段菜单。

        连着掉进兜底两次、每次一字不差，比答不上来更伤体验；顺手把当前还能
        报名的项目列出来，用户至少不会空手离开。
        """
        openings = [
            project for project in self.db.list_projects(include_expired=False)
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
        lines.extend([
            "",
            "**你可以直接说**",
            "- 「我大三，八月有空，推荐乡村振兴方向的实践」——按你的条件筛，并说明推荐和排除的理由",
            "- 直接说出上面任一个项目名——看它的时间、资格、报销和报名方式",
            "- 「比较前两个推荐项目」——逐项对比",
            "- 「帮我写第一个的报名理由」——生成报名材料填写建议，也可以换成外联话术、访谈提纲、报告框架",
        ])
        return "\n".join(lines)
