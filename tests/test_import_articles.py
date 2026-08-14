from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("import_articles", ROOT / "scripts" / "import_articles.py")
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class ImportArticlesTests(unittest.TestCase):
    def test_import_metadata_preserves_image_urls(self):
        record = {
            "source_account": "清华大学学生公益",
            "source_url": "https://mp.weixin.qq.com/s/example",
            "title": "志愿者招募",
            "publish_date": "2026-08-11",
            "images": ["https://mmbiz.qpic.cn/a.jpg", "  ", "https://mmbiz.qpic.cn/b.jpg"],
        }
        metadata = MODULE.import_metadata(record)
        self.assertEqual(metadata["images"], [
            "https://mmbiz.qpic.cn/a.jpg",
            "https://mmbiz.qpic.cn/b.jpg",
        ])


if __name__ == "__main__":
    unittest.main()
