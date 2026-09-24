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


def test_status_command(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from newsroom import cli
    from newsroom.settings import Settings

    settings = Settings(data_dir=tmp_path)
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    with pytest.raises(SystemExit) as exit_:
        cli.main(["status"])
    assert exit_.value.code == 1  # no database yet
    conn = connect(settings.db_path)
    migrate(conn)
    conn.close()
    backup(settings.db_path, settings.backup_dir, keep=3)
    with pytest.raises(SystemExit) as exit_:
        cli.main(["status"])
    out = capsys.readouterr().out
    assert exit_.value.code == 0
    assert "last full run:   never" in out
    assert "newsroom-" in out and "1 kept" in out
    assert "CONTACT_EMAIL is not set" in out


def test_status_shows_running_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from newsroom import cli
    from newsroom.settings import Settings

    settings = Settings(data_dir=tmp_path)
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    conn = connect(settings.db_path)
    migrate(conn)
    conn.execute(
        "INSERT INTO ingest_runs (source, started_at, window_start, window_end, status)"
        " VALUES ('gdelt', '2026-09-24T10:00:00Z', 'a', 'b', 'running')"
    )
    conn.close()
    with pytest.raises(SystemExit):
        cli.main(["status"])
    assert "current run:     running since 2026-09-24T10:00:00Z" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("interval", "offset", "expected"),
    [
        (15, 3, ["00:03", "00:18", "00:33", "00:48", "01:03"]),
        (15, 0, ["00:15", "00:30", "00:45", "01:00", "01:15"]),
        (30, 5, ["00:05", "00:35", "01:05", "01:35", "02:05"]),
        (60, 3, ["00:03", "01:03", "02:03", "03:03", "04:03"]),
        (15, 20, ["00:05", "00:20", "00:35", "00:50", "01:05"]),  # offset wraps to 5
        (120, 3, ["00:03", "02:03", "04:03", "06:03", "08:03"]),
    ],
)
def test_ingest_runs_on_the_clock(interval: int, offset: int, expected: list[str]) -> None:
    from newsroom.settings import Settings
    from newsroom.worker import ingest_trigger

    trigger = ingest_trigger(
        Settings(ingest_interval_minutes=interval, ingest_offset_minutes=offset)
    )
    now = datetime(2026, 9, 24, 0, 0, 1, tzinfo=UTC)
    times, previous = [], None
    for _ in range(5):
        now = trigger.get_next_fire_time(previous, now)
        times.append(now.astimezone(UTC).strftime("%H:%M"))
        previous = now
        now = now + timedelta(seconds=1)
    assert times == expected


def test_odd_interval_falls_back_to_timer() -> None:
    from apscheduler.triggers.interval import IntervalTrigger

    from newsroom.settings import Settings
    from newsroom.worker import ingest_trigger

    assert isinstance(ingest_trigger(Settings(ingest_interval_minutes=7)), IntervalTrigger)


def test_status_lists_outlets_behind(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from newsroom import cli
    from newsroom.settings import Settings

    settings = Settings(data_dir=tmp_path)
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    conn = connect(settings.db_path)
    migrate(conn)
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    for d in ("a.ca", "b.ca", "c.ca"):
        conn.execute(
            "INSERT INTO outlets (domain, display_name, country, language, created_at,"
            " updated_at) VALUES (?, ?, 'CA', 'en', ?, ?)",
            (d, d, now, now),
        )
    conn.execute("INSERT INTO ingest_cursors VALUES ('gdelt', 'a.ca', ?, ?)", (now, now))
    conn.close()
    with pytest.raises(SystemExit):
        cli.main(["status"])
    out = capsys.readouterr().out
    assert "outlets current: 1 of 3" in out
    assert "behind:          b.ca, c.ca" in out


def test_pubdates_run_five_minutes_after_ingest() -> None:
    from newsroom.settings import Settings
    from newsroom.worker import pubdates_trigger

    trigger = pubdates_trigger(Settings())
    now = datetime(2026, 9, 24, 0, 0, 1, tzinfo=UTC)
    times, previous = [], None
    for _ in range(4):
        now = trigger.get_next_fire_time(previous, now)
        times.append(now.astimezone(UTC).strftime("%H:%M"))
        previous = now
        now = now + timedelta(seconds=1)
    assert times == ["00:08", "00:23", "00:38", "00:53"]


def test_job_kinds_have_independent_locks(tmp_path: Path) -> None:
    from newsroom.services.ingest import IngestBusy, ingest_lock
    from newsroom.settings import Settings

    s = Settings(data_dir=tmp_path)
    assert len({s.lock_path(k) for k in ("ingest", "records", "pubdates")}) == 3
    with ingest_lock(s.lock_path("ingest")):
        with ingest_lock(s.lock_path("records")), ingest_lock(s.lock_path("pubdates")):
            pass  # a long ingest doesn't block the others
        with pytest.raises(IngestBusy, match="ingest"), ingest_lock(s.lock_path("ingest")):
            pass
