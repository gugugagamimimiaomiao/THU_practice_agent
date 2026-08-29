"""对话里导入的通知必须进隔离区，绝不能改动采集来的项目。

2026-08-29 审出来的洞，是这个项目上最严重的一个，因为它**能改数据**而不只是
影响某一次输出：

  清小搭上任何用户，在对话框里打一句「导入这则通知：……」，就会走到
  _import_notice —— 这是 reply() 的第一个分支，没有任何管理鉴权。它会
  insert_article + upsert_project，真的写库。

  致命的是 id 怎么来的：

      project_id = stable_id(source_url or title, organizer, deadline or "")

  source_url 是从用户消息里正则抠出来的。用户只要把某个真实项目的原文链接
  抄进去，算出的 id 就跟那个项目一模一样，upsert 的 ON CONFLICT(id) DO UPDATE
  把它整条覆盖——标题、状态、报名截止、地点全换成他写的。
  而原文链接印在我们每一张推荐卡上，人人可见。

  状态那一行又补了一刀：

      status = "needs_review" if "source_url" in uncertain_fields else "published"

  用户自己给了链接 => "有出处"成立 => 直接 published => 当场进正式推荐。

本地库副本上实证过：

    AI助老与社区数字素养服务实践 / expired / 北京市海淀区
    -> 原招募已取消，请勿报名 / published / 海南三亚
    库里 8 条还是 8 条：原地覆盖、不新增、无任何提示，随后出现在正式推荐里。

修法是三条一起，少一条都能绕过：id 加前缀、状态钉死 needs_review、打标记。
下面每条都对应其中一条，别删。
"""
import shutil
import tempfile
import unittest
from pathlib import Path

import chat_adapter
from chat_adapter import PracticeChatAdapter
from database import Database

NOTICE = ("导入这则通知：某某支队招募\n"
          "主办方：某某学生组织\n"
          "报名截止：2036-12-31\n"
          "实践时间：2036-01-01 至 2036-01-02\n"
          "地点：海南三亚\n"
          "报名方式：扫码报名\n"
          "面向：全校同学\n")


class ImportQuarantineTests(unittest.TestCase):
    def setUp(self):
        chat_adapter.llm.is_enabled = lambda: False
        self.tempdir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.tempdir.name) / "chat.db")
        self.adapter = PracticeChatAdapter(self.database)
        self.victim = next(p for p in self.adapter._projects(include_expired=True)
                           if p.get("source_url"))

    def tearDown(self):
        self.tempdir.cleanup()

    def do_import(self, text):
        result = self.adapter.reply([{"role": "user", "content": text}])
        self.adapter._reset_project_cache()
        return result

    def test_an_import_never_rewrites_an_existing_project(self):
        """核心那条。攻击者只需要目标的原文链接，而链接印在推荐卡上。"""
        before = dict(self.victim)
        self.do_import(NOTICE + f"原文链接：{before['source_url']}")
        after = self.database.get_project(before["id"])
        self.assertEqual(after["title"], before["title"])
        self.assertEqual(after["status"], before["status"])
        self.assertEqual(after.get("signup_deadline"), before.get("signup_deadline"))
        self.assertEqual(after.get("location"), before.get("location"))

    def test_the_import_is_added_rather_than_swallowed(self):
        """不覆盖，但也不能默默丢掉——学生贡献的线索还是要留下。"""
        count = len(self.adapter._projects(include_expired=True))
        self.do_import(NOTICE + f"原文链接：{self.victim['source_url']}")
        self.assertEqual(len(self.adapter._projects(include_expired=True)), count + 1)

    def test_an_imported_project_is_never_publishable(self):
        """用户自己贴的链接不构成"有出处"。只有真去抓过原文才算。"""
        result = self.do_import(NOTICE + "原文链接：https://mp.weixin.qq.com/s/whatever")
        self.assertEqual(self.database.get_project(result.project_id)["status"], "needs_review")

    def test_an_imported_project_stays_out_of_the_recommendations(self):
        result = self.do_import(NOTICE + "原文链接：https://mp.weixin.qq.com/s/whatever")
        title = self.database.get_project(result.project_id)["title"]
        listed = self.adapter.reply([{"role": "user", "content": "推荐几个实践"}]).content
        formal = listed.split("## 线索")[0]
        self.assertNotIn(title, formal)

    def test_imported_ids_can_never_collide_with_collected_ones(self):
        """光去掉 find_duplicate 是不够的：id 本身就是从 source_url 算的，
        撞车发生在 upsert 的 ON CONFLICT 上。前缀才是真正隔开的那一层。"""
        result = self.do_import(NOTICE + f"原文链接：{self.victim['source_url']}")
        self.assertTrue(result.project_id.startswith(PracticeChatAdapter.CHAT_IMPORT_PREFIX))
        for project in self.adapter._projects(include_expired=True):
            if project["id"] != result.project_id:
                self.assertFalse(project["id"].startswith(PracticeChatAdapter.CHAT_IMPORT_PREFIX))

    def test_imported_rows_are_labelled_so_they_can_be_purged(self):
        """出了事得能一条命令全找出来。"""
        result = self.do_import(NOTICE)
        project = self.database.get_project(result.project_id)
        self.assertTrue(project.get("chat_import"))
        self.assertEqual(project.get("source_account"), PracticeChatAdapter.CHAT_IMPORT_ACCOUNT)
        self.assertIn("对话导入，未经核验", project.get("risk_notes", []))

    def test_the_reply_says_it_is_not_verified(self):
        """用户得知道这条没进正式库，否则他会以为报名信息已经生效。"""
        content = self.do_import(NOTICE).content
        self.assertIn("线索", content)
        self.assertIn("不会覆盖", content)

    def test_repeating_the_same_paste_does_not_multiply_rows(self):
        self.do_import(NOTICE)
        count = len(self.adapter._projects(include_expired=True))
        self.do_import(NOTICE)
        self.assertEqual(len(self.adapter._projects(include_expired=True)), count)

    def test_a_huge_paste_is_capped(self):
        """请求体上限 2MB，不截断的话整段都会进 articles 表。

        第一版这条是空的：拿 latest_article_text("") 查，它恒返回空字符串，
        断言必然通过。直接查表才算数。
        """
        self.do_import(NOTICE + "啊" * 50000)
        with self.database.connect() as db:
            longest = db.execute("SELECT MAX(LENGTH(raw_text)) FROM articles").fetchone()[0]
        self.assertIsNotNone(longest, "前提变了：这次导入根本没往 articles 写")
        self.assertGreater(longest, 100, "前提变了：存进去的正文短得不正常")
        self.assertLessEqual(longest, PracticeChatAdapter.MAX_IMPORT_CHARS)


class OnlyOneWritePathReachesTheDatabaseTests(unittest.TestCase):
    """对话是只读的，唯一的例外是导入。这条要是破了，上面那些防护就绕过去了。"""

    def test_chat_adapter_writes_only_through_the_import_path(self):
        source = Path(__file__).resolve().parent.parent / "chat_adapter.py"
        text = source.read_text(encoding="utf-8")
        writes = [line.strip() for line in text.split("\n")
                  if ("self.db.insert_article" in line
                      or "self.db.upsert_project" in line
                      or "self.db.delete_" in line)]
        self.assertEqual(len(writes), 2, f"对话层多了写库的地方：{writes}")


if __name__ == "__main__":
    unittest.main()
