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

    def test_next_step_hint_does_not_hardcode_a_project_title(self):
        """引导语既不能写死演示项目名，也不能塞完整标题。

        它曾经写死「滇西乡村教育数字化调研」，换真实数据后会指向不存在的项目；
        后来改成引用当次头名，但真实通知的标题有三四十字
        （「关于组建2026年赴湖南省湘西州花垣县开展……支队的通知」），
        照着这句说没人打得出来。现在用序号指代。
        """
        result = self.reply("我大三，八月有空，推荐实践")
        self.assertEqual(result.intent, "recommend")

        # 取「接下来可以说」那一段，而不是末尾固定长度的字符窗口。
        # 原来截 content[-220:]，而回复长度会随当天日期变化（演示数据的日期
        # 跟着当天平移，进推荐的条数就跟着变），窗口会滑进项目列表，测试
        # 于是在某些日期无缘无故地失败——测的也不是它想测的东西。
        hints = [block for block in result.content.split("\n\n") if "接下来可以说" in block]
        self.assertTrue(hints, f"回复里找不到引导语：{result.content[-300:]}")
        hint = hints[-1]

        titles = [p["title"] for p in self.adapter.db.list_projects()]
        self.assertFalse(
            any(title in hint for title in titles),
            f"引导语里塞了完整项目标题，用户打不出来：{hint}",
        )
        self.assertIn("第一个", hint)

    def test_partial_title_finds_the_project(self):
        """真实标题三四十字，学生只会说其中几个字。

        原来要求整个标题是用户输入的子串，在真实数据下这条路等于不通。
        """
        title = self.project["title"]
        fragment = title[2:8] if len(title) >= 8 else title
        result = self.reply(f"{fragment}那个项目")
        self.assertIn(result.intent, {"project_detail", "project_candidates"})
        self.assertNotEqual(result.intent, "fallback")

    def test_ambiguous_fragment_lists_candidates_instead_of_guessing(self):
        db = self.adapter.db
        base = db.get_project(self.project["id"])
        for suffix in ("甲队", "乙队"):
            clone = json.loads(json.dumps(base))
            clone["id"] = f"{base['id']}_{suffix}"
            clone["title"] = f"赴同一个地方开展同一主题调研支队{suffix}"
            db.upsert_project(clone, note="测试用重名项目", log_activity=False)
        result = self.reply("同一主题调研支队")
        self.assertEqual(result.intent, "project_candidates")
        self.assertIn("甲队", result.content)
        self.assertIn("乙队", result.content)

    def test_uncertain_fields_are_shown_in_chinese(self):
        # 抽取器内部是英文字段名，但这些名字会一路显示给学生看。
        pending = [p for p in self.adapter.db.list_projects() if p.get("uncertain_fields")]
        if not pending:
            self.skipTest("演示数据里没有带待确认字段的项目")
        content = self.reply("还有哪些实践机会").content
        for bad in ("eligibility", "reimbursement", "signup_method", "practice_dates"):
            self.assertNotIn(bad, content)

    def test_welcome_does_not_promise_itinerary_in_chat(self):
        # 行程需要先勾选点位和住宿位置，对话里做不到；承诺了就是骗用户扑空。
        content = self.reply("你能做什么").content
        self.assertIn("行动工作台", content)
        self.assertNotIn("生成报名陈述、外联话术、访谈提纲、行程", content)


