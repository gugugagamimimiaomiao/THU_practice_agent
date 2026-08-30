"""学生贴一段自我介绍求推荐——真实使用里最常见的输入形态。

实测一段 130 字的自述，系统把用户的意思读反了两处：

    做过一年的**校内**志愿服务          -> 地点偏好「校内」
    不是走马观花的**实践项目**          -> 排除词「实践项目」
    平时**比较**喜欢和人打交道          -> 路由成 compare，还自己补了两个项目去比

他想深入基层，我们推校内的；他想要实践，我们理解成他不要实践。

三个根因各不相同，所以分了三组测试：
  一、过去经历被当成诉求（做过/参加过/以前/去年…）
  二、否定小句里挖出来的是通用词残片（「实践项目」「实践项」「践项」）
  三、「比较」的副词用法被当成动词用法
"""
import tempfile
import unittest
from pathlib import Path

import chat_adapter
from chat_adapter import PracticeChatAdapter, _COMPARE_RE, _month_span
from database import Database

LONG = ("我是清华大学电子系大二的学生，平时比较喜欢和人打交道，做过一年的"
        "校内志愿服务，也参加过一次短期的乡村调研。这个暑假想找一个能真正"
        "深入基层、不是走马观花的实践项目，最好能待上一周以上。我对教育和"
        "乡村振兴这两个方向都挺感兴趣的，如果有经费支持就更好了。")


class LongSelfDescriptionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tempdir = tempfile.TemporaryDirectory()
        cls.database = Database(Path(cls.tempdir.name) / "chat.db")

    @classmethod
    def tearDownClass(cls):
        cls.tempdir.cleanup()

    def setUp(self):
        chat_adapter.llm.is_enabled = lambda: False
        self.adapter = PracticeChatAdapter(self.database)

    # ---- 一、过去经历不是诉求 ----

    def test_past_experience_is_not_a_location_preference(self):
        """「做过一年的校内志愿服务」是他做过什么，不是他想去哪。"""
        profile = self.adapter._extract_profile(LONG)
        self.assertEqual(profile["preferred_locations"], [])

    def test_experience_clauses_do_not_swallow_the_actual_request(self):
        """剥掉经历句不能把后面真正的诉求一起剥掉。"""
        profile = self.adapter._extract_profile(LONG)
        self.assertEqual(profile["grade"], "大二")
        self.assertEqual(profile["department"], "电子系")
        self.assertIn("乡村振兴", profile["themes"])
        self.assertEqual(profile["reimbursement_preference"], "preferred")

    def test_a_plain_request_is_untouched(self):
        for text, place in (("我想去云南支教", "云南"), ("我是大二的，想找北京的实践", "北京")):
            with self.subTest(text=text):
                self.assertIn(place, self.adapter._extract_profile(text)["preferred_locations"])

    # ---- 二、否定小句里挖出来的不能是通用词残片 ----

    def test_a_negated_modifier_does_not_exclude_the_noun(self):
        """「不是走马观花的实践项目」说的是不要"走马观花"，不是不要"实践项目"。
        他想要实践，读成他不要实践，正好相反。"""
        self.assertEqual(self.adapter._extract_profile(LONG)["excluded_terms"], [])

    def test_generic_compounds_are_recognised(self):
        for token in ("实践项目", "实践活动", "项目实践", "招募活动"):
            with self.subTest(token=token):
                self.assertTrue(PracticeChatAdapter._all_stopwords(token))

    def test_meaningful_terms_survive(self):
        for token in ("学生骨干", "支教", "宝庆微光", "乡村振兴", "校内"):
            with self.subTest(token=token):
                self.assertFalse(PracticeChatAdapter._all_stopwords(token))

    def test_a_real_exclusion_still_works(self):
        """收紧通用词判定不能把真正的排除项也挡掉。

        第一版这条用了「不考虑学生骨干岗位」——测试库里根本没有带「学生骨干」
        的标题，而 _terms_worth_excluding 要求这个词真能命中标题，所以它必然
        返回空，跟改动无关。改用库里确实存在的词，并先断言这个前提。
        """
        titles = [p["title"] for p in self.adapter._projects(include_expired=True)]
        word = next((w for w in ("非遗", "生态", "医疗")
                     if any(w in t for t in titles)), "")
        self.assertTrue(word, "测试库里一个可排除的词都没有，这条测试是空的")
        profile = self.adapter._extract_profile(f"推荐几个实践，但不要{word}的")
        self.assertTrue(any(word in term for term in profile["excluded_terms"]),
                        f"「{word}」没能进排除表：{profile['excluded_terms']}")

    # ---- 三、「比较」的副词用法 ----

    def test_comparing_as_an_adverb_is_not_a_compare_request(self):
        for text in ("平时比较喜欢和人打交道", "比较好", "比较适合我", "我比较想去西部"):
            with self.subTest(text=text):
                self.assertFalse(_COMPARE_RE.search(text))

    def test_comparing_as_a_verb_still_is(self):
        for text in ("比较前两个", "比较这两个项目", "对比一下"):
            with self.subTest(text=text):
                self.assertTrue(_COMPARE_RE.search(text))

    def test_the_whole_paragraph_routes_to_a_recommendation(self):
        result = self.adapter.reply([{"role": "user", "content": LONG}])
        self.assertIn(result.intent, {"recommend", "recommend_corrected"})


class SpokenTimeSpanTests(unittest.TestCase):
    """「八月底九月初」被读成整个八月——一个月的窗口跟什么都不冲突，
    等于时间这个条件白说。"""

    def test_a_cross_month_span_is_not_widened_to_a_whole_month(self):
        span = _month_span("八月底九月初有空")
        self.assertIsNotNone(span)
        start, end = span
        self.assertTrue(start.endswith("-08-21"), start)
        self.assertTrue(end.endswith("-09-10"), end)

    def test_colloquial_thirds_of_a_month(self):
        for text, tail in (("九月上旬", ("-09-01", "-09-10")),
                           ("9月月底", ("-09-21", "-09-30"))):
            with self.subTest(text=text):
                start, end = _month_span(text)
                self.assertTrue(start.endswith(tail[0]), start)
                self.assertTrue(end.endswith(tail[1]), end)

    def test_a_span_that_is_still_running_is_this_year_not_next(self):
        """年份看**结束**那天。今天是 8 月底时说「八月底九月初」指的是现在，
        按开始日（已过）判年会推到明年，把正在发生的窗口算成一年之后。"""
        from datetime import date
        start, end = _month_span("八月底九月初")
        self.assertGreaterEqual(date.fromisoformat(end), date.today())

    def test_a_whole_month_is_still_a_whole_month(self):
        start, end = _month_span("八月")
        self.assertTrue(start.endswith("-08-01"))
        self.assertTrue(end.endswith("-08-31"))


if __name__ == "__main__":
    unittest.main()
