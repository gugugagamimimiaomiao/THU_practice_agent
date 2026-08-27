import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).parents[1] / "scripts" / "wewe_post_login.py"
SPEC = importlib.util.spec_from_file_location("wewe_post_login", SCRIPT)
worker = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(worker)


class WewePostLoginTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.queue = Path(self.directory.name) / "wewe-subscriptions.json"
        self.database = Path(self.directory.name) / "wewe-rss.db"
        self.original_queue = worker.QUEUE
        self.original_database = worker.WEWE_DB
        self.original_backfill_state = worker.BACKFILL_STATE
        worker.QUEUE = self.queue
        worker.WEWE_DB = self.database
        worker.BACKFILL_STATE = Path(self.directory.name) / "wewe-backfill-pending.json"

    def tearDown(self):
        worker.QUEUE = self.original_queue
        worker.WEWE_DB = self.original_database
        worker.BACKFILL_STATE = self.original_backfill_state
        self.directory.cleanup()

    def test_requires_scan_before_any_write_or_import(self):
        self.queue.write_text(json.dumps({"pending_resolution": [{"name": "甲", "article_url": "https://mp.weixin.qq.com/s/x"}]}), encoding="utf-8")
        with patch.object(worker, "_enabled_account", return_value=False), patch.object(worker.subprocess, "run") as run:
            self.assertEqual(worker.main(), 2)
        self.assertEqual(json.loads(self.queue.read_text(encoding="utf-8"))["pending_resolution"][0]["name"], "甲")
        run.assert_not_called()

    def test_login_resolves_exact_source_subscribes_and_imports(self):
        self.queue.write_text(json.dumps({"pending_resolution": [{"name": "清华大学乡村振兴工作站", "article_url": "https://mp.weixin.qq.com/s/x"}], "verified": []}), encoding="utf-8")
        feed = {"name": "清华大学乡村振兴工作站", "article_url": "https://mp.weixin.qq.com/s/x", "feed_id": "MP_1", "cover": "", "intro": "", "update_time": 1}
        completed = type("Completed", (), {"returncode": 0, "stdout": '{"ok": true}\n'})()
        with patch.object(worker, "_enabled_account", return_value=True), \
                patch.object(worker, "_resolve_pending", return_value=feed), \
                patch.object(worker, "_subscribe", return_value="subscribed"), \
                patch.object(worker, "_activate_one") as activate, \
                patch.object(worker, "_sync_new_feed", return_value={"ok": True}) as sync, \
                patch.object(worker, "_daily_import", return_value=completed), \
                patch.object(worker, "_push_server", return_value={"ok": True, "action": "not_configured", "pushed": 0}):
            self.assertEqual(worker.main(), 0)
        saved = json.loads(self.queue.read_text(encoding="utf-8"))
        self.assertEqual(saved["pending_resolution"], [])
        self.assertEqual(saved["verified"][0]["feed_id"], "MP_1")
        self.assertEqual(saved["verified"][0]["status"], "subscribed")
        activate.assert_not_called()
        sync.assert_called_once_with("MP_1", unittest.mock.ANY)

    def test_new_activation_syncs_history_and_backfills_before_push(self):
        self.queue.write_text(json.dumps({"pending_resolution": [], "verified": []}), encoding="utf-8")
        completed = type("Completed", (), {"returncode": 0, "stdout": '{"ok": true}\n'})()
        with patch.object(worker, "_enabled_account", return_value=True), \
                patch.object(worker, "_activate_one", return_value={"ok": True, "activated": "新公众号", "feed_id": "MP_NEW"}), \
                patch.object(worker, "_sync_new_feed", return_value={"ok": True, "articles": 50}) as sync, \
                patch.object(worker, "_daily_import", return_value=completed) as daily, \
                patch.object(worker, "_push_server", return_value={"ok": True, "action": "pushed", "pushed": 3}):
            self.assertEqual(worker.main(), 0)
        sync.assert_called_once_with("MP_NEW", unittest.mock.ANY)
        self.assertIn("新公众号", daily.call_args.args[0])
        self.assertEqual(daily.call_args.args[2], 80)
        self.assertFalse(worker.BACKFILL_STATE.exists())

    def test_incomplete_backfill_resumes_before_another_activation(self):
        self.queue.write_text(json.dumps({"pending_resolution": [], "verified": []}), encoding="utf-8")
        worker.BACKFILL_STATE.write_text(json.dumps({"name": "恢复号", "feed_id": "MP_RESUME"}), encoding="utf-8")
        failed = type("Completed", (), {"returncode": 1, "stdout": ""})()
        with patch.object(worker, "_enabled_account", return_value=True), \
                patch.object(worker, "_activate_one") as activate, \
                patch.object(worker, "_sync_new_feed", return_value={"ok": True}) as sync, \
                patch.object(worker, "_daily_import", return_value=failed), \
                patch.object(worker, "_push_server") as push:
            self.assertEqual(worker.main(), 1)
        activate.assert_not_called()
        sync.assert_called_once_with("MP_RESUME", unittest.mock.ANY)
        push.assert_not_called()
        self.assertTrue(worker.BACKFILL_STATE.exists())


if __name__ == "__main__":
    unittest.main()
