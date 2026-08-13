from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from database import Database
from domain import extract_project, generate_asset, recommend_local_sites, recommend_projects, score_project


NOTICE = """乡村数字教育实践招募
主办单位：清华大学某实践团队
实践时间：2026年8月12日—2026年8月18日
实践地点：云南省大理州（线下）
招募对象：面向全校本科生、研究生，无专业限制
报名截止：2026年7月29日
经费说明：提供每人1800元交通与住宿补贴
报名方式：扫描原文二维码填写报名表
所需材料：报名表、个人陈述，后续安排面试
项目围绕乡村学校数字资源使用与教师培训开展调研。
"""


class ExtractionTests(unittest.TestCase):
    def test_extracts_critical_fields_and_evidence(self):
        project = extract_project(
            NOTICE,
            {
                "source_account": "清华大学社会实践",
                "source_url": "https://mp.weixin.qq.com/s?__biz=demo&mid=1&idx=1&sn=abc&utm_source=x",
                "input_type": "copied_text",
            },
            today=date(2026, 7, 13),
        )
        self.assertEqual(project["title"], "乡村数字教育实践招募")
        self.assertEqual(project["signup_deadline"], "2026-07-29")
        self.assertEqual(project["practice_start"], "2026-08-12")
        self.assertEqual(project["practice_end"], "2026-08-18")
        self.assertEqual(project["location"]["province"], "云南")
        self.assertTrue(project["eligibility"]["explicit_no_restriction"])
        self.assertTrue(project["reimbursement"]["has_reimbursement"])
        self.assertEqual(project["reimbursement"]["amount"], 1800.0)
        self.assertEqual(project["status"], "published")
        self.assertIn("signup_deadline", project["field_evidence"])
        self.assertNotIn("utm_source", project["source_url"])

    def test_ocr_missing_critical_fields_needs_review(self):
        project = extract_project(
            "社区儿童空间调研\n地点：上海\n提供500元补贴",
            {"input_type": "ocr_text", "source_account": "用户投稿"},
            today=date(2026, 7, 13),
        )
        self.assertEqual(project["status"], "needs_review")
        self.assertIn("signup_deadline", project["uncertain_fields"])
        self.assertIn("source_url", project["uncertain_fields"])
        self.assertTrue(any("OCR" in note for note in project["risk_notes"]))

    def test_expired_deadline_is_expired(self):
        notice = NOTICE.replace("2026年7月29日", "2026年6月29日")
        project = extract_project(notice, {"source_url": "https://example.com/a"}, today=date(2026, 7, 13))
        self.assertEqual(project["status"], "expired")

    def test_demo_notice_is_explicitly_marked(self):
        project = extract_project(
            NOTICE.replace("乡村数字教育实践招募", "乡村数字教育实践招募（演示通知）"),
            {"source_url": "https://example.invalid/demo"},
            today=date(2026, 7, 13),
        )
        self.assertTrue(project["demo_data"])
        self.assertEqual(sum("演示数据" in note for note in project["risk_notes"]), 1)

    def test_preserves_multiple_explicit_schedule_segments(self):
        project = extract_project(
            "社区科普志愿者招募\n活动时间及内容\n具身智能科普课堂\n2026年7月20日—7月24日（每日下午）\n城市职业启蒙科普课堂\n2026年7月20日—7月23日\n7月27日—7月30日（每日上午）\n报名方式：原文二维码报名",
            {"source_url": "https://mp.weixin.qq.com/s?mid=schedule", "input_type": "ocr_text"},
            today=date(2026, 7, 13),
        )
        self.assertEqual(project["practice_start"], "2026-07-20")
        self.assertEqual(project["practice_end"], "2026-07-30")
        self.assertEqual(len(project["schedule_segments"]), 3)
        self.assertEqual(project["schedule_segments"][0]["period"], "下午")

    def test_extracts_dates_below_a_time_arrangement_heading(self):
        project = extract_project(
            "志愿者招募\n时间安排\n同源大厦：6月1日17点-18点；6月2日9点-11点。\n荷清大厦：7月28日18点-19点；7月29日9点-11点。\n报名方式：扫描二维码报名。",
            {"input_type": "wechat_batch", "publish_date": "2026-05-20"},
        )
        self.assertEqual(project["practice_start"], "2026-06-01")
        self.assertEqual(project["practice_end"], "2026-07-29")
        self.assertNotIn("practice_dates", project["uncertain_fields"])


