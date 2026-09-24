from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from newsroom.config import OutletConfig
from newsroom.services.ingest import (
    IngestBusy,
    ingest_lock,
    prune,
    run_ingest,
    sync_outlets,
)
from newsroom.services.tagging import Tagger, retag_all
from newsroom.sources.base import QueryResult
from tests.helpers import OUTLETS, TAGS, FakeSource, make_db, rec

NOW = datetime(2026, 9, 24, 12, tzinfo=UTC)
T = NOW - timedelta(hours=2)


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    return make_db(tmp_path / "db.sqlite3", NOW)


def count(conn: sqlite3.Connection, table: str) -> int:
    return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def test_inserts_dedups_and_tags(conn: sqlite3.Connection) -> None:
    source = FakeSource(
        [
            QueryResult(
                "https://api/1",
                [
                    rec("https://www.cbc.ca/news/a", "Housing plan and election talk", T),
                    rec("https://cbc.ca/news/a/?utm_source=x", "Housing plan (dup)", T),
                    rec(
                        "https://ici.radio-canada.ca/n/1", "Crise du logement", T, "radio-canada.ca"
                    ),
                    rec("https://unknown.example/x", "Not an outlet", T, "unknown.example"),
                ],
            )
        ]
    )
    summary = run_ingest(conn, source, Tagger(TAGS), now=NOW)
    assert summary.status == "ok"
    assert summary.fetched == 4
    assert summary.inserted == 2
    assert count(conn, "articles") == 2

    row = conn.execute("SELECT * FROM articles WHERE url LIKE '%cbc.ca/news/a'").fetchone()
    assert row["source"] == "fake"
    assert row["source_url"] == "https://api/1"
    assert row["published_at"] == "2026-09-24T10:00:00Z"
    assert row["retrieved_at"]

    tags = conn.execute(
        "SELECT a.title, t.slug, at.matched FROM article_tags at"
        " JOIN articles a ON a.id = at.article_id JOIN tags t ON t.id = at.tag_id"
        " ORDER BY a.id, t.slug"
    ).fetchall()
    assert [(r["slug"], r["matched"]) for r in tags] == [
        ("elections", "election"),
        ("housing", "housing"),
        ("housing", "logement"),
    ]

    # Running again with the same data changes nothing (idempotent).
    again = run_ingest(conn, source, Tagger(TAGS), now=NOW + timedelta(hours=1))
    assert again.inserted == 0
    assert count(conn, "articles") == 2


def test_window_resumes_from_last_ok_run(conn: sqlite3.Connection) -> None:
    first = FakeSource([QueryResult("u", [])])
    run_ingest(conn, first, Tagger(TAGS), now=NOW, backfill=timedelta(hours=72))
    assert first.calls == [(NOW - timedelta(hours=72), NOW)]

    second = FakeSource([QueryResult("u", [])])
    later = NOW + timedelta(hours=1)
    run_ingest(conn, second, Tagger(TAGS), now=later, overlap=timedelta(hours=1))
    assert second.calls == [(NOW - timedelta(hours=1), later)]


def test_partial_run_does_not_advance_cursor(conn: sqlite3.Connection) -> None:
    run_ingest(conn, FakeSource([QueryResult("u", [])]), Tagger(TAGS), now=NOW)
    partial = FakeSource(
        [
            QueryResult("ok", [rec("https://cbc.ca/1", "Kept", T)]),
            QueryResult("bad", error="HTTP 503"),
        ]
    )
    s = run_ingest(conn, partial, Tagger(TAGS), now=NOW + timedelta(hours=1))
    assert s.status == "partial"
    assert s.inserted == 1  # the good query was committed

    retry = FakeSource([QueryResult("u", [])])
    run_ingest(conn, retry, Tagger(TAGS), now=NOW + timedelta(hours=2), overlap=timedelta(0))
    assert retry.calls[0][0] == NOW  # still from the last *ok* run


