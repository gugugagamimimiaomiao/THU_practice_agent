import unittest

from opportunity_filter import candidate_decision


class OpportunityFilterTests(unittest.TestCase):
    def test_body_recruitment_can_pass_without_title_keyword(self):
        result = candidate_decision({"title": "本周实践活动", "content": "现面向全校招募志愿者，报名方式：扫描二维码填写问卷。"})
        self.assertTrue(result["candidate"])

    def test_preview_cannot_become_opportunity(self):
        result = candidate_decision({"title": "赴韩国实践支队行前预告", "content": "欢迎大家加入关注。"})
        self.assertFalse(result["candidate"])
        self.assertTrue(result["hard_excluded"])

    def test_preview_with_action_is_audited_but_still_not_promoted(self):
        result = candidate_decision({"title": "实践支队行前预告", "content": "申请方式请见原文。"})
        self.assertFalse(result["candidate"])
        self.assertFalse(result["hard_excluded"])

    def test_practice_recap_cannot_become_opportunity(self):
        result = candidate_decision({"title": "学生社区志愿劳动侧记", "content": "文中回顾了此前志愿者报名和招募情况。"})
        self.assertFalse(result["candidate"])
        self.assertTrue(result["hard_excluded"])

    def test_recruitment_title_is_not_rejected_by_a_historical_body_reference(self):
        result = candidate_decision({"title": "2026年秋校团委志愿中心组长招募", "content": "上一届工作已结项，现开放报名通道并招募新组长。"})
        self.assertTrue(result["candidate"])

    def test_all_server_title_exclusions_are_rejected(self):
        for phrase in (
            "资源推介", "基地推介", "基地介绍", "风采展示", "实践日报", "工作简报",
            "实践简报", "实践总结", "实践纪实", "经验分享", "心得体会", "选课说明",
            "学习说明", "培训说明", "课程说明", "圆满结束", "顺利举行", "圆满举行",
            "成功举办", "结项", "成果展示", "行前预告", "活动回顾", "侧记",
        ):
            with self.subTest(phrase=phrase):
                result = candidate_decision({"title": f"{phrase}｜清华活动", "content": ""})
                self.assertFalse(result["candidate"])
                self.assertTrue(result["hard_excluded"])

    def test_title_recruitment_signal_overrides_editorial_exclusion(self):
        result = candidate_decision({
            "title": "实践基地进清华｜2026年项目成果展示，招募启动！",
            "content": "",
        })
        self.assertTrue(result["candidate"])
        self.assertFalse(result["hard_excluded"])

    def test_body_only_recruitment_does_not_override_recap_title(self):
        result = candidate_decision({
            "title": "暑期实践活动回顾",
            "content": "回顾此前志愿者招募和报名情况。",
        })
        self.assertFalse(result["candidate"])

    def test_explicit_recruitment_end_is_never_overridden(self):
        result = candidate_decision({
            "title": "志愿者报名结束通知",
            "content": "本轮招募已经结束，请关注后续活动。",
        })
        self.assertFalse(result["candidate"])

    def test_future_steps_after_signup_closes_do_not_mean_signup_is_closed(self):
        result = candidate_decision({
            "title": "报名开启丨2026年紫荆杯暑期电竞大赛",
            "content": (
                "报名时间：即日起至7月23日。报名方式：填写报名问卷。"
                "具体赛程将在报名结束后进行安排。"
            ),
        })
        self.assertTrue(result["candidate"], result["reasons"])


class RealDataRegressionTests(unittest.TestCase):
    """来自真实公众号数据的回归。标题和文风照抄线上样本，只改掉具体地名。

    背景：协作方推来 67 篇真实推送后，发现 28 篇「实践基地进清华 | XX基地
    资源推介」里有 24 篇被判成了可报名机会，生成了假的项目卡；剩下 4 篇被
    拦下纯属侥幸——那 4 篇正文里恰好没出现过「招募」两个字。
    """

    BASE_INTRO_BODY = (
        "本期为大家介绍位于某省某市的实践基地。基地依托当地产业优势，"
        "已累计接待清华大学师生 300 余人次。往年共有 12 支支队在此开展调研，"
        "基地曾配合支队完成队员招募与行前对接工作。基地可提供住宿、"
        "会议室与产业参观资源，欢迎各院系了解。"
    )

    def test_base_introduction_is_not_an_opportunity(self):
        # 正文里飘过一次「招募」，说明这篇文章提到了招募，不说明它本身在招募。
        result = candidate_decision({
            "title": "实践基地进清华 | 某省某市基地资源推介",
            "content": self.BASE_INTRO_BODY,
        })
        self.assertFalse(result["candidate"], result["reasons"])

    def test_same_series_but_actually_recruiting_still_passes(self):
        # 同一个系列、同样的标题前缀，但这一篇真的在招募，不能一并拦掉。
        result = candidate_decision({
            "title": "实践基地进清华｜2026年“揭榜挂帅”项目，招募启动！",
            "content": "现面向全校招募支队承接基地课题。报名方式：填写问卷并提交方案。",
        })
        self.assertTrue(result["candidate"], result["reasons"])

    def test_interview_feature_about_past_practice_is_not_an_opportunity(self):
        # 采访报道：正文里有「招募」「志愿者」「加入」，但没有任何报名线索。
        result = candidate_decision({
            "title": "红色寻迹丨口述史传承烽火记忆，把答卷写在祖国大地上",
            "content": (
                "Q：当初是什么契机让你选择这一支队？\n"
                "柯同学：看到支队招募时我就报了名，后来又加入了口述史整理组，"
                "和志愿者们一起采访了多位老战士。\n"
                "Q：这次实践最大的收获是什么？"
            ),
        })
        self.assertFalse(result["candidate"], result["reasons"])

    def test_real_recruitment_titles_all_pass(self):
        # 线上 27 篇判为机会的真实标题里挑出的代表，一条都不能误伤。
        body = "现面向全校招募队员。报名方式：填写报名表并发送至邮箱。招募对象：本科生、研究生。"
        for title in [
            "实践招募 | 机械系“宝庆微光”赴湖南新宁支教实践支队招募",
            "实践招募丨新雅“一事”计划 基层助力—孝昌乡村振兴项目",
            "志愿者招募 | 新楼新“净”界：邀你一起为同学的新家“测健康”",
            "“助梦1+1”导师招募丨助梦西部学子，成就青春梦想",
            "新一批！“青年实干家计划——百名硕博建功邯郸”行动2026年度招募公告",
            "2026年秋校团委学习实践部组长、副组长及组员招募",
        ]:
            with self.subTest(title=title):
                self.assertTrue(candidate_decision({"title": title, "content": body})["candidate"])

    def test_daily_report_and_course_notice_are_not_opportunities(self):
        for title, content in [
            ("2026年7月11日实践日报", "今日各支队分别在多地开展调研，累计走访 8 个村庄。"),
            ("2026年暑期学生社会实践培训课程学习说明", "请已报名同学按课表完成学习，未完成者影响结项。"),
            ("关于2025-2026学年夏季学期“思政实践”课的选课说明", "选课流程如下，请同学们按院系安排选课。"),
            ("实践总结丨“笃行津城·实践致新”赴天津联合学期中实践总结", "支队圆满完成全部调研任务，感谢志愿者们的付出。"),
        ]:
            with self.subTest(title=title):
                self.assertFalse(candidate_decision({"title": title, "content": content})["candidate"])


if __name__ == "__main__":
    unittest.main()