class PublishGateTests(unittest.TestCase):
    """发布门槛与过期判定。取消人工核验后，这两条直接决定学生看到什么。"""

    META = {"title": "支队招募", "source_url": "https://mp.weixin.qq.com/s/gate"}

    def test_missing_optional_fields_still_publishes(self):
        # 原来要求 5 个关键字段全齐，实测真实数据里 published 直接是 0——
        # 而缺字段多半是原文没写，不是抽取失败，不该由学生承担。
        project = extract_project("某某支队招募队员，欢迎报名。报名方式：详见原文。", self.META)
        self.assertEqual(project["status"], "published")

    def test_without_a_source_link_it_still_needs_review(self):
        # 原文链接是最后的退路：其他字段都能写"未写明"，唯独没有出处时
        # 学生无从核对，这种不能直接进正式推荐。
        project = extract_project("某某支队招募队员，欢迎报名。", {"title": "支队招募"})
        self.assertEqual(project["status"], "needs_review")
        self.assertIn("source_url", project["uncertain_fields"])

    def test_finished_practice_expires_even_without_a_deadline(self):
        # 真实数据里 30 条属于这种：没写报名截止，但实践 7 月就做完了。
        # 只看报名截止的话，它们会一直挂在推荐里。
        notice = "支队招募\n实践时间：2026年7月2日至2026年7月9日\n报名方式：扫码"
        project = extract_project(notice, self.META, today=date(2026, 8, 13))
        self.assertEqual(project["status"], "expired")

    def test_upcoming_practice_is_not_expired(self):
        notice = "支队招募\n实践时间：2026年8月24日至2026年8月31日\n报名方式：扫码"
        project = extract_project(notice, self.META, today=date(2026, 8, 13))
        self.assertEqual(project["status"], "published")

    def test_mixed_notice_uses_the_last_open_signup_deadline(self):
        notice = "\n".join([
            "篮球邀请赛预告、领票通知及志愿者招募",
            "观众报名通道于8月12日23:00截止。",
            "现面向全校同学招募志愿者，志愿工作时间为8月16日下午。",
            "志愿者招募于8月14日12:00截止。",
            "报名方式：扫描原文二维码。",
        ])
        project = extract_project(notice, self.META, today=date(2026, 8, 13))
        self.assertEqual(project["signup_deadline"], "2026-08-14")
        self.assertEqual(project["practice_start"], "2026-08-16")
        self.assertEqual(project["practice_end"], "2026-08-16")
        self.assertEqual(project["status"], "published")

    def test_extracts_deadline_written_as_register_before_date(self):
        notice = "\n".join([
            "ICBS 2026 清华专场报名",
            "时间：2026年8月15日9:30-17:30",
            "请扫描二维码填写问卷报名",
            "请于2026年8月14日上午10:00前扫码报名",
        ])
        project = extract_project(notice, self.META, today=date(2026, 8, 13))
        self.assertEqual(project["signup_deadline"], "2026-08-14")
        self.assertEqual(project["status"], "published")

    def test_lead_in_line_is_not_mistaken_for_the_eligibility(self):
        """「报名要求：我们希望你是：」本身不含条件，真正的条件在后面几行。

        取消人工核验后这条会直接显示给学生，所以既不能拿引导语充数，
        也不能顺手把下面的「报名方式」一起吞进资格说明里。
        """
        notice = "\n".join([
            "2026年秋校团委志愿中心组长招募",
            "报名要求",
            "我们希望你是：",
            "年级不限，有相关工作经验者优先",
            "对志愿公益有热情",
            "报名方式：发送简历至邮箱",
        ])
        text = extract_project(notice, self.META)["eligibility"]["restriction_text"]
        self.assertIn("年级不限", text)
        self.assertNotIn("我们希望你是", text)
        self.assertNotIn("报名方式", text, "把下一个字段吞进资格说明里了")


