"""SQLite persistence for 实践小搭."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from domain import deep_merge, json_dumps, now_iso, refresh_status, validate_project


ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "data" / "practice_xiaoda.db"
SEED_FILE = ROOT / "seed_data.json"


class Database:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path or os.getenv("PRACTICE_XIAODA_DB", DEFAULT_DB))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

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
        self.seed_if_empty()

    def seed_if_empty(self) -> None:
        with self.connect() as db:
            count = db.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
        if count or not SEED_FILE.exists():
            return
        projects = json.loads(SEED_FILE.read_text(encoding="utf-8"))
        for project in projects:
            self.upsert_project(project, note="初始化演示数据", log_activity=False)
        self.log("seed", f"已载入 {len(projects)} 条演示项目")

    def insert_article(self, payload: dict[str, Any]) -> int:
        with self.connect() as db:
            cursor = db.execute(
                """
                INSERT INTO articles(input_type, source_account, source_url, title, raw_text,
                                     collector_status, collector_warning, created_at)
                VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    payload.get("input_type", "copied_text"),
                    payload.get("source_account", "用户投稿"),
                    payload.get("source_url", ""),
                    payload.get("title", ""),
                    payload.get("raw_text", ""),
                    payload.get("collector_status", "success"),
                    payload.get("collector_warning", ""),
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
        for row in rows:
            project = refresh_status(json.loads(row["document"]))
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
