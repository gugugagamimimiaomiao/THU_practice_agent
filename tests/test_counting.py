"""问数字就给数字，而且那个数字得对。

原来这一类要么掉兜底（「库里一共有多少个项目」「过期的有多少」
「哪个省份最多」），要么甩一份列表回去（「有经费支持的有几个」）。
数字我们手上全都有，只是从来没往外说过。评委随口就会问这一类。

修的时候连着踩了三个坑，每个都留了测试：

  一、拿 recommend_projects 的 eligible 去数。地点和主题在打分里是加分项
      不是过滤器，eligible 里塞满了"条件对不上但按时间补位"的项目。
      「北京的有几个」因此答成 25，而同一次会话里「哪个省份最多」显示
      北京只有 3 个——同一份数据自己打自己脸，比答不上来更糟。

  二、经费只按 required 过滤。用户说「有经费的」抽出来是 preferred，
      于是「有经费支持的有几个」又答成 25（全部在招的）。数数的时候
      required 和 preferred 该一样处理。

  三、「推荐几个实践」被计数分支抢走。「几个」确实是数量词，但前面挂着
      动词就是**索取**不是问数。用户要列表，拿到一个数字。
"""
import re
import tempfile
import unittest
from collections import Counter
from pathlib import Path

import chat_adapter
from chat_adapter import PracticeChatAdapter
from database import Database


class CountingTests(unittest.TestCase):
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

    def ask(self, text):
        return self.adapter.reply([{"role": "user", "content": text}])

    def number_in(self, text):
        found = re.search(r"\*\*(\d+) 个\*\*", text)
        return int(found.group(1)) if found else None

    def test_counting_questions_are_answered_with_a_number(self):
        for text in ("库里一共有多少个项目", "现在还能报名的有几个", "过期的有多少",
                     "北京的有几个", "支教类的有多少个"):
            with self.subTest(text=text):
                result = self.ask(text)
                self.assertEqual(result.intent, "count")
                self.assertIsNotNone(self.number_in(result.content),
                                     f"没给出数字：{result.content[:80]}")

    def test_a_distribution_question_gets_a_distribution(self):
        result = self.ask("哪个省份的项目最多")
        self.assertEqual(result.intent, "count")
        # 演示库里的项目没填 province，那就该老实说数不出来，而不是编一个分布。
        self.assertTrue("按省份数" in result.content or "数不出来" in result.content,
                        result.content[:80])

    def test_a_filtered_count_never_exceeds_the_distribution(self):
        """一号坑。「北京的有几个」不能大于按省份数出来的北京总数。

        原来拿 eligible 去数，答案是"全部在招的条数"，跟地点毫无关系。
        """
        projects = [p for p in self.adapter._projects(include_expired=True)
                    if not p.get("demo_data")]
        by_province = Counter((p.get("location") or {}).get("province") or ""
                              for p in projects)
        for province, total in by_province.most_common(3):
            if not province:
                continue
            with self.subTest(province=province):
                answered = self.number_in(self.ask(f"{province}的有几个").content)
                self.assertIsNotNone(answered)
                self.assertLessEqual(
                    answered, total,
                    f"{province}：答了 {answered} 个，可全库（含过期）才 {total} 个")

    def test_a_money_count_only_counts_projects_that_have_money(self):
        """二号坑。用户说「有经费的」抽出来是 preferred，只按 required
        过滤的话会把全部在招项目都算进去。"""
        published = [p for p in self.adapter._projects(include_expired=True)
                     if p.get("status") == "published" and not p.get("demo_data")]
        truly = sum(1 for p in published
                    if (p.get("reimbursement") or {}).get("has_reimbursement") is True)
        answered = self.number_in(self.ask("有经费支持的有几个").content)
        self.assertIsNotNone(answered)
        self.assertEqual(answered, truly)
        if truly < len(published):
            self.assertLess(answered, len(published), "把没经费的也数进去了")

    def test_asking_for_some_is_not_asking_how_many(self):
        """三号坑。「几个」前面挂着动词就是索取，不是问数。"""
        for text in ("推荐几个实践", "给我三个支教的", "找几个北京的",
                     "来两个有经费的"):
            with self.subTest(text=text):
                self.assertNotEqual(self.ask(text).intent, "count",
                                    "用户要列表，却被当成问数量")

    def test_quantities_about_something_else_are_not_project_counts(self):
        """「招多少人」问的是某个项目的名额，「补贴多少钱」问的是经费，
        「实践多少天」问的是时长——都不是"库里有几条"。"""
        for text in ("招多少人", "有多少个名额", "补贴多少钱", "实践多少天"):
            with self.subTest(text=text):
                self.assertNotEqual(self.ask(text).intent, "count")

    def test_leads_win_over_counting(self):
        """「看看那几条线索」里的「几条」会被计数正则抓走，但线索区的标题
        本来就写着「共 N 条」，那一问已经答了。"""
        self.assertEqual(self.ask("看看那几条线索").intent, "list_leads")

    def test_corpus_questions_still_go_to_corpus_stats(self):
        self.assertEqual(self.ask("你收录了多少篇推送").intent, "corpus_stats")

    def test_the_total_adds_up(self):
        """三个分项加起来要等于合计，不然用户一眼就看出在瞎报。"""
        content = self.ask("库里一共有多少个项目").content
        numbers = [int(n) for n in re.findall(r"(\d+) [个条]", content)]
        self.assertGreaterEqual(len(numbers), 4, f"没报够四个数：{content[:100]}")
        published, expired, pending, total = numbers[:4]
        self.assertEqual(published + expired + pending, total,
                         f"分项加起来对不上合计：{content[:120]}")


if __name__ == "__main__":
    unittest.main()
