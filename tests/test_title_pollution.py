"""用户打出来的项目名是指称，不是他对自己的条件。

第三轮扫描 P0-3。点开一次「研究生支教团」看详情，「研究生」就被当成
提问者的年级抽进了 profile；条件跨轮沿用，之后每一次筛选都被它压着，
而回复里从不说这条限制打哪来，用户看不出自己被过滤了什么。

「赴湖南新宁支教实践支队 怎么报名」同理——湖南是那个项目在哪，
不是用户想去哪。

反过来也要守住：只撞上通用主题词（「我想找乡村振兴主题的实践」）时
不能误摘，否则用户真正说出口的条件反而丢了。
"""
import tempfile
import unittest
from pathlib import Path

import chat_adapter
from chat_adapter import PracticeChatAdapter
from database import Database


def _project(pid, title, *, province, theme, restriction=""):
    return {
        "id": pid,
        "title": title,
        "status": "published",
        "summary": title,
        "source_url": f"https://mp.weixin.qq.com/s/{pid}",
        "practice_start": "2036-07-01",
        "practice_end": "2036-07-10",
        "signup_deadline": "2036-06-20",
        "signup_method": "原文二维码报名",
        "eligibility": {"restriction_text": restriction},
        "reimbursement": {"has_reimbursement": None, "text": ""},
        "location": {"province": province, "detail": province},
        "theme_tags": [theme],
        "uncertain_fields": [],
        "field_evidence": {},
    }


class TitleIsNotACondition(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tempdir = tempfile.TemporaryDirectory()
        cls.database = Database(Path(cls.tempdir.name) / "chat.db")
        for project in (
            _project("p-yjs", "清华大学第二十八届研究生支教团招募",
                     province="甘肃", theme="教育帮扶",
                     restriction="面向拟推免的应届本科毕业生"),
            _project("p-bqwg", "机械系“宝庆微光”赴湖南新宁支教实践支队招募",
                     province="湖南", theme="教育帮扶"),
            _project("p-xczx", "乡村振兴工作站暑期实践招募",
                     province="浙江", theme="乡村振兴"),
            _project("p-jjj", "京郊基层治理观察实践",
                     province="北京", theme="基层治理"),
        ):
            cls.database.upsert_project(project)

    @classmethod
    def tearDownClass(cls):
        cls.tempdir.cleanup()

    def setUp(self):
        chat_adapter.llm.is_enabled = lambda: False
        self.adapter = PracticeChatAdapter(self.database)

    # ---- 摘掉项目名 ----

    def test_grade_in_a_title_is_not_the_users_grade(self):
        profile = self.adapter._profile_from_turns(["研究生支教团 还能报吗"])
        self.assertEqual(profile["grade"], "",
                         "「研究生」是这个项目要什么人，不是提问的人是什么年级")

    def test_province_in_a_title_is_not_where_the_user_wants_to_go(self):
        """标题里带地名才会撞上——「宝庆微光」四个字里没有「湖南」，
        用那句话测等于什么都没测。"""
        for text in ["“宝庆微光”赴湖南新宁支教实践支队 怎么报名",
                     "赴湖南新宁支教实践支队招募 还能报吗"]:
            with self.subTest(text=text):
                self.assertEqual(self.adapter._extract_profile(text)["preferred_locations"],
                                 ["湖南"], "前提变了：这句话本来就抽不出湖南，测试是空的")
                self.assertEqual(self.adapter._profile_from_turns([text])["preferred_locations"], [])

    def test_the_pollution_does_not_survive_into_the_next_turn(self):
        """真正的伤害在这：条件跨轮沿用，看过一眼就一直挂着。"""
        profile = self.adapter._profile_from_turns(
            ["研究生支教团 还能报吗", "还有别的吗，我想去京津冀"])
        self.assertEqual(profile["grade"], "")
        self.assertIn("北京", profile["preferred_locations"])

    # ---- 不能误摘 ----

    def test_a_generic_theme_word_is_still_a_real_condition(self):
        """「乡村振兴」同时是库里某个标题的一部分，但用户是在说主题偏好。"""
        profile = self.adapter._profile_from_turns(["我想找乡村振兴主题的实践"])
        self.assertIn("乡村振兴", profile["themes"])

    def test_the_users_own_grade_still_counts(self):
        profile = self.adapter._profile_from_turns(["我是研究生，想找教育帮扶类的"])
        self.assertEqual(profile["grade"], "研究生")

    def test_grade_survives_when_stated_alongside_a_named_project(self):
        """项目名摘掉，用户自己那句话还在。"""
        profile = self.adapter._profile_from_turns(
            ["宝庆微光 还能报吗", "我是研究生，还有别的吗"])
        self.assertEqual(profile["grade"], "研究生")

    def test_naming_a_project_still_resolves_to_it(self):
        """摘的只是抽条件用的那份文本，定位项目本身不受影响。"""
        project = self.adapter._resolve_project([], "宝庆微光 怎么报名", latest_only=True, loose=True)
        self.assertIsNotNone(project)
        self.assertEqual(project["id"], "p-bqwg")

    def test_plain_requests_are_left_alone(self):
        for text in ["我想去湖南支教", "有没有北京的调研项目", "推荐几个乡村振兴的"]:
            with self.subTest(text=text):
                self.assertEqual(self.adapter._strip_project_names(text), text)


if __name__ == "__main__":
    unittest.main()
