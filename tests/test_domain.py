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


if __name__ == "__main__":
    unittest.main()
