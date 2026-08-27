"""Curated default WeChat accounts for social-practice lead discovery.

The department list is sourced from THU Book's "各院系官方公众号"
directory. Department and residential-college accounts are intentionally kept
even when they are not practice-specific: many opportunities are announced by
the organizer's home department before they reach school-wide channels.
"""

from __future__ import annotations


SOURCE_DIRECTORY_URL = "https://yourschool.cc.cd/thubook/gzh.html#各院系官方公众号"
SOURCE_DIRECTORY_CHECKED_AT = "2026-08-12"

SCHOOLWIDE_PRACTICE_ACCOUNTS = (
    "清华大学",
    "清华紫荆之声",
    "清华大学学生会",
    "清华大学社会实践",
    "清华大学学生公益",
    "清华大学学生社团",
)

DEPARTMENT_ACCOUNTS = (
    "建院宣传中心",
    "建院学生会THU",
    "水木华声",
    "水利宣传",
    "环境人ENV",
    "环小研",
    "机械之声",
    "机械正发声",
    "精小仪",
    "精仪系研究生",
    "清能动力",
    "源力酱",
    "车辆人",
    "新车匠",
    "微爱意",
    "IE小研",
    "电机之声",
    "无限之声",
    "酒井资讯",
    "紫冬话语",
    "软小宣",
    "芯系清华",
    "力小翼",
    "天工物华",
    "卡安",
    "象图学院",
    "数无穹",
    "清物语",
    "清化宣传",
    "莱福",
    "清心地学",
    "茶园资讯",
    "清华经管家园",
    "公管声音",
    "吾道清年",
    "Lawgic",
    "清华清小新",
    "清马来了",
    "文心载道",
    "社氏有声",
    "清美团宣",
    "核研人",
    "清医色",
    "网研之家",
    "雅人新致",
    "THU求真寻理",
    "THU为先",
    "THU水木秀钟",
    "强基致理想",
    "THU长乐未央",
    "探微观止",
    "THU天行健",
    "THU清清园中葵",
    "THU臻于至善",
    "THU笃实光辉",
    "清华大学无穹书院",
    "清华大学水木书院",
    "THU水清木华",
    "清华大学自强书院",
    "自强eEnery",
    "清华大学紫荆书院",
)

DEFAULT_ACCOUNTS = tuple(dict.fromkeys(SCHOOLWIDE_PRACTICE_ACCOUNTS + DEPARTMENT_ACCOUNTS))
MAX_ACCOUNTS = 100

# The public-feed worker polls only high-value sources selected for daily
# opportunity discovery. This stays separate from the broader developer-panel
# catalog, where adding an account should not silently increase daily load.
DAILY_PRIORITY_ACCOUNTS = (
    "清华大学学生会",
    "清华大学学生社团",
    "清华紫荆之声",
    "清华大学学生公益",
    "清华大学乡村振兴工作站",
)

# Used only to migrate installations that still have the original untouched
# defaults. Any other saved list is treated as an explicit developer choice.
LEGACY_DEFAULT_ACCOUNTS = ("清华大学社会实践", "无限之声", "清华大学学生公益")
