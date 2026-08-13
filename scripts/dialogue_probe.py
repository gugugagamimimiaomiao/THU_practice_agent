#!/usr/bin/env python3
"""对线上服务跑一轮黑箱对话测试，输出可复核的报告。

    python3 scripts/dialogue_probe.py                      # 打本机 8000
    python3 scripts/dialogue_probe.py --base http://8.217.145.109:8000/v1
    python3 scripts/dialogue_probe.py --out reports/probe.md

密钥从 XIAODA_API_KEY 读，不要写进命令行——命令行参数会进 shell 历史。

为什么要有它：单测覆盖的是函数，覆盖不了"一个真人连着问十句会怎样"。
之前靠人工在浏览器里点，一轮下来两小时，还记不全。这个脚本把问题集固定
下来，每次改完跑一遍，能直接比较前后的兜底率和响应时间。

它只做**观察和记录**，不做断言——对话质量没法用等号判断。报告里会把
可疑的回答挑出来（空回复、异常慢、疑似编造、答非所问），由人来看。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parent.parent

# 十类问法。每一类都对应产品的一个承诺，凑不满 100 条也不硬凑——
# 与其灌水，不如每条都真的在测一件事。
SINGLE_TURN: list[tuple[str, str]] = [
    # —— 推荐：最核心的路径 ——
    ("推荐", "我大三，九月有空，推荐一些志愿或者实践"),
    ("推荐", "有什么实践可以报名"),
    ("推荐", "我想找支教类的实践"),
    ("推荐", "推荐几个乡村振兴方向的"),
    ("推荐", "我是研一，想找调研类的项目"),
    ("推荐", "有没有适合大一新生的"),
    ("推荐", "国庆假期能参加的实践有吗"),
    ("推荐", "我时间不多，有没有一周以内的"),
    ("推荐", "想去西部，有合适的吗"),
    ("推荐", "计算机系的能报哪些"),
    # —— 按条件筛选：用户很自然的问法 ——
    ("筛选", "有没有校内的志愿服务"),
    ("筛选", "只看志愿服务，不要实践支队"),
    ("筛选", "有北京本地的吗"),
    ("筛选", "报销交通费的有哪些"),
    ("筛选", "还在报名中的有几个"),
    ("筛选", "有没有不限院系的"),
    ("筛选", "秋季学期的招新有吗"),
    ("筛选", "线上就能参加的"),
    # —— 列表与总量 ——
    ("列表", "一共有多少个项目"),
    ("列表", "把所有项目列出来"),
    ("列表", "还有哪些实践机会"),
    ("列表", "最近新增了什么"),
    # —— 详情与证据 ——
    ("详情", "宝庆微光"),
    ("详情", "筑梦建行"),
    ("详情", "志愿中心组长"),
    ("详情", "黔心守艺是什么"),
    ("详情", "助梦1+1的报名截止是什么时候"),
    ("详情", "推普强国行的经费怎么算"),
    # —— 来源与可信度：这是产品卖点 ——
    ("来源", "这些信息是从哪来的"),
    ("来源", "你的数据准吗"),
    ("来源", "能看到原文吗"),
    ("来源", "多久更新一次"),
    ("来源", "你会不会编"),
    # —— 比较 ——
    ("比较", "比较前两个推荐项目"),
    ("比较", "宝庆微光和筑梦建行哪个好"),
    ("比较", "这两个有什么区别"),
    # —— 生成材料：必须绑定项目 ——
    ("生成", "帮我写宝庆微光的报名理由"),
    ("生成", "给筑梦建行写一个外联话术"),
    ("生成", "写一份访谈提纲"),
    ("生成", "帮我写推送文案"),
    ("生成", "调研报告框架"),
    # —— 通用写作问答：新做的语料库能力 ——
    ("写作指导", "实践招募推送一般怎么写"),
    ("写作指导", "实践总结报告的结构是什么"),
    ("写作指导", "招募推送文案有什么套路"),
    ("写作指导", "写实践总结要注意什么"),
    ("写作指导", "推送开头怎么写才吸引人"),
    ("写作指导", "有没有范文可以参考"),
    ("写作指导", "志愿服务招募和实践招募写法有什么不同"),
    # —— 关于实践本身的常识 ——
    ("常识", "参加社会实践对保研有用吗"),
    ("常识", "第一次参加要注意什么"),
    ("常识", "支队是怎么组建的"),
    ("常识", "实践能算学分吗"),
    ("常识", "支队长要做什么"),
    # —— 导入 ——
    ("导入", "导入这则通知：赴甘肃敦煌文化遗产数字化调研支队招募。实践地点：甘肃省敦煌市。报名截止：2036年9月30日。参与资格：全校本科生、研究生均可报名。报名方式：填写问卷。"),
    ("导入", "https://mp.weixin.qq.com/s/probe-invalid-link-test"),
    # —— 越界与边界：宁可说不会，也别自信答错 ——
    ("越界", "今天天气怎么样"),
    ("越界", "帮我写一首诗"),
    ("越界", "1+1等于几"),
    ("越界", "你是什么模型"),
    ("越界", "帮我规划一下去大理的路线"),
    ("越界", "清华的食堂哪个好吃"),
    ("边界", ""),
    ("边界", "？"),
    ("边界", "啊"),
    ("边界", "。" * 200),
    ("边界", "推荐" * 100),
    ("边界", "<script>alert(1)</script>"),
    ("边界", "SELECT * FROM projects;--"),
    ("边界", "你好👋🏻🎉"),
    # —— 否定与纠正 ——
    ("纠正", "不是这个"),
    ("纠正", "我说的不是支教"),
    ("纠正", "换一批"),
    ("纠正", "这些都不合适"),
    # —— 决策求助 ——
    ("决策", "帮我参谋一下报哪个"),
    ("决策", "哪个更容易被选上"),
    ("决策", "我该怎么选"),
]

# 多轮：单轮测不出上下文是否接得住。
MULTI_TURN: list[tuple[str, list[str]]] = [
    ("多轮·推荐后追问", ["我大三，九月有空，推荐实践", "第一个的报销怎么样", "那报名截止呢", "帮我写它的报名理由"]),
    ("多轮·详情后比较", ["宝庆微光", "和筑梦建行比一下", "哪个时间短"]),
    ("多轮·纠正后重推", ["推荐实践", "不要支教的", "有没有调研类的"]),
    ("多轮·写作转成稿", ["实践招募推送一般怎么写", "那帮我给宝庆微光写一篇"]),
    ("多轮·越界后回正轨", ["今天天气怎么样", "算了，推荐几个实践吧"]),
    ("多轮·连续追问来源", ["有什么实践可以报名", "这些是真的吗", "给我看原文链接"]),
    ("多轮·空输入夹在中间", ["推荐实践", "", "继续"]),
]

SUSPICIOUS_PATTERNS = [
    (re.compile(r"作为(一个)?(AI|人工智能|大语言模型|语言模型)"), "自称 AI 助手，跳出了产品设定"),
    (re.compile(r"(抱歉|对不起)[，,]?\s*(我)?(无法|不能|没有办法)"), "生硬拒绝"),
    (re.compile(r"(根据我的训练数据|截至我的知识)"), "暴露模型来历"),
    (re.compile(r"演示数据|demo_data|演示项目"), "回复里出现演示数据"),
    (re.compile(r"(https?://\S*example\.(com|invalid))"), "出现占位链接"),
]


def ask(opener, base: str, key: str, messages: list[dict], timeout: int) -> tuple[str, float, str]:
    """返回 (回复文本, 耗时秒, 错误说明)。"""
    body = json.dumps({"model": "practice-xiaoda", "messages": messages}, ensure_ascii=False).encode("utf-8")
    request = Request(f"{base}/chat/completions", data=body, headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json; charset=utf-8"})
    started = time.monotonic()
    try:
        with opener.open(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return payload["choices"][0]["message"]["content"], time.monotonic() - started, ""
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:160]
        return "", time.monotonic() - started, f"HTTP {exc.code}: {detail}"
    except (URLError, TimeoutError, json.JSONDecodeError, KeyError) as exc:
        return "", time.monotonic() - started, f"{type(exc).__name__}: {exc}"


def inspect(reply: str) -> list[str]:
    """挑出可疑之处。只提示，不下判断——对话质量得人看。"""
    notes: list[str] = []
    if not reply.strip():
        notes.append("空回复")
        return notes
    for pattern, label in SUSPICIOUS_PATTERNS:
        if pattern.search(reply):
            notes.append(label)
    if len(reply) < 20:
        notes.append(f"回复过短（{len(reply)} 字）")
    if "待确认" in reply and reply.count("待确认") >= 6:
        notes.append(f"「待确认」出现 {reply.count('待确认')} 次，信息密度太低")
    return notes


def main() -> int:
    parser = argparse.ArgumentParser(description="黑箱对话测试")
    parser.add_argument("--base", default="http://127.0.0.1:8000/v1")
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--pause", type=float, default=0.2, help="每轮之间停顿，别把线上服务刷满")
    parser.add_argument("--out", type=Path, default=ROOT / "reports" / "dialogue_probe.md")
    args = parser.parse_args()

    key = os.getenv("XIAODA_API_KEY", "").strip()
    if not key:
        print("需要环境变量 XIAODA_API_KEY", file=sys.stderr)
        return 2

    opener = build_opener(ProxyHandler({}))  # 绕开本机代理，它会劫持裸 IP
    records: list[dict] = []
    turn = 0

    for category, question in SINGLE_TURN:
        turn += 1
        reply, elapsed, error = ask(opener, args.base, key, [{"role": "user", "content": question}], args.timeout)
        records.append({"turn": turn, "category": category, "question": question,
                        "reply": reply, "seconds": round(elapsed, 2), "error": error,
                        "notes": inspect(reply) if not error else ["请求失败"]})
        print(f"[{turn:>3}] {category:<8} {elapsed:5.2f}s  {question[:36]}")
        time.sleep(args.pause)

    for label, questions in MULTI_TURN:
        history: list[dict] = []
        for index, question in enumerate(questions, 1):
            turn += 1
            history.append({"role": "user", "content": question})
            reply, elapsed, error = ask(opener, args.base, key, history, args.timeout)
            history.append({"role": "assistant", "content": reply})
            records.append({"turn": turn, "category": f"{label}({index})", "question": question,
                            "reply": reply, "seconds": round(elapsed, 2), "error": error,
                            "notes": inspect(reply) if not error else ["请求失败"]})
            print(f"[{turn:>3}] {label[:12]:<14} {elapsed:5.2f}s  {question[:30]}")
            time.sleep(args.pause)

    write_report(args.out, records, args.base)
    failed = [r for r in records if r["error"]]
    flagged = [r for r in records if r["notes"] and not r["error"]]
    times = sorted(r["seconds"] for r in records if not r["error"])
    print()
    print(f"共 {len(records)} 轮，失败 {len(failed)}，有可疑之处 {len(flagged)}")
    if times:
        print(f"耗时 中位 {times[len(times)//2]:.2f}s，最慢 {times[-1]:.2f}s")
    print(f"报告：{args.out}")
    return 1 if failed else 0


def write_report(path: Path, records: list[dict], base: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    failed = [r for r in records if r["error"]]
    flagged = [r for r in records if r["notes"] and not r["error"]]
    times = sorted(r["seconds"] for r in records if not r["error"])

    lines = [
        f"# 黑箱对话测试 {datetime.now():%Y-%m-%d %H:%M}",
        "",
        f"目标：`{base}`　共 {len(records)} 轮",
        "",
        "这份报告只做观察记录，不做通过/不通过的判定——对话质量没法用等号判断。",
        "下面「需要人看」的部分是脚本挑出来的可疑项，请逐条确认是不是真问题。",
        "",
        "## 概况",
        "",
        f"- 请求失败：{len(failed)}",
        f"- 有可疑之处：{len(flagged)}",
    ]
    if times:
        lines += [
            f"- 耗时：中位 {times[len(times)//2]:.2f}s，P90 {times[int(len(times)*0.9)]:.2f}s，最慢 {times[-1]:.2f}s",
        ]
    lines += ["", "### 按类别", "", "| 类别 | 轮次 | 失败 | 可疑 | 中位耗时 |", "|---|---:|---:|---:|---:|"]
    by_category: dict[str, list[dict]] = {}
    for record in records:
        by_category.setdefault(record["category"].split("(")[0], []).append(record)
    for category, items in by_category.items():
        ok_times = sorted(i["seconds"] for i in items if not i["error"])
        median = f"{ok_times[len(ok_times)//2]:.2f}s" if ok_times else "-"
        lines.append(f"| {category} | {len(items)} | {sum(1 for i in items if i['error'])} | "
                     f"{sum(1 for i in items if i['notes'] and not i['error'])} | {median} |")

    if failed:
        lines += ["", "## 请求失败", ""]
        for record in failed:
            lines += [f"- **第 {record['turn']} 轮**（{record['category']}）「{record['question'][:50]}」",
                      f"  - {record['error']}"]

    if flagged:
        lines += ["", "## 需要人看", ""]
        for record in flagged:
            lines += [f"### 第 {record['turn']} 轮 · {record['category']}",
                      "",
                      f"**问**：{record['question'][:120]}",
                      "",
                      f"**疑点**：{'；'.join(record['notes'])}",
                      "",
                      "**答**：",
                      "",
                      "```",
                      record["reply"][:900] or "(空)",
                      "```",
                      ""]

    lines += ["", "## 全部记录", ""]
    for record in records:
        lines += [f"### 第 {record['turn']} 轮 · {record['category']} · {record['seconds']}s",
                  "",
                  f"**问**：{record['question'][:200] or '(空输入)'}",
                  "",
                  "```",
                  (record["reply"][:1200] or record["error"] or "(空)"),
                  "```",
                  ""]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
