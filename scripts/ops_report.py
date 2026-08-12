#!/usr/bin/env python3
"""一条命令看清线上到底在发生什么。

    python3 scripts/ops_report.py            # 看最近 24 小时
    python3 scripts/ops_report.py --hours 72
    python3 scripts/ops_report.py --json     # 给脚本用

为什么要有它：今天排查「平台有没有转发请求」时，用 grep 查日志得到空结果就
下了结论——而 grep 因为日志含中文按二进制处理、压根没输出；日志本身也不记
来源 IP，还带着块缓冲，看到的是一小时前的状态。三层遮挡叠在一起，结论就错了。

所以这里的原则是：**每个数字都说清楚是从哪算出来的**，能交叉验证的用两个来源
互相印证，看不到的就明说看不到，不糊弄过去。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DB_PATH = os.getenv("PRACTICE_XIAODA_DB", str(ROOT / "data" / "practice_xiaoda.db"))
ACCESS_LOG = Path("/var/log/practice-xiaoda.log")
HEALTH_LOG = Path("/var/log/practice-xiaoda-health.log")
BACKUP_DIR = Path(os.getenv("BACKUP_DIR", "/opt/backups"))

# 自检请求的特征：固定问句 + max_tokens=8。用于把它和真实用户分开。
PROBE_MAX_TOKENS = 8


def _rows(con, sql, args=()):
    try:
        return con.execute(sql, args).fetchall()
    except sqlite3.Error:
        return []


def collect(hours: int) -> dict:
    since = (datetime.now() - timedelta(hours=hours)).isoformat(timespec="seconds")
    out: dict = {"since": since, "hours": hours, "generated_at": datetime.now().isoformat(timespec="seconds")}

    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)

    # ---- 项目库 ----
    status = dict(_rows(con, "SELECT status, COUNT(*) FROM projects GROUP BY status"))
    projects = [json.loads(r[0]) for r in _rows(con, "SELECT document FROM projects")]
    demo = sum(1 for p in projects if p.get("demo_data"))
    no_link = sum(1 for p in projects if not p.get("source_url"))
    no_deadline = sum(1 for p in projects if not p.get("signup_deadline"))
    uncertain = Counter()
    for p in projects:
        for field in p.get("uncertain_fields", []):
            uncertain[field] += 1
    out["projects"] = {
        "total": len(projects),
        "by_status": status,
        "demo": demo,
        "missing_source_url": no_link,
        "missing_deadline": no_deadline,
        "top_uncertain_fields": uncertain.most_common(5),
        "sources": len({p.get("source_account", "") for p in projects if p.get("source_account")}),
    }

    # ---- 对话 ----
    chats = _rows(
        con, "SELECT metadata, created_at FROM activity WHERE event_type='chat' AND created_at >= ?", (since,)
    )
    intents = Counter()
    unanswered: list[tuple[str, str]] = []
    probes = 0
    for meta, created in chats:
        data = json.loads(meta or "{}")
        if data.get("max_tokens") == PROBE_MAX_TOKENS:
            probes += 1
            continue
        intents[data.get("intent", "?")] += 1
        if data.get("asked"):
            unanswered.append((created[11:19], data["asked"]))
    real = sum(intents.values())
    miss = intents.get("fallback", 0)
    out["chat"] = {
        "real_turns": real,
        "probe_turns_excluded": probes,
        "fallback": miss,
        "fallback_rate": round(miss / real * 100, 1) if real else 0.0,
        "by_intent": intents.most_common(),
        "unanswered_samples": unanswered[-12:],
    }

    # ---- 导入 ----
    out["ingest"] = {
        "articles_total": _rows(con, "SELECT COUNT(*) FROM articles")[0][0] if _rows(con, "SELECT COUNT(*) FROM articles") else 0,
        "recent_events": [
            {"type": t, "message": m[:60], "at": c}
            for t, m, c in _rows(
                con,
                "SELECT event_type, message, created_at FROM activity "
                "WHERE event_type IN ('ingest','wechat_link','lead','collector','seed') "
                "AND created_at >= ? ORDER BY id DESC LIMIT 8",
                (since,),
            )
        ],
    }

    feedback = _rows(con, "SELECT COUNT(*), ROUND(AVG(rating),2) FROM feedback")
    out["feedback"] = {"count": feedback[0][0] if feedback else 0,
                       "avg_rating": feedback[0][1] if feedback else None}
    con.close()

    # ---- 访问日志：区分自检 / 外部 / 扫描器 ----
    # 用 errors="replace" 而不是交给 grep：日志里有中文，grep 会当二进制处理，
    # 返回空输出——今天就是这么误判的。
    access: dict = {"available": ACCESS_LOG.is_file()}
    if access["available"]:
        text = ACCESS_LOG.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        pat = re.compile(r'^\[([^\]]+)\]\s+(\S+)\s+"(\S+)\s+([^"]*?)\s+[^"]*"\s+(\d{3})')
        parsed = [m.groups() for m in (pat.match(l.strip()) for l in lines) if m]
        access["lines_total"] = len(lines)
        access["lines_parsed"] = len(parsed)
        if parsed:
            by_ip = Counter(p[1] for p in parsed)
            access["by_source_ip"] = by_ip.most_common(6)
            external = [p for p in parsed if p[1] not in {"127.0.0.1", "::1", "-"}]
            access["external_requests"] = len(external)
            access["external_sample"] = [f"{p[1]} {p[0]} {p[2]} {p[3]} {p[4]}" for p in external[-5:]]
            bad = [p for p in parsed if p[4] not in {"200", "201", "202", "204"}]
            access["non_2xx"] = len(bad)
            access["non_2xx_paths"] = Counter(p[3] for p in bad).most_common(5)
        else:
            # 老格式不带来源 IP。说清楚而不是假装能分辨。
            access["note"] = "日志行无法解析（可能是加来源 IP 之前的旧格式），无法区分请求来源"
    out["access"] = access

    # ---- 自检与备份 ----
    health: dict = {"available": HEALTH_LOG.is_file()}
    if health["available"]:
        hl = HEALTH_LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-500:]
        health["ok"] = sum(1 for l in hl if l.endswith("OK"))
        health["fail"] = sum(1 for l in hl if " FAIL" in l)
        health["alerts"] = [l for l in hl if "ALERT" in l][-3:]
        health["last"] = hl[-1] if hl else ""
    out["health"] = health

    backups = sorted(BACKUP_DIR.glob("practice_xiaoda-*.db.gz")) if BACKUP_DIR.is_dir() else []
    out["backup"] = {
        "count": len(backups),
        "latest": backups[-1].name if backups else None,
        "latest_size_kb": round(backups[-1].stat().st_size / 1024, 1) if backups else None,
        "latest_age_hours": round(
            (datetime.now() - datetime.fromtimestamp(backups[-1].stat().st_mtime)).total_seconds() / 3600, 1
        ) if backups else None,
    }

    # ---- 服务与模型 ----
    def _cmd(args: list[str]) -> str:
        try:
            return subprocess.run(args, capture_output=True, text=True, timeout=10).stdout.strip()
        except Exception:
            return "?"

    out["service"] = {
        "unit": _cmd(["systemctl", "is-active", "practice-xiaoda"]),
        "health_timer": _cmd(["systemctl", "is-active", "practice-xiaoda-health.timer"]),
        "backup_timer": _cmd(["systemctl", "is-active", "practice-xiaoda-backup.timer"]),
        "disk_used_percent": _cmd(["sh", "-c", "df --output=pcent / | tail -1 | tr -dc '0-9'"]),
    }
    try:
        import llm
        out["writing_model"] = llm.status()
    except Exception:
        out["writing_model"] = {"enabled": False}
    return out


ALERT = "⚠"


def render(d: dict) -> str:
    L: list[str] = []
    a = L.append
    a(f"实践小搭 运行报告   最近 {d['hours']} 小时   {d['generated_at'][:16].replace('T', ' ')}")
    a("=" * 66)

    s = d["service"]
    disk = s.get("disk_used_percent", "?")
    a("")
    a("【服务】")
    a(f"  主服务 {s['unit']}   自检定时器 {s['health_timer']}   备份定时器 {s['backup_timer']}")
    a(f"  磁盘使用 {disk}%" + (f"  {ALERT} 接近写满" if disk.isdigit() and int(disk) >= 85 else ""))
    m = d["writing_model"]
    a(f"  写作模型 {'已启用 ' + m.get('model', '') if m.get('enabled') else '未配置（写推送会降级成要点清单）'}")

    h = d["health"]
    a("")
    a("【自检】")
    if h.get("available"):
        a(f"  最近 500 次：OK {h.get('ok', 0)}   失败 {h.get('fail', 0)}")
        if h.get("alerts"):
            for line in h["alerts"]:
                a(f"  {ALERT} {line}")
        a(f"  最后一次：{h.get('last', '')}")
    else:
        a("  没有自检日志（本机开发环境正常）")

    b = d["backup"]
    a("")
    a("【备份】")
    if b["count"]:
        stale = b["latest_age_hours"] and b["latest_age_hours"] > 30
        a(f"  共 {b['count']} 份   最新 {b['latest']}（{b['latest_size_kb']} KB，{b['latest_age_hours']} 小时前）"
          + (f"  {ALERT} 超过 30 小时没有新备份" if stale else ""))
    else:
        a(f"  {ALERT} 没有任何备份")

    p = d["projects"]
    a("")
    a("【项目库】")
    a(f"  共 {p['total']} 条：" + "，".join(f"{k} {v}" for k, v in p["by_status"].items()))
    a(f"  来源账号 {p['sources']} 个   演示数据 {p['demo']} 条"
      + (f"  {ALERT} 推荐里仍然是演示项目" if p["demo"] and p["demo"] >= p["total"] / 2 else ""))
    a(f"  缺原文链接 {p['missing_source_url']} 条   缺报名截止 {p['missing_deadline']} 条")
    if p["top_uncertain_fields"]:
        a("  最常缺的字段：" + "，".join(f"{k}×{v}" for k, v in p["top_uncertain_fields"]))
    pending = p["by_status"].get("needs_review", 0)
    if pending:
        a(f"  {ALERT} {pending} 条待人工核验——核验前不会进入正式推荐")

    c = d["chat"]
    a("")
    a("【对话】")
    a(f"  真实轮次 {c['real_turns']}（已排除自检 {c['probe_turns_excluded']} 次）")
    if c["real_turns"]:
        a(f"  没接住 {c['fallback']} 次，占 {c['fallback_rate']}%"
          + (f"  {ALERT} 偏高" if c["fallback_rate"] > 15 else ""))
        a("  意图分布：" + "，".join(f"{k} {v}" for k, v in c["by_intent"][:8]))
    if c["unanswered_samples"]:
        a("  最近没接住的问题：")
        for when, asked in c["unanswered_samples"][-6:]:
            a(f"    {when}  {asked}")

    ac = d["access"]
    a("")
    a("【请求来源】")
    if not ac.get("available"):
        a("  没有访问日志")
    elif ac.get("note"):
        a(f"  {ALERT} {ac['note']}")
    else:
        a(f"  可解析 {ac['lines_parsed']}/{ac['lines_total']} 行")
        for ip, n in ac.get("by_source_ip", []):
            tag = "（本机自检）" if ip in {"127.0.0.1", "::1"} else ""
            a(f"    {ip:<18} {n} 次 {tag}")
        a(f"  外部请求 {ac.get('external_requests', 0)} 次   非 2xx {ac.get('non_2xx', 0)} 次")
        if ac.get("non_2xx_paths"):
            a("  被拒的路径（多为公网扫描器）：" + "，".join(f"{p}×{n}" for p, n in ac["non_2xx_paths"]))

    f = d["feedback"]
    a("")
    a("【反馈】")
    a(f"  {f['count']} 条" + (f"，平均 {f['avg_rating']} 分" if f["avg_rating"] else ""))

    ing = d["ingest"]
    if ing["recent_events"]:
        a("")
        a("【最近导入】")
        for e in ing["recent_events"][:5]:
            a(f"  {e['at'][11:16]}  [{e['type']}] {e['message']}")
    return "\n".join(L)


def main() -> int:
    parser = argparse.ArgumentParser(description="线上运行报告")
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    data = collect(args.hours)
    print(json.dumps(data, ensure_ascii=False, indent=2) if args.json else render(data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
