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


if __name__ == "__main__":
    unittest.main()
