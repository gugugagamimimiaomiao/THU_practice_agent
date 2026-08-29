"""2026-08-21 第三轮（100 轮 agent 地毯式扫描）挖出来的问题。

这一轮是让一个独立 agent 用 100 条互不重复的真实问法打线上库。它抓到的东西
比我自己造的用例狠得多——下面每条都对应报告里一个可复现的失败。
"""
import tempfile
import unittest
from pathlib import Path

import chat_adapter
from chat_adapter import PracticeChatAdapter, _cell, _STATUS_LABELS
from database import Database
from wechat_ingest import import_article_text


class SilentConstraintTests(unittest.TestCase):
    """P0-1：11 句完全不同的话返回逐字节相同的回复。

    扫描时做了 md5 校验，下面这些的输出 hash 全一样、长度全是 894：

        校内的有吗 / 有没有管吃管住报销路费的 / 补贴超过2000的有没有 /
        大一新生能报什么 / 我是研究生，有啥适合我的 / 机械系的有吗 /
        推荐三个 / 国庆期间有实践吗 …

    回复里一个字都没提用户给的条件满不满足。用户以为筛过了，其实没有。
    地域和时间已经各有一段说明，但口子远不止这两个。
    """

    @classmethod
    def setUpClass(cls):
        cls.tempdir = tempfile.TemporaryDirectory()
        cls.database = Database(Path(cls.tempdir.name) / "chat.db")
        import_article_text(
            cls.database,
            {"title": "赴湖南新宁支教实践支队招募", "source_account": "清华大学社会实践",
             "source_url": "https://mp.weixin.qq.com/s/scan1"},
            "现面向全校招募队员。\n报名截止：2036年9月10日\n参与资格：全校本科生\n"
            "报名方式：扫码\n经费：往返交通费全额报销\n",
        )

    @classmethod
    def tearDownClass(cls):
        cls.tempdir.cleanup()

    def setUp(self):
        chat_adapter.llm.is_enabled = lambda: False
        self.adapter = PracticeChatAdapter(self.database)

    def reply(self, text: str):
        return self.adapter.reply([{"role": "user", "content": text}])

    def test_each_stated_condition_gets_a_receipt(self):
        for text, expected in [
            ("大一新生能报什么", "年级"),
            ("机械系的有吗", "院系"),
            ("有没有管吃管住报销路费的", "经费"),
        ]:
            with self.subTest(text=text):
                self.assertIn(expected, self.reply(text).content,
                              f"「{text}」的回复里完全没提这个条件")

    def test_colloquial_money_phrasings_are_recognised(self):
        for text in ["有没有管吃管住报销路费的", "补贴超过2000的有没有",
                     "包吃住的实践", "有补贴的项目"]:
            with self.subTest(text=text):
                self.assertNotEqual(
                    self.adapter._extract_profile(text)["reimbursement_preference"],
                    "not_important", f"「{text}」里的经费要求没读出来")

    def test_asking_for_n_returns_n(self):
        for text, want in [("推荐三个", 3), ("给我两个实践", 2), ("来五个项目", 5)]:
            with self.subTest(text=text):
                self.assertEqual(self.adapter._extract_profile(text)["wanted_count"], want)

    def test_different_questions_do_not_produce_identical_replies(self):
        """这条是整组的兜底：不同条件必须产生不同回复。"""
        seen = {}
        for text in ["大一新生能报什么", "机械系的有吗", "有没有管吃管住报销路费的",
                     "推荐三个", "推荐一些实践"]:
            body = self.reply(text).content
            self.assertNotIn(body, seen, f"「{text}」和「{seen.get(body)}」返回了完全相同的回复")
            seen[body] = text


class OrdinalNeverFuzzyTests(unittest.TestCase):
    """P0-2：「第二个」被拿去模糊匹配标题里的「第二批」。

    扫描时三处独立复现，全部串到用户从未见过的项目，而且返回的是已截止
    三个月的项目卡，还煞有介事地列出经费原文。

    根因：序数处理原来包在 `if not latest_only:` 里，而 reply() 后半段有一次
    latest_only=True 的调用，走到那里序数逻辑被跳过，直接掉进模糊匹配。
    """

    @classmethod
    def setUpClass(cls):
        cls.tempdir = tempfile.TemporaryDirectory()
        cls.database = Database(Path(cls.tempdir.name) / "chat.db")
        for title in ["以爱为炬赴夏之约唐社暑假实践支队招募（第二批）",
                      "筑梦建行春山在望中学生支教夏令营招募（第一批次）",
                      "赴湖南新宁支教实践支队招募"]:
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

    def test_ordinal_never_matches_a_batch_number_in_a_title(self):
        """没有列表可参照时，「第二个」宁可答不上来，也不能命中「第二批」。"""
        for text in ["第二个详细说说", "刚才第一个是什么来着", "第一个的经费怎么样"]:
            with self.subTest(text=text):
                bound = self.adapter._resolve_project(
                    [{"role": "user", "content": text}], text)
                self.assertIsNone(bound, f"「{text}」绑到了「{bound['title'] if bound else ''}」")

    def test_latest_only_path_also_respects_ordinals(self):
        text = "第二个详细说说"
        self.assertIsNone(self.adapter._resolve_project([], text, latest_only=True))


