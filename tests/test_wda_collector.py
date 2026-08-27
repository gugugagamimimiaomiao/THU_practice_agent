"""wechat-download-api 适配器：两种读法要产出同一份可投稿的 JSONL。"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import wda_collector  # noqa: E402
from wda_collector import (  # noqa: E402
    collect_images,
    html_to_text,
    iso_date,
    markdown_to_text,
    parse_link_line,
    parse_since,
    read_from_db,
    read_from_links,
    unproxy,
)

PROXIED = (
    "http://localhost:5000/api/image?url="
    "http%3A//mmbiz.qpic.cn/mmbiz_jpg/testimage001/640%3Fwx_fmt%3Djpeg"
)
ORIGINAL = "http://mmbiz.qpic.cn/mmbiz_jpg/testimage001/640?wx_fmt=jpeg"


class UnproxyTest(unittest.TestCase):
    def test_proxy_url_is_restored_to_wechat_cdn(self):
        # 代理地址只有部署那台机器打得开，投出去必须是原图地址。
        self.assertEqual(unproxy(PROXIED), ORIGINAL)

    def test_plain_url_is_untouched(self):
        self.assertEqual(unproxy(ORIGINAL), ORIGINAL)

    def test_empty_is_empty(self):
        self.assertEqual(unproxy(""), "")


class HtmlTextTest(unittest.TestCase):
    def test_block_tags_become_line_breaks(self):
        # 换行位置是抽取判断字段边界的依据，不能被压成一行。
        text = html_to_text("<p>实践地点：云南</p><p>报名截止：2026年9月20日</p>")
        self.assertEqual(text.splitlines(), ["实践地点：云南", "报名截止：2026年9月20日"])

    def test_script_and_style_are_dropped(self):
        self.assertEqual(html_to_text("<p>正文</p><script>var a=1;</script>"), "正文")

    def test_images_are_collected_and_unproxied(self):
        html = f'<p>x</p><img src="{PROXIED}" data-src="{PROXIED}" />'
        self.assertEqual(collect_images(html), [ORIGINAL])


class MarkdownTest(unittest.TestCase):
    def test_frontmatter_title_and_images_are_separated(self):
        markdown = (
            "---\ntitle: 招募通知\nsource_url: https://mp.weixin.qq.com/s/x\n---\n\n"
            f"# 招募通知\n\n报名截止：2026年9月20日\n\n![]({PROXIED})\n"
        )
        text, images = markdown_to_text(markdown, "招募通知")
        self.assertEqual(images, [ORIGINAL])
        self.assertNotIn("---", text)
        self.assertNotIn("![", text)
        self.assertNotIn(")", text)          # 图片语法要整段消掉，不能留下半个括号
        self.assertNotIn("招募通知", text)     # 标题另有字段，不重复塞进正文
        self.assertIn("报名截止：2026年9月20日", text)

    def test_body_heading_that_is_not_the_title_is_kept(self):
        text, _ = markdown_to_text("---\ntitle: A\n---\n\n# 一、报名方式\n\n扫码", "A")
        self.assertIn("一、报名方式", text)


class SinceTest(unittest.TestCase):
    def test_date_and_timestamp_both_accepted(self):
        self.assertEqual(parse_since("1787000000"), 1787000000)
        self.assertGreater(parse_since("2026-08-01"), 0)
        self.assertEqual(parse_since(""), 0)

    def test_iso_date_uses_beijing_time(self):
        self.assertEqual(iso_date(1787453409), "2026-08-23")
        self.assertEqual(iso_date(0), "")


class ReadFromDbTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "rss.db"
        connection = sqlite3.connect(self.path)
        connection.executescript(
            """
            CREATE TABLE subscriptions (fakeid TEXT PRIMARY KEY, nickname TEXT);
            CREATE TABLE articles (
                id INTEGER PRIMARY KEY AUTOINCREMENT, fakeid TEXT, aid TEXT, title TEXT,
                link TEXT, digest TEXT, cover TEXT, author TEXT, content TEXT,
                plain_content TEXT, publish_time INTEGER, fetched_at INTEGER, source TEXT
            );
            """
        )
        connection.execute("INSERT INTO subscriptions VALUES ('F1','清华大学学生社团')")
        connection.executemany(
            "INSERT INTO articles (fakeid,aid,title,link,digest,cover,author,content,"
            "plain_content,publish_time,fetched_at,source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                ("F1", "a1", "招募通知", "https://mp.weixin.qq.com/s/A", "", "", "",
                 f'<p>正文</p><img src="{PROXIED}" />', "正文", 1787000000, 0, "poll"),
                ("F1", "a2", "还没抓到正文的", "https://mp.weixin.qq.com/s/B", "", "", "",
                 "", "", 1787100000, 0, "poll"),
            ],
        )
        connection.commit()
        connection.close()

    def tearDown(self):
        self.temp.cleanup()

    def test_rows_carry_account_name_and_content_flag(self):
        rows = list(read_from_db(self.path, since=0, limit=10))
        self.assertEqual([row["title"] for row in rows], ["招募通知", "还没抓到正文的"])
        self.assertEqual(rows[0]["nickname"], "清华大学学生社团")
        self.assertEqual(rows[0]["images"], [ORIGINAL])
        self.assertTrue(rows[0]["content_fetched"])
        # 正文没抓到的要能被认出来，而不是当成"空正文的正常文章"投出去。
        self.assertFalse(rows[1]["content_fetched"])

    def test_since_cursor_skips_already_exported(self):
        rows = list(read_from_db(self.path, since=1787000000, limit=10))
        self.assertEqual([row["title"] for row in rows], ["还没抓到正文的"])

    def test_database_is_opened_read_only(self):
        list(read_from_db(self.path, since=0, limit=10))
        connection = sqlite3.connect(self.path)
        try:
            count = connection.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(count, 2)


class LinkLineTest(unittest.TestCase):
    """链接清单来源不一，三种写法都得认。"""

    def test_bare_url(self):
        self.assertEqual(
            parse_link_line("https://mp.weixin.qq.com/s/A"), ("", "https://mp.weixin.qq.com/s/A")
        )

    def test_account_then_url(self):
        self.assertEqual(
            parse_link_line("清华大学社会实践\thttps://mp.weixin.qq.com/s/A"),
            ("清华大学社会实践", "https://mp.weixin.qq.com/s/A"),
        )

    def test_jsonl_line_from_wewe_export(self):
        line = '{"source_account": "清华紫荆之声", "source_url": "https://mp.weixin.qq.com/s/A", "title": "x"}'
        self.assertEqual(parse_link_line(line), ("清华紫荆之声", "https://mp.weixin.qq.com/s/A"))

    def test_blank_comment_and_garbage_are_skipped(self):
        for line in ("", "   ", "# 注释", "这一行没有链接", "{坏掉的 json"):
            self.assertIsNone(parse_link_line(line))


class ReadFromLinksTest(unittest.TestCase):
    """不碰网络：把 post_article 换掉，验证解析、重试和失败可见。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "links.txt"
        self.path.write_text(
            "https://mp.weixin.qq.com/s/OK\n"
            "清华大学社会实践 https://mp.weixin.qq.com/s/SLOW\n"
            "https://mp.weixin.qq.com/s/DEAD\n",
            "utf-8",
        )
        self.calls: list[str] = []
        self.original = wda_collector.post_article

        def fake_post(base, url, timeout=180):
            self.calls.append(url)
            if "SLOW" in url and self.calls.count(url) == 1:
                return {"success": False, "error": "文章获取过快，请0秒后重试（防风控）"}
            if "DEAD" in url:
                return {"success": False, "error": "触发微信安全验证。"}
            return {"success": True, "data": {
                "title": "招募通知",
                "plain_content": "正文" * 100,
                "images": [PROXIED],
                "author": "清华大学学生社团",
                "publish_time": 1787453409,
            }}

        wda_collector.post_article = fake_post

    def tearDown(self):
        wda_collector.post_article = self.original
        self.temp.cleanup()

    def rows(self):
        return list(read_from_links(self.path, "http://x", pause=0, limit=10))

    def test_account_falls_back_to_author(self):
        rows = self.rows()
        self.assertEqual(rows[0]["nickname"], "清华大学学生社团")   # 清单里没写，用 author
        self.assertEqual(rows[1]["nickname"], "清华大学社会实践")   # 清单里写了，以清单为准

    def test_rate_limit_is_waited_out_not_dropped(self):
        rows = self.rows()
        self.assertTrue(rows[1]["content_fetched"])
        self.assertEqual(self.calls.count("https://mp.weixin.qq.com/s/SLOW"), 2)

    def test_failure_keeps_its_reason(self):
        dead = self.rows()[2]
        self.assertFalse(dead["content_fetched"])
        self.assertIn("验证", dead["error"])

    def test_images_are_unproxied(self):
        self.assertEqual(self.rows()[0]["images"], [ORIGINAL])


class JsonlShapeTest(unittest.TestCase):
    """产出的每一行必须过 import_articles.py 的字段校验。"""

    def test_record_passes_import_check(self):
        from import_articles import check_record  # noqa: PLC0415

        record = {
            "source_account": "清华大学学生社团",
            "source_url": "https://mp.weixin.qq.com/s/TESTaaa111",
            "title": "2026年寒假社会实践支队招募通知",
            "publish_date": iso_date(1787453409),
            "raw_text": "为深入学习贯彻相关精神，现面向全校同学招募寒假社会实践支队队员。\n"
                        "实践地点：云南省大理白族自治州\n报名截止：2026年9月20日24:00\n"
                        "招募对象：全校本科生、研究生，不限院系\n经费支持：人均补助1500元\n"
                        "报名方式：扫描下方二维码填写报名表\n联系人：张老师",
            "images": [ORIGINAL],
        }
        self.assertEqual(check_record(1, json.loads(json.dumps(record))), [])


if __name__ == "__main__":
    unittest.main()
