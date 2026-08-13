#!/usr/bin/env python3
"""Create a consistent, integrity-checked SQLite snapshot for deployment."""
from __future__ import annotations

import argparse
import gzip
import json
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, default=Path("data/practice_xiaoda.db"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.database.exists():
        raise SystemExit(f"database not found: {args.database}")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="practice-db-snapshot-") as directory:
        snapshot = Path(directory) / "practice_xiaoda.db"
        source = sqlite3.connect(f"file:{args.database}?mode=ro", uri=True)
        try:
            target = sqlite3.connect(snapshot)
            try:
                source.backup(target)
            finally:
                target.close()
        finally:
            source.close()
        check = sqlite3.connect(snapshot)
        integrity = check.execute("PRAGMA integrity_check").fetchone()[0]
        counts = {
            "articles": check.execute("SELECT COUNT(*) FROM articles").fetchone()[0],
            "projects": check.execute("SELECT COUNT(*) FROM projects").fetchone()[0],
            "activity": check.execute("SELECT COUNT(*) FROM activity").fetchone()[0],
        }
        check.close()
        if integrity != "ok":
            raise SystemExit(f"integrity check failed: {integrity}")
        with open(snapshot, "rb") as raw, gzip.open(args.output, "wb") as packed:
            shutil.copyfileobj(raw, packed)
    print(json.dumps({"output": str(args.output), "integrity": integrity, "counts": counts, "created_at": datetime.now(timezone.utc).isoformat()}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