class FlattenedNoticeTests(unittest.TestCase):
    """正文被压平成一整行、或资格标签不在关键词表里时的抽取。

    两种都来自真实数据：转发版和采集器压平的正文常常丢掉换行；而「参与资格」
    这个最常见的标签此前根本不在关键词表里，导致抽取器退而匹配开场白里的
    「现面向全校招募……」，把整句套话当成了参与资格。
    """

    NOTICE = "\n".join([
        "为引导同学在实践中了解国情民情，现面向全校招募赴云南乡村教育调研支队队员。",
        "实践地点：云南省大理白族自治州",
        "报名截止：2026年9月26日",
        "参与资格：全校本科生、研究生均可报名，优先考虑教育学、社会学相关专业",
    ])
    FLAT = (
        "为引导同学在实践中了解国情民情，现面向全校招募赴云南乡村教育调研支队队员。"
        "报名截止：2026年9月26日。实践地点：云南省大理白族自治州。欢迎踊跃报名。"
    )
    META = {"title": "调研支队招募", "source_url": "https://mp.weixin.qq.com/s/case"}

    def test_explicit_eligibility_label_beats_the_boilerplate_opening(self):
        project = extract_project(self.NOTICE, self.META)
        text = project["eligibility"]["restriction_text"]
        self.assertIn("全校本科生", text)
        self.assertNotIn("为引导同学", text, "抽到的是开场白套话，不是那一行参与资格")
        self.assertIn("本科生", project["eligibility"]["grades"])

    def test_flattened_body_does_not_swallow_neighbouring_sentences(self):
        project = extract_project(self.FLAT, self.META)
        detail = project["location"]["detail"]
        self.assertEqual(detail, "云南省大理白族自治州")
        self.assertNotIn("报名截止", detail, "地点字段里混进了别的字段")
        self.assertLessEqual(len(detail), 40)

    def test_absurdly_long_capture_is_dropped_rather_than_shown_as_confirmed(self):
        # 抽不干净时宁可留空、进 uncertain_fields 让人工核验，也不要给出一个
        # 看起来已确认、实际是一整段正文的字段。
        runon = "本次活动地点安排如下所述并将于近期公布详细方案" + "另有若干补充说明" * 12
        project = extract_project(runon, {"title": "无标签通知", "source_url": ""})
        self.assertLessEqual(len(project["location"]["detail"]), 40)


