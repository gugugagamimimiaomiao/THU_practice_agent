"""有些问题的正确答案是"我答不了"，不是硬凑一个。

第三轮扫描 P1-12。修之前这四类分别掉进了不同的坑：

    这个项目谁报名了      -> help，念一遍功能表，一个字不提我答不了
    负责人的手机号是多少   -> help，同上
    哪个含金量最高        -> recommend，原样出列表
    哪个通过率高          -> compare，还自己补了两个项目去比

中间两条最危险。用户问「哪个含金量最高」，拿到一份排好序的列表，自然会
把第一条当成答案——而排序依据是报名截止和条件匹配，跟含金量毫无关系。
这是拿排序冒充一个没有依据的判断。

同样要守住的是不能滥拒：「怎么报名」「招多少人」「有多少个名额」问的是
推送里写了的东西，照答。
"""
import tempfile
import unittest
from pathlib import Path

import chat_adapter
from chat_adapter import PracticeChatAdapter
from database import Database


class OutOfScopeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tempdir = tempfile.TemporaryDirectory()
        cls.database = Database(Path(cls.tempdir.name) / "chat.db")
        for index in range(2):
            cls.database.upsert_project({
                "id": f"p-{index}",
                "title": f"乡村振兴暑期实践支队招募（第{index + 1}批）",
                "status": "published",
                "summary": "暑期实践招募",
                "source_url": f"https://mp.weixin.qq.com/s/x{index}",
                "practice_start": "2036-07-01",
                "practice_end": "2036-07-10",
                "signup_deadline": "2036-06-20",
                "signup_method": "原文二维码报名",
                "eligibility": {"restriction_text": "全校本科生"},
                "reimbursement": {"has_reimbursement": None, "text": ""},
                "location": {"province": "浙江", "detail": "浙江"},
                "theme_tags": ["乡村振兴"],
                "uncertain_fields": [],
                "field_evidence": {},
            })

    @classmethod
    def tearDownClass(cls):
        cls.tempdir.cleanup()

    def setUp(self):
        chat_adapter.llm.is_enabled = lambda: False
        self.adapter = PracticeChatAdapter(self.database)

    def _reply(self, text):
        return self.adapter.reply([{"role": "user", "content": text}])

    def test_signup_counts_and_rosters_are_refused(self):
        for text in ["这个项目谁报名了", "已经有多少人报名了", "报名人数是多少",
                     "去年参加的都是哪些人", "往届入选名单有吗"]:
            with self.subTest(text=text):
                self.assertEqual(self._reply(text).intent, "out_of_scope")

    def test_personal_contacts_are_refused(self):
        for text in ["负责人的手机号是多少", "把带队老师的联系方式给我", "队长微信多少"]:
            with self.subTest(text=text):
                self.assertEqual(self._reply(text).intent, "out_of_scope")

    def test_subjective_rankings_are_refused(self):
        for text in ["哪个含金量最高", "哪个最好进", "哪个通过率高",
                     "这几个哪个最水", "报名竞争激烈吗", "性价比最高的是哪个"]:
            with self.subTest(text=text):
                self.assertEqual(self._reply(text).intent, "out_of_scope")

    def test_a_ranked_list_is_never_offered_as_the_answer(self):
        """最要紧的一条：拒答时不能顺手再给一份列表。

        用户问的是"哪个含金量最高"，只要下面跟着一份排好序的清单，
        他就会把第一条当答案——拒答那句话等于白说。
        """
        content = self._reply("哪个含金量最高").content
        self.assertNotIn("正式推荐", content)
        self.assertNotIn("乡村振兴暑期实践支队招募", content)

    def test_the_refusal_comes_first(self):
        """拒答必须在开头。埋在一堆正常输出后面就等于没说。"""
        for text in ["这个项目谁报名了", "负责人的手机号是多少", "哪个含金量最高"]:
            with self.subTest(text=text):
                head = self._reply(text).content.strip().split("\n")[0]
                self.assertTrue(
                    any(word in head for word in ("我没有", "我不给", "我答不了")),
                    f"第一行没有把答不了说清楚：{head}")

    # ---- 不能滥拒 ----

    def test_questions_the_source_article_answers_are_not_refused(self):
        """名额、报名方式、截止日期，推送里都写了，照答。"""
        for text in ["怎么报名", "报名截止是哪天", "报名方式是什么",
                     "招多少人", "有多少个名额", "多少人能参加", "这个项目招几个人"]:
            with self.subTest(text=text):
                self.assertNotEqual(self._reply(text).intent, "out_of_scope")

    def test_normal_requests_are_untouched(self):
        for text in ["推荐几个能报名的", "有没有管吃管住的", "比较前两个项目",
                     "为什么推荐这几个", "参加社会实践对保研有用吗"]:
            with self.subTest(text=text):
                self.assertNotEqual(self._reply(text).intent, "out_of_scope")


if __name__ == "__main__":
    unittest.main()
