"""否定、排他、条件覆盖——用户说的限制必须真的生效。

来自 2026-08-21 的 100 轮实测。原来的抽取器完全没有否定处理，
`_recommend` 又是把所有用户消息拼成一坨去抽条件，后果：

  - 「不考虑学生骨干岗位」之后，系统只列出五个学生骨干岗位
    （「学生骨干」在拼接文本里成了正向关键词）
  - 「不要拿外地项目凑数」之后，照样返回外地项目
  - 先说京津冀、后改湖南，系统理解成"京津冀和湖南都要"
  - 「没有合适就直接说没有」之后，系统输出了整个项目库
"""
import tempfile
import unittest
from pathlib import Path

import chat_adapter
from chat_adapter import PracticeChatAdapter
from database import Database
from wechat_ingest import import_article_text

FIXTURES = [
    ("2026年秋校团委宣传部学生骨干招募", "北京"),
    ("2026年秋校团委志愿中心学生骨干招募", "北京"),
    ("宝庆微光赴湖南新宁支教实践支队招募", "湖南新宁"),
    ("赴河北保定乡村教育调研支队招募", "河北保定"),
]


class ConstraintTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tempdir = tempfile.TemporaryDirectory()
        cls.database = Database(Path(cls.tempdir.name) / "chat.db")
        for title, place in FIXTURES:
            import_article_text(
                cls.database,
                {"title": title, "source_account": "清华大学社会实践",
                 "source_url": f"https://mp.weixin.qq.com/s/{abs(hash(title)) % 10 ** 9}"},
                f"现面向全校招募队员，前往{place}开展实践。\n"
                f"报名截止：2036年9月10日\n参与资格：全校本科生\n报名方式：扫码报名\n",
            )

    @classmethod
    def tearDownClass(cls):
        cls.tempdir.cleanup()

    def setUp(self):
        chat_adapter.llm.is_enabled = lambda: False
        self.adapter = PracticeChatAdapter(self.database)

    def reply(self, *texts: str):
        return self.adapter.reply([{"role": "user", "content": t} for t in texts])

    # ── 否定 ──────────────────────────────────────────────────────────
    def _recommended_titles(self, content: str) -> list[str]:
        import chat_adapter as _ca
        return [m.group(2) for m in _ca._LISTED_RE.finditer(content.split("## 线索")[0])]

    def test_negated_term_is_actually_excluded(self):
        # 只看推荐条目。回复开头会复述「我理解你想避开：学生骨干」——那句
        # 出现这几个字是应该的，它让用户能当场纠正我理解错的地方。
        for text in ["推荐一些实践，不考虑学生骨干岗位",
                     "不考虑学生骨干岗位"]:
            with self.subTest(text=text):
                titles = self._recommended_titles(self.reply(text).content)
                self.assertTrue(titles, "一条推荐都没有，这条用例失去意义")
                for title in titles:
                    self.assertNotIn("学生骨干", title)

    def test_not_wanting_to_teach_excludes_teaching_projects(self):
        """「主要做技术支持，不讲课」实测推出来的第一条正是支教项目。

        词表里只有"支教""课程"，认不出"讲课"说的是同一件事，
        于是这句否定完全没生效。
        """
        profile = self.adapter._extract_profile("想找实践，主要做技术支持，不讲课")
        self.assertIn("教育", profile["excluded_themes"])
        titles = self._recommended_titles(self.reply("想找实践，主要做技术支持，不讲课").content)
        for title in titles:
            self.assertNotIn("支教", title)

    def test_wanting_to_teach_still_reads_as_education(self):
        # 反向不能坏：说「想去讲课」得认出教育主题。
        self.assertIn("教育", self.adapter._extract_profile("想去讲课")["themes"])

    def test_intangible_heritage_is_not_read_as_a_negation(self):
        """「非」曾经在否定词表里，于是「想做非遗相关的实践」被整句判成否定，
        把文化传承主题扔进了排除表。非遗是这个领域的常用词，不是否定。"""
        profile = self.adapter._extract_profile("想做非遗相关的实践")
        self.assertIn("文化传承", profile["themes"])
        self.assertEqual(profile["excluded_themes"], [])

    def test_affirmative_bu_phrases_are_not_negations(self):
        for text in ["不限年级都可以", "不少人推荐这个", "不仅要支教还想调研"]:
            with self.subTest(text=text):
                profile = self.adapter._extract_profile(text)
                self.assertEqual(profile["excluded_themes"], [])
                self.assertEqual(profile["excluded_terms"], [])

    def test_bare_negation_turn_is_not_mistaken_for_a_project_lookup(self):
        """「不考虑学生骨干岗位」单独成一轮时，实测被模糊匹配成项目名，
        端出五个学生骨干岗位让用户挑——跟他说的正好相反。

        原因是否定判定挂在一张单独的 NEGATION_WORDS 词表上，而那张表里
        没有「不考虑」。现在改成跟推荐用同一套否定小句逻辑，两张表不会再漂。
        """
        result = self.reply("不考虑学生骨干岗位")
        self.assertNotEqual(result.intent, "project_candidates")

    def test_negation_does_not_kill_the_positive_part_of_the_same_sentence(self):
        """「想去支教，不要学生骨干」——支教要保住，学生骨干要挡掉。"""
        profile = self.adapter._extract_profile("想去支教，不要学生骨干")
        self.assertIn("教育", profile["themes"])
        self.assertIn("学生骨干", profile["excluded_terms"])

    def test_padding_refusal_becomes_a_hard_location_filter(self):
        """「不要拿外地项目凑数」不该被抽成"排除含『项目』二字的标题"。

        第一版就是这样：excluded_terms 抽出了「项目」，那会误伤一大片。
        这句话真正的意思是把地域从偏好升级成硬条件。
        """
        profile = self.adapter._extract_profile("只要京津冀的，不要拿外地项目凑数")
        self.assertTrue(profile["location_strict"])
        self.assertNotIn("项目", profile["excluded_terms"])

    def test_structural_words_never_become_exclusions(self):
        for text in ["不要那些项目", "不看这类活动", "不考虑这些招募"]:
            with self.subTest(text=text):
                terms = self.adapter._extract_profile(text)["excluded_terms"]
                self.assertFalse(
                    set(terms) & {"项目", "活动", "招募", "实践"},
                    f"把领域结构词当成了排除条件：{terms}",
                )

    # ── 条件覆盖 ──────────────────────────────────────────────────────
    def test_later_turn_overrides_earlier_location(self):
        profile = self.adapter._profile_from_turns(["想找京津冀的实践", "算了，改成湖南的"])
        self.assertEqual(profile["location_labels"], ["湖南"])
        self.assertNotIn("北京", profile["preferred_locations"])

    def test_unmentioned_conditions_carry_over(self):
        # 第二轮只改了地点，主题不该跟着丢。
        profile = self.adapter._profile_from_turns(["想去支教", "换成河北的"])
        self.assertIn("教育", profile["themes"])
        self.assertEqual(profile["location_labels"], ["河北"])

    def test_changing_mind_cancels_an_earlier_exclusion(self):
        profile = self.adapter._profile_from_turns(["不要北京的", "算了，还是看北京吧"])
        self.assertNotIn("北京", profile["excluded_locations"])

    # ── 排他 ──────────────────────────────────────────────────────────
    def test_only_x_excludes_everything_else(self):
        content = self.reply("只要河北的实践").content
        self.assertNotIn("湖南新宁", content)

    def test_empty_result_is_stated_plainly_when_the_user_asked_for_that(self):
        """「没有合适就直接说没有」之后，实测系统输出了整个项目库。"""
        content = self.reply("只要西藏的实践，没有合适就直接说没有").content
        self.assertIn("没有完全匹配的", content)
        self.assertNotIn("宝庆微光", content)

    def test_strict_mode_does_not_offer_to_relax_on_its_own(self):
        content = self.reply("只要西藏的实践，没有合适就直接说没有").content
        self.assertIn("在你说之前我不会自己放", content)

    def test_empty_result_does_not_promise_items_it_will_not_show(self):
        """实测线上出现过前后两句打架：

            > 你提到了「西藏」：库里目前一个都没有。下面几条不在这个范围内…
            **没有完全匹配的。**你要求的是：只要西藏…

        前一句承诺「下面几条」，后一句说一条都没有。自相矛盾比答不上来更伤。
        """
        content = self.reply("只要西藏的实践，没有合适就直接说没有").content
        self.assertIn("没有完全匹配的", content)
        for promise in ("下面几条", "排在后面的"):
            self.assertNotIn(promise, content, f"空结果却承诺了「{promise}」：\n{content}")


