"""地域约束与「为什么这么推荐」。

这批用例全部来自 2026-08-20 在线上真实库里复现出来的两个投诉：

1. 问「想找京津冀附近的调研或支教类实践」，推出来第一条是湖南新宁、第二条是河南，
   而整段回复里一个地名都不提。查下来 `preferred_locations` 是空列表——`KNOWN_LOCATIONS`
   只有省份平铺表，认不出「京津冀」。而库里当时明明有北京 1 个、河北 1 个，
   外加两个存成「中关村街道」「紫荆学生区」的校内项目。
2. 接着追问「为什么推荐不在京津冀的？是库里没有吗」，它没解释，把同一份列表
   原样重跑了一遍——因为这句里的"推荐"被 RECOMMEND_WORDS 接走了。

所以这里每条断言对应一次真实的失败，不是想象出来的场景。
"""
import tempfile
import unittest
from pathlib import Path

import chat_adapter
from chat_adapter import PracticeChatAdapter
from database import Database
from domain import expand_location_query, project_location_text, recommend_projects


class LocationVocabularyTests(unittest.TestCase):
    def test_region_words_expand_to_provinces(self):
        for text, label, must_contain in [
            ("想找京津冀附近的调研或支教类实践", "京津冀", ["北京", "天津", "河北"]),
            ("长三角有什么实践", "长三角", ["上海", "江苏", "浙江"]),
            ("想去西部支教", "西部", ["甘肃", "青海", "新疆"]),
            ("东北的项目有吗", "东北", ["辽宁", "吉林", "黑龙江"]),
        ]:
            with self.subTest(text=text):
                labels, provinces = expand_location_query(text)
                self.assertIn(label, labels)
                for province in must_contain:
                    self.assertIn(province, provinces)

    def test_plain_province_still_works(self):
        labels, provinces = expand_location_query("河南的项目")
        self.assertEqual(labels, ["河南"])
        self.assertEqual(provinces, ["河南"])

    def test_campus_locations_count_as_beijing(self):
        """校内项目的 province 是空的，地点只存在 detail 里。

        不补这一层，问「京津冀」的人看不到「中关村街道（学校周边社区）」
        这种明明就在北京的项目——线上真实库里这类有 2 个。
        """
        project = {"location": {"detail": "紫荆学生区（C楼门口）"}}
        self.assertIn("北京", project_location_text(project))

    def test_no_location_mentioned_stays_empty(self):
        # 不能因为想认出区域词就把无关的句子也判成有地点偏好。
        self.assertEqual(expand_location_query("帮我写一份实践总结"), ([], []))


def _project(pid: str, title: str, province: str, *, theme: str = "教育") -> dict:
    return {
        "id": pid, "title": title, "status": "published", "confidence": 0.9,
        "theme_tags": [theme], "location": {"province": province, "detail": province},
        "uncertain_fields": [], "reimbursement": {}, "source_account": "清华大学社会实践",
    }


class LocationRankingTests(unittest.TestCase):
    def setUp(self):
        self.projects = [
            _project("p-hunan", "赴湖南新宁支教实践支队招募", "湖南"),
            _project("p-henan", "河南支教夏令营招募", "河南"),
            _project("p-hebei", "赴河北调研支队招募", "河北"),
        ]

    def test_matching_location_ranks_first_even_with_lower_score(self):
        """地点原来只值 +15，很容易被主题（+25）和时间（+25）压过去。

        线上实测就是这样：问京津冀，湖南和河南排在前两位，河北的项目排在后面。
        """
        profile = {"preferred_locations": ["北京", "天津", "河北"], "themes": ["教育"]}
        result = recommend_projects(self.projects, profile)
        self.assertEqual(result["eligible"][0]["project"]["id"], "p-hebei")
        self.assertTrue(result["eligible"][0]["location_match"])
        self.assertEqual(result["location_matched"], 1)

    def test_no_match_is_counted_not_hidden(self):
        profile = {"preferred_locations": ["北京", "天津"], "themes": []}
        result = recommend_projects(self.projects, profile)
        self.assertTrue(result["location_asked"])
        self.assertEqual(result["location_matched"], 0)
        # 不匹配的仍然给出来——暑期数据本来就少，硬筛容易空列表。
        self.assertEqual(len(result["eligible"]), 3)


class RecommendationWordingTests(unittest.TestCase):
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

    def reply(self, *texts: str):
        return self.adapter.reply([{"role": "user", "content": t} for t in texts])

    def test_recommendation_always_mentions_the_requested_region(self):
        """说了地域就必须给个准话，三种情况都不许沉默。

        原来的行为：命中了才在「理由」里带一句，没命中就整段不提——
        学生无从判断是自己没说清楚、是库里没有、还是系统没听见。
        """
        result = self.reply("想找京津冀附近的调研或支教类实践")
        self.assertEqual(result.intent, "recommend")
        self.assertIn("京津冀", result.content)

    def test_match_score_number_is_gone(self):
        """匹配度是相对当次提问算的，同一项目换个问法就从 65 变 52。

        差的 13 分来自主题词有没有命中（+25 对 +12），跟项目本身无关。
        只给个光秃秃的数字，看起来像随机跳动。
        """
        self.assertNotIn("匹配度", self.reply("推荐一些实践").content)


class ExplainIntentTests(unittest.TestCase):
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

    def reply(self, *texts: str):
        return self.adapter.reply([{"role": "user", "content": t} for t in texts])

    def test_follow_up_does_not_rerun_the_recommendation(self):
        result = self.reply("想找京津冀附近的调研或支教类实践",
                            "为什么推荐不在京津冀的？是库里没有吗")
        self.assertEqual(result.intent, "explain_recommendation")
        self.assertNotIn("## 正式推荐", result.content)

    def test_explanation_answers_whether_the_library_has_any(self):
        # 「是库里没有吗」是个可以被证伪的事实问题，必须给数字。
        content = self.reply("想找京津冀附近的实践",
                             "为什么推荐不在京津冀的？是库里没有吗").content
        self.assertIn("京津冀", content)
        # 断言实质而不是字面：要么给出一个可核对的数量，要么明说一个都没有。
        # 绑死措辞的话，以后改一个字这条就会误报。
        import re as _re
        self.assertTrue(
            _re.search(r"符合的一共 \d+ 个", content) or "一个都没有" in content,
            f"没有正面回答库里有没有：\n{content}",
        )

    def test_explanation_says_the_score_is_query_relative(self):
        # 用户自己发现同一项目两次分数不同并起了疑。这一点必须主动讲清楚。
        content = self.reply("推荐实践", "你凭什么这么排序").content
        self.assertIn("针对你这次的提问", content)

    def test_various_phrasings_reach_the_explanation(self):
        for follow_up in ["为什么这么推荐", "凭什么排第一", "排序依据是什么",
                          "这几个是怎么选出来的", "为啥推荐这些"]:
            with self.subTest(follow_up=follow_up):
                self.assertEqual(
                    self.reply("推荐一些实践", follow_up).intent,
                    "explain_recommendation",
                )

    def test_writing_question_with_why_is_not_stolen(self):
        """「为什么」不能一刀切地抢走——这句问的是写作，不是推荐依据。"""
        self.assertNotEqual(
            self.reply("为什么实践总结这么难写").intent,
            "explain_recommendation",
        )


if __name__ == "__main__":
    unittest.main()