def test_all_queries_failing_is_failed(conn: sqlite3.Connection) -> None:
    s = run_ingest(conn, FakeSource([QueryResult("x", error="e")]), Tagger(TAGS), now=NOW)
    assert s.status == "failed"


def test_crash_mid_run_keeps_committed_batches(conn: sqlite3.Connection) -> None:
    source = FakeSource(
        [
            QueryResult("a", [rec("https://cbc.ca/1", "One", T)]),
            QueryResult("b", [rec("https://cbc.ca/2", "Two", T)]),
        ],
        explode=True,
    )
    s = run_ingest(conn, source, Tagger(TAGS), now=NOW)
    assert s.status == "failed"
    assert count(conn, "articles") == 1
    run = conn.execute("SELECT status, error FROM ingest_runs").fetchone()
    assert run["status"] == "failed"
    assert "boom" in run["error"]
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_interrupted_run_is_closed_out(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO ingest_runs (source, started_at, window_start, window_end, status)"
        " VALUES ('fake', 'x', '2026-09-20T00:00:00Z', '2026-09-21T00:00:00Z', 'running')"
    )
    run_ingest(conn, FakeSource([]), Tagger(TAGS), now=NOW)
    statuses = [r[0] for r in conn.execute("SELECT status FROM ingest_runs ORDER BY id")]
    assert statuses == ["failed", "ok"]


def test_window_is_capped(conn: sqlite3.Connection) -> None:
    source = FakeSource([])
    run_ingest(conn, source, Tagger(TAGS), now=NOW, backfill=timedelta(days=90))
    assert source.calls[0][0] == NOW - timedelta(days=7)


def test_outlet_sync_deactivates_removed(conn: sqlite3.Connection) -> None:
    renamed_cbc = OutletConfig("cbc.ca", "CBC", "CA", "en")
    sync_outlets(conn, [renamed_cbc, OUTLETS[1]], NOW)  # nytimes.com removed
    rows = {r["domain"]: r for r in conn.execute("SELECT * FROM outlets")}
    assert rows["nytimes.com"]["active"] == 0
    assert rows["cbc.ca"]["display_name"] == "CBC"
    assert rows["cbc.ca"]["active"] == 1


def test_retag_all(conn: sqlite3.Connection) -> None:
    run_ingest(
        conn,
        FakeSource([QueryResult("u", [rec("https://cbc.ca/1", "Interest rate cut", T)])]),
        Tagger([]),
        now=NOW,
    )
    assert count(conn, "article_tags") == 0
    assert retag_all(conn, Tagger(TAGS)) == 1
    assert count(conn, "article_tags") == 1


def test_prune(conn: sqlite3.Connection) -> None:
    old = NOW - timedelta(days=400)
    run_ingest(
        conn,
        FakeSource(
            [
                QueryResult(
                    "u",
                    [
                        rec("https://cbc.ca/old", "Old housing", old),
                        rec("https://cbc.ca/new", "New", T),
                    ],
                )
            ]
        ),
        Tagger(TAGS),
        now=NOW,
    )
    assert prune(conn, 365, now=NOW) == 1
    assert [r[0] for r in conn.execute("SELECT url FROM articles")] == ["https://cbc.ca/new"]
    assert count(conn, "article_tags") == 0  # cascaded
    # FTS index no longer finds the deleted row
    assert (
        conn.execute("SELECT count(*) FROM articles_fts WHERE articles_fts MATCH 'old'").fetchone()[
            0
        ]
        == 0
    )
    assert prune(conn, 0, now=NOW) == 0


def test_bad_url_rejected_by_schema(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO articles (url, url_key, title, outlet_id, published_at, source,"
            " source_url, retrieved_at) VALUES ('javascript:x', 'k', 't', 1, 'x', 's', 'u', 'r')"
        )


def test_ingest_lock(tmp_path: Path) -> None:
    lock = tmp_path / "ingest.lock"
    with ingest_lock(lock), pytest.raises(IngestBusy), ingest_lock(lock):
        pass
    with ingest_lock(lock):  # released
        pass
