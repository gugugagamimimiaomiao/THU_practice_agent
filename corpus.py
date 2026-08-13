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


class WritingCorpus:
    """真实推文的检索器。构造时算一次 IDF，之后每次检索都是纯内存匹配。"""

    def __init__(self, samples: Iterable[Sample]) -> None:
        self.samples: list[Sample] = [s for s in samples if len(s.text.strip()) >= MIN_SAMPLE_LENGTH]
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

    def search(self, query: str, *, genre: str = "", limit: int = 3) -> list[Sample]:
        """按主题找最像的几篇。genre 非空时只在该体裁内找。"""
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
            results.append(sample)
        results.sort(key=lambda s: s.score, reverse=True)
        return results[:limit]


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