class NegationScopeTests(unittest.TestCase):
    """P0-3：「我想去云南但是不要支教」把云南也排除了。

    整句没有标点，被当成一个否定小句，于是用户明确想去的地方进了排除表，
    回复是「我理解你想避开：云南、教育、支教」——跟他说的正好相反。
    """

    def setUp(self):
        chat_adapter.llm.is_enabled = lambda: False
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.adapter = PracticeChatAdapter(Database(Path(self.tempdir.name) / "c.db"))

    def test_contrastive_conjunction_splits_the_clause(self):
        profile = self.adapter._extract_profile("我想去云南但是不要支教")
        self.assertIn("云南", profile["preferred_locations"])
        self.assertNotIn("云南", profile["excluded_locations"])
        self.assertIn("教育", profile["excluded_themes"])

    def test_other_conjunctions(self):
        for text in ["想去河南不过不要学生骨干", "去青海，只是不想讲课"]:
            with self.subTest(text=text):
                profile = self.adapter._extract_profile(text)
                self.assertTrue(profile["preferred_locations"],
                                f"「{text}」里想去的地方被否定辖域吃掉了")


class WritingFlowContinuityTests(unittest.TestCase):
    """P0-4：写作流一追问就断。

    扫描里四个不同材料类型上都断了：

        帮宝庆微光写个招募推送   → 正常出稿
        标题再抓人一点           → 「先告诉我给哪个项目写推送」
        把报名截止时间也加上     → 「请提供项目名称…以及报名截止时间」
        加两个关于家庭情况的问题 → 「这句我没接住」

    改稿是学生拿到材料后必然会做的第二步。断在这里比第一版稿子写得一般严重得多。
    """

    @classmethod
    def setUpClass(cls):
        cls.tempdir = tempfile.TemporaryDirectory()
        cls.database = Database(Path(cls.tempdir.name) / "chat.db")
        import_article_text(
            cls.database,
            {"title": "赴湖南新宁支教实践支队招募", "source_account": "清华大学社会实践",
             "source_url": "https://mp.weixin.qq.com/s/flow1"},
            "现面向全校招募队员。\n报名截止：2036年9月10日\n参与资格：全校本科生\n报名方式：扫码\n",
        )

    @classmethod
    def tearDownClass(cls):
        cls.tempdir.cleanup()

    def setUp(self):
        chat_adapter.llm.is_enabled = lambda: False
        self.adapter = PracticeChatAdapter(self.database)

    def _after_material(self, follow_up: str):
        first = self.adapter.reply(
            [{"role": "user", "content": "帮我写赴湖南新宁支教实践支队招募的报名理由"}])
        return self.adapter.reply([
            {"role": "user", "content": "帮我写赴湖南新宁支教实践支队招募的报名理由"},
            {"role": "assistant", "content": first.content},
            {"role": "user", "content": follow_up},
        ])

    def test_material_output_carries_its_project(self):
        result = self.adapter.reply(
            [{"role": "user", "content": "帮我写赴湖南新宁支教实践支队招募的报名理由"}])
        self.assertIn("为「赴湖南新宁支教实践支队招募」", result.content,
                      "材料输出没写明是给谁做的，下一轮就认不回来了")

    def test_next_material_keeps_the_binding(self):
        for follow_up, expected in [("再给它写个访谈提纲", "generate_interview"),
                                    ("外联话术也来一份", "generate_outreach")]:
            with self.subTest(follow_up=follow_up):
                self.assertEqual(self._after_material(follow_up).intent, expected)

    def test_edit_phrasings_the_scan_found(self):
        for text in ["标题再抓人一点", "加两个关于受助学生家庭情况的问题",
                     "把最后一部分删掉", "再具体一些", "换个语气", "再来一版"]:
            with self.subTest(text=text):
                self.assertTrue(
                    self.adapter._is_revision_request(
                        text, [{"role": "assistant", "content": "尊敬的老师：我希望申请加入。"}]),
                    f"「{text}」没被认成改稿指令")

    def test_ordinary_questions_are_not_edits(self):
        history = [{"role": "assistant", "content": "尊敬的老师：我希望申请加入。"}]
        for text in ["推荐一些实践", "有没有加分的项目", "这个项目怎么样"]:
            with self.subTest(text=text):
                self.assertFalse(self.adapter._is_revision_request(text, history))

    def test_revision_prompt_forbids_asking_the_user_for_data(self):
        """实测：用户说「把报名截止时间也加上」，模型回「请提供…报名截止时间」。
        他要是知道，就不会来问了。

        断言的是真正发给模型的那段 system prompt——把它抓出来是为了让这条
        约束不会在后续改写 prompt 时被顺手删掉。
        """
        captured = {}
        chat_adapter.llm.is_enabled = lambda: True
        chat_adapter.llm.complete = lambda system, user, **kw: captured.setdefault("s", system) and "改好了" or "改好了"
        self.adapter.reply([
            {"role": "user", "content": "帮我写报名理由"},
            {"role": "assistant", "content": "尊敬的老师：我希望申请加入贵支队，长期关注乡村教育。"},
            {"role": "user", "content": "把报名截止时间也加上"},
        ])
        self.assertIn("不要反过来向用户要信息", captured.get("s", ""))
        self.assertIn("有字数要求就严格照做", captured.get("s", ""))


