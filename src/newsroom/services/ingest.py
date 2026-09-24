"""Article ingestion: idempotent, resumable, one transaction per API query.

Window: from the end of the last fully successful run (minus an overlap, since
GDELT indexes with a delay) to now. A partial or failed run does not advance the
cursor, so the next run re-covers the same window; dedup on the canonical URL
makes that harmless. A crash leaves only whole, committed queries behind.
"""

from __future__ import annotations

import fcntl
import logging
import sqlite3
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from newsroom.config import OutletConfig
from newsroom.services.tagging import Tagger, tag_article, tag_ids
from newsroom.sources.base import ArticleSource
from newsroom.urls import canonical_key

log = logging.getLogger(__name__)

TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def ts(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime(TS_FORMAT)


def parse_ts(value: str) -> datetime:
    return datetime.strptime(value, TS_FORMAT).replace(tzinfo=UTC)


class IngestBusy(RuntimeError):
    pass


@contextmanager
def ingest_lock(
    path: Path,
    wait: float = 0,
    poll: float = 5.0,
    sleep: Callable[[float], None] = time.sleep,
) -> Iterator[None]:
    """One writing job at a time (ingest, ownership, funding, retag; worker or CLI).

    With `wait`, keep trying for that many seconds before giving up, so a scheduled
    job queues behind a long-running one instead of being skipped."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as handle:
        waited = 0.0
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if waited >= wait:
                    raise IngestBusy(
                        "another job (ingest, ownership or funding) is running; try again shortly"
                    ) from exc
                if waited == 0:
                    log.info("waiting for another job to finish", extra={"max_wait_s": wait})
                sleep(poll)
                waited += poll
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


@dataclass
class RunSummary:
    run_id: int
    status: str
    window_start: datetime
    window_end: datetime
    queries: int = 0
    query_errors: int = 0
    fetched: int = 0
    inserted: int = 0


def sync_outlets(conn: sqlite3.Connection, outlets: Sequence[OutletConfig], now: datetime) -> None:
    """Make the outlets table mirror outlets.yaml. Removed outlets are deactivated, not deleted."""
    stamp = ts(now)
    conn.execute("BEGIN IMMEDIATE")
    try:
        for o in outlets:
            conn.execute(
                "INSERT INTO outlets (domain, display_name, country, language, active,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, 1, ?, ?)"
                " ON CONFLICT (domain) DO UPDATE SET display_name = excluded.display_name,"
                " country = excluded.country, language = excluded.language, active = 1,"
                " updated_at = excluded.updated_at",
                (o.domain, o.name, o.country, o.language, stamp, stamp),
            )
        domains = [o.domain for o in outlets]
        placeholders = ",".join("?" * len(domains)) or "''"
        conn.execute(
            f"UPDATE outlets SET active = 0, updated_at = ? WHERE active = 1"  # noqa: S608
            f" AND domain NOT IN ({placeholders})",
            [stamp, *domains],
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def next_window_start(
    conn: sqlite3.Connection,
    source: str,
    now: datetime,
    *,
    backfill: timedelta,
    overlap: timedelta,
    max_window: timedelta,
) -> datetime:
    row = conn.execute(
        "SELECT max(window_end) FROM ingest_runs WHERE source = ? AND status = 'ok'", (source,)
    ).fetchone()
    start = parse_ts(row[0]) - overlap if row[0] else now - backfill
    return max(start, now - max_window)


def run_ingest(
    conn: sqlite3.Connection,
    source: ArticleSource,
    tagger: Tagger,
    *,
    now: datetime | None = None,
    backfill: timedelta = timedelta(hours=72),
    overlap: timedelta = timedelta(hours=1),
    max_window: timedelta = timedelta(days=7),
) -> RunSummary:
    now = (now or datetime.now(UTC)).replace(microsecond=0)
    # A previous process that died mid-run leaves a 'running' row; close it out.
    conn.execute(
        "UPDATE ingest_runs SET status = 'failed', error = 'interrupted' "
        "WHERE source = ? AND status = 'running'",
        (source.name,),
    )
    outlets = {
        row["domain"]: row["id"]
        for row in conn.execute("SELECT id, domain FROM outlets WHERE active = 1")
    }
    start = next_window_start(
        conn, source.name, now, backfill=backfill, overlap=overlap, max_window=max_window
    )
    cur = conn.execute(
        "INSERT INTO ingest_runs (source, started_at, window_start, window_end, status)"
        " VALUES (?, ?, ?, ?, 'running')",
        (source.name, ts(datetime.now(UTC)), ts(start), ts(now)),
    )
    summary = RunSummary(cur.lastrowid or 0, "running", start, now)
    ids = tag_ids(conn)
    log.info(
        "ingest starting",
        extra={
            "source": source.name,
            "window_start": ts(start),
            "window_end": ts(now),
            "outlets": len(outlets),
        },
    )
    error: str | None = None
    try:
        for result in source.fetch(list(outlets), start, now):
            summary.queries += 1
            if result.error:
                summary.query_errors += 1
                _progress(conn, summary)
                continue
            retrieved = ts(datetime.now(UTC))
            conn.execute("BEGIN IMMEDIATE")
            try:
                for rec in result.records:
                    summary.fetched += 1
                    outlet_id = outlets.get(rec.domain)
                    if outlet_id is None:
                        continue
                    cur = conn.execute(
                        "INSERT INTO articles (url, url_key, title, outlet_id, published_at,"
                        " language, image_url, source, source_url, retrieved_at)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                        " ON CONFLICT (url_key) DO NOTHING",
                        (
                            rec.url,
                            canonical_key(rec.url),
                            rec.title,
                            outlet_id,
                            ts(rec.published_at),
                            rec.language,
                            rec.image_url,
                            source.name,
                            result.source_url,
                            retrieved,
                        ),
                    )
                    if cur.rowcount == 1:
                        summary.inserted += 1
                        tag_article(conn, tagger, cur.lastrowid or 0, rec.title, ids)
                _progress(conn, summary)  # same transaction: counts match what is saved
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
    except Exception as exc:  # unexpected: record it, keep committed batches
        error = f"{type(exc).__name__}: {exc}"
        log.exception("ingest aborted")

    if error or (summary.queries and summary.query_errors == summary.queries):
        summary.status = "failed"
    elif summary.query_errors:
        summary.status = "partial"
    else:
        summary.status = "ok"
    conn.execute(
        "UPDATE ingest_runs SET status = ?, finished_at = ?, queries = ?, query_errors = ?,"
        " fetched = ?, inserted = ?, error = ? WHERE id = ?",
        (
            summary.status,
            ts(datetime.now(UTC)),
            summary.queries,
            summary.query_errors,
            summary.fetched,
            summary.inserted,
            error,
            summary.run_id,
        ),
    )
    log.info(
        "ingest finished",
        extra={k: v for k, v in vars(summary).items() if k not in {"window_start", "window_end"}},
    )
    return summary


def _progress(conn: sqlite3.Connection, s: RunSummary) -> None:
    """Record a running run's counts after each request, for `newsroom status`."""
    conn.execute(
        "UPDATE ingest_runs SET queries = ?, query_errors = ?, fetched = ?, inserted = ?"
        " WHERE id = ?",
        (s.queries, s.query_errors, s.fetched, s.inserted, s.run_id),
    )


def prune(conn: sqlite3.Connection, retention_days: int, now: datetime | None = None) -> int:
    """Delete articles older than the retention period. 0 or less keeps everything."""
    if retention_days <= 0:
        return 0
    now = now or datetime.now(UTC)
    cutoff = ts(now - timedelta(days=retention_days))
    total = 0
    while True:
        cur = conn.execute(
            "DELETE FROM articles WHERE id IN "
            "(SELECT id FROM articles WHERE published_at < ? LIMIT 5000)",
            (cutoff,),
        )
        total += cur.rowcount
        if cur.rowcount < 5000:
            break
    conn.execute("DELETE FROM ingest_runs WHERE started_at < ?", (ts(now - timedelta(days=90)),))
    conn.execute("PRAGMA optimize")
    return total
