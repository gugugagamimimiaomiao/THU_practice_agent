"""「改上一份」不该被重新分类成另一个任务。

来自 2026-08-21 的 100 轮实测。用户只是要求修改刚拿到的材料，却被按当前
这一句里的关键词重新分类：

    「加入两个比较维度」  → 跳成单项目详情（"比较"被 _COMPARE_RE 抢走）
    「压缩到 120 字」     → 跳成推荐列表
    「不要用常识补充」    → 跳成重新筛选
    「加入未成年人保护」  → 跳成校园讲解志愿者推文
    修改访谈提纲          → 跳成报名表

路由器只看当前这一句有什么词，不看"现在正在干什么"。

这里的安全边界是：只有上一条回复确实是一份**自由文本草稿**时才算改稿。
如果上一条是推荐列表或项目卡，「换成湖南的」说的是筛选条件，不是稿子。
"""
import tempfile
import unittest
from pathlib import Path

import chat_adapter
from chat_adapter import PracticeChatAdapter
from database import Database

DRAFT = (
    "尊敬的老师：\n\n我是清华大学的本科生，希望申请加入贵支队。我对乡村教育议题长期关注，"
    "曾在校内参与过课业辅导相关的志愿活动，具备基本的沟通与组织能力。\n\n"
    "如蒙录用，我会认真完成课程筹备与活动设计工作。\n\n此致\n敬礼"
)
RECOMMEND_REPLY = (
    "## 正式推荐\n\n1. **实践招募 | 宝庆微光赴湖南新宁支教实践支队招募**\n   - 地点 湖南\n"
)


class RevisionRoutingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tempdir = tempfile.TemporaryDirectory()
        cls.database = Database(Path(cls.tempdir.name) / "chat.db")

    @classmethod
    def tearDownClass(cls):
        cls.tempdir.cleanup()

    def setUp(self):
        chat_adapter.llm.is_enabled = lambda: False  # 不打真实模型，只验路由
        self.adapter = PracticeChatAdapter(self.database)

    def after_draft(self, instruction: str):
        return self.adapter.reply([
            {"role": "user", "content": "帮我写一份报名理由"},
            {"role": "assistant", "content": DRAFT},
            {"role": "user", "content": instruction},
        ])

    def test_edit_instructions_go_to_revision(self):
        for instruction in ["加入两个比较维度", "压缩到 120 字", "不要用常识补充",
                            "加入未成年人保护的内容", "语气再正式一点",
                            "去掉最后一段", "改成第一人称"]:
            with self.subTest(instruction=instruction):
                result = self.after_draft(instruction)
                self.assertIn(result.intent, {"revise", "revise_degraded"},
                              f"「{instruction}」被路由成了 {result.intent}")

    def test_structured_reply_is_not_treated_as_a_draft(self):
        """上一条是推荐列表时，「换成湖南的」改的是筛选条件，不是稿子。"""
        result = self.adapter.reply([
            {"role": "user", "content": "推荐一些实践"},
            {"role": "assistant", "content": RECOMMEND_REPLY},
            {"role": "user", "content": "换成湖南的"},
        ])
        self.assertNotIn(result.intent, {"revise", "revise_degraded"})

    def test_no_previous_draft_means_no_revision(self):
        # 第一句就说「压缩到120字」——没有可改的东西，不能假装在改。
        result = self.adapter.reply([{"role": "user", "content": "压缩到 120 字"}])
        self.assertNotIn(result.intent, {"revise", "revise_degraded"})

    def test_long_pasted_text_is_polishing_not_revising(self):
        """贴一整段新文稿要求改，那是润色，改的不是我上一份。"""
        pasted = "帮我改改这段：" + "盛夏的七月，我们踏上了前往西部的列车，窗外的风景一路向后退去。" * 3
        result = self.adapter.reply([
            {"role": "user", "content": "帮我写一份报名理由"},
            {"role": "assistant", "content": DRAFT},
            {"role": "user", "content": pasted},
        ])
        self.assertIn(result.intent, {"polish", "polish_degraded"})

    def test_degraded_reply_repeats_the_instruction_back(self):
        # 没配模型时也得让用户知道我听懂了什么，而不是掉一句通用兜底。
        content = self.after_draft("压缩到 120 字").content
        self.assertIn("压缩到", content)


class RevisionPromptTests(unittest.TestCase):
    """改稿时不许自己加事实——实测里模型给没有支教经历的用户写上了
    「具备课堂协助、需求访谈与资料整理所需的基础能力」。"""

    @classmethod
    def setUpClass(cls):
        cls.tempdir = tempfile.TemporaryDirectory()
        cls.database = Database(Path(cls.tempdir.name) / "chat.db")

    @classmethod
    def tearDownClass(cls):
        cls.tempdir.cleanup()

    def test_system_prompt_forbids_inventing_facts(self):
        captured = {}

        def fake_complete(system_prompt, user_prompt, **kwargs):
            captured["system"] = system_prompt
            captured["user"] = user_prompt
            return "改好的稿子"

        chat_adapter.llm.is_enabled = lambda: True
        chat_adapter.llm.complete = fake_complete
        adapter = PracticeChatAdapter(self.database)
        result = adapter.reply([
            {"role": "user", "content": "帮我写一份报名理由"},
            {"role": "assistant", "content": DRAFT},
            {"role": "user", "content": "加入我的支教经历"},
        ])
        self.assertEqual(result.intent, "revise")
        self.assertIn("不许新增事实", captured["system"])
        self.assertIn("占位", captured["system"])
        # 上一版稿子必须完整传给模型，否则它只能凭空重写。
        self.assertIn("乡村教育议题长期关注", captured["user"])


if __name__ == "__main__":
    unittest.main()
