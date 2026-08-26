import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("wewe_collector", Path(__file__).parents[1] / "scripts" / "wewe_collector.py")
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


class WeweCollectorTests(unittest.TestCase):
    def test_html_and_images(self):
        source = '<p>实践招募</p><script>secret()</script><img data-src="https://mmbiz.qpic.cn/a.jpg">'
        self.assertEqual(MODULE.html_to_text(source), "实践招募")
        self.assertEqual(MODULE.extract_images(source), ["https://mmbiz.qpic.cn/a.jpg"])

    def test_candidate_title_gate(self):
        self.assertTrue(MODULE.likely_candidate("实践招募｜支队队员报名"))
        self.assertTrue(MODULE.likely_candidate("社区志愿招募通知"))
        self.assertTrue(MODULE.likely_candidate("志愿服务报名"))
        self.assertFalse(MODULE.likely_candidate("实践总结｜支队纪实"))
        self.assertTrue(MODULE.likely_candidate("成果展示｜秋季志愿者招募启动"))
        self.assertFalse(MODULE.likely_candidate("工作简报｜秋季志愿服务回顾"))

    def test_iso_date(self):
        self.assertEqual(MODULE.iso_date("2026-07-10T12:26:03.000Z"), "2026-07-10")

    def test_public_feed_name_mapping(self):
        original = MODULE.get_json
        MODULE.get_json = lambda path: [{"id": "MP_1", "name": "清华大学社会实践"}]
        try:
            self.assertEqual(MODULE.discover_feeds(), {"清华大学社会实践": "MP_1"})
        finally:
            MODULE.get_json = original

    def test_empty_completed_scan_is_successful(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(MODULE, "collect_articles", return_value=([], [], [])):
            original_output_dir = MODULE.OUTPUT_DIR
            MODULE.OUTPUT_DIR = Path(folder)
            try:
                with patch("sys.argv", ["wewe_collector.py", "collect", "清华大学乡村振兴工作站"]):
                    self.assertEqual(MODULE.main(), 0)
                batch = json.loads(next(Path(folder).glob("articles_*.json")).read_text(encoding="utf-8"))
                self.assertEqual(batch["articles"], [])
                self.assertFalse(batch["partial"])
            finally:
                MODULE.OUTPUT_DIR = original_output_dir


if __name__ == "__main__":
    unittest.main()
