"""功能二（语料库写作辅助）的路由与边界。

这批用例全部来自线上实测：拿真实问法打服务，把答非所问的挑出来固化成测试。
所以每条都对应一次真实的失败，不是想象出来的场景。
"""
import tempfile
import unittest
from pathlib import Path

import chat_adapter
from chat_adapter import PracticeChatAdapter
from database import Database
from wechat_ingest import import_article_text


class WritingAssistRoutingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tempdir = tempfile.TemporaryDirectory()
        database = Database(Path(cls.tempdir.name) / "chat.db")
        # 放几篇够长的真实感推文，让语料库不为空。
        for title, account, body in [
            ("实践招募 | 赴云南大理支教实践支队招募", "清华大学社会实践",
             "现面向全校招募赴云南大理支教实践支队队员，走进当地中学开展课业辅导与教育资源调研。"),
            ("实践总结丨十四天的西部记忆", "清华大学社会实践",
             "支队一行十二人抵达西部小城，开始为期十四天的实践，走访小学与农户，收获远比付出多。"),
        ]:
            text = body
            while len(text) < 400:
                text += body
            import_article_text(database, {
                "title": title, "source_account": account,
                "source_url": f"https://mp.weixin.qq.com/s/{abs(hash(title)) % 10 ** 8}",
            }, text)
        cls.database = database

    @classmethod
    def tearDownClass(cls):
        cls.tempdir.cleanup()

    def setUp(self):
        chat_adapter.llm.is_enabled = lambda: False  # 不打真实模型，只验路由
        self.adapter = PracticeChatAdapter(self.database)

    def reply(self, text: str):
        return self.adapter.reply([{"role": "user", "content": text}])

    def test_pasted_draft_goes_to_polishing(self):
        """贴一段文字要求改，以前回「我得先知道是给哪个项目写」。

        这是写作辅助最基本的用法。更糟的一次是「你看看我这个开头写得怎么样：
        盛夏的七月，我们踏上了前往西部的列车」——整句被模糊匹配成了某个项目，
        端出那个项目的详情页，还附了一个抽错的经费字段。
        """
        for text in [
            "帮我润色这段：我们支队将前往云南开展调研，希望通过实地走访了解当地教育情况。",
            "你看看我这个开头写得怎么样：盛夏的七月，我们踏上了前往西部的列车。",
            "这段读起来通顺吗：我们希望用一个假期的时间，把课堂带到山里去。",
        ]:
            with self.subTest(text=text[:20]):
                self.assertIn(self.reply(text).intent, {"polish", "polish_degraded"})

    def test_short_question_is_not_mistaken_for_a_draft(self):
        # 「怎么样」也在润色词表里，但这句后面没有待处理的正文。
        self.assertNotIn(self.reply("这个项目怎么样").intent, {"polish", "polish_degraded"})

    def test_corpus_statistics_questions(self):
        """「实践一般去哪些地方」这类能从库里算出来，不该掉进项目匹配。

        实测过的三种走偏：「哪些主题的实践比较多」被"比较"抢去做项目对比；
        「实践招募一般什么时候发布」里的"实践招募"模糊命中一堆标题，变成
        项目候选列表；「清华的实践一般都去哪些地方」直接掉兜底。
        """
        for text in ["清华的实践一般都去哪些地方", "支教类的实践一般多长时间",
                     "实践招募一般什么时候发布", "哪些主题的实践比较多",
                     "语料库里收录了多少篇"]:
            with self.subTest(text=text):
                self.assertEqual(self.reply(text).intent, "corpus_stats")

    def test_comparing_two_projects_still_works(self):
        # 修「比较多」的误匹配时，不能把真正的项目对比也一起挡掉。
        self.assertEqual(self.reply("比较前两个推荐项目").intent, "compare")

    def test_naming_request_does_not_demand_a_project(self):
        # 起名时用户往往还没立项，要求他先选项目等于把人挡在门外。
        for text in ["帮我想几个支队名字", "给支队起个名字", "帮我想个推送标题"]:
            with self.subTest(text=text):
                self.assertEqual(self.reply(text).intent, "writing_guide")

    def test_generic_writing_question_still_routes_to_guide(self):
        self.assertEqual(self.reply("实践总结报告一般怎么写").intent, "writing_guide")

    def test_writing_for_a_named_project_still_needs_the_project(self):
        # 给具体项目出材料时，事实必须来自项目卡——这条边界不能因为
        # 放开了通用写作就跟着松掉。
        self.assertIn(self.reply("帮我写报名理由").intent,
                      {"generate_needs_project", "writing_help"})


if __name__ == "__main__":
    unittest.main()