class CarriedOverConditionTests(unittest.TestCase):
    """条件沿用是对的，但得说清楚它是沿用的。

    浏览器复测时撞上的：这一轮只说了地域和主题，回执却写「你说了 2026-09-01
    到 2026-09-30 有空」——那是几轮前说的。第一反应是"它在胡说"。我是最熟
    这套逻辑的人尚且如此，学生只会更困惑。条件本身没错，错的是措辞让人
    以为是刚说的；说清楚是沿用的，他才知道那条还挂着、可以撤。
    """

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

    def reply(self, *texts):
        return self.adapter.reply([{"role": "user", "content": t} for t in texts])

    def test_conditions_stated_this_turn_read_as_just_said(self):
        content = self.reply("我大二，九月有空，想找北京的实践").content
        self.assertIn("你说了 2026-09-01", content)
        self.assertNotIn("你之前说过 2026-09-01", content)
        self.assertNotIn("还生效着", content)

    def test_conditions_carried_from_an_earlier_turn_say_so(self):
        content = self.reply("我大二，九月有空", "想找北京的实践").content
        self.assertIn("你之前说过 2026-09-01", content)
        self.assertIn("年级「大二」（前面几轮说的，还生效着）", content)

    def test_the_condition_itself_still_applies(self):
        """措辞变了，条件不能跟着失效。"""
        content = self.reply("我大二，九月有空", "想找北京的实践").content
        self.assertIn("2026-09-01", content)
        self.assertIn("大二", content)


if __name__ == "__main__":
    unittest.main()
