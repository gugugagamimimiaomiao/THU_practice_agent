import tempfile
import unittest
from pathlib import Path

from database import Database
from wechat_ingest import (
    _ArticleParser,
    WeChatFetchResult,
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


if __name__ == "__main__":
    unittest.main()
