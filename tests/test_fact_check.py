"""生成稿里的数字必须能在项目卡里找到出处。

来自 2026-08-21 的 100 轮实测。模型没有直接照做「写一个 98% 满意率」这类
明说的虚构要求（这点是好的），但会自己顺手补数字——而数字读起来最像真的，
也最容易被学生当真去照做：一个具体的报名截止、一笔补贴金额、一个百分比。

叙事性的编造（「山里的孩子」「当地教育资源相对有限」）没法机械核对，
但数字可以。这里不拦截输出——文案把「2026-08-24」写成「8月24日」是正常的——
而是把查不到出处的那几个当场点名。
"""
import unittest

from chat_adapter import fabrication_warning, unsupported_names, unsupported_numbers

FACTS = (
    "项目名称：赴湖南新宁支教实践支队招募\n"
    "实践时间：2026-08-24 至 2026-08-31\n"
    "报名截止：待确认\n"
    "经费：为活动志愿者提供往返车费报销，每半天提供50元高温补贴\n"
)


class UnsupportedNumberTests(unittest.TestCase):
    def test_same_date_written_differently_is_supported(self):
        """项目卡是 2026-08-24，文案写成 8月24日——同一天，不该报警。"""
        draft = "实践将于 8月24日 启程，8月31日 结束。"
        self.assertEqual(unsupported_numbers(draft, FACTS), [])

    def test_invented_deadline_is_flagged(self):
        draft = "请于 9月10日 前完成报名。"
        self.assertEqual(unsupported_numbers(draft, FACTS), ["9月10日"])

    def test_amount_present_in_facts_is_supported(self):
        self.assertEqual(unsupported_numbers("每半天有 50 元补贴。", FACTS), [])

    def test_invented_amount_is_flagged(self):
        self.assertIn("2000元", unsupported_numbers("每人可获 2000 元补助。", FACTS))

    def test_invented_percentage_is_flagged(self):
        """实测里模型被要求写「98% 满意率」时没照做，但顺手加数字的风险仍在。"""
        self.assertIn("98%", unsupported_numbers("往届满意率高达 98%。", FACTS))

    def test_text_without_numbers_is_clean(self):
        draft = "这次实践将走进当地中学，和同学们一起完成课业辅导。"
        self.assertEqual(unsupported_numbers(draft, FACTS), [])

    def test_iso_dates_on_both_sides_match(self):
        self.assertEqual(unsupported_numbers("2026-08-24 集合", FACTS), [])

    def test_several_invented_numbers_are_all_listed(self):
        draft = "报名截止 9月10日，每人补贴 300 元，往届录取率 45%。"
        flagged = unsupported_numbers(draft, FACTS)
        self.assertIn("9月10日", flagged)
        self.assertIn("300元", flagged)
        self.assertIn("45%", flagged)

    def test_year_quoted_from_the_source_article_is_supported(self):
        """写推送时原文会一并喂给模型，文案引用其中的年份是合法的。

        实测生成的文案里有「新宁一中的前身可追溯到清代金城书院，1941年正式建校」
        ——这句在原文里逐字存在。如果只拿抽取字段去查，这类真信息会被全部
        误报成编造，警告一多就没人看了。
        """
        source = FACTS + "\n【原文】\n新宁一中的前身可追溯到清代中叶的金城书院，1941 年正式建校。"
        self.assertEqual(unsupported_numbers("1941年正式建校的新宁一中", source), [])

    def test_invented_year_is_still_flagged(self):
        source = FACTS + "\n【原文】\n新宁一中 1941 年正式建校。"
        self.assertIn("1985年", unsupported_numbers("1985年迁入现址", source))

    def test_the_current_year_in_a_full_date_is_not_double_reported(self):
        # 「2026-08-24」已经按日期比过了，不该再作为裸年份报一次。
        self.assertEqual(unsupported_numbers("2026-08-24 出发", FACTS), [])

    def test_durations_are_not_mistaken_for_money(self):
        # 「为期 8 天」「3 个课时」不是金额，不该被点名。
        self.assertEqual(unsupported_numbers("为期 8 天，共 3 个课时。", FACTS), [])


