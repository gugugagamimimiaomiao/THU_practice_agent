"""我们在回复里教用户说的每一句话，都得真的接得住。

这是**同一类病的第三次**，所以这次不修单点，改成把检查固化下来：

    2026-08-29 上午  「看看那 22 条线索」    没有分支，掉进 list_projects
    2026-08-29 下午  「要我放宽哪一条」       用户说了却掉兜底
    2026-08-29 晚上  「不限主题再看看」       我当天刚加的提示语，压根没实现

_next_steps 的文档里自己写着「提一件做不到的事比不提更糟」，然后连着犯了
三次。人是记不住的，得让测试记。

做法：跑一圈主要路径，把回复里所有「」引号中的**祈使句式建议**抽出来，
逐条当成用户的下一句发回去，断言它不掉兜底、不掉功能介绍。以后谁加了新的
提示语却忘了实现，这条测试直接红。

抽取要排掉两类不是建议的东西：项目标题（「宝庆微光…」），以及回执里被引号
引用的条件值（「京津冀」「文化传承」）——后者虽然不是建议，但单独打过来
也该当筛选条件处理，那由 test_bare_condition_values_are_treated_as_filters 管。
"""
import re
import tempfile
import unittest
from pathlib import Path

import chat_adapter
from chat_adapter import PracticeChatAdapter
from database import Database

# 说明"没接住"的意图。
DEAD_ENDS = {"fallback", "help"}

# 走一圈主要路径，覆盖各种会给出建议的回复。
SEEDS = (
    "推荐几个实践",
    "我是大二工科生，想找京津冀的支教，最好有经费",
    "我要去新疆的",
    "有没有非遗方向的实践",
    "你为什么这么推荐",
    "哪个含金量最高",
    "这些信息准吗",
    "招募推送一般怎么写",
    "你好",
    "asdfghjkl",
)

# 建议句里一般会有的动词/句式。用它把「京津冀」这类条件值排除在外。
_LOOKS_LIKE_AN_INSTRUCTION = re.compile(
    r"看|写|帮|比较|换|再|不限|放宽|还有|推荐|详细|为什么|说说|导入")


class SuggestionsAreHonouredTests(unittest.TestCase):
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
        self.titles = {p["title"] for p in self.adapter._projects(include_expired=True)}

    def _say(self, messages, text):
        messages.append({"role": "user", "content": text})
        result = self.adapter.reply(messages)
        body = result.resolve() if result.stream_factory else result.content
        messages.append({"role": "assistant", "content": body})
        return result, body

    def _collect(self):
        """(建议原文, 产生它的那段对话) 的列表。"""
        found: dict[str, list[dict]] = {}
        for seed in SEEDS:
            messages: list[dict] = []
            _, body = self._say(messages, seed)
            for quoted in re.findall(r"「([^「」\n]{2,40})」", body):
                if quoted in self.titles or any(quoted in t for t in self.titles):
                    continue
                if not _LOOKS_LIKE_AN_INSTRUCTION.search(quoted):
                    continue
                found.setdefault(quoted, list(messages))
        return found

    def test_the_audit_actually_finds_suggestions(self):
        """前提断言：抽不出建议的话，下面那条测试就是空跑。"""
        self.assertGreaterEqual(len(self._collect()), 5)

    def test_every_suggestion_we_make_is_answerable(self):
        for phrase, context in sorted(self._collect().items()):
            with self.subTest(phrase=phrase):
                messages = list(context)
                result, _ = self._say(messages, phrase)
                self.assertNotIn(
                    result.intent, DEAD_ENDS,
                    f"我们教用户说「{phrase}」，他照做却得到 {result.intent}")

    def test_relaxing_a_constraint_is_understood(self):
        """「不限地点再看看」「不限主题再看看」是我们自己教的话。"""
        for phrase in ("不限地点再看看", "不限主题再看看", "地点不用管了",
                       "不限时间", "什么方向都行"):
            with self.subTest(phrase=phrase):
                messages: list[dict] = []
                self._say(messages, "我是大二的，想找京津冀八月的支教")
                result, _ = self._say(messages, phrase)
                self.assertNotIn(result.intent, DEAD_ENDS)

    def test_relaxing_actually_drops_the_condition(self):
        """措辞接住了不算数，条件得真的被撤掉。"""
        profile = self.adapter._profile_from_turns(
            ["我想找京津冀的实践", "不限地点再看看"])
        self.assertEqual(profile["preferred_locations"], [])
        self.assertEqual(profile["location_labels"], [])

    def test_relaxing_one_thing_does_not_drop_the_others(self):
        profile = self.adapter._profile_from_turns(
            ["我大二，想找京津冀的支教", "不限地点再看看"])
        self.assertEqual(profile["preferred_locations"], [])
        self.assertEqual(profile["grade"], "大二")
        self.assertIn("教育", profile["themes"])

    def test_bare_condition_values_are_treated_as_filters(self):
        """用户看到回执里写着「主题「文化传承」」「你说了「京津冀」」，
        很可能直接把这几个字打回来。那明显是在筛选，不该掉兜底。"""
        for phrase in ("京津冀", "文化传承", "大二", "湖南"):
            with self.subTest(phrase=phrase):
                self.assertTrue(self.adapter._states_a_constraint(phrase))

    def test_a_sentence_merely_containing_a_condition_word_is_not_a_filter(self):
        """判据是"整句**就是**这个值"，不是"整句里含这个值"。
        「我对教育很感兴趣」是在聊天，不是在下筛选指令。"""
        for phrase in ("我对教育很感兴趣", "今天北京天气怎么样", "社会实践是个好东西"):
            with self.subTest(phrase=phrase):
                self.assertFalse(self.adapter._states_a_constraint(phrase))


if __name__ == "__main__":
    unittest.main()
