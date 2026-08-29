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

from domain import (THEME_SCAN_CHARS, _extract_eligibility, _extract_reimbursement,
                    _extract_themes)

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

    def test_every_wrong_case_found_in_the_live_library(self):
        """2026-08-21 把线上 17 条判为「有经费支持」的项目逐条翻了一遍，
        这七条是误判，全部取自真实原文。

        「助梦1+1」那条尤其说明问题：原文在解释青海物价为什么高，
        系统读成了"这个项目给报销"，意思正好反过来。
        """
        for name, line in [
            ("职责-社团协会部", "计划组主要面向学生社团开展财务工作，负责经费管理：学期末进行财务报销。"),
            ("职责-清年祁旅", "（1）财务（1人）：负责前期物资采购、中期财务记录，后期财务报销等流程。"),
            ("职责-黔心守艺", "对接文旅局与非遗中心；与当地文旅局合作安排日程以及当地的食宿、包车、安全预案等。"),
            ("要求-英纽林", "熟悉学校报销流程，或有订房、租车等后勤经验。"),
            ("议论花费-助梦1+1", "A：青海属于西北高原地区，交通不便，果蔬米粮大部分由外地输送进省，因而食宿费用较高。"),
            ("职责-学习实践部", "加入综合协调组，学习财务报销和数据整理的知识与技能。"),
            ("光秃标题-邯郸", "经费保障"),
        ]:
            with self.subTest(name=name):
                result, _ = _extract_reimbursement([line])
                self.assertIsNone(result["has_reimbursement"], f"{name} 被误判为有经费支持")

    def test_every_real_benefit_found_in_the_live_library(self):
        for line in [
            "各学校将在校内或周边酒店安排食宿，并为支队提供往返大交通和活动文印物料方面的经费支持。",
            "②每半天提供50元高温补贴；",
            "为活动志愿者提供往返车费报销、防暑物资保障",
            "本次实践所有清华志愿者的往返交通、食宿费用均由孙吴县统一承担",
            "路费和住宿费基本可以报销",
            # 这条被第一版规则误伤过：「材料审核」里的"审核"命中了职责词。
            "报名参加“青年技能绿动”暑期专项活动，完成项目任务并通过材料审核的支队，可获得 3000 元额外报销额度。",
        ]:
            with self.subTest(line=line[:18]):
                result, _ = _extract_reimbursement([line])
                self.assertIs(result["has_reimbursement"], True, f"真待遇被误伤：{line}")

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

    def test_duty_word_after_the_hint_does_not_disqualify_the_line(self):
        """判据是位置，不是"有没有职责词"。

        第一版规则写成"整行里有职责词、又没有招募词就跳过"，在真实数据上
        两个方向都错了：

          「"星空计划"面向全校社团会长及优秀骨干，…开展为期一年的系统培养」
              → 因为句尾有「开展」被误杀，而这是真正的资格说明
          「…负责规划、建设并持续运营全校学生社团的一站式信息平台。欢迎加入」
              → 因为段尾出现「欢迎」而逃过，而这是岗位职责

        职责词在关键词之前 = 这一行在说岗位干什么；在之后 = 不影响前半句。
        """
        kept = "“星空计划”面向全校社团会长及优秀骨干，匹配全方位资源，开展为期一年的系统培养"
        skipped = "我们是社团运行的“数字大脑”，负责规划、建设并持续运营全校学生社团的一站式信息平台。欢迎加入"
        self.assertTrue(_extract_eligibility([kept])[0]["restriction_text"])
        self.assertFalse(_extract_eligibility([skipped])[0]["restriction_text"])


class ThemeTaggingTests(unittest.TestCase):
    """主题标签只看标题 + 正文开头。

    原来拿整篇正文撞词表、命中一次即算，43 条真实数据里 28 条被打了 4-5 个
    主题（词表总共才 8 个），「公益志愿」覆盖 42/43。标签没有区分度，而推荐
    的理由行拿它当依据写「匹配主题偏好：文化传承」——用户问非遗看到这行字，
    会以为系统读懂了。理由撒谎比排序不准严重。
    """

    def test_a_passing_mention_deep_in_the_body_is_not_a_theme(self):
        body = "本部门负责校团委日常事务。" + "。" * THEME_SCAN_CHARS + "往期我们也办过非遗文化展。"
        self.assertNotIn("文化传承", _extract_themes("2026年秋校团委组织部学生骨干招募", body))

    def test_the_title_always_counts(self):
        self.assertIn("文化传承",
                      _extract_themes("非遗保护调研支队招募", "。" * 3000))

    def test_a_theme_stated_up_front_is_kept(self):
        body = "本支队赴湘西开展非遗技艺的传承现状调研，走访传统手工艺人。"
        self.assertIn("文化传承", _extract_themes("实践招募 | 赴湘西调研支队", body))

    def test_nothing_matching_falls_back_to_the_generic_tag(self):
        self.assertEqual(_extract_themes("体育助教报名通知", "为保障新生体育工作有序开展。"),
                         ["综合实践"])

    def test_a_specific_word_counts_even_once(self):
        """「支教」出现一次就够。一刀切要求两次会漏掉真项目——实测
        「筑梦建行·春山在望」正文写着"中学生夏令营支教实践"，只此一处。"""
        self.assertIn("教育", _extract_themes("实践招募丨赴霍邱一中支队",
                                             "实践补招募 中学生夏令营支教实践 地区概况……"))

    def test_a_generic_word_needs_more_than_one_mention(self):
        """「健康」出现一次不能给整条定调——实测「百名硕博建功邯郸」
        就是这么被定成唯一标签 健康医疗 的。"""
        body = "为落实团河北省委关于人才工作的要求，面向京津冀高校公开选拔硕博，服务地方发展，关心青年健康成长。"
        self.assertNotIn("健康医疗", _extract_themes("百名硕博建功行动招募公告", body))

    def test_tags_are_ranked_by_hits_not_by_dict_order(self):
        """原来 tags[:5] 按词表书写顺序截断——留下哪几个纯看谁写在前面。"""
        body = ("支教支教支教课程课程儿童儿童讲课，"  # 教育命中很多次
                "同时也提供一点服务。")            # 公益志愿只命中一次
        tags = _extract_themes("暑期支队招募", body)
        self.assertEqual(tags[0], "教育")

    def test_at_most_five_tags(self):
        body = "乡村振兴 支教 人工智能 生态环保 志愿服务 非遗文化 社区治理 养老健康"
        self.assertLessEqual(len(_extract_themes("综合实践招募", body)), 5)


if __name__ == "__main__":
    unittest.main()
