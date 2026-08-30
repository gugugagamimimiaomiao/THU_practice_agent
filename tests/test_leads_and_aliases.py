"""看线索要真的看线索；院系公众号别名；非实践内容排后面。

三条都来自 2026-08-29 的实测反馈。

一、「看看那 22 条线索」是我们自己在「接下来可以说」里教用户说的一句话，
   可它一直没有对应的分支——落进 LIST_WORDS 里的「看看」，先甩一屏正式
   推荐，用户要的线索被埋在底下。_next_steps 的注释里写着「提一件做不到
   的事比不提更糟」，这就是自己犯了那一条。

二、院系公众号别名。「酒井资讯」是计算机系的号，「莱小福」是生命学院的，
   号名里看不出院系。每一条的依据都来自库里这个号发的文章正文里提到该
   院系的次数，不靠回忆——凭印象猜「象图学院是哪个系」，错了没人看得出来，
   而后果是把一批项目静默归给错误的院系。

   **校级号绝对不能进别名表**：「清华大学社会实践」的正文里机械系出现 6 次、
   新雅书院 6 次，映射过去会把全校项目算成某一个院系的。

三、体育队招新、学生节征名、部门招新不是社会实践，但确实在库里。不删，
   只压到后面——用户问「推荐几个暑期实践」时它们不该出现在前排。
"""
import tempfile
import unittest
from pathlib import Path

import chat_adapter
from chat_adapter import PracticeChatAdapter
from database import Database
from domain import (DEPARTMENT_ACCOUNT_ALIASES, SCHOOLWIDE_ACCOUNTS, _is_practice_like,
                    department_affinity, department_of_account, recommend_projects)


class ListLeadsTests(unittest.TestCase):
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

    def reply(self, text):
        return self.adapter.reply([{"role": "user", "content": text}])

    def test_asking_for_leads_shows_leads(self):
        for text in ["看看那 22 条线索", "线索都有哪些", "还有哪些没核实的"]:
            with self.subTest(text=text):
                result = self.reply(text)
                self.assertEqual(result.intent, "list_leads")

    def test_leads_are_not_buried_under_the_published_list(self):
        """原来先甩一屏正式推荐，用户得自己在两段之间找。"""
        content = self.reply("看看那几条线索").content
        self.assertNotIn("当前可正式推荐的已核验项目", content)
        self.assertIn("待核验", content.split("\n")[0])

    def test_the_lead_section_states_its_own_count(self):
        """线索区列 3 条，「接下来可以说」写「看看那 22 条线索」——
        两个数字对不上，看起来像在自相矛盾。"""
        content = self.reply("推荐几个实践").content
        if "## 线索" not in content:
            self.skipTest("这次没有待核验线索")
        head = next(line for line in content.split("\n") if "## 线索" in line)
        self.assertIn("共", head)


