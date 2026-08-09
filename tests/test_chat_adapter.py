import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

import chat_adapter
from chat_adapter import (
    ChatRequestError,
    PracticeChatAdapter,
    completion_payload,
    stream_events,
    validate_chat_request,
)
from database import Database


class ChatAdapterProtocolTests(unittest.TestCase):
    def test_model_may_be_missing_empty_or_null_and_max_tokens_is_accepted(self):
        base = {"messages": [{"role": "user", "content": "你好"}], "max_tokens": 1}
        for model_value in ("__missing__", "", None):
            payload = dict(base)
            if model_value != "__missing__":
                payload["model"] = model_value
            messages, stream, model = validate_chat_request(payload)
            self.assertEqual(messages[0]["role"], "user")
            self.assertFalse(stream)
            self.assertTrue(model)

    def test_system_user_and_assistant_roles_are_accepted(self):
        messages, _, _ = validate_chat_request({"messages": [
            {"role": "system", "content": "规则"},
            {"role": "user", "content": "问题"},
            {"role": "assistant", "content": "旧回答"},
        ]})
        self.assertEqual([item["role"] for item in messages], ["system", "user", "assistant"])

    def test_stream_must_be_a_json_boolean(self):
        with self.assertRaises(ChatRequestError):
            validate_chat_request({
                "messages": [{"role": "user", "content": "你好"}],
                "stream": "false",
            })

    def test_missing_messages_and_images_are_rejected(self):
        with self.assertRaises(ChatRequestError):
            validate_chat_request({})
        with self.assertRaises(ChatRequestError):
            validate_chat_request({"messages": [{
                "role": "user",
                "content": [{"type": "image_url", "image_url": {"url": "https://example.test/a.png"}}],
            }]})

    def test_non_streaming_shape_and_usage(self):
        messages = [{"role": "user", "content": "你好"}]
        payload = completion_payload(messages, "你好，我是实践小搭。", "practice-xiaoda")
        self.assertIsInstance(payload["choices"][0]["message"]["content"], str)
        self.assertEqual(payload["choices"][0]["finish_reason"], "stop")
        self.assertEqual(
            set(payload["usage"]),
            {"prompt_tokens", "completion_tokens", "total_tokens"},
        )

    def test_sse_frame_order_is_exact(self):
        messages = [{"role": "user", "content": "你好"}]
        events = list(stream_events(messages, "回答", "practice-xiaoda"))
        self.assertEqual(events[-1], "data: [DONE]\n\n")
        frames = [json.loads(item[len("data: "):].strip()) for item in events[:-1]]
        self.assertEqual(frames[0]["choices"][0]["delta"], {"role": "assistant"})
        role_frames = [frame for frame in frames if "role" in frame["choices"][0]["delta"]]
        stop_frames = [frame for frame in frames if frame["choices"][0]["finish_reason"] is not None]
        self.assertEqual(len(role_frames), 1)
        self.assertEqual(len(stop_frames), 1)
        stop = stop_frames[0]
        self.assertEqual(stop["choices"][0]["delta"], {})
        self.assertEqual(stop["choices"][0]["finish_reason"], "stop")
        self.assertEqual(set(stop["usage"]), {"prompt_tokens", "completion_tokens", "total_tokens"})


