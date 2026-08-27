"""SQLite persistence for 实践小搭."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterator

from domain import deep_merge, json_dumps, now_iso, refresh_status, validate_project


ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "data" / "practice_xiaoda.db"
SEED_FILE = ROOT / "seed_data.json"

# 演示数据里的日期是围绕这一天写的：截止日在其后几天到两周，实践期在其后三到五周。
# 载入时会把所有日期整体平移 (今天 - 锚点) 天，这样无论哪天首次启动，
# 演示库里都有处于"报名中"的 published 项目，而"历史项目"仍然保持过期。
# seed_data.json 里可以用 anchor_date 覆盖这个默认值。
SEED_ANCHOR_DATE = date(2026, 7, 15)

# 需要平移的日期写法：ISO 日期（可带时间后缀）、带年份的中文日期、不带年份的中文日期。
# 顺序有意义——带年份的中文写法必须排在不带年份的前面，否则会被后者先吃掉。
_SEED_DATE_RE = re.compile(
    r"(?P<iso>\d{4}-\d{2}-\d{2})"
    r"|(?P<cn_full>(?P<cy>\d{4})年(?P<cm>\d{1,2})月(?P<cd>\d{1,2})日)"
    r"|(?P<cn_short>(?P<sm>\d{1,2})月(?P<sd>\d{1,2})日)"
)

# 这些字段是标识符，即使长得像日期也不能改。
_SEED_DATE_SKIP_KEYS = frozenset({"id", "source_url"})


def show_demo_projects() -> bool:
    """演示项目是否参与展示。

    数据一行不动，只在读取层过滤——想放回来翻个环境变量即可，不用改库、
    也不用重新导入。这比删行安全：删了就回不去了。

    生产模式默认隐藏。真实数据进来之后，演示项目会和真项目混在同一份推荐里，
    而它们恰好字段最齐、评分最高，实测能把真实项目全挤到后面去。评委问一句
    「这个项目在哪报名」就穿帮了。
    开发和测试环境默认显示，这样空库也能看到完整流程。
    """
    raw = os.getenv("SHOW_DEMO_DATA", "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return os.getenv("PRACTICE_XIAODA_ENV", "development").strip().lower() != "production"


def _shift_seed_dates(value: Any, delta: timedelta, *, key: str | None = None) -> Any:
    """把演示数据里所有日期整体平移 delta 天，保持字段之间的先后关系不变。

    连原文引用里的"报名截止：2026年7月23日"一起平移，否则字段值和证据引用会
    互相矛盾——而"每个关键字段都能回查原文"正是这个产品的立身之本。
    """
    if key in _SEED_DATE_SKIP_KEYS:
        return value
    if isinstance(value, str):
        return _SEED_DATE_RE.sub(lambda match: _shift_one_date(match, delta), value)
    if isinstance(value, list):
        return [_shift_seed_dates(item, delta) for item in value]
    if isinstance(value, dict):
        return {k: _shift_seed_dates(v, delta, key=k) for k, v in value.items()}
    return value


def _shift_one_date(match: re.Match[str], delta: timedelta) -> str:
    if match.group("iso"):
        return (date.fromisoformat(match.group("iso")) + delta).isoformat()
    if match.group("cn_full"):
        moved = date(int(match.group("cy")), int(match.group("cm")), int(match.group("cd"))) + delta
        return f"{moved.year}年{moved.month}月{moved.day}日"
    # 不带年份的中文日期按锚点所在年份解析；平移跨年时仍按原格式输出（不补年份），
    # 以免和同一句话里的其它写法不一致。
    moved = date(SEED_ANCHOR_DATE.year, int(match.group("sm")), int(match.group("sd"))) + delta
    return f"{moved.month}月{moved.day}日"


class Database:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path or os.getenv("PRACTICE_XIAODA_DB", DEFAULT_DB))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def _open(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Yield a connection that always commits-or-rolls-back and then closes.

        sqlite3 connections are *not* closed by their own context manager, so
        relying on ``with sqlite3.connect(...)`` leaks file handles until the
        garbage collector runs. On Windows an open handle makes the database
        file undeletable, which breaks any caller that works inside a temporary
        directory. Closing explicitly keeps behaviour identical across platforms.
        """
        connection = self._open()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS articles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    input_type TEXT NOT NULL,
                    source_account TEXT NOT NULL,
                    source_url TEXT,
                    title TEXT,
                    raw_text TEXT,
                    collector_status TEXT NOT NULL,
                    collector_warning TEXT,
                    -- 原文配图的 URL（JSON 数组）。以前只把图片挂在项目卡上，
                    -- 而判为非招募或只入语料的文章根本不生成项目卡，图片就被
                    -- 静默丢掉了——实测采集方推来 290 个 URL，只存下 15 个。
                    -- 图片型推送的关键信息全在图里，丢了就没法补 OCR。
                    image_sources TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL,
                    signup_deadline TEXT,
                    source_account TEXT,
                    source_url TEXT,
                    document TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_projects_status ON projects(status);
                CREATE INDEX IF NOT EXISTS idx_projects_deadline ON projects(signup_deadline);

                CREATE TABLE IF NOT EXISTS project_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    document TEXT NOT NULL,
                    note TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(project_id) REFERENCES projects(id)
                );

                CREATE TABLE IF NOT EXISTS feedback (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id TEXT,
                    rating INTEGER,
                    outcome TEXT,
                    comment TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS activity (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    message TEXT NOT NULL,
                    metadata TEXT,
                    created_at TEXT NOT NULL
                );
                """
            )
        self._add_missing_columns()
        self.seed_if_empty()

    # 后加的列。CREATE TABLE IF NOT EXISTS 对已存在的表什么都不做，所以老库
    # 必须显式补列——线上那个库是几周前建的，直接改建表语句对它无效。
    _LATE_COLUMNS = (
        ("articles", "image_sources", "TEXT"),
    )

    def _add_missing_columns(self) -> None:
        with self.connect() as db:
            for table, column, column_type in self._LATE_COLUMNS:
                existing = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
                if column not in existing:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")

    def seed_if_empty(self) -> None:
        # 换成真实数据之后，把演示项目删干净再重启，本方法会把它们又灌回来——
        # 因为判据只看"表是不是空的"。生产环境用 SEED_DEMO_DATA=false 关掉，
        # 免得真实项目库里混进演示条目。
        if os.getenv("SEED_DEMO_DATA", "true").strip().lower() in {"0", "false", "no", "off"}:
            return
        with self.connect() as db:
            count = db.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
        if count or not SEED_FILE.exists():
            return
        payload = json.loads(SEED_FILE.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            anchor = date.fromisoformat(payload.get("anchor_date", SEED_ANCHOR_DATE.isoformat()))
            projects = payload.get("projects", [])
        else:  # 兼容旧版：整个文件就是一个项目数组
            anchor = SEED_ANCHOR_DATE
            projects = payload
        delta = date.today() - anchor
        if delta:
            projects = _shift_seed_dates(projects, delta)
        for project in projects:
            self.upsert_project(project, note="初始化演示数据", log_activity=False)
        self.log("seed", f"已载入 {len(projects)} 条演示项目（日期整体平移 {delta.days} 天）")

    def latest_article_text(self, source_url: str) -> str:
        """这个链接最新一版的原文正文。

        写推送时需要它。原来只把抽取出来的十来条字段喂给模型，然后要求写
        400–700 字——信息量根本不够，模型只能编：实测生成的文案里出现了
        「湘西南的群山之间」「你站在讲台上，面对的是一双双好奇的眼睛」
        「也许你讲的是函数图像，是牛顿定律」，项目卡里一个字都没有。

        而原文我们整篇存着，一直没拿来用。同一个链接可能有多版（订正推送），
        取最后一版。
        """
        if not source_url:
            return ""
        with self.connect() as db:
            row = db.execute(
                "SELECT raw_text FROM articles WHERE source_url=? ORDER BY id DESC LIMIT 1",
                (source_url,),
            ).fetchone()
        return (row["raw_text"] or "").strip() if row else ""

    def insert_article(self, payload: dict[str, Any]) -> int:
        with self.connect() as db:
            cursor = db.execute(
                """
                INSERT INTO articles(input_type, source_account, source_url, title, raw_text,
                                     collector_status, collector_warning, image_sources, created_at)
                VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    payload.get("input_type", "copied_text"),
                    payload.get("source_account", "用户投稿"),
                    payload.get("source_url", ""),
                    payload.get("title", ""),
                    payload.get("raw_text", ""),
                    payload.get("collector_status", "success"),
                    payload.get("collector_warning", ""),
                    # 原文归档必须连图片一起存。图片型推送的关键信息全在图里，
                    # URL 丢了就再也补不了 OCR——只能回头求采集方重抓。
                    json.dumps([str(url).strip() for url in (payload.get("images") or []) if str(url).strip()],
                               ensure_ascii=False),
                    now_iso(),
                ),
            )
            return int(cursor.lastrowid)

    def upsert_project(self, project: dict[str, Any], *, note: str = "更新项目", log_activity: bool = True) -> dict[str, Any]:
        errors = validate_project(project)
        if errors:
            raise ValueError("; ".join(errors))
        project = refresh_status(project)
        with self.connect() as db:
            existing = db.execute("SELECT document FROM projects WHERE id=?", (project["id"],)).fetchone()
            if existing:
                current = json.loads(existing["document"])
                if current != project:
                    version = db.execute(
                        "SELECT COALESCE(MAX(version),0)+1 FROM project_versions WHERE project_id=?",
                        (project["id"],),
                    ).fetchone()[0]
                    db.execute(
                        "INSERT INTO project_versions(project_id,version,document,note,created_at) VALUES(?,?,?,?,?)",
                        (project["id"], version, json_dumps(current), note, now_iso()),
                    )
            db.execute(
                """
                INSERT INTO projects(id,title,status,signup_deadline,source_account,source_url,document,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                  title=excluded.title,
                  status=excluded.status,
                  signup_deadline=excluded.signup_deadline,
                  source_account=excluded.source_account,
                  source_url=excluded.source_url,
                  document=excluded.document,
                  updated_at=excluded.updated_at
                """,
                (
                    project["id"], project["title"], project["status"], project.get("signup_deadline"),
                    project.get("source_account"), project.get("source_url"), json_dumps(project),
                    project.get("created_at", now_iso()), project.get("updated_at", now_iso()),
                ),
            )
        if log_activity:
            self.log("project_upsert", f"已保存项目：{project['title']}", {"project_id": project["id"]})
        return project

    def find_duplicate(self, project: dict[str, Any]) -> dict[str, Any] | None:
        source_url = project.get("source_url")
        with self.connect() as db:
            if source_url:
                row = db.execute("SELECT document FROM projects WHERE source_url=? LIMIT 1", (source_url,)).fetchone()
                if row:
                    return json.loads(row["document"])
            rows = db.execute("SELECT document FROM projects WHERE title=?", (project.get("title"),)).fetchall()
        for row in rows:
            candidate = json.loads(row["document"])
            if candidate.get("organizer") == project.get("organizer") and candidate.get("signup_deadline") == project.get("signup_deadline"):
                return candidate
        return None

    def delete_projects_by_source(self, source_url: str, *, note: str = "非招募内容移出机会库") -> int:
        """Remove opportunity cards for a source while retaining article audit history."""
        if not source_url:
            return 0
        with self.connect() as db:
            rows = db.execute("SELECT id, title FROM projects WHERE source_url=?", (source_url,)).fetchall()
            if not rows:
                return 0
            ids = [row["id"] for row in rows]
            marks = ",".join("?" for _ in ids)
            db.execute(f"DELETE FROM project_versions WHERE project_id IN ({marks})", ids)
            db.execute(f"DELETE FROM projects WHERE id IN ({marks})", ids)
        for row in rows:
            self.log("project_removed", f"已移出非招募内容：{row['title']}", {"project_id": row["id"], "reason": note})
        return len(rows)

    @staticmethod
    def _demo_visibility_note() -> str:
        return "生产模式默认隐藏演示项目；SHOW_DEMO_DATA=true 可临时放出来。"

    def list_projects(self, *, status: str = "", query: str = "", include_expired: bool = True) -> list[dict[str, Any]]:
        clauses, params = [], []
        if status:
            clauses.append("status=?")
            params.append(status)
        if query:
            clauses.append("(title LIKE ? OR source_account LIKE ? OR document LIKE ?)")
            needle = f"%{query}%"
            params.extend([needle, needle, needle])
        if not include_expired:
            clauses.append("status != 'expired'")
        sql = "SELECT document FROM projects"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY CASE status WHEN 'published' THEN 0 WHEN 'needs_review' THEN 1 ELSE 2 END, signup_deadline ASC, updated_at DESC"
        with self.connect() as db:
            rows = db.execute(sql, params).fetchall()
        projects = []
        hide_demo = not show_demo_projects()
        for row in rows:
            project = refresh_status(json.loads(row["document"]))
            if hide_demo and project.get("demo_data"):
                continue
            projects.append(project)
        return projects

    def get_project(self, project_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT document FROM projects WHERE id=?", (project_id,)).fetchone()
        return refresh_status(json.loads(row["document"])) if row else None

    def review_project(self, project_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        current = self.get_project(project_id)
        if not current:
            raise KeyError(project_id)
        allowed = {
            "title", "organizer", "summary", "theme_tags", "practice_start", "practice_end", "schedule_segments",
            "signup_deadline", "location", "eligibility", "reimbursement", "quota", "signup_method",
            "contact", "required_materials", "risk_notes", "uncertain_fields", "field_evidence", "image_sources", "image_ocr_status",
            "confidence", "status", "source_account", "source_url",
        }
        safe_patch = {key: value for key, value in patch.items() if key in allowed}
        updated = deep_merge(current, safe_patch)
        updated["updated_at"] = now_iso()
        if updated.get("status") == "published" and updated.get("uncertain_fields"):
            updated["risk_notes"] = list(dict.fromkeys(updated.get("risk_notes", []) + ["审核发布时仍有待确认字段"]))
        return self.upsert_project(updated, note="人工审核更新")

    def stats(self) -> dict[str, Any]:
        with self.connect() as db:
            counts = {row["status"]: row["n"] for row in db.execute("SELECT status, COUNT(*) n FROM projects GROUP BY status")}
            source_count = db.execute("SELECT COUNT(DISTINCT source_account) FROM projects").fetchone()[0]
            feedback_count = db.execute("SELECT COUNT(*) FROM feedback").fetchone()[0]
            avg_rating = db.execute("SELECT ROUND(AVG(rating),1) FROM feedback WHERE rating IS NOT NULL").fetchone()[0]
        total = sum(counts.values())
        return {
            "total": total,
            "published": counts.get("published", 0),
            "needs_review": counts.get("needs_review", 0),
            "expired": counts.get("expired", 0),
            "rejected": counts.get("rejected", 0),
            "sources": source_count,
            "feedback_count": feedback_count,
            "average_rating": avg_rating,
        }

    def add_feedback(self, payload: dict[str, Any]) -> int:
        rating = payload.get("rating")
        if rating is not None and (not isinstance(rating, int) or not 1 <= rating <= 5):
            raise ValueError("rating must be an integer from 1 to 5")
        with self.connect() as db:
            cursor = db.execute(
                "INSERT INTO feedback(project_id,rating,outcome,comment,created_at) VALUES(?,?,?,?,?)",
                (payload.get("project_id"), rating, payload.get("outcome", ""), payload.get("comment", "")[:1000], now_iso()),
            )
        self.log("feedback", "收到一条用户反馈", {"project_id": payload.get("project_id")})
        return int(cursor.lastrowid)

    def log(self, event_type: str, message: str, metadata: dict[str, Any] | None = None) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO activity(event_type,message,metadata,created_at) VALUES(?,?,?,?)",
                (event_type, message, json_dumps(metadata or {}), now_iso()),
            )

    def recent_activity(self, limit: int = 8) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT event_type,message,metadata,created_at FROM activity ORDER BY id DESC LIMIT ?",
                (max(1, min(limit, 30)),),
            ).fetchall()
        return [
            {
                "event_type": row["event_type"], "message": row["message"],
                "metadata": json.loads(row["metadata"] or "{}"), "created_at": row["created_at"],
            }
            for row in rows
        ]

    def versions(self, project_id: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT version,note,created_at FROM project_versions WHERE project_id=? ORDER BY version DESC",
                (project_id,),
            ).fetchall()
        return [dict(row) for row in rows]
