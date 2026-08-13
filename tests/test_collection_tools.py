import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_script(name):
    path = ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(name.replace(".py", ""), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CollectionQualityTests(unittest.TestCase):
    def test_ocr_recovers_fields_and_recap_stays_negative(self):
        evaluator = load_script("evaluate_collection_quality.py")
        samples = [json.loads(line) for line in (ROOT / "data" / "collection_quality_samples.example.jsonl").read_text(encoding="utf-8").splitlines()]
        report = evaluator.evaluate(samples)
        self.assertEqual(report["classification"]["precision"], 1.0)
        self.assertEqual(report["classification"]["recall"], 1.0)
        self.assertGreaterEqual(report["ocr_fields"]["recovered_from_images"], 3)

    def test_backfill_batches_are_bounded_and_complete(self):
        backfill = load_script("backfill_wechat.py")
        accounts = [f"account-{index}" for index in range(17)]
        groups = backfill.batches(accounts, 6)
        self.assertEqual([len(group) for group in groups], [6, 6, 5])
        self.assertEqual([item for group in groups for item in group], accounts)

    def test_backfill_state_resets_when_scope_changes(self):
        backfill = load_script("backfill_wechat.py")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            path.write_text(json.dumps({"since": "2026-01-01", "accounts": ["a"], "completed_batches": [0]}), encoding="utf-8")
            state = backfill.load_state(path, "2026-02-01", ["a"])
            self.assertEqual(state["completed_batches"], [])


if __name__ == "__main__":
    unittest.main()
