#!/usr/bin/env python3
"""给 SQLite 项目库做在线备份。

为什么不用 cp：服务是常驻的，随时可能在写库。直接复制文件可能拷到一个
写到一半的状态，恢复时才发现备份本身是坏的。sqlite3 的 backup API 会走
正常的加锁流程，产出的一定是一致快照，而且不需要停服务。

爬虫接上之后，库里是人工核验过的真实项目——重建成本远高于这点磁盘。
"""
from __future__ import annotations

import gzip
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

SOURCE = Path(os.getenv("PRACTICE_XIAODA_DB", "/opt/practice-xiaoda/data/practice_xiaoda.db"))
DEST_DIR = Path(os.getenv("BACKUP_DIR", "/opt/backups"))
KEEP_DAYS = int(os.getenv("BACKUP_KEEP_DAYS", "14"))


def main() -> int:
    if not SOURCE.exists():
        print(f"[backup] 源库不存在：{SOURCE}", file=sys.stderr)
        return 1
    DEST_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    tmp = DEST_DIR / f".practice_xiaoda-{stamp}.db"
    final = DEST_DIR / f"practice_xiaoda-{stamp}.db.gz"

    source = sqlite3.connect(f"file:{SOURCE}?mode=ro", uri=True)
    try:
        target = sqlite3.connect(tmp)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()

    with open(tmp, "rb") as raw, gzip.open(final, "wb") as packed:
        shutil.copyfileobj(raw, packed)
    tmp.unlink()

    # 备份完立刻验一次：坏掉的备份比没有备份更危险，因为它给人虚假的安全感。
    check_tmp = DEST_DIR / f".verify-{stamp}.db"
    with gzip.open(final, "rb") as packed, open(check_tmp, "wb") as raw:
        shutil.copyfileobj(packed, raw)
    try:
        probe = sqlite3.connect(check_tmp)
        ok = probe.execute("PRAGMA integrity_check").fetchone()[0]
        count = probe.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
        probe.close()
    finally:
        check_tmp.unlink(missing_ok=True)
    if ok != "ok":
        print(f"[backup] 备份校验失败：{ok}", file=sys.stderr)
        final.unlink(missing_ok=True)
        return 2

    cutoff = datetime.now() - timedelta(days=KEEP_DAYS)
    removed = 0
    for old in DEST_DIR.glob("practice_xiaoda-*.db.gz"):
        if datetime.fromtimestamp(old.stat().st_mtime) < cutoff:
            old.unlink()
            removed += 1

    size_kb = final.stat().st_size / 1024
    print(f"[backup] {final.name}  {size_kb:.0f} KB  项目 {count} 条  完整性 {ok}  清理旧备份 {removed} 个")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
