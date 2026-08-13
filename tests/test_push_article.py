import importlib.util
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("push_article", ROOT / "scripts" / "push_article.py")
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class PushArticleInputTests(unittest.TestCase):
    def test_reads_jsonl(self):
        records = list(MODULE.iter_json_records('{"title":"one"}\n{"title":"two"}\n'))
        self.assertEqual([(1, "one"), (2, "two")], [(line, row["title"]) for line, row in records])

    def test_reads_pretty_printed_consecutive_objects(self):
        text = json.dumps({"title": "one"}, indent=2) + "\n" + json.dumps({"title": "two"}, indent=2)
        records = list(MODULE.iter_json_records(text))
        self.assertEqual(["one", "two"], [row["title"] for _, row in records])

    def test_reads_json_array(self):
        records = list(MODULE.iter_json_records('[{"title":"one"},{"title":"two"}]'))
        self.assertEqual(["one", "two"], [row["title"] for _, row in records])

    def test_rejects_non_object_record(self):
        with self.assertRaises(json.JSONDecodeError):
            list(MODULE.iter_json_records('["not an article"]'))


if __name__ == "__main__":
    unittest.main()
