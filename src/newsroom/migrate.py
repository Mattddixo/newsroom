"""Plain versioned SQL migrations.

Files in migrations/ are named NNNN_description.sql and applied in order, each
in its own transaction. Applied versions are recorded in schema_migrations.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


def available(directory: Path = MIGRATIONS_DIR) -> list[tuple[int, Path]]:
    found = []
    for path in sorted(directory.glob("*.sql")):
        match = _NAME.match(path.name)
        if not match:
            raise ValueError(f"Bad migration filename: {path.name}")
        found.append((int(match.group(1)), path))
    versions = [v for v, _ in found]
    if len(versions) != len(set(versions)):
        raise ValueError("Duplicate migration version numbers")
    return found


def applied(conn: sqlite3.Connection) -> set[int]:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
    )
    return {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}


def migrate(conn: sqlite3.Connection, directory: Path = MIGRATIONS_DIR) -> list[int]:
    """Apply pending migrations. Returns the versions applied."""
    done = applied(conn)
    newly: list[int] = []
    for version, path in available(directory):
        if version in done:
            continue
        sql = path.read_text(encoding="utf-8")
        log.info("applying migration", extra={"version": version, "file": path.name})
        conn.execute("BEGIN IMMEDIATE")
        try:
            for statement in _split(sql):
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                (version, path.name, datetime.now(UTC).isoformat()),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        newly.append(version)
    return newly


def _split(sql: str) -> list[str]:
    """Split a script into complete statements (handles triggers with inner semicolons)."""
    statements: list[str] = []
    buf = ""
    for line in sql.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            if buf.strip():
                statements.append(buf.strip())
            buf = ""
    if buf.strip():
        raise ValueError("Migration ends with an incomplete statement")
    return statements
