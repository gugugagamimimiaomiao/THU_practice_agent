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


if __name__ == "__main__":
    unittest.main()
