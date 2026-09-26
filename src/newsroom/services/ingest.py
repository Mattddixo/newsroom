"""Article ingestion: idempotent, resumable, one transaction per API query.

Each outlet has a cursor: everything up to it is in. A run fetches each group of
outlets from its cursor (minus an overlap, since GDELT indexes with a delay) to now,
oldest slice first, and moves the cursor forward after every successful slice. At
the first error the group stops and the next run carries on from there; dedup on
the canonical URL makes any re-fetch harmless. When the API keeps refusing, the run
stops early rather than hammer it. A crash leaves only whole, committed queries behind.
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
from newsroom.places import EU, place_name
from newsroom.services.tagging import Tagger, encode_themes, tag_article, tag_ids
from newsroom.sources.base import ArticleRecord, ArticleSource, QueryResult
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
    name: str = "ingest",
) -> Iterator[None]:
    """One job of a kind at a time, worker or CLI. Each kind has its own lock file
    (Settings.lock_path); SQLite's busy timeout serialises their short writes.

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
                        f"another {name} job is already running; try again shortly"
                    ) from exc
                if waited == 0:
                    log.info(
                        "waiting for another job to finish", extra={"job": name, "max_wait_s": wait}
                    )
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
                "INSERT INTO outlets (domain, display_name, country, language, aliases, feeds,"
                " note, active, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)"
                " ON CONFLICT (domain) DO UPDATE SET display_name = excluded.display_name,"
                " country = excluded.country, language = excluded.language,"
                " aliases = excluded.aliases, feeds = excluded.feeds, note = excluded.note,"
                " active = 1, updated_at = excluded.updated_at",
                (
                    o.domain,
                    o.name,
                    o.country,
                    o.language,
                    " ".join(o.also),
                    " ".join(o.feeds),
                    o.note,
                    stamp,
                    stamp,
                ),
            )
            _sync_pin(conn, o, stamp)
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


PINNED = "set in outlets.yaml"


def _sync_pin(conn: sqlite3.Connection, o: OutletConfig, stamp: str) -> None:
    """Apply `wikidata:` from outlets.yaml. Removing the line hands the outlet back to
    automatic matching; a pin made with `newsroom outlets set-qid` is left alone."""
    row = conn.execute(
        "SELECT wikidata_qid, match_status, match_note FROM outlets WHERE domain = ?",
        (o.domain,),
    ).fetchone()
    if o.wikidata:
        qid = None if o.wikidata == "none" else o.wikidata
        if (row["wikidata_qid"], row["match_status"], row["match_note"]) != (qid, "manual", PINNED):
            conn.execute(
                "UPDATE outlets SET wikidata_qid = ?, match_status = 'manual',"
                " match_source_url = ?, match_note = ?, matched_at = ?,"
                " ownership_checked_at = NULL WHERE domain = ?",
                (
                    qid,
                    f"https://www.wikidata.org/wiki/{qid}" if qid else None,
                    PINNED,
                    stamp,
                    o.domain,
                ),
            )
    elif row["match_status"] == "manual" and row["match_note"] == PINNED:
        conn.execute(
            "UPDATE outlets SET wikidata_qid = NULL, match_status = 'unmatched',"
            " match_source_url = NULL, match_note = '', ownership_checked_at = NULL"
            " WHERE domain = ?",
            (o.domain,),
        )


