from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "historical_snapshot", ROOT / "scripts" / "build_historical_wechat_snapshot.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


def article(title: str, link: str, deadline_line: str) -> dict:
    return {
        "title": title,
        "account": "测试公众号",
        "link": link,
        "publishDate": "2026-06-01",
        "content": f"现面向全校招募。\n{deadline_line}\n报名方式：填写在线问卷",
        "images": [],
    }


class HistoricalSnapshotTests(unittest.TestCase):
    def test_loads_a_single_article_collector_file(self):
        sample = article("单篇支队招募", "https://mp.weixin.qq.com/s/single", "报名截止：6月5日")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "article.json"
            path.write_text(json.dumps(sample, ensure_ascii=False), encoding="utf-8")
            loaded = MODULE.load_articles([path])
        self.assertEqual(loaded, [sample])

    def test_requires_an_explicit_deadline_open_on_publish_date(self):
        valid = MODULE.historical_candidate(article("支队招募", "https://mp.weixin.qq.com/s/a", "报名截止：6月5日"))
        missing = MODULE.historical_candidate(article("另一支队招募", "https://mp.weixin.qq.com/s/b", "请尽快报名"))
        expired = MODULE.historical_candidate(article("过期支队招募", "https://mp.weixin.qq.com/s/c", "报名截止：5月31日"))
        self.assertIsNotNone(valid)
        self.assertIsNone(missing)
        self.assertIsNone(expired)

    def test_realistic_open_notice_may_describe_steps_after_signup_ends(self):
        sample = {
            "title": "报名开启丨2026年紫荆杯暑期电竞大赛",
            "account": "软小宣",
            "link": "https://mp.weixin.qq.com/s/esports",
            "publishDate": "2026-07-20",
            "content": (
                "报名时间：即日起至7月23日。\n"
                "报名方式：填写报名问卷。\n"
                "具体赛程将在报名结束后进行安排。"
            ),
            "images": [],
        }
        record = MODULE.historical_candidate(sample)
        self.assertIsNotNone(record)
        self.assertEqual(record["historical_signup_deadline"], "2026-07-23")

    def test_deduplicates_reposts_and_keeps_alternate_urls(self):
        first = MODULE.historical_candidate(article(
            "建院作品征集活动启动", "https://mp.weixin.qq.com/s/original", "作品征集截止：6月5日"
        ))
        second = MODULE.historical_candidate(article(
            "建院作品征集倒计时4天", "https://mp.weixin.qq.com/s/reminder", "作品征集截止：6月5日"
        ))
        records = MODULE.deduplicate([first, second])
        self.assertEqual(len(records), 1)
        self.assertEqual(len(records[0]["alternate_source_urls"]), 1)


if __name__ == "__main__":
    unittest.main()