class DraftPostTests(unittest.TestCase):
    """写推送文案是唯一走大模型的能力，重点守住"模型挂了不能报错"。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.adapter = PracticeChatAdapter(Database(Path(self.tmp.name) / "post.db"))
        self.project = next(
            p for p in self.adapter.db.list_projects() if p["status"] == "published"
        )
        self._enabled = chat_adapter.llm.is_enabled
        self._stream = chat_adapter.llm.stream

    def tearDown(self):
        chat_adapter.llm.is_enabled = self._enabled
        chat_adapter.llm.stream = self._stream
        self.tmp.cleanup()

    def reply(self, text):
        return self.adapter.reply([{"role": "user", "content": text}])

    def test_without_a_model_it_returns_an_outline_not_an_error(self):
        chat_adapter.llm.is_enabled = lambda: False
        result = self.reply(f"帮我写{self.project['title']}的推送文案")
        self.assertEqual(result.intent, "draft_post_fallback")
        content = result.resolve()
        self.assertIn("推送要点", content)
        # 降级也必须带上已核验的事实，用户拿到手就能改写。
        self.assertIn(self.project["signup_deadline"], content)

    def test_model_output_is_streamed_and_carries_a_disclaimer(self):
        chat_adapter.llm.is_enabled = lambda: True
        chat_adapter.llm.stream = lambda *a, **k: iter(["一起去", "滇西看看"])
        result = self.reply(f"帮我写{self.project['title']}的推送文案")
        self.assertEqual(result.intent, "draft_post")
        self.assertIsNotNone(result.stream_factory)
        content = result.resolve()
        self.assertIn("一起去滇西看看", content)
        self.assertIn("发布前请逐条核对原文通知", content)

    def test_model_failure_falls_back_instead_of_raising(self):
        chat_adapter.llm.is_enabled = lambda: True

        def boom(*_args, **_kwargs):
            raise chat_adapter.llm.LLMUnavailable("模型返回 HTTP 401")
            yield  # pragma: no cover - 让它是个生成器

        chat_adapter.llm.stream = boom
        content = self.reply(f"帮我写{self.project['title']}的推送文案").resolve()
        self.assertIn("推送要点", content)
        self.assertIn("401", content)

    def test_post_intent_wins_over_generic_write_wording(self):
        # 「帮我写推送」里的"帮我写"也在生成材料词表里，顺序错了就会去出报名表建议。
        chat_adapter.llm.is_enabled = lambda: False
        self.assertIn(
            self.reply(f"帮我写{self.project['title']}的推送").intent,
            {"draft_post", "draft_post_fallback"},
        )

    def test_asking_for_a_post_without_naming_a_project(self):
        chat_adapter.llm.is_enabled = lambda: False
        self.assertEqual(self.reply("帮我写个推送文案").intent, "draft_post_needs_project")


class IntentSafetyTests(unittest.TestCase):
    """关键词匹配最大的毛病不是接不住，而是接错。

    这些用例全部来自实测：句子里出现「实践」「想参加」「帮我写」就被抢去
    走推荐或生成，返回一整页牛头不对马嘴的内容。自信地答错比老实说不会更伤。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.adapter = PracticeChatAdapter(Database(Path(self.tmp.name) / "intent.db"))
        self._enabled = chat_adapter.llm.is_enabled
        chat_adapter.llm.is_enabled = lambda: False  # 先只验规则，不让模型参与

    def tearDown(self):
        chat_adapter.llm.is_enabled = self._enabled
        self.tmp.cleanup()

    def reply(self, text):
        return self.adapter.reply([{"role": "user", "content": text}])

    def test_writing_request_without_a_project_is_not_treated_as_search(self):
        for phrase in ("帮我改改这段话：我很想参加这个实践", "帮我写一个乡村教育的调研提纲"):
            with self.subTest(phrase=phrase):
                self.assertEqual(self.reply(phrase).intent, "writing_help")

    def test_questions_about_practice_itself_are_not_answered_with_projects(self):
        for phrase in ("参加社会实践对保研有帮助吗", "怎么组建一个支队"):
            with self.subTest(phrase=phrase):
                self.assertEqual(self.reply(phrase).intent, "about_practice")

    def test_provenance_questions_are_answered_with_real_numbers(self):
        # 这是本产品最该答好的一类问题，以前全部掉兜底。
        for phrase in ("这些信息准吗", "你怎么知道这个截止日期的", "你的数据什么时候更新的"):
            with self.subTest(phrase=phrase):
                result = self.reply(phrase)
                self.assertEqual(result.intent, "provenance")
                self.assertIn("原文", result.content)

    def test_a_passing_mention_of_a_theme_word_is_not_a_project_lookup(self):
        # 「实践」「教育」几乎每条标题里都有，不能因为提到就返回项目卡。
        for phrase in ("社会实践是个好东西", "我对教育很感兴趣"):
            with self.subTest(phrase=phrase):
                self.assertEqual(self.reply(phrase).intent, "fallback")

    def test_two_char_place_name_works_when_the_sentence_points_at_a_project(self):
        # 「滇西」「湘西」「龙岩」这类地名只有两个字，但带了指代就该认。
        self.assertNotEqual(self.reply("滇西那个项目").intent, "fallback")

    def test_model_fallback_is_optional_and_never_leaks_errors(self):
        chat_adapter.llm.is_enabled = lambda: True

        def boom(_text):
            raise chat_adapter.llm.LLMUnavailable("模型挂了")

        original = chat_adapter.llm.classify_intent
        chat_adapter.llm.classify_intent = boom
        try:
            # 模型不可用时照常掉兜底，不能把外部故障变成用户看到的报错
            self.assertEqual(self.reply("今天天气怎么样").intent, "fallback")
        finally:
            chat_adapter.llm.classify_intent = original

    def test_model_fallback_routes_long_tail_wording(self):
        # 词表接不住的说法：不含推荐/找项目/有空等任何关键词。
        phrase = "有没有那种能去山里待一阵子的机会"
        self.assertEqual(self.reply(phrase).intent, "fallback")  # 关掉模型时确实掉兜底

        chat_adapter.llm.is_enabled = lambda: True
        original = chat_adapter.llm.classify_intent
        chat_adapter.llm.classify_intent = lambda _text: "recommend"
        try:
            self.assertEqual(self.reply(phrase).intent, "recommend")
        finally:
            chat_adapter.llm.classify_intent = original


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
