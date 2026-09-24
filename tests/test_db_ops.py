from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from newsroom.backup import backup
from newsroom.db import connect, connect_readonly
from newsroom.migrate import migrate
from newsroom.worker import beat, heartbeat_ok


def write_migrations(directory: Path, files: dict[str, str]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name, sql in files.items():
        (directory / name).write_text(sql)


def test_migrate_applies_in_order_and_is_idempotent(tmp_path: Path) -> None:
    mdir = tmp_path / "m"
    write_migrations(
        mdir,
        {
            "0001_a.sql": "CREATE TABLE a (id INTEGER PRIMARY KEY);",
            "0002_b.sql": (
                "CREATE TABLE b (id INTEGER PRIMARY KEY, n INT);\n"
                "CREATE TRIGGER t AFTER INSERT ON a BEGIN INSERT INTO b (n) VALUES (1); END;"
            ),
        },
    )
    conn = connect(tmp_path / "db.sqlite3")
    assert migrate(conn, mdir) == [1, 2]
    assert migrate(conn, mdir) == []
    conn.execute("INSERT INTO a DEFAULT VALUES")
    assert conn.execute("SELECT count(*) FROM b").fetchone()[0] == 1


def test_failed_migration_rolls_back(tmp_path: Path) -> None:
    mdir = tmp_path / "m"
    write_migrations(
        mdir,
        {"0001_bad.sql": "CREATE TABLE ok (id INT);\nTHIS IS NOT SQL;"},
    )
    conn = connect(tmp_path / "db.sqlite3")
    with pytest.raises(sqlite3.Error):
        migrate(conn, mdir)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "ok" not in tables
    assert conn.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == 0


def test_readonly_connection_refuses_writes(tmp_path: Path) -> None:
    path = tmp_path / "db.sqlite3"
    rw = connect(path)
    rw.execute("CREATE TABLE t (x INT)")
    ro = connect_readonly(path)
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("INSERT INTO t VALUES (1)")


def test_backup_snapshot_and_prune(tmp_path: Path) -> None:
    db = tmp_path / "db.sqlite3"
    conn = connect(db)
    conn.execute("CREATE TABLE t (x INT)")
    conn.execute("INSERT INTO t VALUES (42)")
    out = tmp_path / "backups"
    start = datetime(2026, 1, 1, tzinfo=UTC)
    paths = [backup(db, out, keep=3, now=start + timedelta(days=i)) for i in range(5)]
    remaining = sorted(out.glob("*.sqlite3"))
    assert remaining == paths[-3:]
    snap = sqlite3.connect(remaining[-1])
    assert snap.execute("SELECT x FROM t").fetchone()[0] == 42
    assert not list(out.glob("*.partial"))


def test_heartbeat(tmp_path: Path) -> None:
    hb = tmp_path / "hb"
    assert not heartbeat_ok(hb)
    beat(hb)
    assert heartbeat_ok(hb)
    hb.write_text("0")
    assert not heartbeat_ok(hb)
