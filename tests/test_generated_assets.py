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


class AssetKindRoutingTests(unittest.TestCase):
    """说法稍微一变就认不出来，是用户反馈的原话：「必须要非常非常严格的关键词」。

    原来 GENERATE_WORDS 和类型判断是两处各写各的：词表里有「调研报告」，
    而判断类型时只认「报告框架」「报告大纲」。于是说「帮我写这个项目的调研报告」，
    词表放行了、类型却掉回默认的 application——用户要调研报告，拿到一份报名表建议。
    """

    def test_report_phrasings(self):
        from chat_adapter import _asset_kind
        for text in ["帮我写宝庆微光的调研报告", "调研报告框架", "写一份实践报告",
                     "结题报告怎么写", "调研提纲", "调研方案", "报告结构给我看看",
                     "研究报告大纲"]:
            with self.subTest(text=text):
                self.assertEqual(_asset_kind(text), "report")

    def test_other_kinds_still_route(self):
        from chat_adapter import _asset_kind
        for text, kind in [("帮我写访谈提纲", "interview"), ("外联话术", "outreach"),
                           ("行程安排", "itinerary"), ("帮我写报名理由", "application")]:
            with self.subTest(text=text):
                self.assertEqual(_asset_kind(text), kind)

    def test_every_keyword_is_reachable_from_the_router(self):
        """类型表里的词必须都在 GENERATE_WORDS 里，否则根本进不了 _generate。

        这条就是为了防止两张表再次分叉。
        """
        from chat_adapter import GENERATE_WORDS, _ASSET_KEYWORDS
        for kind, words in _ASSET_KEYWORDS:
            for word in words:
                self.assertIn(word, GENERATE_WORDS, f"{kind} 的「{word}」进不了路由")


class DistinctiveOverlapTests(unittest.TestCase):
    """判断"这句话点名了某个项目吗"，不能只看有没有字重合。

    原来的护栏要求打出完整标题（「实践招募 | 机械系"宝庆微光"赴湖南新宁支教
    实践支队招募」），没人这么打，于是「宝庆微光 调研提纲」被判成没点名项目，
    掉进不绑项目的通用建议。

    但一放宽就走到另一头：「帮我写一个乡村教育的调研提纲」命中了
    「滇西乡村教育数字化调研」，被当成给那个项目写材料。

    判据是重合片段要够长**且不是领域通用词拼出来的**。
    """

    def test_proper_noun_counts_as_naming(self):
        from chat_adapter import _distinctive_overlap
        self.assertEqual(
            _distinctive_overlap("实践招募 | 机械系“宝庆微光”赴湖南新宁支教实践支队招募",
                                 "宝庆微光 调研提纲"),
            "宝庆微光")

    def test_generic_topic_words_do_not_count(self):
        from chat_adapter import _distinctive_overlap
        for title, text in [
            ("滇西乡村教育数字化调研", "帮我写一个乡村教育的调研提纲"),
            ("青海生态保护与社区发展调研", "生态保护类的实践一般怎么写报告"),
            ("城市社区儿童友好空间共创", "社区服务的调研方案"),
        ]:
            with self.subTest(text=text):
                self.assertEqual(_distinctive_overlap(title, text), "",
                                 "通用主题词被当成了点名项目")

    def test_two_characters_is_too_short(self):
        from chat_adapter import _distinctive_overlap
        self.assertEqual(_distinctive_overlap("某某支队招募", "这个支队怎么样"), "")


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
