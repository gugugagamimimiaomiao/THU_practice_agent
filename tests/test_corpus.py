import tempfile
import unittest
from pathlib import Path

from corpus import (
    Sample,
    WritingCorpus,
    build_reference_block,
    clean_body,
    detect_genre,
    is_writing_sample,
    load_corpus,
)
from database import Database
from wechat_ingest import import_article_text


# 一篇形态贴近真实推送的实践总结：分段、够长。
#
# 夹具必须像真实数据。之前用"一句话重复 N 遍拼成一整行"，先后被长度门槛和
# 分段规则挡掉两次，每次都得回头改夹具——那等于测试在测自己，而不是测代码。
REALISTIC_RECAP = "\n".join([
    "七月的风裹着热浪。支队一行十二人从北京出发，辗转两天抵达西部小城，"
    "开始了为期十四天的社会实践。出发前我们做了很多准备，但真正落地后才发现，"
    "书本上的认识和眼前的现实之间，隔着的东西比想象中多。",
    "第一天我们走访了当地小学。教室的窗户还是老式木框，黑板边缘已经磨白，"
    "但孩子们的眼神很亮。我们原本准备了一套科普课程，讲了十分钟就发现节奏不对——"
    "他们感兴趣的不是概念，而是那些概念背后具体的、可以动手的东西。当天晚上我们把课重写了一遍。",
    "第五天，我们完成了第一轮入户问卷，累计走访三十余户人家。有位老人拉着我们说了很久，"
    "讲他年轻时怎么把水引上坡地。这些细节问卷上一格也填不下，但它们才是这片土地真正的样子。",
    "第十天，我们与当地干部座谈，了解产业帮扶的进展和现实困难。政策文件里干净利落的几行字，"
    "落到具体的村、具体的人身上，会遇到各种意想不到的阻力。",
    "回望这十四天，我们收获的远比付出的多。感谢一路同行的伙伴，也感谢接待我们的当地群众。"
    "实践不只是走出去看，更是带着问题去想、带着答案回来。",
])


def sample(title: str, account: str, body: str, genre: str = "") -> Sample:
    """造一篇够格当范例的样本。

    真实推送是 800~6400 字、分很多段。夹具要长得像真实数据——之前用"一句话
    重复 N 遍拼成一整行"，结果连着两次被长度门槛和分段规则误伤，每次都要回头
    改夹具而不是改代码，等于测试在测夹具。
    """
    paragraphs = [f"{body}{suffix}" for suffix in
                  ("", "我们希望通过这次活动让更多同学参与进来。",
                   "具体安排会在群内另行通知，请留意查收。",
                   "期待与你在这个夏天相遇，一起把事情做成。")]
    text = "\n".join(paragraphs)
    while len(text) < 400:
        text += "\n" + body
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


