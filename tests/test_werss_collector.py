import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "werss_collector.py"
SPEC = importlib.util.spec_from_file_location("werss_collector", SCRIPT)
werss_collector = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(werss_collector)


def create_werss_database(path: Path) -> None:
    database = sqlite3.connect(path)
    database.executescript("""
        CREATE TABLE feeds (id TEXT PRIMARY KEY, mp_name TEXT, status INTEGER);
        CREATE TABLE articles (
            id TEXT PRIMARY KEY, mp_id TEXT, title TEXT, url TEXT, description TEXT,
            content TEXT, content_html TEXT, publish_time INTEGER, status INTEGER
        );
        INSERT INTO feeds VALUES ('mp-1', '清华大学社会实践', 1);
        INSERT INTO articles VALUES (
            'article-1', 'mp-1', '暑期社会实践支队招募',
            'https://mp.weixin.qq.com/s/example', '', '',
            '<p>面向全校同学招募，报名截止：2026年8月20日。</p><img data-src="https://mmbiz.qpic.cn/poster.png">',
            1786500000, 1
        );
    """)
    database.commit()
    database.close()


class WeRssCollectorTests(unittest.TestCase):
    def test_reads_matching_accounts_and_converts_html(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "db.db"
            create_werss_database(path)
            articles, missing = werss_collector.collect_articles(
                path, ["清华大学社会实践", "不存在的公众号"], "2026-08-01", 50,
            )
            self.assertEqual(missing, ["不存在的公众号"])
            self.assertEqual(len(articles), 1)
            self.assertEqual(articles[0]["query"], articles[0]["account"])
            self.assertIn("面向全校同学招募", articles[0]["content"])
            self.assertEqual(articles[0]["images"], ["https://mmbiz.qpic.cn/poster.png"])

    def test_cli_emits_the_existing_collector_batch_contract(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            database = root / "db.db"
            output = root / "output"
            create_werss_database(database)
            environment = {**os.environ, "WECHAT_DIGEST_OUTPUT_DIR": str(output)}
            completed = subprocess.run(
                [sys.executable, str(SCRIPT), "--database", str(database), "collect", "清华大学社会实践", "--since", "2026-08-01", "--count", "50", "--no-kb"],
                env=environment, capture_output=True, text=True, check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = json.loads(completed.stdout)
            self.assertEqual(result["articles"], 1)
            batch = json.loads(Path(result["output"]).read_text(encoding="utf-8"))
            self.assertEqual(batch["collector"], "we-mp-rss")
            self.assertFalse(batch["partial"])
            self.assertEqual(len(batch["articles"]), 1)


if __name__ == "__main__":
    unittest.main()
