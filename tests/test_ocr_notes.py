"""OCR 跑完之后，别再挂着「配图尚未识别」那句话。

2026-08-30 的空跑暴露的：那句话是入库时写死进 risk_notes 的
（wechat_ingest._attach_images），重抽会原样再写一遍——而 --ocr 刚刚
**已经识别过了**。直接 apply 的话，界面上会挂一句假话，还指着一个
不存在的出路（"关键信息在 24 张配图里，去看图"）。

真相更有用：图也读了，里面确实没有报名信息。审核的人看到这句才知道
别再等 OCR，直接去问主办方。

这一整晚修的都是同一类东西——**系统说的话要跟事实对得上**。这条只是
它在数据管线里的版本。
"""
import re
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "reextract.py"


class OCRNotesTests(unittest.TestCase):
    def setUp(self):
        self.source = SCRIPT.read_text(encoding="utf-8")

    def test_the_stale_note_is_stripped_after_ocr(self):
        """OCR 分支必须把「配图尚未识别」从 risk_notes 里清掉。"""
        block = self.source.split('fresh["image_ocr_status"] = "completed"')[1][:1200]
        self.assertIn("配图尚未识别", block, "清理那一步不见了")
        self.assertRegex(
            block,
            r'if\s+"配图尚未识别"\s+not\s+in\s+note',
            "没有把旧提示过滤掉——OCR 完了还挂着「尚未识别」就是假话")

    def test_it_says_what_is_still_missing(self):
        """光删掉不够，得说清楚图里也没有什么，否则审核的人不知道下一步。"""
        block = self.source.split('fresh["image_ocr_status"] = "completed"')[1][:1200]
        self.assertIn("配图已识别", block)
        for field in ("报名截止", "参与资格", "报名方式"):
            self.assertIn(field, block, f"缺字段清单里没有{field}")
        self.assertIn("不要再等 OCR", block)

    def test_the_ingest_side_note_still_exists_for_un_ocred_projects(self):
        """还没 OCR 的项目，那句提示是**对的**，不能一起删掉。"""
        ingest = (SCRIPT.parent.parent / "wechat_ingest.py").read_text(encoding="utf-8")
        self.assertIn("配图尚未识别", ingest)

    def test_the_promotion_guard_is_still_there(self):
        """配图没识别的项目不能因为重抽被放行——这道闸门跟上面的提示是一套。"""
        self.assertIn('image_ocr_status") != "completed"', self.source)
        self.assertIn('fresh["status"] = "needs_review"', self.source)


if __name__ == "__main__":
    unittest.main()
