import importlib.util
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "daily_wechat_update.py"
SPEC = importlib.util.spec_from_file_location("daily_wechat_update", SCRIPT)
daily_wechat_update = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(daily_wechat_update)


class DailyWeChatCandidateTests(unittest.TestCase):
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

    def test_retrospective_is_excluded_even_if_it_mentions_volunteers(self):
        decision = daily_wechat_update.candidate_decision({
            "title": "活动回顾｜志愿者服务纪实",
            "content": "感谢所有志愿者参与。",
        })
        self.assertFalse(decision["candidate"])
        self.assertTrue(any("排除信号" in reason for reason in decision["reasons"]))

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


if __name__ == "__main__":
    unittest.main()
