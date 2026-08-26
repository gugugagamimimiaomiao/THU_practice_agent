"""2026-08-21 第二轮实测补出来的路由缺口。

第一轮修完之后，把 100 轮报告里的失败场景在真实库上重跑，又抓到四个：

  「给我三个支教项目」   → 掉给模型分类成 list，端出整个项目库，
                          列出来的五条一个支教都没有
  「是不是串线了」       → 掉兜底。而这恰恰是最该答得上来的问题
  项目卡摘要             → 「机械系"宝庆微光"…招募实践招募机械系"宝庆微光"…招募」
                          同一个标题读三遍
  「不考虑学生骨干岗位」 → 见 test_constraints.py，那条在那边钉
"""
import tempfile
import unittest
from pathlib import Path

import chat_adapter
from chat_adapter import PracticeChatAdapter
from database import Database
from domain import _summarize
from wechat_ingest import import_article_text


class AskForNTests(unittest.TestCase):
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

    def test_asking_for_n_of_something_is_a_recommendation(self):
        for text in ["给我三个支教项目", "来两个调研的实践", "要五个志愿机会",
                     "找三个乡村振兴项目"]:
            with self.subTest(text=text):
                self.assertEqual(self.adapter.reply(
                    [{"role": "user", "content": text}]).intent, "recommend")

    def test_theme_in_such_a_request_is_actually_read(self):
        profile = self.adapter._extract_profile("给我三个支教项目")
        self.assertIn("教育", profile["themes"])

    def test_asking_what_exists_is_still_a_listing(self):
        # 「有哪些实践机会」问的是全部，不能被上面那条抢走。
        self.assertEqual(self.adapter.reply(
            [{"role": "user", "content": "有哪些实践机会"}]).intent, "list_projects")


class BindingSelfCheckTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tempdir = tempfile.TemporaryDirectory()
        cls.database = Database(Path(cls.tempdir.name) / "chat.db")
        for title in ["春山在望中学生支教夏令营实践招募",
                      "宝庆微光赴湖南新宁支教实践支队招募"]:
            import_article_text(
                cls.database,
                {"title": title, "source_account": "清华大学社会实践",
                 "source_url": f"https://mp.weixin.qq.com/s/{abs(hash(title)) % 10 ** 9}"},
                "现面向全校招募队员。\n报名截止：2036年9月10日\n参与资格：全校本科生\n报名方式：扫码\n",
            )

    @classmethod
    def tearDownClass(cls):
        cls.tempdir.cleanup()

    def setUp(self):
        chat_adapter.llm.is_enabled = lambda: False
        self.adapter = PracticeChatAdapter(self.database)

    def test_asking_whether_it_got_confused_is_answerable(self):
        """实测第 100 轮问「是不是串线了」掉了兜底。

        这是最该答得上来的问题：把我认定的列表摆出来，用户一眼就能判断。
        """
        first = self.adapter.reply([{"role": "user", "content": "推荐一些实践"}])
        result = self.adapter.reply([
            {"role": "user", "content": "推荐一些实践"},
            {"role": "assistant", "content": first.content},
            {"role": "user", "content": "是不是串线了"},
        ])
        self.assertEqual(result.intent, "explain_binding")
        # 断言"还原出来的 == 上一条实际显示的"，而不是某个具体标题——
        # 后者会被夹具里项目的排序影响，测的其实不是这段逻辑。
        shown = [m.group(2) for m in chat_adapter._LISTED_RE.finditer(first.content)]
        self.assertTrue(shown, "上一条没有编号列表，这条用例失去意义")
        for title in shown:
            self.assertIn(title, result.content, f"「{title}」没有被还原出来")

    def test_various_phrasings(self):
        for text in ["你搞混了吧", "刚才说的是哪个项目", "前后不一致啊"]:
            with self.subTest(text=text):
                self.assertEqual(self.adapter.reply(
                    [{"role": "user", "content": text}]).intent, "explain_binding")

    def test_without_a_list_it_says_so_rather_than_guessing(self):
        content = self.adapter.reply([{"role": "user", "content": "是不是串线了"}]).content
        self.assertIn("没有编号列表", content)


