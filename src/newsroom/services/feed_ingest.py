"""Ingest outlets' own RSS/Atom feeds (outlets.yaml `feeds:`).

One request per feed per run, paced, robots.txt obeyed. Each feed is stored in its
own transaction; a failing feed doesn't stop the others. Recorded in ingest_runs as
source 'feeds' so `newsroom status` can report it.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from newsroom.net.http import ApiError
from newsroom.net.safe_fetch import FetchBlocked
from newsroom.services.ingest import RunSummary, _progress, _store, ts
from newsroom.services.tagging import Tagger, tag_ids
from newsroom.sources.base import QueryResult
from newsroom.sources.feeds import feed_records, parse_feed
from newsroom.sources.pubdate import ALLOWED, Robots

log = logging.getLogger(__name__)

SOURCE = "feeds"
# (feed URL, the outlet's own domains) -> body. The feed's host and the outlet's domains
# are the only places it may be fetched or redirected to.
FeedFetcher = Callable[[str, list[str]], bytes]


def run_feeds(
    conn: sqlite3.Connection,
    fetch: FeedFetcher,
    robots: Robots,
    tagger: Tagger,
    *,
    now: datetime | None = None,
    max_age: timedelta = timedelta(hours=48),
    min_interval: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> RunSummary:
    now = (now or datetime.now(UTC)).replace(microsecond=0)
    conn.execute(
        "UPDATE ingest_runs SET status = 'failed', error = 'interrupted'"
        " WHERE source = ? AND status = 'running'",
        (SOURCE,),
    )
    outlets = conn.execute(
        "SELECT id, domain, aliases, feeds, language FROM outlets"
        " WHERE active = 1 AND feeds != '' ORDER BY domain"
    ).fetchall()
    cur = conn.execute(
        "INSERT INTO ingest_runs (source, started_at, window_start, window_end, status)"
        " VALUES (?, ?, ?, ?, 'running')",
        (SOURCE, ts(datetime.now(UTC)), ts(now - max_age), ts(now)),
    )
    summary = RunSummary(cur.lastrowid or 0, "running", now - max_age, now)
    ids = tag_ids(conn)
    last: float | None = None
    for o in outlets:
        for url in o["feeds"].split():
            summary.queries += 1
            if robots.check(url) != ALLOWED:
                summary.query_errors += 1
                log.info("feed not fetched: robots.txt", extra={"feed": url})
                _progress(conn, summary)
                continue
            if last is not None and (wait := min_interval - (clock() - last)) > 0:
                sleep(wait)
            last = clock()
            try:
                items = parse_feed(fetch(url, [o["domain"], *o["aliases"].split()]), url)
            except (FetchBlocked, ApiError) as exc:
                summary.query_errors += 1
                log.warning("feed failed", extra={"feed": url, "error": str(exc)})
                _progress(conn, summary)
                continue
            result = feed_records(
                items, o["domain"], o["aliases"].split(), o["language"], now, max_age
            )
            _store(
                conn,
                SOURCE,
                QueryResult(source_url=url, records=result.records),
                {o["domain"]: o["id"]},
                tagger,
                ids,
                summary,
            )
    if summary.queries and summary.query_errors == summary.queries:
        summary.status = "failed"
    elif summary.query_errors:
        summary.status = "partial"
    else:
        summary.status = "ok"
    conn.execute(
        "UPDATE ingest_runs SET status = ?, finished_at = ?, queries = ?, query_errors = ?,"
        " fetched = ?, inserted = ? WHERE id = ?",
        (
            summary.status,
            ts(datetime.now(UTC)),
            summary.queries,
            summary.query_errors,
            summary.fetched,
            summary.inserted,
            summary.run_id,
        ),
    )
    log.info(
        "feeds finished",
        extra={k: v for k, v in vars(summary).items() if k not in {"window_start", "window_end"}},
    )
    return summary
