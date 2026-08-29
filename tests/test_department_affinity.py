"""本院系/本书院自己办的实践，本院系学生该先看到。

用户报的：「给任何人的推荐都一样，你没有写入清华各个书院、院系的详细区别」。

查下来书院词表其实是有的，问题在于院系**只当硬过滤**用——而 73 条真实项目里
只有 3 条在原文里写明了院系限制。实测未央、新雅、机械、美术四个院系问同一句
「推荐几个暑期实践」，前三名一模一样。

但有 9 条项目的标题或公众号号名里明写着院系/书院：

    未央实践丨"智汇北票"赴北票实践支队补招募
    实践招募 | 机械系"宝庆微光"赴湖南新宁支教实践支队
    实践招募丨新雅"一事"计划 基层助力—孝昌乡村振兴项目

这些信息一直没被用上。改成排序信号（不是硬条件，别的院系通常也能报）。

加权时踩过一个坑，两条测试专门盯着它：第一版没有 _is_practice_like 这道前提，
计算机系学生问「推荐几个暑期实践」，前三名变成「学生节征名」「男篮招新」
「中长跑队招新」——酒井资讯是计算机系的号，这些全被判成"本院系发的"。
「本院系」应该是同类之中优先，不是把不同类的顶上来。
"""
import unittest

from domain import COLLEGE_SHORT_NAMES, KNOWN_DEPARTMENTS, department_affinity


def project(title, **extra):
    base = {"title": title, "summary": "", "organizer": "", "source_account": "",
            "theme_tags": ["教育"]}
    base.update(extra)
    return base


class DepartmentAffinityTests(unittest.TestCase):
    def test_a_college_named_in_the_title_counts(self):
        self.assertEqual(
            department_affinity(project("实践招募 | 机械系“宝庆微光”赴湖南新宁支教实践支队招募"),
                                "机械系"),
            "机械系")

    def test_a_residential_college_short_name_counts(self):
        """标题里写「未央实践」，不会带"书院"两个字。"""
        self.assertEqual(
            department_affinity(project("未央实践丨“智汇北票”赴北票实践支队补招募"), "未央书院"),
            "未央")

    def test_the_summary_counts_too(self):
        """院系名常常不在标题里，而在正文摘要里——「赴湖北武汉调研生物医药
        产业发展」的标题不提生命学院，摘要里提了。"""
        self.assertTrue(department_affinity(
            project("实践招募｜赴湖北武汉调研生物医药产业发展",
                    summary="生命学院联合发起，赴武汉调研生物医药产业"),
            "生命学院"))

    def test_an_account_nickname_is_not_matched(self):
        """「建院宣传中心」里没有「建筑学院」四个字，就不算命中。

        第一版测试想当然地以为号名能对上，实际数据里命中的是摘要。
        别名映射（建院→建筑学院、酒井→计算机系）是另一件事，要做得单独
        建表并逐条核过，不能靠子串碰运气。
        """
        self.assertEqual(department_affinity(
            project("研团招新丨2026，为你而来！", source_account="建院宣传中心"),
            "建筑学院"), "")

    def test_another_department_gets_nothing(self):
        self.assertEqual(
            department_affinity(project("实践招募 | 机械系“宝庆微光”赴湖南新宁支教实践支队招募"),
                                "美术学院"),
            "")

    # ---- 不能把不同类的顶上来 ----

    def test_a_sports_team_recruitment_is_not_a_practice(self):
        """实测退化：计算机系学生问「推荐几个暑期实践」，前三名成了体育队招新。"""
        self.assertEqual(
            department_affinity(project("【代表队招新】体育集结号——贵系男篮篇",
                                        source_account="酒井资讯"),
                                "计算机系"),
            "")

    def test_a_student_org_recruitment_is_not_a_practice(self):
        self.assertEqual(
            department_affinity(project("未央书院学生组织2026年秋季学期补招新与转岗报名开始啦！"),
                                "未央书院"),
            "")

    def test_theme_tags_alone_do_not_qualify(self):
        """主题标签本身就宽（「公益志愿」曾覆盖 42/43），拿它放行会把部门招新
        也当成实践。只看标题和摘要。"""
        self.assertEqual(
            department_affinity(project("学生节征名丨计算机系第四十四届学生节主题征集",
                                        theme_tags=["公益志愿", "教育"]),
                                "计算机系"),
            "")

    # ---- 词表本身 ----

    def test_only_residential_colleges_have_short_names(self):
        """「机械」「物理」「数学」既是院系简称也是普通学科词，缩进去会把一堆
        无关项目算成本院系办的。书院名是专有名词，才配有简称。"""
        for name in COLLEGE_SHORT_NAMES:
            self.assertTrue(name.endswith("书院"), f"{name} 不是书院，不该有简称")

    def test_every_short_name_belongs_to_a_known_department(self):
        for name in COLLEGE_SHORT_NAMES:
            self.assertIn(name, KNOWN_DEPARTMENTS)

    def test_all_eight_residential_colleges_are_known(self):
        for name in ("新雅书院", "致理书院", "日新书院", "未央书院",
                     "探微书院", "行健书院", "求真书院", "笃实书院"):
            self.assertIn(name, KNOWN_DEPARTMENTS)

    def test_no_department_matches_an_empty_query(self):
        self.assertEqual(department_affinity(project("实践招募丨某某支队"), ""), "")


if __name__ == "__main__":
    unittest.main()
