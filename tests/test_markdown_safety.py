"""回复是按 Markdown 渲染的，正文里的字符会跟语法打架。

这一类问题单元测试历来抓不到——断言的是"字符串里有没有这句话"，而它确实
有；坏的是**渲染出来长什么样**，只有在真实界面上才看得见。这个项目上已经
栽过三次：

  1. 公众号链接里的 __biz 和 &，被当成斜体和实体，链接被截断
  2. 手机号打码用的 139****4658，**** 被当成加粗标记吃掉，显示成 1394658
  3. 「**下面几条主题都对不上。**库里主题对得上的是……」
     结束的 ** 紧跟在中文句号后、后面又紧接汉字，CommonMark 不认这个闭合
     标记，于是它跟下一个 ** 配了对——星号原样显示，而后半句反倒被加粗

第三条的规则（CommonMark 6.2 right-flanking）：结束用的 ** 如果前面是标点，
就必须后接空白或标点才算数。中文句子里"。**紧接下一句"正好踩中。

所以把规则写成测试，扫真实产出的回复，而不是每次靠人盯着界面看。
"""
import os
import re
import tempfile
import unittest
from pathlib import Path

import chat_adapter
from chat_adapter import PracticeChatAdapter
from database import Database

_PUNCT = "。！？，、；：（）「」“”,.!?;:()[]<>\"'"


def _bad_bold(text: str) -> str:
    """挑出渲染不出来的加粗，返回出错处的上下文；都正常就返回空串。

    必须先按出现顺序两两配对，再判断——单靠一条正则分不清哪个是开、哪个
    是闭，而开和闭的规则正好相反（第一版就栽在这：把合法的结束标记
    「……都对不上**。」也判成了错）。
    """
    marks = [m.start() for m in re.finditer(r"\*\*", text)]
    for open_at, close_at in zip(marks[0::2], marks[1::2]):
        after_open = text[open_at + 2:open_at + 3]
        before_close = text[close_at - 1:close_at]
        after_close = text[close_at + 2:close_at + 3]
        context = repr(text[max(0, open_at - 15):close_at + 20])
        # 开标记后面不能是空白（left-flanking）
        if not after_open or after_open.isspace():
            return "开标记后面是空白：" + context
        # 闭标记前面不能是空白；前面是标点时，后面必须接空白或标点，
        # 否则这个闭合标记不成立（CommonMark 6.2 right-flanking）
        if not before_close or before_close.isspace():
            return "闭标记前面是空白：" + context
        if before_close in _PUNCT and after_close and not (
                after_close.isspace() or after_close in _PUNCT):
            return "句末标点被包进了粗体，闭合标记不成立：" + context
    return ""

QUERIES = [
    "推荐几个实践",
    "想找京津冀附近的调研或支教类实践",
    "我要去新疆的",
    "有没有非遗方向的实践",
    "有没有支教类的",
    "不要校内的，我想出去",
    "我是大二工科生，只想找不用出京的，八月有空，还要有经费",
    "哪个含金量最高",
    "负责人的手机号是多少",
    "这个项目谁报名了",
    "你为什么这么推荐",
    "是不是串线了",
    "帮我写报名理由",
    "参加社会实践对保研有用吗",
    "这些信息准吗",
    "看看那几条线索",
    "比较前两个项目",
]


class MarkdownSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tempdir = tempfile.TemporaryDirectory()
        cls._seed_flag = os.environ.get("SEED_DEMO_DATA")
        os.environ.pop("SEED_DEMO_DATA", None)  # 要演示数据，才有东西可推
        cls.database = Database(Path(cls.tempdir.name) / "chat.db")

    @classmethod
    def tearDownClass(cls):
        if cls._seed_flag is not None:
            os.environ["SEED_DEMO_DATA"] = cls._seed_flag
        cls.tempdir.cleanup()

    def setUp(self):
        chat_adapter.llm.is_enabled = lambda: False
        self.adapter = PracticeChatAdapter(self.database)

    def _replies(self):
        for text in QUERIES:
            yield text, self.adapter.reply([{"role": "user", "content": text}]).content

    def test_every_bold_run_actually_renders(self):
        for text, content in self._replies():
            with self.subTest(text=text):
                self.assertEqual(_bad_bold(content), "")

    def test_the_checker_catches_the_bug_that_shipped(self):
        """线上真实出过的那一句，检查器必须认得出来；改好的那句必须放行。"""
        self.assertIn("闭合标记不成立",
                      _bad_bold("主题「文化传承」：**下面几条主题都对不上。**库里对得上的是甲、乙"))
        self.assertEqual(
            _bad_bold("主题「文化传承」：**下面几条主题都对不上**。库里对得上的是甲、乙"), "")
        # 后面接空格的写法本来就是对的，别误伤
        self.assertEqual(_bad_bold("**这个我没有。** 我的库只有公开推送里的内容"), "")

    def test_bold_markers_are_balanced(self):
        for text, content in self._replies():
            with self.subTest(text=text):
                self.assertEqual(content.count("**") % 2, 0, "粗体标记落单")

    def test_urls_are_autolinked_so_underscores_survive(self):
        """公众号链接里有 __biz 和 &，裸着写会被当成斜体和实体。"""
        for text, content in self._replies():
            with self.subTest(text=text):
                for match in re.finditer(r"(?<![<(])https?://\S+", content):
                    self.fail(f"链接没有用 <> 包起来：{match.group(0)[:60]}")


if __name__ == "__main__":
    unittest.main()
