"""生成稿里的数字必须能在项目卡里找到出处。

来自 2026-08-21 的 100 轮实测。模型没有直接照做「写一个 98% 满意率」这类
明说的虚构要求（这点是好的），但会自己顺手补数字——而数字读起来最像真的，
也最容易被学生当真去照做：一个具体的报名截止、一笔补贴金额、一个百分比。

叙事性的编造（「山里的孩子」「当地教育资源相对有限」）没法机械核对，
但数字可以。这里不拦截输出——文案把「2026-08-24」写成「8月24日」是正常的——
而是把查不到出处的那几个当场点名。
"""
import unittest

from chat_adapter import unsupported_numbers

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


if __name__ == "__main__":
    unittest.main()
