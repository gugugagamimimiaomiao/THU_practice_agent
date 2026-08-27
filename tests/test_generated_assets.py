"""生成的材料要跟这个项目有关，而且不能把内部字段名端给用户。

2026-08-21 用户反馈「调研报告提纲和推送会生成一坨屎」。拉出线上真实输出一看：

  调研报告框架  除了 H1 里的标题，正文七节一个项目变量都没用——换成任何项目
                输出一字不差。而 theme / work_principle / _location_area() 早就
                算好摆在那儿，隔壁访谈提纲用得好好的。
                一份通用学术骨架网上一搜一大把，学生要它没用。

  所有材料末尾  「生成前仍需核实：signup_deadline、reimbursement」
                ——英文字段名直接漏给学生，而且写调研报告框架跟报名截止、
                报销有什么关系？
"""
import unittest

from domain import generate_asset

PROJECT = {
    "id": "p-test",
    "title": "赴湖南新宁支教实践支队招募",
    "status": "published",
    "summary": "面向全校招募队员，前往新宁一中开展支教实践。",
    "theme_tags": ["教育"],
    "location": {"province": "湖南", "city": "邵阳市", "detail": "湖南省邵阳市新宁县第一中学"},
    "practice_start": "2036-08-24",
    "practice_end": "2036-08-31",
    "eligibility": {"restriction_text": "全校本科生"},
    "reimbursement": {"has_reimbursement": None, "text": ""},
    "uncertain_fields": ["signup_deadline", "reimbursement"],
}


class ReportSpecificityTests(unittest.TestCase):
    def test_report_frame_mentions_this_project(self):
        content = generate_asset(PROJECT, "report")["content"]
        for expected in ("新宁", "教育服务与学习支持", "2036-08-24"):
            self.assertIn(expected, content, f"报告框架里没有「{expected}」，等于通用模板")

    def test_two_different_projects_give_different_frames(self):
        other = dict(PROJECT, title="赴青海湖生态保护调研支队招募",
                     summary="围绕生态保护与鸟类栖息地开展调研。",
                     theme_tags=["生态环保"],
                     location={"province": "青海", "city": "海南州", "detail": "青海省青海湖"})
        a = generate_asset(PROJECT, "report")["content"]
        b = generate_asset(other, "report")["content"]
        self.assertNotEqual(a, b, "换了项目，报告框架却一字不差")
        self.assertIn("生态", b)
        self.assertNotIn("青海", a)

    def test_no_empty_placeholder_table_rows(self):
        content = generate_asset(PROJECT, "report")["content"]
        self.assertNotIn("[待填写]", content)

    def test_practice_gains_block_only_appears_when_provided(self):
        without = generate_asset(PROJECT, "report")["content"]
        self.assertNotIn("你已经记下的", without)
        self.assertNotIn("请在实践后补充", without)
        with_gains = generate_asset(
            PROJECT, "report", {"practice_gains": "第一次意识到备课和上课完全是两件事"}
        )["content"]
        self.assertIn("备课和上课完全是两件事", with_gains)


class FieldLabelLeakTests(unittest.TestCase):
    def test_warnings_never_show_internal_field_names(self):
        for kind in ("application", "outreach", "interview", "report"):
            with self.subTest(kind=kind):
                warnings = " ".join(generate_asset(PROJECT, kind)["warnings"])
                for internal in ("signup_deadline", "reimbursement", "practice_dates",
                                 "eligibility", "signup_method", "source_url"):
                    self.assertNotIn(internal, warnings,
                                     f"{kind} 把内部字段名 {internal} 漏给了用户：{warnings}")

    def test_only_fields_this_asset_actually_needs_are_flagged(self):
        """写调研报告框架跟报名截止、报销没关系，不该提示核对它们。"""
        report = " ".join(generate_asset(PROJECT, "report")["warnings"])
        self.assertNotIn("报名截止", report)
        self.assertNotIn("经费", report)
        # 但写报名理由时，报名截止是要紧的。
        application = " ".join(generate_asset(PROJECT, "application")["warnings"])
        self.assertIn("报名截止", application)


if __name__ == "__main__":
    unittest.main()
