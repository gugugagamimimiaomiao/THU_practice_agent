"""展示给用户的文本里不能出现个人联系方式。

2026-08-29 第三轮扫描抓到的最严重一条：问「欢迎六字班丨生命学院迎新志愿者
招募 还能报吗」，项目卡的「原文依据」区块直接吐出

    - 实践时间：「时间安排 / 8月18日晚上进行迎新培训…… / 如有问题请联系 /
      赵宗棋 13940974658 / 顾兆阳 19524895995」（导入文本）

用户问的是"还能报吗"，没问联系方式。这段手机号是长引用顺带扫进来的，
不是「实践时间」这个字段的内容。全库 51 条里 10 条中招，133 篇原文里 19 篇
含手机号。而且这是**正常查项目就会触发**的路径，不需要任何诱导。

处理原则：
  手机号、身份证  一律打码。它们绑定到具体的人，而原文链接就在旁边，
                  真要联系点原文去。
  邮箱            分情况。「报名方式」「联系方式」里的邮箱**就是报名渠道**，
                  公众号公开发布正是为了让人用，打掉等于删功能；出现在其它
                  字段的引用里时同样是顺带扫进来的，照打。

在展示层做而不是抽取层：库里已有的记录立刻就好，不用重跑抽取，也不会被
某条漏改的旧记录绕过。
"""
import tempfile
import unittest
from pathlib import Path

import chat_adapter
from chat_adapter import PracticeChatAdapter
from database import Database
from domain import project_snapshot, redact_contacts

LEAKY_QUOTE = ("时间安排 8月18日晚上进行迎新培训，8月19日上岗。"
               "如有问题请联系 赵宗棋 13940974658 顾兆阳 19524895995")


class RedactionTests(unittest.TestCase):
    def test_mobile_numbers_are_masked(self):
        out = redact_contacts(LEAKY_QUOTE)
        self.assertNotIn("13940974658", out)
        self.assertNotIn("19524895995", out)
        self.assertIn("139****4658", out)
        # 名字保留——它在公开推送里，且脱掉之后这句话就读不懂了。
        self.assertIn("赵宗棋", out)

    def test_id_cards_are_masked(self):
        self.assertNotIn("110101199003071234",
                         redact_contacts("身份证号 110101199003071234 请填写"))

    def test_signup_email_survives_because_it_is_the_channel(self):
        text = "报名请发送简历至 shetuan@mail.tsinghua.edu.cn"
        self.assertIn("shetuan@mail.tsinghua.edu.cn", redact_contacts(text, keep_email=True))

    def test_incidental_email_is_masked(self):
        text = "报名请发送简历至 shetuan@mail.tsinghua.edu.cn"
        self.assertNotIn("shetuan@mail", redact_contacts(text))

    def test_amounts_and_dates_are_untouched(self):
        """打码不能误伤金额和日期——它们也是数字串。"""
        for text in ["往返交通费全额报销，每人每天80元", "报名截止 2026-09-10",
                     "实践时间 2026-08-24 至 2026-08-31", "共 15 个名额"]:
            with self.subTest(text=text):
                self.assertEqual(redact_contacts(text), text)

    def test_landline_is_left_alone(self):
        """座机是单位电话，不是个人信息，且常常就是报名咨询方式。"""
        self.assertIn("010-62783001", redact_contacts("咨询电话 010-62783001"))


class ProjectCardPrivacyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tempdir = tempfile.TemporaryDirectory()
        cls.database = Database(Path(cls.tempdir.name) / "chat.db")
        cls.database.upsert_project({
            "id": "p-leak",
            "title": "生命学院迎新志愿者招募",
            "status": "published",
            "summary": "迎新志愿者招募",
            "source_url": "https://mp.weixin.qq.com/s/leak",
            "practice_start": "2036-08-18",
            "practice_end": "2036-08-19",
            "signup_method": "报名请发送简历至 shetuan@mail.tsinghua.edu.cn",
            "contact": "赵宗棋 13940974658",
            "eligibility": {"restriction_text": "全校本科生"},
            "reimbursement": {"has_reimbursement": None, "text": ""},
            "location": {"detail": "北京"},
            "theme_tags": ["公益志愿"],
            "uncertain_fields": [],
            "field_evidence": {"practice_dates": {"quote": LEAKY_QUOTE, "source_location": "导入文本"}},
        })

    @classmethod
    def tearDownClass(cls):
        cls.tempdir.cleanup()

    def setUp(self):
        chat_adapter.llm.is_enabled = lambda: False
        self.adapter = PracticeChatAdapter(self.database)

    def test_detail_card_never_shows_a_mobile_number(self):
        project = self.database.get_project("p-leak")
        detail = self.adapter._project_detail(project)
        self.assertNotIn("13940974658", detail)
        self.assertNotIn("19524895995", detail)

    def test_detail_card_still_shows_the_signup_email(self):
        detail = self.adapter._project_detail(self.database.get_project("p-leak"))
        self.assertIn("shetuan@mail.tsinghua.edu.cn", detail,
                      "报名邮箱被打掉了——那是报名渠道，删了等于删功能")

    def test_facts_given_to_the_model_are_redacted(self):
        """喂给模型的材料也要脱敏，否则它可能把手机号照抄进推送——
        那就是我们主动把私人号码公开发布出去。"""
        facts = self.adapter._project_facts_block(self.database.get_project("p-leak"))
        self.assertNotIn("13940974658", facts)

    def test_snapshot_used_by_templates_is_redacted(self):
        snapshot = project_snapshot(self.database.get_project("p-leak"))
        self.assertNotIn("13940974658", snapshot)


if __name__ == "__main__":
    unittest.main()
