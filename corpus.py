"""写作语料库：把采集来的真实推文当范例检索出来。

用途只有一个——**给写作提供文风和结构参考**。

采集进来的文章里，只有一部分是可报名机会；剩下的实践总结、实践纪实、基地
推介、活动通讯，作为机会是噪音，作为**范文**却正好：它们是真实的、清华语境
的、已经发出去的推送，比任何模板都贴近实际写法。

硬约束，写在最前面：

    范例只提供**怎么写**，不提供**写什么**。

范文里的时间、地点、联系方式、报销标准，绝不能被当成用户项目的事实。生成
材料时的事实一律来自已核验的项目卡（SQLite），这条和整个项目的承诺是一致的：
让模型参与事实判断，就等于把一个会流畅说出错误截止日期的部件放进链路里。

检索用纯标准库实现。中文没有空格分词，这里走二元字组（bigram）交集，配合
逆文档频率降低"实践""同学""我们"这类高频词的权重。对几百篇的规模足够了，
真要上规模再换向量检索不迟。
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterable

# 语料只取正文足够长的——太短的多半是图片型推送或抓取失败，学不到写法。
MIN_SAMPLE_LENGTH = 300
# 匹配门槛。不够格就当作"没有同类范例"——宁可说没有，也不要硬塞一篇不相干的
# 当范文，那会让模型认真地照着一篇无关文章学语气。
#
# 在 82 篇真实语料上量过八个查询，最高分依次是：
#
#     学生骨干招募推送怎么写      0.35   ✓ 该匹配
#     志愿服务纪实怎么写          0.44   ✓
#     支教实践总结怎么写          0.27   ✓
#     秋季招新的推送有什么特点     0.27   ✓
#     量子计算机的散热方案设计     0.23   ✗ 不该匹配
#     怎么写实践基地介绍          0.22   ✗（语料里说"推介"、用户说"介绍"，词对不上）
#     赴西部调研的招募文案        0.18   ✗
#     食堂哪个窗口好吃            0.15   ✗
#
# 0.25 一刀切下去，八个全对。中途试过再加一个"查询覆盖率"判据，实测发现它
# 既不增加区分力（0.36 的覆盖率里好坏各一半），又会误伤——去掉了。
#
# 两条局限说在前面：一是这是八个样本调出来的，0.27 和 0.23 之间只差 0.04，
# 边界很薄；二是 IDF 依赖文档总数，这个值只对八十多篇的规模成立，语料涨到
# 几百篇要重新量。好在失败方向是安全的：拿不准时判为"没有范例"。
MIN_MATCH_SCORE = 0.25
# 给模型的范文长度上限。太长会挤掉真正的事实，也会让模型倾向于整段照抄。
MAX_EXCERPT_CHARS = 1100
# 中文停用字组：出现在大多数文章里，对区分主题没有帮助。
_NOISE = re.compile(r"[\s，。、；：！？（）()【】「」“”‘’\-—…·\|｜丨/\\]+")


# 微信页面模板噪音。抓下来的正文开头常带这些，既占范文额度，又会让模型
# 学到一些不属于文章的东西。实测线上语料里"在小说阅读器读本章""去阅读"
# 出现在多篇文章的头几行。
_BOILERPLATE = (
    "在小说阅读器读本章", "去阅读", "在小说阅读器中沉浸阅读",
    "点击上方蓝字", "点击蓝字", "关注我们", "星标我们", "长按识别二维码关注",
    "预览时标签不可点", "微信扫一扫关注该公众号", "轻点两下取消赞",
    "继续滑动看下一个", "向上滑动看下一个",
)


def clean_body(text: str, title: str = "") -> str:
    """去掉页面模板噪音和开头重复的标题行。

    公众号正文抓下来常常前几行就是标题重复两三遍，再跟一段阅读器提示。
    留着它们没有害处但很占地方——范文额度本来就只有一千出头字。
    """
    lines: list[str] = []
    stripped_title = title.strip()
    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            continue
        if any(noise in line for noise in _BOILERPLATE):
            continue
        # 开头几行里重复出现的标题跳过；正文中间再出现就保留（可能是小标题）。
        if stripped_title and line == stripped_title and len(lines) < 4:
            continue
        lines.append(line)
    return "\n".join(lines)


@dataclass
class Sample:
    """一篇可作为范例的真实推文。"""

    title: str
    account: str
    text: str
    genre: str
    score: float = 0.0

    def excerpt(self, limit: int = MAX_EXCERPT_CHARS) -> str:
        body = clean_body(self.text, self.title)
        return body if len(body) <= limit else body[:limit] + "……（原文更长，此处截断）"


# 体裁标签。写不同东西要看不同范文：写招募推送该学招募推送，写总结该学总结。
GENRE_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("招募推送", ("招募", "报名", "招新", "纳新", "选拔")),
    ("实践总结", ("总结", "纪实", "回顾", "侧记", "心得", "感悟", "收获")),
    ("基地/资源介绍", ("基地", "资源推介", "推介", "资源介绍")),
    ("活动通讯", ("举行", "举办", "顺利", "圆满", "开幕", "启动仪式")),
    ("课程/通知", ("说明", "通知", "选课", "培训", "课程")),
)


# 不适合当写作范例的体裁。它们进语料没有害处（原文该存还是要存），但作为
# "照着这个学怎么写"的样本没有价值，还会挤掉真正的范文。
#
# 实测：问「怎么写实践基地介绍」，返回的是《还有1天！2026年清华大学"实践基地
# 进清华"交流活动等你来！》和《还有2天！……》——倒计时海报文，通篇就是一句
# 吆喝加一张图，学不到任何东西。
_NOT_A_WRITING_SAMPLE = (
    "还有1天", "还有2天", "还有3天", "还有一天", "还有两天", "倒计时",
    "就在今天", "就在明天", "最后一天", "今晚", "明天见",
)


def is_writing_sample(title: str, text: str) -> bool:
    """这篇够不够格当写作范例。"""
    body = text.strip()
    if len(body) < MIN_SAMPLE_LENGTH:
        return False
    if any(word in title for word in _NOT_A_WRITING_SAMPLE):
        return False
    # 又短、又不分段的，基本是一句吆喝配张图。
    #
    # 这里刻意不要求"必须分段"：正文被压平成一整行在真实数据里很常见——
    # 转发版会丢排版，采集器压平也会。那种只是没了段落，内容还在，够长就该收。
    substantial_lines = [line for line in body.split("\n") if len(line.strip()) >= 15]
    return len(substantial_lines) >= 2 or len(body) >= 800


def detect_genre(title: str, text: str = "") -> str:
    """判断一篇文章属于哪种体裁。标题优先——标题基本就定了性。"""
    for genre, words in GENRE_PATTERNS:
        if any(word in title for word in words):
            return genre
    head = text[:200]
    for genre, words in GENRE_PATTERNS:
        if any(word in head for word in words):
            return genre
    return "其他"


def _bigrams(text: str) -> Counter:
    """中文二元字组。没有分词器也能算出像样的相似度。"""
    cleaned = _NOISE.sub("", text)
    return Counter(cleaned[index:index + 2] for index in range(len(cleaned) - 1))


def _spread_sources(ranked: list[Sample], limit: int) -> list[Sample]:
    """按分数取前几篇，但每个公众号先只取一篇，不够再回头补。

    语料来源很不均：85 篇里 66 篇来自「清华大学社会实践」。不打散的话，
    给模型的三篇范例大概率全是同一个号，学到的"这类文章怎么写"其实是
    那个号小编的个人习惯。
    """
    picked: list[Sample] = []
    seen: set[str] = set()
    for sample in ranked:
        if len(picked) >= limit:
            return picked
        if sample.account not in seen:
            picked.append(sample)
            seen.add(sample.account)
    for sample in ranked:  # 来源不够多时，按分数补齐名额
        if len(picked) >= limit:
            break
        if sample not in picked:
            picked.append(sample)
    return picked


class WritingCorpus:
    """真实推文的检索器。构造时算一次 IDF，之后每次检索都是纯内存匹配。"""

    def __init__(self, samples: Iterable[Sample]) -> None:
        self.samples: list[Sample] = [s for s in samples if is_writing_sample(s.title, s.text)]
        self._grams: list[Counter] = [_bigrams(f"{s.title}\n{s.text}") for s in self.samples]
        # 逆文档频率：一个字组出现在越多文章里，越说明它是套话。
        document_frequency: Counter = Counter()
        for grams in self._grams:
            document_frequency.update(grams.keys())
        total = max(1, len(self.samples))
        self._idf = {gram: math.log(total / (1 + count)) + 1.0
                     for gram, count in document_frequency.items()}

    def __len__(self) -> int:
        return len(self.samples)

    def genres(self) -> dict[str, int]:
        return dict(Counter(s.genre for s in self.samples))

    def representatives(self, genre: str, *, limit: int = 3) -> list[Sample]:
        """某个体裁的代表作：够长、来源尽量分散。

        用户问「实践总结怎么写」时，体裁本身就是足够强的相关信号——他要的就是
        这一类怎么写，给几篇这一类的好样本完全对路，不需要主题也对得上。

        这条路是被实测逼出来的：分数跟查询长度挂钩，短查询的字组少、加权和小，
        「实践总结怎么写」只有 0.19 分，被门槛挡成"没有范例"——而库里躺着
        15 篇实践总结。为了挡噪音把最常见的问法也挡掉，比不挡更糟。
        """
        pool = [s for s in self.samples if s.genre == genre]
        # 长文的结构更完整，当范例更有得学。
        pool.sort(key=lambda s: len(s.text), reverse=True)
        for sample in pool:
            sample.score = 0.0
        return _spread_sources(pool, limit)

    def search(self, query: str, *, genre: str = "", limit: int = 3,
               min_score: float = MIN_MATCH_SCORE, diversify: bool = True) -> list[Sample]:
        """按主题找最像的几篇。genre 非空时只在该体裁内找。

        分数不够就不返回——匹配不上就该说没有。调用方拿到空列表时会明确告诉
        用户"库里还没有同类范例"，而不是照着一篇不相干的文章瞎学。阈值可以
        显式传入：默认值按线上语料规模定，换个规模要重新量（见常量注释）。

        diversify 让结果尽量来自不同公众号：线上 82 篇里 66 篇来自同一个号，
        不打散的话前三篇很容易全是它，学到的"文风"其实是那个号的习惯。
        """
        query_grams = _bigrams(query)
        if not query_grams:
            return []
        results: list[Sample] = []
        for sample, grams in zip(self.samples, self._grams):
            if genre and sample.genre != genre:
                continue
            shared = query_grams.keys() & grams.keys()
            if not shared:
                continue
            # 用 IDF 加权的交集，再按长度归一，避免长文章仅凭字数取胜。
            weight = sum(self._idf.get(gram, 1.0) * min(query_grams[gram], grams[gram]) for gram in shared)
            sample.score = weight / math.sqrt(sum(grams.values()) or 1)
            if sample.score >= min_score:
                results.append(sample)
        results.sort(key=lambda s: s.score, reverse=True)
        return _spread_sources(results, limit) if diversify else results[:limit]


def load_corpus(database: Any) -> WritingCorpus:
    """从 articles 表构建语料库。每个链接只取最后一版原文。"""
    latest: dict[str, Sample] = {}
    with database.connect() as db:
        rows = db.execute(
            "SELECT source_url, source_account, title, raw_text FROM articles "
            "WHERE raw_text IS NOT NULL AND raw_text != '' ORDER BY id"
        ).fetchall()
    for row in rows:
        title = (row["title"] or "").strip()
        text = (row["raw_text"] or "").strip()
        if not text:
            continue
        key = row["source_url"] or f"__notitle__{title}"
        latest[key] = Sample(
            title=title or "(无标题)",
            account=(row["source_account"] or "").strip(),
            text=text,
            genre=detect_genre(title, text),
        )
    return WritingCorpus(latest.values())


def build_reference_block(samples: list[Sample]) -> str:
    """把范文拼成给模型看的一段，并把边界说死。

    这段话必须留在提示词里：模型看到具体日期和联系方式时，很自然会顺手用进
    正文——而那些是别人项目的事实。
    """
    if not samples:
        return ""
    parts = [
        "以下是同类真实推文，**仅供参考写法、结构和语气**。",
        "严禁照抄其中任何具体信息：日期、地点、人名、联系方式、报销标准、名额，",
        "这些都属于别的项目，写进正文就是编造。",
        "",
    ]
    for index, sample in enumerate(samples, 1):
        source = f"{sample.account}·{sample.genre}" if sample.account else sample.genre
        parts.append(f"【范例 {index}｜{source}】{sample.title}")
        parts.append(sample.excerpt())
        parts.append("")
    return "\n".join(parts)