class AccountAliasTests(unittest.TestCase):
    def test_evidence_backed_aliases_resolve(self):
        self.assertEqual(department_of_account("酒井资讯"), "计算机系")
        self.assertEqual(department_of_account("莱小福"), "生命学院")
        self.assertEqual(department_of_account("建院宣传中心"), "建筑学院")

    def test_schoolwide_accounts_never_map_to_a_department(self):
        """最要紧的一条。「清华大学社会实践」的正文里机械系出现 6 次、
        新雅书院 6 次——映射过去会把全校项目静默算成某一个院系的，
        比不加权还糟，因为它是错的而不是没有。"""
        for account in ("清华大学社会实践", "清华大学学生公益", "清华紫荆之声",
                        "清华大学小研在线", "清小搭对话导入"):
            with self.subTest(account=account):
                self.assertEqual(department_of_account(account), "")

    def test_no_schoolwide_account_leaked_into_the_alias_table(self):
        for account in DEPARTMENT_ACCOUNT_ALIASES:
            self.assertNotIn(account, SCHOOLWIDE_ACCOUNTS)

    def test_an_unknown_account_maps_to_nothing(self):
        """查不出对应院系的号故意不进表——猜错了没人看得出来。"""
        self.assertEqual(department_of_account("天工物华"), "")
        self.assertEqual(department_of_account("某个没见过的号"), "")

    def test_college_accounts_resolve_through_their_short_name(self):
        """书院不用进别名表：号名本身就带简称。"""
        self.assertEqual(department_of_account("THU长乐未央"), "未央书院")
        self.assertEqual(department_of_account("探微观止"), "探微书院")

    def test_the_alias_drives_affinity(self):
        project = {"title": "实践招募丨赴某地支教实践支队", "summary": "",
                   "organizer": "", "source_account": "酒井资讯", "theme_tags": []}
        self.assertTrue(department_affinity(project, "计算机系"))
        self.assertFalse(department_affinity(project, "美术学院"))

    def test_a_schoolwide_account_does_not_grant_affinity(self):
        project = {"title": "实践招募丨机械系某支队赴某地实践", "summary": "",
                   "organizer": "", "source_account": "清华大学社会实践",
                   "theme_tags": []}
        # 标题里确实写了机械系，那条照常算；但不能因为"号发过机械系的文章"
        # 就把别的院系也算进来。
        self.assertTrue(department_affinity(project, "机械系"))
        self.assertFalse(department_affinity(project, "新雅书院"))


class NonPracticeIsSetAsideTests(unittest.TestCase):
    """校内活动不进正式推荐，但也不删——单独一桶。

    原来是扣 12 分压到后面。后来量了真实数据：已发布的 25 条里只有 8 条是
    实践，17 条是体育代表队、学生组织招新、学科竞赛。池子里三分之二是杂物，
    扣多少分都会有几条挤进前五。改成分桶。
    """

    def _project(self, title, **extra):
        project = {"id": title, "title": title, "summary": "", "status": "published",
                   "theme_tags": ["公益志愿"], "uncertain_fields": [], "confidence": 0.9,
                   "eligibility": {}, "reimbursement": {}, "location": {},
                   "signup_deadline": "2099-12-31", "source_account": "", "organizer": ""}
        project.update(extra)
        return project

    def _recommend(self, *titles):
        projects = [self._project(t) for t in titles]
        return recommend_projects(projects, {"themes": [], "preferred_locations": []})

    def test_a_sports_team_never_enters_the_recommendation(self):
        result = self._recommend("实践招募丨赴某地支教实践支队招募",
                                 "【代表队招新】体育集结号——贵系男篮篇")
        listed = [item["project"]["title"] for item in result["eligible"]]
        self.assertIn("实践招募丨赴某地支教实践支队招募", listed)
        self.assertNotIn("【代表队招新】体育集结号——贵系男篮篇", listed)

    def test_it_is_set_aside_not_deleted(self):
        """判错了要能翻出来，采集也是一条条攒的，不该直接扔。"""
        result = self._recommend("【代表队招新】体育集结号——贵系男篮篇")
        aside = [item["project"]["title"] for item in result["campus"]]
        self.assertIn("【代表队招新】体育集结号——贵系男篮篇", aside)

    def test_a_real_practice_is_never_set_aside(self):
        result = self._recommend("实践招募丨赴某地支教实践支队招募",
                                 "志愿者招募 | 社区暑期儿童科普课堂")
        self.assertEqual(result["campus"], [])
        self.assertEqual(len(result["eligible"]), 2)

    def test_the_scoring_helper_agrees_with_the_classifier(self):
        """打分里那道判据和采集侧的分类器必须是同一套，不能各说各话。"""
        for title, want in (("实践招募丨赴某地支教实践支队招募", True),
                            ("【代表队招新】体育集结号——贵系男篮篇", False),
                            ("2026年秋校团委宣传部学生骨干招募", False)):
            with self.subTest(title=title):
                self.assertEqual(_is_practice_like(self._project(title)), want)


if __name__ == "__main__":
    unittest.main()