class MatchQualityTests(unittest.TestCase):
    """匹配不上就该说没有。这是「宁可说不会，也别自信答错」在检索层的落实。

    这里显式传阈值而不是用默认值：IDF 依赖文档总数，几篇的测试语料算出来的
    分数天然比线上八十多篇低一截。用默认值测，验的是"夹具规模"而不是"机制"。
    """

    def setUp(self):
        self.corpus = WritingCorpus([
            sample("实践招募 | 赴云南大理支教实践支队招募", "清华大学社会实践",
                   "现面向全校招募赴云南大理支教实践支队队员，走进当地中学开展课业辅导与教育资源调研。"),
            sample("志愿者招募 | 暑期校园讲解志愿服务", "清华大学学生公益",
                   "现招募校园讲解志愿者，为进校游客提供讲解服务，每半天提供高温补贴并计入志愿工时。"),
        ])

    def test_unrelated_query_returns_nothing_rather_than_noise(self):
        """线上实测：「赴西部调研的招募文案」拿到 0.18 分的《生命学院迎新志愿者招募》。

        跟西部、调研都没关系，只是碰巧共享了几个常见字组。这种东西被当成范文
        喂给模型，模型会认真地照着一篇无关的文章学语气。
        """
        hits = self.corpus.search("量子计算机的散热方案设计", limit=3, min_score=0.0)
        self.assertEqual(hits, [], f"不相干的查询不该返回范例：{[h.title for h in hits]}")

    def test_related_query_still_matches(self):
        # 门槛不能高到把真正对得上的也挡掉。这里传低阈值：小语料的 IDF 天然低，
        # 用线上那个值测的是夹具规模不是机制。
        hits = self.corpus.search("赴云南支教的招募推送怎么写", genre="招募推送",
                                  limit=2, min_score=0.1)
        self.assertTrue(hits)
        self.assertIn("云南", hits[0].title)

    def test_threshold_actually_filters(self):
        # 把阈值提到高于任何真实分数，应当一条都不返回——验的是门槛真的生效。
        hits = self.corpus.search("赴云南支教的招募推送怎么写", limit=3, min_score=99.0)
        self.assertEqual(hits, [])

    def test_short_generic_question_still_gets_examples_of_that_genre(self):
        """「实践总结怎么写」这类最自然的短问法，不能被分数门槛挡成"没有范例"。

        分数跟查询长度挂钩：短查询字组少、IDF 加权和小。线上实测「实践总结
        怎么写」只有 0.19 分够不着门槛，而库里躺着 15 篇实践总结。为了挡噪音
        把最常见的问法也挡掉，比不挡更糟。

        用户点明了体裁，体裁本身就是强相关信号，这时候按体裁取代表作即可。
        """
        self.assertEqual(self.corpus.search("招募推送怎么写", genre="招募推送"), [],
                         "短查询确实够不着分数门槛（这是前提）")
        reps = self.corpus.representatives("招募推送", limit=2)
        self.assertTrue(reps, "但按体裁取代表作应该拿得到")
        self.assertTrue(all(s.genre == "招募推送" for s in reps))

    def test_representatives_of_an_empty_genre_is_empty(self):
        self.assertEqual(self.corpus.representatives("活动通讯"), [])

    def test_countdown_teaser_is_not_a_writing_sample(self):
        """「还有1天！」这类倒计时海报文没有可学的写法，却占着范例名额。

        实测问「怎么写实践基地介绍」，返回的是《还有1天！2026年清华大学
        "实践基地进清华"交流活动等你来！》和《还有2天！……》。
        """
        for title in ["还有1天！2026年清华大学“实践基地进清华”交流活动等你来！",
                      "还有2天！交流活动等你来", "倒计时3天｜报名通道即将关闭",
                      "就在今天！活动开始"]:
            with self.subTest(title=title):
                self.assertFalse(is_writing_sample(title, REALISTIC_RECAP))

    def test_normal_article_is_still_a_sample(self):
        self.assertTrue(is_writing_sample("实践总结丨十四天的西部记忆", REALISTIC_RECAP))

    def test_flattened_long_article_is_kept(self):
        # 正文被压平成一整行在真实数据里很常见（转发版、采集器压平）。
        # 那只是丢了排版，内容还在，够长就该收——不能因为不分段就丢掉。
        flat = REALISTIC_RECAP.replace("\n", "") + REALISTIC_RECAP.replace("\n", "")
        self.assertGreaterEqual(len(flat), 800)
        self.assertTrue(is_writing_sample("实践总结丨压平的一篇", flat))


class SourceDiversityTests(unittest.TestCase):
    """三篇范例不该全来自同一个号。

    85 篇语料里 66 篇来自「清华大学社会实践」。不打散的话，模型学到的
    "这类文章怎么写"其实是那一个号小编的个人习惯。
    """

    def test_results_spread_across_accounts(self):
        corpus = WritingCorpus([
            sample("实践招募 | 赴云南支教实践支队招募（一）", "清华大学社会实践",
                   "现面向全校招募赴云南支教实践支队队员，开展课业辅导与教育调研。"),
            sample("实践招募 | 赴云南支教实践支队招募（二）", "清华大学社会实践",
                   "现面向全校招募赴云南支教实践支队队员，开展课业辅导与教育调研。"),
            sample("实践招募 | 赴云南支教实践支队招募（三）", "清华大学社会实践",
                   "现面向全校招募赴云南支教实践支队队员，开展课业辅导与教育调研。"),
            sample("志愿招募 | 赴云南支教志愿服务", "清华大学学生公益",
                   "现招募赴云南支教志愿者，开展课业辅导与教育调研服务。"),
        ])
        hits = corpus.search("赴云南支教招募", limit=3, min_score=0.0)
        accounts = {h.account for h in hits}
        self.assertGreaterEqual(len(accounts), 2, f"三篇范例来源太单一：{[h.account for h in hits]}")

    def test_diversify_can_be_turned_off(self):
        corpus = WritingCorpus([
            sample("甲号的支教招募", "甲号", "现面向全校招募赴云南支教实践支队队员，开展课业辅导。"),
            sample("乙号的支教招募", "乙号", "现面向全校招募赴云南支教实践支队队员，开展课业辅导。"),
        ])
        hits = corpus.search("赴云南支教招募", limit=2, min_score=0.0, diversify=False)
        self.assertEqual(len(hits), 2)


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
        long_recap = REALISTIC_RECAP
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
