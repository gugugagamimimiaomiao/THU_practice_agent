import os
import stat
import tempfile
import unittest
from pathlib import Path

import collector_settings
from wechat_sources import DEFAULT_ACCOUNTS, LEGACY_DEFAULT_ACCOUNTS


class CollectorSettingsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.original_path = collector_settings.SETTINGS_PATH
        collector_settings.SETTINGS_PATH = Path(self.directory.name) / "collector_settings.json"

    def tearDown(self):
        collector_settings.SETTINGS_PATH = self.original_path
        self.directory.cleanup()

    def test_developer_save_keeps_credentials_out_of_public_status(self):
        status = collector_settings.save_from_developer({
            "accounts": "清华大学社会实践, 无限之声, 清华大学学生公益",
            "daily_time": "08:30",
            "enabled": True,
            "collector_path": "/tmp/wechat_collector.py",
            "replace_credentials": True,
            "token": "not-for-public-output",
            "cookie": "cookie-not-for-public-output",
        })
        self.assertTrue(status["credential_configured"])
        self.assertNotIn("cookie-not-for-public-output", str(status))
        self.assertEqual(collector_settings.credentials(), ("not-for-public-output", "cookie-not-for-public-output"))
        # POSIX 权限位只在类 Unix 系统上有意义。Windows 的 os.chmod 只能切换只读位，
        # NTFS 上文件会保持 0o666，因此这里不能断言 0o600。
        # 注意这不是测试放水：在 Windows 上开发时，凭据文件确实没有 0o600 保护，
        # 真实部署（Docker/Linux）才有。开发机上请不要长期存放真实 Token/Cookie。
        if os.name == "posix":
            mode = stat.S_IMODE(collector_settings.SETTINGS_PATH.stat().st_mode)
            self.assertEqual(mode, 0o600)

    def test_clear_credentials_disables_daily_job(self):
        collector_settings.save_from_developer({"replace_credentials": True, "token": "t", "cookie": "c", "enabled": True})
        status = collector_settings.clear_credentials()
        self.assertFalse(status["credential_configured"])
        self.assertFalse(status["enabled"])

    def test_profiles_can_be_saved_and_selected_later(self):
        first = collector_settings.save_from_developer({"profile_name": "学生公益", "replace_credentials": True, "token": "t1", "cookie": "c1"})
        second = collector_settings.save_from_developer({"profile_id": "__new__", "profile_name": "社会实践", "replace_credentials": True, "token": "t2", "cookie": "c2"})
        self.assertEqual(len(second["profiles"]), 2)
        restored = collector_settings.select_profile(first["active_profile_id"])
        self.assertEqual(restored["profile_name"], "学生公益")
        self.assertEqual(collector_settings.credentials(), ("t1", "c1"))

    def test_delete_switches_to_a_remaining_profile(self):
        first = collector_settings.save_from_developer({"profile_name": "甲", "replace_credentials": True, "token": "t1", "cookie": "c1"})
        second = collector_settings.save_from_developer({"profile_id": "__new__", "profile_name": "乙", "replace_credentials": True, "token": "t2", "cookie": "c2"})
        result = collector_settings.delete_profile(second["active_profile_id"])
        self.assertEqual(len(result["profiles"]), 1)
        self.assertEqual(result["active_profile_id"], first["active_profile_id"])

    def test_defaults_cover_all_department_accounts(self):
        status = collector_settings.public_status()
        self.assertEqual(status["accounts"], list(DEFAULT_ACCOUNTS))
        self.assertGreater(len(status["accounts"]), 60)
        for account in ("建院宣传中心", "无限之声", "清华经管家园", "清华大学紫荆书院"):
            self.assertIn(account, status["accounts"])

    def test_untouched_legacy_defaults_are_migrated(self):
        status = collector_settings.save_from_developer({"accounts": list(LEGACY_DEFAULT_ACCOUNTS)})
        self.assertEqual(status["accounts"], list(DEFAULT_ACCOUNTS))

    def test_explicit_large_account_list_is_not_truncated_at_twelve(self):
        accounts = [f"院系公众号{i}" for i in range(30)]
        status = collector_settings.save_from_developer({"accounts": accounts})
        self.assertEqual(status["accounts"], accounts)


if __name__ == "__main__":
    unittest.main()