class UnsupportedNameTests(unittest.TestCase):
    """数字之外的编造——书名、虚构引语、朝代。

    这类比编错数字危险：数字有时会跟项目卡里的字段对不上、用户能察觉，
    而「据《新宁县志》记载」「李老师告诉我们」读起来跟真的一模一样，
    跟任何字段都不冲突，用户没有任何线索。而且这份稿子是要署他自己
    名字发出去的。
    """

    def test_an_invented_document_is_flagged(self):
        self.assertEqual(unsupported_names("据《新宁县志》记载，崀山自古有名。", "崀山是世界自然遗产。"),
                         ["作品或文献「新宁县志」"])

    def test_a_document_named_in_the_source_is_not_flagged(self):
        self.assertEqual(unsupported_names("原文提到《县域教育发展报告》。",
                                           "详见《县域教育发展报告》第二章。"), [])

    def test_an_invented_quote_source_is_flagged(self):
        self.assertEqual(unsupported_names("李建国老师告诉我们，学生基础薄弱。",
                                           "新宁一中是当地重点中学。"),
                         ["引述的人「李建国老师」"])
        self.assertEqual(unsupported_names("张校长表示学校支持这项工作。", ""),
                         ["引述的人「张校长」"])

    def test_a_person_named_in_the_source_is_not_flagged(self):
        self.assertEqual(unsupported_names("王大爷说村里年轻人都走了。",
                                           "王大爷在村口开小卖部。"), [])

    def test_generic_roles_are_not_people(self):
        """判据只认「跟着说话动词的」。

        抓所有人名做不到：中文没有空格，正则只能贪婪切，「任课老师」的
        「任」、「高三年级主任」的「高」都正好是姓氏，实测全过。收窄到
        虚构引语，既对准了危害，也顺手把泛指挡在外面——泛指后面跟的是
        顿号或「的」，不会跟「告诉」。
        """
        for text in ["访谈对象包括高三年级主任、任课老师、带队老师。",
                     "请与当地老师、指导老师沟通。",
                     "需要访谈任课老师了解情况。"]:
            with self.subTest(text=text):
                self.assertEqual(unsupported_names(text, "支教对象为高三学生。"), [])

    def test_an_invented_era_is_flagged(self):
        self.assertEqual(unsupported_names("金城书院始建于明代。", "金城书院是当地文化地标。"),
                         ["年代「明代」"])

    def test_an_era_stated_in_the_source_is_not_flagged(self):
        self.assertEqual(unsupported_names("原文说这里明代就有书院。", "此地明代即建有书院。"), [])

    def test_single_characters_that_happen_to_be_dynasties_are_ignored(self):
        """「明」在「明确」里、「清」在「清华」里——不能命中。"""
        self.assertEqual(unsupported_names("我们要明确目标，清华的传统是行胜于言。", ""), [])

    def test_organisation_names_are_deliberately_not_checked(self):
        """机构名这条判据砍掉了，别再加回来。

        试过 `[一-鿿]{2,10}(?:大学|中学|村委会|…)`，拿 5 个真实项目生成的
        推送和调研报告一量，10 条告警 10 条误报：「与高中生聊聊大学」
        「本报告基于清华大学」「他们与村委会」。两个原因叠在一起——正则
        只能贪婪往左吃、切出来的不是名词短语；而且在"大学生去中学支教"
        这个语境里，「大学」「中学」本来就是高频普通词。
        """
        for text in ["与高中生聊聊大学生活，介绍大学专业。",
                     "他们与村委会沟通，本项目与素兰基金会合作。",
                     "本报告基于清华大学机械系支队的实践。"]:
            with self.subTest(text=text):
                self.assertEqual(unsupported_names(text, ""), [])

    def test_the_warning_lists_numbers_and_names_separately(self):
        text = "据《新宁县志》记载，8月15日有集会。"
        warning = fabrication_warning(text, "崀山是世界自然遗产。", where="原文和项目卡")
        self.assertIn("数字：", warning)
        self.assertIn("专名：", warning)
        self.assertIn("新宁县志", warning)
        self.assertIn("8月15日", warning)

    def test_a_clean_draft_produces_no_warning(self):
        self.assertEqual(fabrication_warning("这次实践去湖南新宁。", "实践地点：湖南新宁。",
                                             where="原文和项目卡"), "")


if __name__ == "__main__":
    unittest.main()