class ChatIntentTests(unittest.TestCase):
    """守住那些"用户照着帮助文案说话却被兜底"的说法。

    这些用例全部来自在清小搭上线前的实测：协议层一直是对的，坏的是意图匹配
    太窄——同一个意图换个常用词就掉进兜底菜单。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.adapter = PracticeChatAdapter(Database(Path(self.tmp.name) / "chat.db"))
        self.project = next(
            p for p in self.adapter.db.list_projects() if p["status"] == "published"
        )

    def tearDown(self):
        self.tmp.cleanup()

    def reply(self, text):
        return self.adapter.reply([{"role": "user", "content": text}])

    def test_generation_accepts_the_wording_used_in_the_docs(self):
        # README 和网页 UI 写的是"报名陈述"，实现里原本只认"报名理由"。
        for phrase in ("报名理由", "报名陈述", "个人陈述", "申请材料"):
            with self.subTest(phrase=phrase):
                result = self.reply(f"为{self.project['title']}生成{phrase}")
                self.assertEqual(result.intent, "generate_application")

    def test_bare_project_name_returns_the_project_card(self):
        # 兜底文案自己写着"查询项目：说出项目名称"，照做必须有结果。
        result = self.reply(self.project["title"])
        self.assertEqual(result.intent, "project_detail")
        self.assertEqual(result.project_id, self.project["id"])

    def test_open_ended_questions_do_not_hit_the_fallback(self):
        for phrase in ("哪些项目有报销？", "最近哪个项目快截止了？", "还有哪些实践机会？"):
            with self.subTest(phrase=phrase):
                self.assertNotEqual(self.reply(phrase).intent, "fallback")

    def test_short_pasted_notice_is_imported(self):
        result = self.reply(
            "导入这则通知：浙江数字乡村调研支队招募。报名截止：2026年9月20日。"
            "面向全校本科生。地点：浙江杭州。"
        )
        self.assertEqual(result.intent, "import")
        # 前缀"导入这则通知："必须被剥掉，否则它会变成项目标题。
        self.assertNotIn("这则通知", result.content)
        self.assertIn("浙江数字乡村调研支队招募", result.content)

    def test_itinerary_without_sites_explains_instead_of_erroring(self):
        # 以前这里会抛异常，清小搭那侧直接显示成"对话失败"。
        result = self.reply(f"为{self.project['title']}生成行程")
        self.assertEqual(result.intent, "generate_itinerary_needs_sites")
        self.assertIn("行动工作台", result.content)

    def test_fallback_still_offers_something_useful(self):
        result = self.reply("今天北京天气怎么样")
        self.assertEqual(result.intent, "fallback")
        self.assertIn("现在还能报名的项目", result.content)

    def test_natural_phrasing_without_the_word_recommend(self):
        # 学生实际说话很少带"推荐"两个字。
        for phrase in ("我八月有空，想去云南做实践", "大三，九月有时间，想参加乡村振兴的实践"):
            with self.subTest(phrase=phrase):
                self.assertEqual(self.reply(phrase).intent, "recommend")

    def test_project_card_shows_source_quotes(self):
        # "关键字段可回查原文"是这个产品的核心主张，对话里必须看得见。
        result = self.reply(self.project["title"])
        self.assertIn("原文依据", result.content)
        quote = self.project["field_evidence"]["signup_deadline"]["quote"]
        self.assertIn(quote, result.content)

    def test_next_step_hint_uses_a_real_project_not_a_hardcoded_one(self):
        # 这句提示曾经写死了演示项目名，换成真实数据后会指向不存在的项目。
        result = self.reply("我大三，八月有空，推荐实践")
        self.assertEqual(result.intent, "recommend")
        titles = [p["title"] for p in self.adapter.db.list_projects()]
        tail = result.content[-200:]
        self.assertTrue(
            any(title in tail for title in titles),
            f"推荐结尾的引导语没有引用任何真实项目：{tail}",
        )

    def test_welcome_does_not_promise_itinerary_in_chat(self):
        # 行程需要先勾选点位和住宿位置，对话里做不到；承诺了就是骗用户扑空。
        content = self.reply("你能做什么").content
        self.assertIn("行动工作台", content)
        self.assertNotIn("生成报名陈述、外联话术、访谈提纲、行程", content)


class MonthSpanTests(unittest.TestCase):
    """月份要跟着当前年份走。项目里已经因为写死日期栽过两次。"""

    def test_month_span_rolls_over_to_next_year_once_the_month_has_passed(self):
        real = date

        class Frozen(real):
            @classmethod
            def today(cls):
                return real(2026, 12, 20)

        original = chat_adapter.date
        chat_adapter.date = Frozen
        try:
            self.assertEqual(chat_adapter._month_span("十二月有空"), ("2026-12-01", "2026-12-31"))
            self.assertEqual(chat_adapter._month_span("八月有空"), ("2027-08-01", "2027-08-31"))
            self.assertEqual(chat_adapter._month_span("3月"), ("2027-03-01", "2027-03-31"))
            self.assertIsNone(chat_adapter._month_span("随便聊聊"))
        finally:
            chat_adapter.date = original


if __name__ == "__main__":
    unittest.main()
