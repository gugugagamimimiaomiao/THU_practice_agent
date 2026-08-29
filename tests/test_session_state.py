"""多轮会话状态：序号指代、指代消解，不许串到别的项目上。

来自 2026-08-21 的 100 轮浏览器实测。最严重的一条端到端证据：

    第 96 轮  「推荐两个项目」          → 回答前两项是春山、宝庆
    第 97 轮  「给第一项生成报名风险清单」 → 系统绑成了宣传部学生骨干
    第 98 轮  「给第二项生成公众号推送」   → 仍然绑宣传部
    第 99 轮  「分别核对这两项」         → 只输出宣传部项目卡
    第 100 轮 「你是不是串线了」         → 「这句我没接住」

根因在旧的 _resolve_project：

    conversation = "\\n".join(item["content"] for item in messages)
    mentioned = [p for p in projects if p["title"] in conversation]
    return mentioned[1] if "第二个" in latest else mentioned[0]

`mentioned` 是**数据库顺序**，不是屏幕上的顺序。「第一个」等于"历史上被提到过的
项目里，在库里排最前的那个"。给错项目写材料比答不上来严重得多。
"""
import tempfile
import unittest
from pathlib import Path

import chat_adapter
from chat_adapter import PracticeChatAdapter
from database import Database
from wechat_ingest import import_article_text


def _article(title: str, place: str) -> tuple[dict, str]:
    return (
        {"title": title, "source_account": "清华大学社会实践",
         "source_url": f"https://mp.weixin.qq.com/s/{abs(hash(title)) % 10 ** 9}"},
        f"现面向全校招募队员，前往{place}开展支教实践。\n"
        f"报名截止：2036年9月10日\n参与资格：全校本科生\n报名方式：扫码报名\n"
        f"实践时间：2036年9月15日至2036年9月20日\n",
    )


class OrdinalReferenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tempdir = tempfile.TemporaryDirectory()
        cls.database = Database(Path(cls.tempdir.name) / "chat.db")
        for title, place in [
            ("春山在望中学生支教夏令营实践招募", "安徽霍邱"),
            ("宝庆微光赴湖南新宁支教实践支队招募", "湖南新宁"),
            ("校团委宣传部学生骨干招募", "北京"),
        ]:
            import_article_text(cls.database, *_article(title, place))

    @classmethod
    def tearDownClass(cls):
        cls.tempdir.cleanup()

    def setUp(self):
        chat_adapter.llm.is_enabled = lambda: False
        self.adapter = PracticeChatAdapter(self.database)

    def _listed(self, content: str) -> list[str]:
        return [m.group(2).strip() for m in chat_adapter._LISTED_RE.finditer(content)]

    def test_ordinal_binds_to_what_the_user_just_saw(self):
        first = self.adapter.reply([{"role": "user", "content": "推荐一些实践"}])
        titles = self._listed(first.content)
        self.assertGreaterEqual(len(titles), 2, f"这条用例需要至少两条推荐：\n{first.content}")

        for index, word in enumerate(["第一个", "第二个"]):
            with self.subTest(word=word):
                messages = [
                    {"role": "user", "content": "推荐一些实践"},
                    {"role": "assistant", "content": first.content},
                    {"role": "user", "content": f"帮我写{word}的报名理由"},
                ]
                bound = self.adapter._resolve_project(messages, f"帮我写{word}的报名理由")
                self.assertIsNotNone(bound, f"{word} 没能绑上任何项目")
                self.assertEqual(
                    bound["title"], titles[index],
                    f"{word} 应该绑到「{titles[index]}」，实际绑到「{bound['title']}」",
                )

    def test_two_consecutive_ordinals_do_not_collapse_to_one_project(self):
        """97、98 两轮分别说"第一项""第二项"，实测两次都绑到了同一个项目。"""
        first = self.adapter.reply([{"role": "user", "content": "推荐一些实践"}])
        titles = self._listed(first.content)
        history = [
            {"role": "user", "content": "推荐一些实践"},
            {"role": "assistant", "content": first.content},
        ]
        a = self.adapter._resolve_project(history + [{"role": "user", "content": "第一项"}], "第一项")
        b = self.adapter._resolve_project(history + [{"role": "user", "content": "第二项"}], "第二项")
        self.assertNotEqual(a["id"], b["id"], "两个不同的序号绑到了同一个项目")
        self.assertEqual([a["title"], b["title"]], titles[:2])

    def test_unresolvable_ordinal_returns_nothing_rather_than_guessing(self):
        """说了序号但没有列表可参照时，宁可答不上来，也不能挑一个给他。

        给错项目写材料，用户不会发现——那份材料看起来完全正常。
        """
        messages = [{"role": "user", "content": "帮我写第三个的报名理由"}]
        self.assertIsNone(self.adapter._resolve_project(messages, "帮我写第三个的报名理由"))

    def test_ordinal_beyond_list_length_returns_nothing(self):
        first = self.adapter.reply([{"role": "user", "content": "推荐一些实践"}])
        messages = [
            {"role": "user", "content": "推荐一些实践"},
            {"role": "assistant", "content": first.content},
            {"role": "user", "content": "第九个怎么样"},
        ]
        self.assertIsNone(self.adapter._resolve_project(messages, "第九个怎么样"))

    def test_pronoun_binds_to_the_last_detailed_project(self):
        detail = self.adapter.reply([{"role": "user", "content": "宝庆微光"}])
        messages = [
            {"role": "user", "content": "宝庆微光"},
            {"role": "assistant", "content": detail.content},
            {"role": "user", "content": "帮我写这个项目的报名理由"},
        ]
        bound = self.adapter._resolve_project(messages, "帮我写这个项目的报名理由")
        self.assertIsNotNone(bound)
        self.assertIn("宝庆微光", bound["title"])

    def test_shown_list_prefers_the_most_recent_one(self):
        """用户又问了一次、列表变了，序号必须跟着新的那份走。"""
        old = "## 正式推荐\n\n1. **校团委宣传部学生骨干招募**\n   - 地点 北京\n"
        new = "## 正式推荐\n\n1. **宝庆微光赴湖南新宁支教实践支队招募**\n   - 地点 湖南\n"
        messages = [
            {"role": "user", "content": "推荐"}, {"role": "assistant", "content": old},
            {"role": "user", "content": "换一批"}, {"role": "assistant", "content": new},
            {"role": "user", "content": "第一个"},
        ]
        bound = self.adapter._resolve_project(messages, "第一个")
        self.assertIn("宝庆微光", bound["title"])

    def test_potential_opportunities_are_not_numbered_targets(self):
        """线索区用的是不带编号的 `- **标题**`，不该被「第 N 个」指到。"""
        content = (
            "## 正式推荐\n\n1. **宝庆微光赴湖南新宁支教实践支队招募**\n   - 地点 湖南\n"
            "\n## 线索（尚未核实，不能作为报名依据）\n"
            "- 线索待核验：**校团委宣传部学生骨干招募** — 字段待核\n"
        )
        messages = [
            {"role": "user", "content": "推荐"},
            {"role": "assistant", "content": content},
            {"role": "user", "content": "第二个"},
        ]
        self.assertIsNone(self.adapter._resolve_project(messages, "第二个"))

    def test_switching_projects_mid_conversation_actually_switches(self):
        """给 A 做过材料之后再点开 B，接下来的材料必须写给 B。

        原来「看过谁的详情」和「给谁做过材料」是两个函数各自倒着扫，
        调用方先问后者、问不到才问前者——于是谁先被*问到*谁赢，
        而不是谁更*近*谁赢。实测：

            3  帮我写报名理由      -> A
            4  宝庆微光 详细说说    -> B   用户明确换过来了
            5  帮我写报名理由      -> A   又绑回去了
            6  访谈提纲也来一份    -> A   一直错下去

        第 5 轮往后每一份材料写的都是 A，而输出看起来完全正常。
        """
        messages = []

        def say(text):
            messages.append({"role": "user", "content": text})
            result = self.adapter.reply(messages)
            messages.append({"role": "assistant", "content": result.content})
            return result

        say("推荐一些实践")
        say("第一个详细说说")
        first = self.adapter._current_project(messages)
        self.assertIsNotNone(first)
        say("帮我写报名理由")
        say("宝庆微光 详细说说")
        say("帮我写报名理由")
        bound = self.adapter._resolve_project(messages, "访谈提纲也来一份")
        self.assertIsNotNone(bound)
        self.assertIn("宝庆微光", bound["title"])
        self.assertNotEqual(bound["id"], first["id"], "前提变了：这两轮点的是同一个项目，测试是空的")

    def test_the_binding_self_check_reports_what_is_actually_bound(self):
        """「是不是串线了」报的必须和实际绑定一致，否则这个出口会在
        用户最需要它的时候撒谎。"""
        messages = []
        for text in ("推荐一些实践", "第一个详细说说", "帮我写报名理由", "宝庆微光 详细说说"):
            messages.append({"role": "user", "content": text})
            messages.append({"role": "assistant", "content": self.adapter.reply(messages).content})
        bound = self.adapter._resolve_project(messages, "访谈提纲也来一份")
        said = self.adapter._explain_binding(messages).content
        self.assertIn(bound["title"], said)


if __name__ == "__main__":
    unittest.main()
