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
    def test_negated_term_is_actually_excluded(self):
        content = self.reply("推荐一些实践，不考虑学生骨干岗位").content
        self.assertNotIn("学生骨干", content.split("## 潜在机会")[0])

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


if __name__ == "__main__":
    unittest.main()