class ReplyLengthTests(unittest.TestCase):
    """推荐回复太长会直接变成等待时间——清小搭那边渲染很慢。

    实测一条 1272 字，其中五张完整卡片占 954、线索区占 317。
    直接砍到三条又损失选择面（真实数据本来就少），所以做成分层：
    前三条完整卡片，第四五条一行，线索区只留标题。
    """

    @classmethod
    def setUpClass(cls):
        cls.tempdir = tempfile.TemporaryDirectory()
        cls.database = Database(Path(cls.tempdir.name) / "chat.db")
        for i in range(6):
            title = f"赴某地实践支队招募第{i}期"
            import_article_text(
                cls.database,
                {"title": title, "source_account": "清华大学社会实践",
                 "source_url": f"https://mp.weixin.qq.com/s/len{i}"},
                f"现面向全校招募队员，前往某地开展实践。\n报名截止：2036年9月1{i}日\n"
                f"参与资格：全校本科生\n报名方式：扫码\n实践时间：2036年9月20日至2036年9月25日\n",
            )

    @classmethod
    def tearDownClass(cls):
        cls.tempdir.cleanup()

    def setUp(self):
        chat_adapter.llm.is_enabled = lambda: False
        self.adapter = PracticeChatAdapter(self.database)

    def test_five_options_are_still_offered(self):
        content = self.adapter.reply([{"role": "user", "content": "推荐一些实践"}]).content
        listed = [m.group(1) for m in chat_adapter._LISTED_RE.finditer(content)]
        self.assertEqual(listed[:5], ["1", "2", "3", "4", "5"],
                         "压缩不能以少给选项为代价")

    def test_only_the_first_three_get_full_cards(self):
        content = self.adapter.reply([{"role": "user", "content": "推荐一些实践"}]).content
        # 完整卡片会带「原文：」那一行，一行式条目不带。
        head = content.split("4. **")[0]
        self.assertEqual(head.count("- 原文："), 3)
        self.assertNotIn("- 原文：", content.split("4. **")[1].split("## ")[0])


class SummaryTests(unittest.TestCase):
    def test_summary_does_not_repeat_the_title(self):
        """真实数据上出现过的摘要：

            机械系“宝庆微光”赴湖南新宁支教实践支队招募实践招募机械系“宝庆微光”赴湖南新宁支教实践支队招募

        公众号开头常把标题拆成几行重复排版，原来的 "".join(lines[1:4])
        就把它们无分隔地拼到了一起。
        """
        title = "实践招募 | 机械系“宝庆微光”赴湖南新宁支教实践支队招募"
        lines = [
            title,
            "实践招募",
            "机械系“宝庆微光”赴湖南新宁支教实践支队招募",
            "现面向全校招募队员，前往湖南省邵阳市新宁县第一中学开展支教实践。",
            "报名截止：2026年8月20日",
        ]
        summary = _summarize(lines, title, "\n".join(lines))
        self.assertIn("前往湖南省邵阳市", summary)
        self.assertLess(summary.count("宝庆微光"), 2, f"标题被重复进摘要：{summary}")

    def test_summary_skips_wechat_boilerplate(self):
        """跳过重复标题之后紧接着的往往是公众号名和阅读器提示。

        第一版没管，于是真实数据上的摘要变成了

            清华大学社会实践 在小说阅读器读本章 去阅读 在小说阅读器中沉浸阅读

        比原来的"标题读三遍"还糟——那至少还是这个项目的字。
        """
        title = "实践招募 | 宝庆微光赴湖南新宁支教实践支队招募"
        lines = [
            title,
            "清华大学社会实践",
            "在小说阅读器读本章",
            "去阅读",
            "在小说阅读器中沉浸阅读",
            "现面向全校招募队员，前往湖南省邵阳市新宁县第一中学开展支教实践。",
        ]
        summary = _summarize(lines, title, "\n".join(lines), account="清华大学社会实践")
        self.assertIn("前往湖南省邵阳市", summary)
        for noise in ("小说阅读器", "去阅读", "清华大学社会实践"):
            self.assertNotIn(noise, summary)

    def test_summary_falls_back_when_everything_repeats_the_title(self):
        title = "某支队招募"
        lines = [title, "某支队招募", "某支队招募"]
        self.assertTrue(_summarize(lines, title, "某支队招募的正文内容"))


if __name__ == "__main__":
    unittest.main()
