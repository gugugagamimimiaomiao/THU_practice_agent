import importlib.util
import argparse
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "daily_wechat_update.py"
SPEC = importlib.util.spec_from_file_location("daily_wechat_update", SCRIPT)
daily_wechat_update = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(daily_wechat_update)


class DailyWeChatCandidateTests(unittest.TestCase):
    def test_cli_allows_a_department_wide_polite_crawl_window(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('parser.add_argument("--timeout", type=int, default=1800)', source)

    def test_daily_job_limits_default_sources_to_current_priority_accounts(self):
        self.assertEqual(daily_wechat_update.CURRENT_PRIORITY_ACCOUNTS, (
            "清华大学学生会", "清华大学学生社团", "清华紫荆之声", "清华大学学生公益",
            "清华大学乡村振兴工作站",
        ))
        self.assertNotIn("清华大学社会实践", daily_wechat_update.CURRENT_PRIORITY_ACCOUNTS)
        self.assertNotIn("无限之声", daily_wechat_update.CURRENT_PRIORITY_ACCOUNTS)

    def test_requires_an_explicit_recruitment_signal(self):
        self.assertTrue(daily_wechat_update.is_candidate({"title": "志愿招募 | 社区暑期儿童科普课堂志愿者招募"}))
        self.assertTrue(daily_wechat_update.is_candidate({"title": "2026 年暑期社会实践报名通知"}))
        self.assertFalse(daily_wechat_update.is_candidate({"title": "“AIEE之城”赴英国社会实践支队 | 行前预告"}))
        self.assertFalse(daily_wechat_update.is_candidate({"title": "薪火实践丨“京港”支队香港公益体验纪实（一）"}))

    def test_body_or_ocr_recruitment_signal_is_not_lost_when_title_is_editorial(self):
        decision = daily_wechat_update.candidate_decision({
            "title": "本周学生公益活动速览",
            "content": "现面向全校同学招募志愿者，请扫描二维码填写报名问卷。",
        })
        self.assertTrue(decision["candidate"])
        self.assertTrue(any("正文/OCR" in reason for reason in decision["reasons"]))

    def test_title_volunteer_signal_overrides_retrospective_for_server_recheck(self):
        decision = daily_wechat_update.candidate_decision({
            "title": "活动回顾｜志愿者服务纪实",
            "content": "感谢所有志愿者参与。",
        })
        self.assertTrue(decision["candidate"])
        self.assertTrue(any("覆盖排除" in reason for reason in decision["reasons"]))

    def test_marks_image_only_fields_for_ocr_review(self):
        from database import Database
        with tempfile.TemporaryDirectory() as folder:
            database = Database(Path(folder) / "test.db")
            outcome = daily_wechat_update.import_article(database, {
                "account": "清华大学学生公益", "title": "志愿者招募", "link": "https://mp.weixin.qq.com/s?mid=image",
                "content": "志愿者招募\n报名方式：扫描原文二维码。请志愿者在群内自由选择可以参与的时间段并完成报名。", "images": ["https://mmbiz.qpic.cn/example.png"],
            })
            self.assertEqual(outcome, "imported")
            project = next(item for item in database.list_projects() if item["title"] == "志愿者招募")
            self.assertIn(project["image_ocr_status"], {"runtime_unavailable", "partial_or_failed", "completed"})
            self.assertEqual(project["image_sources"], ["https://mmbiz.qpic.cn/example.png"])

    def test_repeat_scan_merges_same_link_instead_of_skipping(self):
        from database import Database
        with tempfile.TemporaryDirectory() as folder:
            database = Database(Path(folder) / "test.db")
            article = {
                "account": "清华大学学生公益", "title": "志愿者招募", "link": "https://mp.weixin.qq.com/s?mid=repeat",
                "content": "志愿者招募\n报名方式：扫描原文二维码。请志愿者在群内自由选择可以参与的时间段并完成报名。", "images": [],
            }
            self.assertEqual(daily_wechat_update.import_article(database, article), "imported")
            richer = {**article, "content": article["content"] + "\n活动时间：2026年7月20日—7月24日"}
            self.assertEqual(daily_wechat_update.import_article(database, richer), "merged")
            matches = [item for item in database.list_projects() if item["source_url"].endswith("mid=repeat")]
            self.assertEqual(len(matches), 1)
            self.assertEqual(matches[0]["practice_start"], "2026-07-20")

    def test_one_day_batch_runs_end_to_end_with_all_default_accounts(self):
        from wechat_sources import DEFAULT_ACCOUNTS

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            collector = root / "collector.py"
            collector.write_text("# simulated collector\n", encoding="utf-8")
            database_path = root / "one-day.db"
            audit_dir = root / "audits"

            articles = {
                "partial": False,
                "articles": [
                    {
                        "account": "建院宣传中心",
                        "title": "暑期乡村建设社会实践支队招募",
                        "link": "https://mp.weixin.qq.com/s?mid=one-day-candidate",
                        "publishDate": "2026-08-11",
                        "content": "面向全校同学招募。实践时间：2026年8月20日至8月24日。报名截止：2026年8月15日。",
                        "images": [],
                    },
                    {
                        "account": "无限之声",
                        "title": "暑期实践支队活动回顾",
                        "link": "https://mp.weixin.qq.com/s?mid=one-day-recap",
                        "publishDate": "2026-08-11",
                        "content": "感谢队员参与本次社会实践。",
                        "images": [],
                    },
                ]
            }

            def fake_run(command, *, env, timeout, capture_output, text, check):
                output = Path(env["WECHAT_DIGEST_OUTPUT_DIR"])
                (output / "articles_20260811.json").write_text(json.dumps(articles, ensure_ascii=False), encoding="utf-8")
                self.assertEqual(command[2], "collect")
                account_slice = command[3:command.index("--since")]
                self.assertEqual(account_slice, list(DEFAULT_ACCOUNTS))
                self.assertEqual(command[command.index("--since") + 1], "2026-08-11")
                return type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()

            args = argparse.Namespace(
                collector=str(collector), database=str(database_path), since="2026-08-11", count=5,
                timeout=60, accounts=list(DEFAULT_ACCOUNTS), audit_dir=str(audit_dir),
            )
            with patch.object(daily_wechat_update, "collector_credentials_present", return_value=True), \
                    patch.object(daily_wechat_update.subprocess, "run", side_effect=fake_run):
                self.assertEqual(daily_wechat_update.collect(args), 0)

            from database import Database
            projects = Database(database_path).list_projects()
            self.assertTrue(any(item["source_account"] == "建院宣传中心" for item in projects))
            audits = list(audit_dir.glob("scan-*.json"))
            self.assertEqual(len(audits), 1)
            audit = json.loads(audits[0].read_text(encoding="utf-8"))
            self.assertEqual(audit["summary"]["seen"], 2)
            self.assertEqual(audit["summary"]["candidate"], 1)
            self.assertEqual(audit["summary"]["non_candidate"], 1)

    def test_mismatched_account_search_result_is_not_imported(self):
        from database import Database
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            collector = root / "collector.py"
            collector.write_text("# simulated collector\n", encoding="utf-8")
            database_path = root / "mismatch.db"
            audit_dir = root / "audits"

            def fake_run(command, *, env, timeout, capture_output, text, check):
                payload = {"partial": False, "articles": [{
                    "query": "水利宣传", "account": "水利工程宣传中心",
                    "title": "社会实践招募", "link": "https://mp.weixin.qq.com/s?mid=mismatch",
                    "publishDate": "2026-08-11", "content": "报名方式：填写问卷", "images": [],
                }]}
                Path(env["WECHAT_DIGEST_OUTPUT_DIR"], "articles_20260811.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                return type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()

            args = argparse.Namespace(
                collector=str(collector), database=str(database_path), since="2026-08-11", count=5,
                timeout=60, accounts=["水利宣传"], audit_dir=str(audit_dir),
            )
            with patch.object(daily_wechat_update, "collector_credentials_present", return_value=True), \
                    patch.object(daily_wechat_update.subprocess, "run", side_effect=fake_run):
                self.assertEqual(daily_wechat_update.collect(args), 0)
            self.assertFalse(any(item["source_url"].endswith("mid=mismatch") for item in Database(database_path).list_projects()))
            audit = json.loads(next(audit_dir.glob("scan-*.json")).read_text(encoding="utf-8"))
            self.assertEqual(audit["summary"]["source_mismatch"], 1)


if __name__ == "__main__":
    unittest.main()
