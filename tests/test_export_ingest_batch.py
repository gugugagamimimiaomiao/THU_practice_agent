from __future__ import annotations

import importlib.util
import json
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "export_ingest_batch.py"
SPEC = importlib.util.spec_from_file_location("export_ingest_batch", SCRIPT)
export_ingest_batch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(export_ingest_batch)

HANDOFF_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "wewe_export_handoff.py"
HANDOFF_SPEC = importlib.util.spec_from_file_location("wewe_export_handoff", HANDOFF_SCRIPT)
wewe_export_handoff = importlib.util.module_from_spec(HANDOFF_SPEC)
HANDOFF_SPEC.loader.exec_module(wewe_export_handoff)


class ExportIngestBatchTests(unittest.TestCase):
    def test_exports_only_real_current_opportunities(self):
        today = date.today()
        future = (today + timedelta(days=7)).isoformat()
        past = (today - timedelta(days=7)).isoformat()

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            database = root / "projects.db"
            output = root / "batch.jsonl"
            connection = sqlite3.connect(database)
            connection.executescript(
                """
                CREATE TABLE articles (
                    id INTEGER PRIMARY KEY,
                    raw_text TEXT NOT NULL
                );
                CREATE TABLE projects (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    signup_deadline TEXT,
                    source_url TEXT,
                    document TEXT NOT NULL
                );
                """
            )

            def insert(identifier: int, *, status: str = "published", deadline: str = "",
                       practice_end: str = "", demo: bool = False) -> None:
                url = f"https://mp.weixin.qq.com/s/current-{identifier}"
                document = {
                    "article_id": identifier,
                    "demo_data": demo,
                    "source_account": "可信公众号",
                    "source_url": url,
                    "title": f"机会 {identifier}",
                    "publish_date": today.isoformat(),
                    "practice_end": practice_end,
                }
                connection.execute(
                    "INSERT INTO articles(id, raw_text) VALUES (?, ?)",
                    (identifier, "当前真实招募正文" * 20),
                )
                connection.execute(
                    "INSERT INTO projects(id, status, signup_deadline, source_url, document) VALUES (?, ?, ?, ?, ?)",
                    (str(identifier), status, deadline, url, json.dumps(document, ensure_ascii=False)),
                )

            insert(1, deadline=future)
            insert(2, status="needs_review", practice_end=future)
            insert(3, status="expired", deadline=future)
            insert(4, deadline=past)
            insert(5, practice_end=past)
            insert(6, deadline=future, demo=True)
            connection.commit()
            connection.close()

            records = export_ingest_batch.export(database, output, 20)

        self.assertEqual({record["title"] for record in records}, {"机会 1", "机会 2"})

    def test_handoff_rejects_expired_and_demo_articles_after_fulltext(self):
        current = {
            "source_account": "清华紫荆之声",
            "source_url": "https://mp.weixin.qq.com/s/current",
            "title": "志愿者招募",
            "publish_date": date.today().isoformat(),
            "raw_text": f"面向全校招募志愿者。报名截止：{(date.today() + timedelta(days=2)).isoformat()}。报名方式：扫描二维码。",
        }
        expired = {
            **current,
            "source_url": "https://mp.weixin.qq.com/s/expired",
            "raw_text": f"面向全校招募志愿者。报名截止：{(date.today() - timedelta(days=2)).isoformat()}。报名方式：扫描二维码。",
        }
        demo = {
            **current,
            "source_url": "https://example.invalid/demo",
            "title": "志愿者招募（演示通知）",
        }

        self.assertTrue(wewe_export_handoff.is_current_opportunity(current))
        self.assertFalse(wewe_export_handoff.is_current_opportunity(expired))
        self.assertFalse(wewe_export_handoff.is_current_opportunity(demo))

    def test_historical_corpus_gate_is_high_precision(self):
        accepted = (
            "实践总结丨以书信为桥，以行走为课",
            "志愿劳动 | 科普游园会服务纪实",
            "基层服务 | 支队调研成果总结",
            "国际会议参会心得与实践感悟",
        )
        rejected = (
            "志愿者招募通知",
            "实践支队行前预告",
            "调研团报名开启",
            "普通校园活动回顾",
        )
        for title in accepted:
            self.assertTrue(wewe_export_handoff.is_corpus_article(title), title)
        for title in rejected:
            self.assertFalse(wewe_export_handoff.is_corpus_article(title), title)

    def test_since_and_prior_handoffs_produce_a_true_increment(self):
        today = date.today()
        old = (today - timedelta(days=30)).isoformat()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            database = root / "projects.db"
            output = root / "batch.jsonl"
            prior_json = root / "prior.json"
            prior_jsonl = root / "prior.jsonl"
            connection = sqlite3.connect(database)
            connection.executescript(
                """
                CREATE TABLE articles (id INTEGER PRIMARY KEY, raw_text TEXT NOT NULL);
                CREATE TABLE projects (
                    id TEXT PRIMARY KEY, status TEXT NOT NULL, signup_deadline TEXT,
                    source_url TEXT, document TEXT NOT NULL
                );
                """
            )
            for identifier, published in ((1, today.isoformat()), (2, today.isoformat()), (3, old)):
                url = f"https://mp.weixin.qq.com/s/item-{identifier}"
                document = {
                    "article_id": identifier, "demo_data": False, "source_account": "可信公众号",
                    "source_url": url, "title": f"机会 {identifier}", "publish_date": published,
                    "practice_end": "",
                }
                connection.execute("INSERT INTO articles VALUES (?, ?)", (identifier, "真实招募正文" * 30))
                connection.execute(
                    "INSERT INTO projects VALUES (?, 'published', '', ?, ?)",
                    (str(identifier), url, json.dumps(document, ensure_ascii=False)),
                )
            connection.commit()
            connection.close()
            prior_json.write_text(
                json.dumps({"source_url": "https://mp.weixin.qq.com/s/item-1"}, indent=2)
                + "\n"
                + json.dumps({"source_url": "https://mp.weixin.qq.com/s/also-unrelated"}, indent=2),
                encoding="utf-8",
            )
            prior_jsonl.write_text('{"source_url":"https://mp.weixin.qq.com/s/unrelated"}\n', encoding="utf-8")

            excluded = export_ingest_batch.excluded_source_urls([prior_json, prior_jsonl])
            records = export_ingest_batch.export(
                database, output, 20, since=today.isoformat(), excluded_urls=excluded
            )

        self.assertEqual([record["title"] for record in records], ["机会 2"])


if __name__ == "__main__":
    unittest.main()