class NoticeLayoutTests(unittest.TestCase):
    """真实公众号排版的抽取。两种版式此前会整片抽错。"""

    def test_label_on_its_own_line_with_value_below(self):
        # 最标准的通知写法：小标题一行，值在下一行。抽取器只在同一行找值，
        # 结果截止日期抽不到，「二、实践地点」这行小标题反而成了地点。
        notice = "\n".join([
            "关于组建2026年赴福建龙岩红色文化调研支队的通知",
            "",
            "一、实践时间",
            "2026年9月10日至9月18日",
            "",
            "二、实践地点",
            "福建省龙岩市上杭县、长汀县",
            "",
            "五、报名截止",
            "2026年9月1日18:00",
        ])
        project = extract_project(notice, {}, today=date(2026, 8, 1))
        self.assertEqual(project["signup_deadline"], "2026-09-01")
        self.assertIn("龙岩", project["location"]["detail"])
        self.assertNotIn("实践地点", project["location"]["detail"])
        self.assertEqual(project["practice_start"], "2026-09-10")
        self.assertEqual(project["practice_end"], "2026-09-18")

    def test_fields_packed_into_one_line_with_pipes(self):
        # 微信里很常见：全部要素挤在一行、用 | 分隔。以前整行会被吞进地点，
        # 实践时间还会出现结束早于开始。
        notice = (
            "【招募】清华大学乡村振兴工作站赴云南剑川实践支队 | 实践时间：2026.09.05-09.14 | "
            "地点：云南省大理州剑川县 | 招募对象：全校本科生及研究生 | 报名截止：2026年8月30日"
        )
        project = extract_project(notice, {}, today=date(2026, 8, 1))
        self.assertEqual(project["title"], "清华大学乡村振兴工作站赴云南剑川实践支队")
        self.assertEqual(project["signup_deadline"], "2026-08-30")
        self.assertEqual(project["location"]["detail"], "云南省大理州剑川县")
        self.assertLessEqual(project["practice_start"], project["practice_end"])

    def test_ordinal_prefix_does_not_eat_a_year(self):
        # 序号前缀必须带分隔符才剥，否则「2026年…」的年份会被当成序号吃掉。
        project = extract_project("某支队招募\n报名截止：2026年9月1日", {}, today=date(2026, 8, 1))
        self.assertEqual(project["signup_deadline"], "2026-09-01")


class RecommendationTests(unittest.TestCase):
    def setUp(self):
        self.project = extract_project(
            NOTICE,
            {"source_account": "清华大学社会实践", "source_url": "https://example.com/project"},
            today=date(2026, 7, 13),
        )

    def test_hard_filter_time_conflict(self):
        result = score_project(
            self.project,
            {"available_start": "2026-07-01", "available_end": "2026-07-10"},
            today=date(2026, 7, 13),
        )
        self.assertIn("实践日期与可用时间冲突", result.excluded_reasons)

    def test_preference_score_and_explanation(self):
        result = score_project(
            self.project,
            {
                "available_start": "2026-08-01", "available_end": "2026-08-25",
                "themes": ["教育", "乡村振兴"], "preferred_locations": ["云南"],
                "reimbursement_preference": "required",
            },
            today=date(2026, 7, 13),
        )
        self.assertFalse(result.excluded_reasons)
        self.assertGreater(result.score, 80)
        self.assertTrue(any("主题" in reason for reason in result.reasons))

    def test_needs_review_is_separate(self):
        potential = {**self.project, "id": "potential", "status": "needs_review", "uncertain_fields": ["reimbursement"]}
        result = recommend_projects([self.project, potential], {}, today=date(2026, 7, 13))
        self.assertEqual(len(result["eligible"]), 1)
        self.assertEqual(len(result["potential"]), 1)