def outlet_domains(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """Active outlets' domains, each with its other domains (`also:` in outlets.yaml)."""
    return {
        r["domain"]: r["aliases"].split()
        for r in conn.execute("SELECT domain, aliases FROM outlets WHERE active = 1")
    }


def _fallback_start(
    conn: sqlite3.Connection, source: str, now: datetime, backfill: timedelta, overlap: timedelta
) -> datetime:
    """Start for an outlet with no cursor yet: the last fully OK run (from before
    per-outlet cursors existed), else the backfill period."""
    row = conn.execute(
        "SELECT max(window_end) FROM ingest_runs WHERE source = ? AND status = 'ok'", (source,)
    ).fetchone()
    return parse_ts(row[0]) - overlap if row[0] else now - backfill


def group_start(
    conn: sqlite3.Connection,
    source: str,
    domains: Sequence[str],
    now: datetime,
    *,
    backfill: timedelta,
    overlap: timedelta,
    catch_up: timedelta,
) -> datetime:
    """Where a group's window begins: its least-advanced outlet's cursor, minus overlap
    (GDELT indexes with a delay). An outlet that fell behind resumes at most `catch_up`
    ago (keeping up beats filling old gaps); a new outlet starts `backfill` ago."""
    marks = ",".join("?" * len(domains))
    cursors = dict(
        conn.execute(
            f"SELECT domain, window_end FROM ingest_cursors"  # noqa: S608
            f" WHERE source = ? AND domain IN ({marks})",
            [source, *domains],
        ).fetchall()
    )
    fallback = max(_fallback_start(conn, source, now, backfill, overlap), now - backfill)
    starts = [
        max(parse_ts(cursors[d]) - overlap, now - catch_up) if d in cursors else fallback
        for d in domains
    ]
    return min(starts)


def _advance(conn: sqlite3.Connection, source: str, domains: Sequence[str], end: datetime) -> None:
    stamp = ts(datetime.now(UTC))
    conn.executemany(
        "INSERT INTO ingest_cursors (source, domain, window_end, updated_at) VALUES (?, ?, ?, ?)"
        " ON CONFLICT (source, domain) DO UPDATE SET window_end = excluded.window_end,"
        " updated_at = excluded.updated_at",
        [(source, d, ts(end), stamp) for d in domains],
    )


def run_ingest(
    conn: sqlite3.Connection,
    source: ArticleSource,
    tagger: Tagger,
    *,
    now: datetime | None = None,
    backfill: timedelta = timedelta(hours=48),
    overlap: timedelta | None = None,
    catch_up: timedelta | None = None,
) -> RunSummary:
    """`backfill`: how far back an outlet's first fetch reaches. `catch_up`: how far back
    an outlet that fell behind may resume (defaults to `backfill`). `overlap`: how much
    before the cursor to re-cover (default: the source's own `overlap`, else 1 hour)."""
    now = (now or datetime.now(UTC)).replace(microsecond=0)
    if overlap is None:
        overlap = getattr(source, "overlap", timedelta(hours=1))
    catch_up = min(catch_up or backfill, backfill)
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
    groups = source.groups(list(outlets))
    starts = [
        group_start(
            conn, source.name, g, now, backfill=backfill, overlap=overlap, catch_up=catch_up
        )
        for g in groups
    ]
    # Most-behind groups first, so a run cut short by throttling helps those most.
    order = sorted(zip(starts, groups, strict=True), key=lambda sg: sg[0])
    start = min(starts, default=now)
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
    throttled = False
    try:
        for group_from, group in order:
            group_ok = True
            reported_progress = False
            for result in source.fetch(group, group_from, now):
                summary.queries += 1
                if result.error:
                    summary.query_errors += 1
                    group_ok = False
                    throttled = result.throttled
                    _progress(conn, summary)
                    break  # the next run resumes this group from its last good slice
                _store(conn, source.name, result, outlets, tagger, ids, summary)
                if result.window_end:
                    _advance(conn, source.name, group, result.window_end)
                    reported_progress = True
            if group_ok and not reported_progress:
                # A source that doesn't report progress: all done means done up to `now`.
                _advance(conn, source.name, group, now)
            if throttled:
                log.warning(
                    "source keeps refusing requests; stopping this run early",
                    extra={"source": source.name},
                )
                break
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


def _store(
    conn: sqlite3.Connection,
    source_name: str,
    result: QueryResult,
    outlets: dict[str, int],
    tagger: Tagger,
    ids: dict[str, int],
    summary: RunSummary,
) -> None:
    """Save one request's articles (deduplicated, tagged) in one transaction."""
    retrieved = ts(datetime.now(UTC))
    conn.execute("BEGIN IMMEDIATE")
    try:
        for rec in result.records:
            summary.fetched += 1
            outlet_id = outlets.get(rec.domain)
            if outlet_id is None:
                continue
            stated = ts(rec.outlet_published_at) if rec.outlet_published_at else None
            themes = encode_themes(dict(rec.themes), tagger.themes)
            about = about_text(rec)
            key = canonical_key(rec.url)
            cur = conn.execute(
                "INSERT INTO articles (url, url_key, title, outlet_id, published_at,"
                " language, image_url, source, source_url, retrieved_at,"
                " outlet_published_at, pubdate_method, pubdate_checked_at, gdelt_themes, about)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (url_key) DO NOTHING",
                (
                    rec.url,
                    key,
                    rec.title,
                    outlet_id,
                    ts(rec.published_at),
                    rec.language,
                    rec.image_url,
                    source_name,
                    result.source_url,
                    retrieved,
                    stated,
                    rec.pubdate_method if stated else None,
                    retrieved if stated else None,  # dated by its source: nothing to check
                    themes,
                    about,
                ),
            )
            if cur.rowcount == 1:
                summary.inserted += 1
                tag_article(conn, tagger, cur.lastrowid or 0, themes, "", ids)
                _store_places(conn, cur.lastrowid or 0, rec)
            elif about:
                # Already stored (e.g. from an outlet's feed): add what GDELT read, once.
                row = conn.execute(
                    "UPDATE articles SET about = ? WHERE url_key = ? AND about = '' RETURNING id",
                    (about, key),
                ).fetchone()
                if row:
                    _store_places(conn, row["id"], rec)
            if cur.rowcount != 1 and themes:
                # Already stored (e.g. from an outlet's feed) without GDELT's themes.
                row = conn.execute(
                    "UPDATE articles SET gdelt_themes = ? WHERE url_key = ? AND gdelt_themes = ''"
                    " RETURNING id, sections",
                    (themes, key),
                ).fetchone()
                if row:
                    tag_article(conn, tagger, row["id"], themes, row["sections"], ids)
        _progress(conn, summary)  # same transaction: counts match what is saved
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def about_text(rec: ArticleRecord) -> str:
    """What GDELT says the story is about, as searchable text: the people it's about,
    then the countries ("European Union" too for a member state). Empty if unknown."""
    names = [name for name, _ in rec.people]
    for code, _ in rec.places:
        names.append(place_name(code))
    if any(code in EU for code, _ in rec.places):
        names.append("European Union")
    return " · ".join(names)


def _store_places(conn: sqlite3.Connection, article_id: int, rec: ArticleRecord) -> None:
    conn.executemany(
        "INSERT OR IGNORE INTO article_places (article_id, country, mentions) VALUES (?, ?, ?)",
        [(article_id, code, n) for code, n in rec.places],
    )


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