class FabricatedInstitutionTests(unittest.TestCase):
    """P1-7：外联方案批量生成不存在的机构名。

    模板拿 location.detail 去拼机构类别，而那常常是一所具体学校：

        邵阳市新宁县第一中学教育行政部门
        邵阳市新宁县第一中学青少年活动中心/科普场馆

    四个现实中不存在的机构名，还进了「推荐地点」表格。这比空着危险得多——
    它看起来像真的，学生会拿着去搜。
    """

    PROJECT = {
        "title": "赴湖南新宁支教实践支队招募", "theme_tags": ["教育"], "summary": "支教实践",
        "location": {"province": "湖南", "city": "邵阳市", "detail": "湖南省邵阳市新宁县第一中学"},
    }

    def test_admin_area_stops_at_the_administrative_level(self):
        from domain import _admin_area
        self.assertEqual(_admin_area(self.PROJECT), "邵阳市新宁县")

    def test_site_names_never_glue_a_venue_to_an_institution_type(self):
        from domain import recommend_local_sites
        for option in recommend_local_sites(self.PROJECT)["options"]:
            self.assertNotIn("第一中学教育行政部门", option["name"])
            self.assertNotIn("第一中学青少年活动中心", option["name"])

    def test_the_real_venue_is_still_offered(self):
        from domain import recommend_local_sites
        names = [option["name"] for option in recommend_local_sites(self.PROJECT)["options"]]
        self.assertTrue(any("第一中学" in name for name in names),
                        "真实场馆反而被丢掉了")


class TableAndStatusTests(unittest.TestCase):
    """P1-6 / P2-10：标题里的 `|` 撑坏表格；published/expired 直出英文。"""

    def test_pipe_in_title_is_escaped(self):
        self.assertNotIn("|", _cell("实践招募 | 机械系“宝庆微光”赴湖南新宁支教实践支队招募"))

    def test_newlines_are_flattened(self):
        self.assertNotIn("\n", _cell("第一行\n第二行"))

    def test_status_is_translated(self):
        for internal in ("published", "expired", "needs_review"):
            with self.subTest(internal=internal):
                self.assertIn(internal, _STATUS_LABELS)
                self.assertNotIn(internal, _STATUS_LABELS[internal])


class NextStepsTests(unittest.TestCase):
    """用户反馈：「它答完以后会说我还可以 xxx 哦，这里面有漏的」。

    引导语原来只提「比较前两个」和「帮我写第一个的报名理由」两件事，而系统
    还能写访谈提纲、外联话术、调研报告、推送文案，能解释排序依据，能放宽条件。
    用户看不见就等于没有——100 轮扫描里很多能力从头到尾没被触发过。
    """

    @classmethod
    def setUpClass(cls):
        cls.tempdir = tempfile.TemporaryDirectory()
        cls.database = Database(Path(cls.tempdir.name) / "chat.db")
        for i in range(3):
            import_article_text(
                cls.database,
                {"title": f"赴某地实践支队招募第{i}期", "source_account": "清华大学社会实践",
                 "source_url": f"https://mp.weixin.qq.com/s/ns{i}"},
                "现面向全校招募队员。\n报名截止：2036年9月10日\n参与资格：全校本科生\n报名方式：扫码\n",
            )

    @classmethod
    def tearDownClass(cls):
        cls.tempdir.cleanup()

    def setUp(self):
        chat_adapter.llm.is_enabled = lambda: False
        self.adapter = PracticeChatAdapter(self.database)

    def test_writing_capabilities_are_advertised(self):
        content = self.adapter.reply([{"role": "user", "content": "推荐一些实践"}]).content
        for capability in ("访谈提纲", "外联话术", "调研报告", "推送文案"):
            self.assertIn(capability, content, f"引导语里没提「{capability}」，用户不会知道能这么问")

    def test_explain_is_advertised(self):
        content = self.adapter.reply([{"role": "user", "content": "推荐一些实践"}]).content
        self.assertIn("你为什么这么推荐", content)

    def test_relaxing_is_only_offered_when_it_applies(self):
        """提一件做不到或用不上的事，比不提更糟。"""
        content = self.adapter.reply([{"role": "user", "content": "推荐一些实践"}]).content
        self.assertNotIn("不限地点再看看", content, "没说地域却提示放宽地域")


if __name__ == "__main__":
    unittest.main()
