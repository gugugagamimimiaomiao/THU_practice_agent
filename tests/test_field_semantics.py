"""岗位职责不能被读成待遇或门槛。

来自 2026-08-21 的 100 轮实测，两条都取自线上真实原文（校团委宣传部学生骨干招募）：

  经费：「…综合工作则主要包括宣传部和紫荆传媒的会务和财务工作，负责部门内部的
        会议准备与主持、各类相关经费的报销和代发…」
        → 被判成 has_reimbursement=True，项目卡上显示「有经费支持」。
        学生会理解成"参加这个能报销"，而原文说的是"你要负责给别人办报销"。

  资格：「运营"我在清华听讲座"平台，面向全校同学提供一站式讲座信息交流与检索服务」
        → 因为含「面向全校」被 ELIGIBILITY_LOOSE 捞成参与资格，
        还顺带把 explicit_no_restriction 设成了 true。

两条是同一个病：把"要干的活"读成了"给的待遇/门槛"。
判据都改成：出现职责类措辞、又没有指向参与者（或指向招募）的措辞时，判为没抽到。
宁可标成待确认，也不能说反。
"""
import unittest

from domain import _extract_eligibility, _extract_reimbursement

DUTY_WITH_MONEY = (
    "宣传部综合工作组的工作主要分为联络和综合两个部分。联络工作的主要内容是构建校-系两级"
    "宣传沟通机制和校-系联动宣传网络，同时负责改革与开展“紫荆之声”校园新媒体联创计划、"
    "定期召开院系团委宣传副书记例会等工作；综合工作则主要包括宣传部和紫荆传媒的会务和"
    "财务工作，负责部门内部的会议准备与主持、各类相关经费的报销和代发、部门简报征集和"
    "填写、部门信息登记和内部培训等工作。"
)
DUTY_WITH_SCOPE = "运营“我在清华听讲座”平台，面向全校同学提供一站式讲座信息交流与检索服务，推介相关讲座"


class ReimbursementSemanticsTests(unittest.TestCase):
    def test_job_duty_about_handling_money_is_not_a_benefit(self):
        result, _ = _extract_reimbursement([DUTY_WITH_MONEY])
        self.assertIsNone(
            result["has_reimbursement"],
            "把「负责各类相关经费的报销和代发」读成了项目提供报销",
        )

    def test_other_duty_phrasings(self):
        for line in ["3. 基础事务：完成数据整理、财务报销、会议记录等日常工作。",
                     "负责经费审批与台账管理",
                     "协助办理各类报销手续"]:
            with self.subTest(line=line[:20]):
                result, _ = _extract_reimbursement([line])
                self.assertIsNone(result["has_reimbursement"], f"误判为有经费：{line}")

    def test_real_benefits_still_recognised(self):
        for line, expected in [
            ("经费：往返交通费全额报销，食宿由项目组承担", True),
            ("补贴标准为每人每天 80 元", True),
            ("项目提供交通补贴", True),
            ("费用自理，不提供补贴", False),
        ]:
            with self.subTest(line=line[:20]):
                result, _ = _extract_reimbursement([line])
                self.assertIs(result["has_reimbursement"], expected)

    def test_duty_line_that_also_states_a_benefit_still_counts(self):
        # 有些通知会把两件事写在一行，这时不能一刀切地丢掉。
        line = "负责部门日常事务；参与同学的往返交通费由学校全额报销"
        result, _ = _extract_reimbursement([line])
        self.assertIs(result["has_reimbursement"], True)


class EligibilitySemanticsTests(unittest.TestCase):
    def test_service_scope_is_not_an_eligibility_statement(self):
        result, _ = _extract_eligibility([DUTY_WITH_SCOPE])
        self.assertEqual(result["restriction_text"], "")
        self.assertFalse(
            result["explicit_no_restriction"],
            "把「面向全校同学提供服务」读成了「面向全校招募」",
        )

    def test_real_recruiting_scope_still_recognised(self):
        for line in ["现面向全校招募队员，不限年级",
                     "参与资格：全校本科生、研究生",
                     "面向全校同学招新，欢迎报名"]:
            with self.subTest(line=line[:16]):
                result, _ = _extract_eligibility([line])
                self.assertTrue(result["restriction_text"], f"正常的资格说明被误伤：{line}")

    def test_labelled_line_always_wins_over_the_loose_hint(self):
        lines = [DUTY_WITH_SCOPE, "参与资格：仅限本科生"]
        result, _ = _extract_eligibility(lines)
        self.assertIn("本科生", result["restriction_text"])


if __name__ == "__main__":
    unittest.main()