class GenerationTests(unittest.TestCase):
    def test_generates_safe_application_with_placeholders(self):
        project = extract_project(NOTICE, {"source_url": "https://example.com/project"}, today=date(2026, 7, 13))
        result = generate_asset(project, "application", {})
        self.assertIn("尚未提供具体经历", result["content"])
        self.assertIn("表格栏目", result["content"])
        self.assertIn("均来自本人真实情况", result["content"])

    def test_all_generators_return_content(self):
        project = extract_project(NOTICE, {"source_url": "https://example.com/project"}, today=date(2026, 7, 13))
        for kind in ["application", "outreach", "interview", "report"]:
            with self.subTest(kind=kind):
                self.assertGreater(len(generate_asset(project, kind, {})["content"]), 200)
        with self.assertRaisesRegex(ValueError, "酒店位置"):
            generate_asset(project, "itinerary", {})

    def test_action_generators_use_structured_context(self):
        project = extract_project(NOTICE, {"source_url": "https://example.com/project"}, today=date(2026, 7, 13))
        context = {
            "strengths": "曾负责社团活动资料整理", "ideal_role": "访谈记录与资料整理",
            "motivation": "理解乡村教育的真实需求", "contribution": "协助访谈与证据整理",
            "place": "大理州某学校、县城教育局", "route": "昆明站 → 大理站 → 学校",
            "local_info": "已知当地教育局官网公开电话，待核验", "partner_needs": "当地学校与教育服务机构",
            "practice_gains": "学会将访谈观察与二手资料交叉核验",
        }
        self.assertIn("意向岗位", generate_asset(project, "application", context)["content"])
        outreach = generate_asset(project, "outreach", context)["content"]
        self.assertIn("可选地点与联系核验", outreach)
        self.assertIn("不编造企业名称", outreach)
        self.assertIn("大理州某学校", generate_asset(project, "interview", context)["content"])
        sites = recommend_local_sites(project, context)["options"][:2]
        itinerary = generate_asset(project, "itinerary", {**context, "hotel": "大理古城测试酒店", "selected_sites": sites})["content"]
        self.assertIn("大理古城测试酒店", itinerary)
        self.assertIn("大理州某学校", itinerary)
        self.assertIn("学会将访谈观察", generate_asset(project, "report", context)["content"])

    def test_local_sites_flow_into_interview_and_itinerary(self):
        project = extract_project(NOTICE, {"source_url": "https://example.com/project"}, today=date(2026, 7, 13))
        options = recommend_local_sites(project)["options"]
        self.assertTrue(any("教育" in item["name"] for item in options))
        selected = [options[0]]
        interview = generate_asset(project, "interview", {"selected_sites": selected})["content"]
        self.assertIn(selected[0]["name"], interview)
        self.assertIn("适配实践主题", interview)
        itinerary = generate_asset(project, "itinerary", {"selected_sites": selected, "hotel": "大理古城酒店"})["content"]
        self.assertIn("实时交通核验", itinerary)


class DatabaseTests(unittest.TestCase):
    def test_seed_review_version_and_feedback(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "test.db")
            self.assertEqual(db.stats()["total"], 8)
            project = db.get_project("demo_children_space")
            updated = db.review_project(project["id"], {"status": "published", "confidence": 0.86})
            self.assertEqual(updated["status"], "published")
            self.assertGreaterEqual(len(db.versions(project["id"])), 1)
            feedback_id = db.add_feedback({"project_id": project["id"], "rating": 5, "outcome": "saved"})
            self.assertGreater(feedback_id, 0)
            self.assertEqual(db.stats()["feedback_count"], 1)

    def test_seed_dates_follow_today_so_demo_never_goes_stale(self):
        """演示数据的日期必须跟着当天走，否则推荐结果会随时间推移变成空。

        seed_data.json 里写的是相对 anchor_date 的日期，载入时整体平移。
        这里断言的是结果而不是实现：无论哪天跑，都得有报名中的 published 项目。
        """
        today = date.today()
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "seed.db")
            projects = db.list_projects()

            open_now = [
                p for p in projects
                if p["status"] == "published"
                and p.get("signup_deadline")
                and date.fromisoformat(p["signup_deadline"]) >= today
            ]
            self.assertGreaterEqual(len(open_now), 3, "演示库里没有仍在报名的项目，推荐页会是空的")

            # 历史项目是故意留着演示"已过期"状态的，不能被一起推到未来。
            expired = [p for p in projects if p["status"] == "expired"]
            self.assertTrue(expired, "演示库里应保留至少一条已过期项目")

            # 关键字段的原文引用必须跟着字段一起平移，否则证据和数据自相矛盾。
            quoted = [
                p for p in projects
                if (p.get("field_evidence") or {}).get("signup_deadline", {}).get("quote")
            ]
            self.assertTrue(quoted, "演示数据里应有带截止日期原文引用的项目")
            for project in quoted:
                quote = project["field_evidence"]["signup_deadline"]["quote"]
                deadline = date.fromisoformat(project["signup_deadline"])
                self.assertIn(
                    f"{deadline.month}月{deadline.day}日",
                    quote,
                    f"{project['title']} 的截止日期与原文引用对不上：{quote}",
                )


if __name__ == "__main__":
    unittest.main()
