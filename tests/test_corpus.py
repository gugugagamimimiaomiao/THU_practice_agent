import tempfile
import unittest
from pathlib import Path

from corpus import Sample, WritingCorpus, build_reference_block, clean_body, detect_genre, load_corpus
from database import Database
from wechat_ingest import import_article_text


def sample(title: str, account: str, body: str, genre: str = "") -> Sample:
    # 语料要求正文不少于 300 字（真实推送是 800~6400 字），太短的学不到写法。
    # 这里把段落重复到过线为止，而不是写死重复次数——改文案时不用重新数字数。
    text = body
    while len(text) < 400:
        text += body
    return Sample(title, account, text, genre or detect_genre(title, body))


class GenreTests(unittest.TestCase):
    def test_titles_are_classified_by_genre(self):
        for title, expected in [
            ("实践招募 | 赴贵州黔东南支队招募", "招募推送"),
            ("社团纳新丨2026年秋季学期学生社团招新启动", "招募推送"),
            ("实践总结丨以书信为桥，以行走为课", "实践总结"),
            ("实践基地进清华 | 北京丰台基地资源推介", "基地/资源介绍"),
            ("2026年暑期学生社会实践培训课程学习说明", "课程/通知"),
        ]:
            with self.subTest(title=title):
                self.assertEqual(detect_genre(title), expected)


class CleanBodyTests(unittest.TestCase):
    def test_wechat_boilerplate_and_repeated_title_are_dropped(self):
        """线上语料的正文开头常常是标题重复几遍加一段阅读器提示。

        留着不会出错，但范文额度只有一千出头字，被这些占掉很浪费；
        模型也可能把「去阅读」当成文章的一部分学过去。
        """
        title = "实践招募 | 赴贵州黔东南支队招募"
        raw = "\n".join([title, title, "清华大学社会实践", "在小说阅读器读本章", "去阅读",
                         "实践地介绍", "贵州群山深处，有一座被誉为世界最大民族博物馆的秘境。"])
        cleaned = clean_body(raw, title)
        self.assertNotIn("在小说阅读器", cleaned)
        self.assertNotIn("去阅读", cleaned)
        self.assertIn("贵州群山深处", cleaned)
        self.assertLessEqual(cleaned.count(title), 0, "开头重复的标题应该被去掉")


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.corpus = WritingCorpus([
            sample("实践招募 | 赴云南大理支教实践支队招募", "清华大学社会实践",
                   "现面向全校招募赴云南大理支教实践支队队员，走进当地中学开展课业辅导与教育资源调研。"),
            sample("志愿者招募 | 暑期校园讲解志愿服务", "清华大学学生公益",
                   "现招募校园讲解志愿者，为进校游客提供讲解服务，每半天提供高温补贴并计入志愿工时。"),
            sample("实践总结丨十四天的西部记忆", "清华大学社会实践",
                   "支队一行十二人抵达西部小城开始为期十四天的实践，走访小学与农户，收获远比付出多。"),
        ])

    def test_search_prefers_the_closest_topic(self):
        hits = self.corpus.search("赴云南支教的招募推送", genre="招募推送", limit=2)
        self.assertTrue(hits)
        self.assertIn("云南", hits[0].title)

    def test_genre_filter_excludes_other_kinds(self):
        hits = self.corpus.search("志愿服务", genre="实践总结", limit=3)
        self.assertTrue(all(h.genre == "实践总结" for h in hits))

    def test_short_texts_never_enter_the_corpus(self):
        # 一两百字的多半是图片型推送或抓取失败，当范文只会误导。
        corpus = WritingCorpus([Sample("很短的推送", "某号", "只有一句话。", "招募推送")])
        self.assertEqual(len(corpus), 0)


class ReferenceBlockTests(unittest.TestCase):
    def test_reference_block_states_the_boundary(self):
        """范文块必须自带边界声明。

        模型看到范文里的具体日期和联系方式，很自然会顺手写进正文——而那些是
        别的项目的事实。这段话是整条链路上唯一挡住它的东西，不能省。
        """
        block = build_reference_block([
            sample("实践招募 | 赴云南大理支教实践支队招募", "清华大学社会实践",
                   "现面向全校招募赴云南大理支教实践支队队员，报名截止2026年9月26日。"),
        ])
        self.assertIn("仅供参考写法", block)
        self.assertIn("严禁照抄", block)
        for word in ("日期", "地点", "联系方式"):
            self.assertIn(word, block)

    def test_empty_samples_produce_no_block(self):
        self.assertEqual(build_reference_block([]), "")


class LoadCorpusTests(unittest.TestCase):
    def test_corpus_includes_articles_that_are_not_opportunities(self):
        """非机会的文章才是最好的范文来源。

        实践总结、纪实、基地推介作为可报名机会是噪音，被筛掉了；但作为写作
        范例正好——真实、清华语境、已经发出去过。语料库要收的是全部原文，
        不是只收进了机会库的那些。
        """
        long_recap = ("支队一行十二人抵达西部小城，开始为期十四天的实践，"
                      "走访小学与农户，累计完成三十余份入户问卷。") * 8
        with tempfile.TemporaryDirectory() as folder:
            database = Database(Path(folder) / "test.db")
            result = import_article_text(
                database,
                {"title": "实践总结丨十四天的西部记忆", "source_account": "清华大学社会实践",
                 "source_url": "https://mp.weixin.qq.com/s/recap"},
                long_recap,
            )
            self.assertEqual(result["status"], "not_opportunity", "总结类不该进机会库")

            corpus = load_corpus(database)
            titles = [s.title for s in corpus.samples]
            self.assertIn("实践总结丨十四天的西部记忆", titles, "被筛掉的文章仍应进语料库")


if __name__ == "__main__":
    unittest.main()
