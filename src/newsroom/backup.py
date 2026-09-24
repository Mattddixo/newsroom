"""Consistent SQLite snapshots via the online backup API, for restic to pick up."""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)

PREFIX = "newsroom-"
SUFFIX = ".sqlite3"


def backup(db_path: Path, backup_dir: Path, keep: int, now: datetime | None = None) -> Path:
    """Write a snapshot, verify it, then prune old snapshots beyond `keep`."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    final = backup_dir / f"{PREFIX}{stamp}{SUFFIX}"
    partial = final.with_suffix(".partial")

    src = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    dst = sqlite3.connect(partial)
    try:
        src.backup(dst)
        result = dst.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise RuntimeError(f"Backup integrity check failed: {result}")
    finally:
        dst.close()
        src.close()
    partial.replace(final)  # atomic rename: restic never sees a half-written file
    log.info("backup written", extra={"path": str(final), "bytes": final.stat().st_size})
    prune(backup_dir, keep)
    return final


def prune(backup_dir: Path, keep: int) -> list[Path]:
    snapshots = sorted(backup_dir.glob(f"{PREFIX}*{SUFFIX}"))
    removed = snapshots[:-keep] if keep > 0 else []
    for path in removed:
        path.unlink()
    for stale in backup_dir.glob(f"{PREFIX}*.partial"):
        stale.unlink()
    return removed
