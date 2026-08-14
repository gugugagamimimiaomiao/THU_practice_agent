import tempfile
import unittest
from pathlib import Path

from database import Database
from wechat_ingest import (
    _ArticleParser,
    WeChatFetchResult,
    import_article_text,
    import_wechat_link,
    validate_wechat_url,
)


ARTICLE_HTML = """
<html><head>
  <meta property="og:title" content="备用标题">
  <meta property="og:article:author" content="清华大学社会实践">
</head><body>
  <h1 class="rich_media_title">乡村教育实践队招募</h1>
  <a id="js_name">清华大学社会实践</a>
  <em id="publish_time">2026-07-13</em>
  <div id="js_content"><p>主办单位：清华大学社会实践中心</p>
  <p>实践时间：2026年8月12日—2026年8月18日</p>
  <p>实践地点：云南省大理州</p><p>招募对象：面向全校本科生、研究生，无专业限制</p>
  <p>报名截止：2026年7月29日</p><p>经费说明：提供每人1800元交通补贴</p>
  <p>报名方式：扫描原文二维码填写报名表</p></div>
</body></html>
"""


class WeChatIngestTests(unittest.TestCase):
    def test_parser_extracts_public_article_text_and_metadata(self):
        parser = _ArticleParser()
        parser.feed(ARTICLE_HTML)
        title, account, published, content = parser.article()
        self.assertEqual(title, "乡村教育实践队招募")
        self.assertEqual(account, "清华大学社会实践")
        self.assertEqual(published, "2026-07-13")
        self.assertIn("报名截止：2026年7月29日", content)

    def test_only_real_mp_article_urls_are_accepted(self):
        valid, _ = validate_wechat_url("https://mp.weixin.qq.com/s?__biz=x&mid=1")
        self.assertTrue(valid)
        valid, reason = validate_wechat_url("https://example.com/redirect")
        self.assertFalse(valid)
        self.assertIn("mp.weixin.qq.com", reason)

    def test_link_import_creates_project_when_fetcher_returns_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = Database(Path(tmp) / "test.db")
            result = import_wechat_link(database, {"source_url": "https://mp.weixin.qq.com/s?mid=demo"}, fetcher=lambda _: WeChatFetchResult(
                True, "https://mp.weixin.qq.com/s?mid=demo", "乡村教育实践队招募\n主办单位：清华大学社会实践中心\n实践时间：2026年8月12日—2026年8月18日\n实践地点：云南省大理州\n招募对象：面向全校本科生、研究生，无专业限制\n报名截止：2026年7月29日\n经费说明：提供每人1800元交通补贴\n报名方式：扫描原文二维码填写报名表", "乡村教育实践队招募", "清华大学社会实践", "2026-07-13", "direct"
            ))
            self.assertEqual(result["status"], "imported")
            self.assertEqual(result["collector_method"], "direct")
            self.assertEqual(result["project"]["source_account"], "清华大学社会实践")
            self.assertTrue(result["project"]["source_url"].startswith("https://mp.weixin.qq.com"))

    def test_link_import_never_claims_success_when_fetcher_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = Database(Path(tmp) / "test.db")
            result = import_wechat_link(database, {"source_url": "https://mp.weixin.qq.com/s?mid=blocked"}, fetcher=lambda url: WeChatFetchResult(
                False, url, method="direct", warning="公众号页面触发验证或频控，未读取正文。"
            ))
            self.assertEqual(result["status"], "fetch_failed")
            self.assertIn("未读取正文", result["truthfulness_note"])
            self.assertIsNone(database.get_project("does-not-exist"))

    def test_preview_link_is_audited_but_never_creates_project_card(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Database(Path(folder) / "test.db")
            result = import_wechat_link(database, {"source_url": "https://mp.weixin.qq.com/s?mid=preview"}, fetcher=lambda _: WeChatFetchResult(
                True, "https://mp.weixin.qq.com/s?mid=preview", "实践支队行前预告\n欢迎大家关注支队行程。", "实践支队行前预告", "无限之声", "2026-07-16", "direct"
            ))
            self.assertEqual(result["status"], "not_opportunity")
            self.assertFalse(any(item["source_url"].endswith("mid=preview") for item in database.list_projects()))

    def test_reimport_of_a_poorer_version_does_not_erase_confirmed_fields(self):
        """同一篇文章第二次导入时更稀疏，不能把已有字段清空。

        真实场景：一篇招募推送被多个号转发，转发版常是截断的；采集器重跑时
        也可能因为风控只拿到半截正文。这里曾经的行为是后来者整体覆盖，一条
        字段齐全的项目会被一条只剩标题和截止日期的稀疏版本洗成"信息不全"，
        而且洗完看不出发生过什么。
        """
        url = "https://mp.weixin.qq.com/s?mid=merge-case"
        full = (
            "现面向全校招募赴云南乡村教育调研支队队员。\n"
            "实践时间：2026年9月10日—2026年9月20日\n"
            "实践地点：云南省大理州\n"
            "参与资格：全校本科生、研究生均可报名\n"
            "报名截止：2026年9月1日\n"
            "经费说明：提供每人1800元交通补贴\n"
            "报名方式：扫描原文二维码填写报名表"
        )
        sparse = "现面向全校招募赴云南乡村教育调研支队队员。\n报名截止：2026年9月1日"
        with tempfile.TemporaryDirectory() as folder:
            database = Database(Path(folder) / "test.db")
            for body in (full, sparse):
                import_wechat_link(database, {"source_url": url}, fetcher=lambda _, text=body: WeChatFetchResult(
                    True, url, text, "赴云南乡村教育调研支队招募", "清华大学社会实践", "2026-08-20", "direct"
                ))
            matching = [item for item in database.list_projects() if item["source_url"] == url]
            self.assertEqual(len(matching), 1, "同一链接导入两次不应产生两张项目卡")
            project = database.get_project(matching[0]["id"])
            self.assertEqual(project["practice_start"], "2026-09-10")
            self.assertEqual(project["practice_end"], "2026-09-20")
            self.assertEqual(project["location"]["detail"], "云南省大理州")
            self.assertTrue(project["reimbursement"]["has_reimbursement"])
            self.assertTrue(project["signup_method"])
            # 稀疏版开头那句「现面向全校招募…」也会被抽成资格说明（非空），
            # 但它比不上原版那行「参与资格：全校本科生、研究生均可报名」。
            # 这一条关系到硬过滤——资格被套话顶掉，就会放行本来没资格的人。
            self.assertIn("本科生", project["eligibility"]["grades"])
            self.assertNotIn("reimbursement", project["uncertain_fields"])
            self.assertNotIn("practice_dates", project["uncertain_fields"])

    def test_image_only_article_keeps_its_image_urls_and_waits_for_review(self):
        """图片型推送：不能丢图片 URL，也不能拿空卡冒充信息齐全。

        采集方对"正文只有一张长图"的推送会把图片 URL 放在 images 里。这条路
        以前完全没接——articles 表没有对应的列，入库管线也不看这个字段，
        URL 就无声无息地没了，只剩一张几乎空白的项目卡。取消人工核验之后，
        这种卡会直接进正式推荐。
        """
        url = "https://mp.weixin.qq.com/s?mid=image-only"
        images = ["https://mmbiz.qpic.cn/mmbiz_jpg/a/640", "https://mmbiz.qpic.cn/mmbiz_jpg/b/640"]
        with tempfile.TemporaryDirectory() as folder:
            database = Database(Path(folder) / "test.db")
            result = import_article_text(
                database,
                {"title": "社团纳新丨2026年秋季学期学生社团招新启动", "source_url": url,
                 "source_account": "清华大学学生社团", "images": images},
                "详见下图。",
            )
            project = result["project"]
            self.assertEqual(project["image_sources"], images, "图片 URL 丢了")
            self.assertEqual(project["image_ocr_status"], "pending")
            self.assertEqual(project["status"], "needs_review", "空卡不该直接进正式推荐")
            self.assertTrue(any("配图" in note for note in project["risk_notes"]))

    def test_short_but_complete_notice_with_images_still_publishes(self):
        """短不等于空。字段齐全的通知即使正文短、另有配图，也该正常发布。

        一开始拿正文长度当代用指标，结果一条 137 字、截止日期和报名方式都
        抽到了的通知被降级成待核验。该看的是抽出来的字段够不够用。
        """
        url = "https://mp.weixin.qq.com/s?mid=short-complete"
        with tempfile.TemporaryDirectory() as folder:
            database = Database(Path(folder) / "test.db")
            result = import_article_text(
                database,
                {"title": "学生会招新丨2026年秋季学期干事招募", "source_url": url,
                 "source_account": "清华大学学生会", "images": ["https://mmbiz.qpic.cn/c/640"]},
                "现面向全校招募新一届干事。\n报名截止：2036年9月10日\n"
                "参与资格：全校本科生均可报名\n报名方式：填写问卷并提交",
            )
            project = result["project"]
            self.assertEqual(project["status"], "published")
            self.assertEqual(project["image_ocr_status"], "pending")
            self.assertEqual(len(project["image_sources"]), 1)

    def test_corpus_only_saves_the_article_but_never_creates_a_project(self):
        """回采历史材料时只入语料，绝不进推荐。

        采集方要抓历史实践总结、纪实、志愿故事来扩充写作范例。这些文章早就
        结束了，进推荐没意义；而"让分类器自己判断"这条路不够稳——实测 28 篇
        「XX基地资源推介」里有 24 篇被判成了可报名机会。调用方自己知道这批
        是历史材料，给个显式开关比让规则去猜可靠。
        """
        url = "https://mp.weixin.qq.com/s?mid=corpus-only"
        recap = ("支队一行十二人抵达西部小城，开始为期十四天的实践，走访小学与农户。"
                 "回望这十四天，我们收获的远比付出的多，也更理解了这片土地。") * 6
        with tempfile.TemporaryDirectory() as folder:
            database = Database(Path(folder) / "test.db")
            result = import_article_text(
                database,
                {"title": "实践总结丨十四天的西部记忆", "source_account": "清华大学社会实践",
                 "source_url": url},
                recap,
                corpus_only=True,
            )
            self.assertEqual(result["status"], "corpus_only")
            self.assertFalse([p for p in database.list_projects() if p["source_url"] == url],
                             "只入语料的文章不该生成项目卡")

            # 但它必须进得了写作语料——这正是回采它的目的。
            from corpus import load_corpus
            self.assertIn("实践总结丨十四天的西部记忆",
                          [s.title for s in load_corpus(database).samples])

    def test_corpus_only_skips_extraction_even_for_recruitment_looking_text(self):
        """开关是显式的，不受正文内容影响。

        历史材料里常有"当时我们招募了 20 名队员，报名方式是扫码"这种回忆性
        表述。如果还让分类器判一次，这类会被抽成一个早就不存在的项目。
        """
        url = "https://mp.weixin.qq.com/s?mid=corpus-recruit-words"
        text = ("那年我们面向全校招募队员，报名截止是 2020 年 6 月 30 日，"
                "报名方式是扫描海报二维码。最终 20 人成行。") * 8
        with tempfile.TemporaryDirectory() as folder:
            database = Database(Path(folder) / "test.db")
            result = import_article_text(
                database, {"title": "五年前的那个夏天", "source_url": url}, text, corpus_only=True)
            self.assertEqual(result["status"], "corpus_only")
            self.assertFalse([p for p in database.list_projects() if p["source_url"] == url])

    def test_marked_correction_overrides_even_when_it_says_less(self):
        """数据维护方明确标记的订正必须生效，哪怕新版本信息更少。

        订正常常"更少"：主办方把资格从「仅限环境学院」放宽成「面向全校」，
        或者延期后重发了一版更短的通知。默认的补充式合并会判定成信息量下降
        而挡回去——订正于是悄悄失效，而界面上一切看起来正常。
        """
        url = "https://mp.weixin.qq.com/s?mid=correction-case"
        first = (
            "现招募赴内蒙古草原生态调研支队队员。\n"
            "实践地点：内蒙古自治区锡林郭勒盟\n"
            "参与资格：仅限环境学院、地学系本科生\n"
            "报名截止：2026年9月5日\n"
            "经费说明：提供每人1200元交通补贴\n"
            "报名方式：填写报名表发送至邮箱"
        )
        revised = (
            "赴内蒙古草原生态调研支队报名延长通知。\n"
            "实践地点：内蒙古自治区锡林郭勒盟\n"
            "参与资格：面向全校，不限院系年级\n"
            "报名截止：2026年9月20日\n"
            "经费说明：提供每人1200元交通补贴\n"
            "报名方式：填写报名表发送至邮箱"
        )

        def fetcher_for(text):
            return lambda _: WeChatFetchResult(
                True, url, text, "赴内蒙古草原生态调研支队招募", "清华大学社会实践", "2026-08-20", "direct"
            )

        with tempfile.TemporaryDirectory() as folder:
            database = Database(Path(folder) / "test.db")
            import_wechat_link(database, {"source_url": url}, fetcher=fetcher_for(first))

            # 不带标记：走补充式合并，资格信息量下降会被挡住
            import_wechat_link(database, {"source_url": url}, fetcher=fetcher_for(revised))
            found = [p for p in database.list_projects() if p["source_url"] == url]
            project = database.get_project(found[0]["id"])
            self.assertIn("环境学院", project["eligibility"]["departments"], "默认合并不该让订正生效")
            self.assertEqual(project["signup_deadline"], "2026-09-20", "截止日期是直接替换的，应当已更新")

            # 带标记：整条以新版本为准
            result = import_wechat_link(
                database, {"source_url": url, "correction": True}, fetcher=fetcher_for(revised))
            self.assertTrue(result["corrected"])
            found = [p for p in database.list_projects() if p["source_url"] == url]
            self.assertEqual(len(found), 1, "订正不该新开一条项目")
            project = database.get_project(found[0]["id"])
            self.assertEqual(project["eligibility"]["departments"], [], "订正后不该还挂着旧的院系限制")
            self.assertTrue(project["eligibility"]["explicit_no_restriction"])
            self.assertEqual(project["signup_deadline"], "2026-09-20")


if __name__ == "__main__":
    unittest.main()
