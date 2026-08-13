import importlib.util
import unittest
from datetime import datetime, timezone
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "werss_daily_worker.py"
SPEC = importlib.util.spec_from_file_location("werss_daily_worker", SCRIPT)
worker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(worker)


class WeRssDailyWorkerTests(unittest.TestCase):
    def test_next_run_is_today_before_schedule(self):
        now = datetime(2026, 8, 12, 8, 0, tzinfo=timezone.utc)
        self.assertEqual(worker.seconds_until("08:25", now), 25 * 60)

    def test_next_run_rolls_to_tomorrow_after_schedule(self):
        now = datetime(2026, 8, 12, 9, 0, tzinfo=timezone.utc)
        self.assertEqual(worker.seconds_until("08:25", now), 23 * 3600 + 25 * 60)


if __name__ == "__main__":
    unittest.main()
