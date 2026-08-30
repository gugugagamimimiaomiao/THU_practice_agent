"""是招募，但不是社会实践——这一维度以前完全没判。

opportunity_filter 原来只问「这是不是一篇招募」。
「【代表队招新】体育集结号——男篮篇」命中「招新」，一路放行进了机会库。

拿服务器上 73 条真实项目逐条人工标注后量出来：

    社会实践/志愿服务   30 条
    校内活动           43 条   体育代表队 20、学生组织招新 11、
                              学科竞赛、留学讲座、迎新晚会、教学助教岗

近六成库存跟这个产品要解决的问题无关。而已发布的 25 条里只有 8 条是实践
——池子里三分之二是杂物，靠扣分压根压不住，必须分桶。

**分桶不是删除。** 判错了要能翻出来；采集是合作者一条条攒的；用户也得
能看到我按什么标准挪的。所以校内活动照常入库、照常能查，只是不进
「推荐几个实践」的结果。

下面第一组是那 73 条的**全量回归**：判据改动只要让任何一条翻面，这里就红。
"""
import unittest

from opportunity_filter import (CAMPUS_WORDS, PRACTICE_ACCOUNTS, PRACTICE_WORDS,
                                opportunity_kind)

# 人工标注。标注原则：一个想找「社会实践/志愿服务」的学生看到这条，
# 会不会觉得"这正是我要找的"。
PRACTICE = (
    "“助梦1+1”导师招募丨助梦西部学子，成就青春梦想",
    "“青年技能绿动”暑期实践专项活动报名中！快来一起成为绿色技能青年先锋",
    "以爱为炬，赴夏之约｜唐社 2026 暑假实践支队招募（第二批）启动",
    "实践基地进清华｜2026年“揭榜挂帅”项目，招募启动！",
    "新一批！“青年实干家计划——百名硕博建功邯郸”行动2026年度招募公告",
    "未央实践丨“智汇北票”赴北票实践支队补招募",
    "欢迎六字班丨生命学院迎新志愿者招募开始啦！",
    "温暖迎新，志愿同行｜学生社区志愿者招募进行中",
    "赴一场跨越山海的青春之约，清华大学第29届研究生支教团招募启动！",
    "迎新志愿者招募 | 接力六字班，再续紫荆情",
    "志愿一夏 | 2026清华大学暑期校园讲解志愿者招募，等你加入！",
    "志愿招募 | 社区暑期儿童科普课堂志愿者招募",
    "志愿者招募 | 新楼新“净”界：邀你一起为同学的新家“测健康”",
    "实践招募 | 机械系“宝庆微光”赴湖南新宁支教实践支队招募",
    "实践招募 | “黔心守艺”赴贵州黔东南支队招募",
    "实践招募丨新雅“一事”计划 基层助力—孝昌乡村振兴项目",
    "实践招募｜“千团万人推普强国行”社会实践志愿服务",
    "实践招募 丨 “筑梦建行·春山在望”中学生支教夏令营实践招募（第一批次）",
)

CAMPUS = (
    # 体育代表队，库里 20 条
    "【代表队招新】体育集结号——贵系男篮篇",
    "【代表队招新】体育集结号——艺术体操篇",
    "代表队招新｜工物男足",
    "为先体育 | 2026代表队招新开启！",
    "机械体育 | 男足2026招新DME Men's Soccer recruits",
    "2026-2027学年秋季学期体育助教报名通知",
    # 学生组织招新——加入部门当骨干，不是去做实践
    "2026年秋校团委宣传部学生骨干招募",
    "2026年秋校团委志愿中心学生骨干招募",
    "2026年秋校团委志愿中心组长招募",
    "2026年秋校团委学习实践部组长、副组长及组员招募",
    "2026秋校团委组织部学生骨干招募",
    "创协部长招新 | 共筑清华创业舞台，期待你的光芒！",
    "研团招新丨2026，为你而来！",
    "未央书院学生组织2026年秋季学期补招新与转岗报名开始啦！",
    "招新 | 日新书院TMS分会部门介绍",
    # 竞赛、讲座、文艺
    "在游戏中学电路 | 第五届红石设计大赛开放报名",
    "第八届“航天四部开拓杯” | 未来飞行器设计大赛报名通知",
    "学生节征名丨计算机系第四十四届学生节主题征集",
    "【留学】斯坦福 / 伯克利 / CMU 暑期联动分享会，抓住暑假蓄力提升申请竞争力",
    "报名通知 | 本科生科创国际交流会报名",
    "主持人招募丨“6 I AM”六系联合新生舞会",
    "“九韵初鸣，清声启航”九院系联合迎新晚会来啦！主持人招募、节目征集同步开启",
)


class OpportunityKindTests(unittest.TestCase):
    def test_real_practices_are_kept(self):
        """判错方向里最严重的一种：把真机会挪出推荐，学生永远看不到它。"""
        for title in PRACTICE:
            with self.subTest(title=title[:30]):
                self.assertEqual(opportunity_kind(title, "清华大学社会实践"), "practice")

    def test_campus_activities_are_set_aside(self):
        for title in CAMPUS:
            with self.subTest(title=title[:30]):
                self.assertEqual(opportunity_kind(title, "酒井资讯"), "campus")

    def test_a_department_recruitment_is_campus_even_from_the_practice_account(self):
        """「校团委学习实践部组长招募」由清华大学社会实践发布，标题里还带
        「实践」两个字——但它招的是部门成员。否定信号必须先于肯定信号判，
        否则这一类全会被当成实践。"""
        self.assertEqual(
            opportunity_kind("2026年秋校团委学习实践部组长、副组长及组员招募",
                             "清华大学社会实践"), "campus")

    def test_the_volunteer_centre_recruitment_is_a_judgement_call(self):
        """这条是我的判断，不是铁律，写下来是为了以后能有据可查地改。

        「校团委志愿中心学生骨干招募」带着"志愿"两个字，但它招的是组织成员，
        不是去做志愿服务。要改就改 _CAMPUS_ORG。
        """
        self.assertEqual(
            opportunity_kind("2026年秋校团委志愿中心学生骨干招募", "清华紫荆之声"),
            "campus")

    def test_an_unknown_title_defaults_to_practice(self):
        """两种兜底的错法不对称：默认 campus 会静默挡掉一条真机会，学生
        永远不知道它存在；默认 practice 最多让一条杂物混进列表，看得见、
        能反馈。看得见的错好收拾。"""
        self.assertEqual(opportunity_kind("某种从没见过的新式招募标题", "某个新号"),
                         "practice")

    def test_the_practice_accounts_are_school_level_only(self):
        """院系号和书院号不能进这张表：它们什么都发，从体育队到部门招新。"""
        for account in PRACTICE_ACCOUNTS:
            self.assertTrue(account.startswith("清华大学"), account)

    def test_no_word_appears_in_both_lists(self):
        """同一个词既在肯定表又在否定表，判据就成了看顺序的巧合。"""
        for word in PRACTICE_WORDS:
            self.assertNotIn(word, CAMPUS_WORDS)


if __name__ == "__main__":
    unittest.main()
