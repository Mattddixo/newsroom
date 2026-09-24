"""Fill in publication dates from article pages, politely.

Limits that keep this reasonable for the outlets:
  - only articles GDELT saw in the last `max_age` (default 3 days), each page read once
    (one retry after a transient failure), newest first
  - at most `limit` pages per run (default 150), one request at a time, >= 1 s apart
  - robots.txt obeyed; a host that answers 403/429/5xx is left alone for the rest of the run
  - only the first ~1.5 MB of a page is read and only the date is kept
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from newsroom.net.safe_fetch import FetchBlocked, FetchResult
from newsroom.services.ingest import parse_ts, ts
from newsroom.sources.pubdate import Robots, extract
from newsroom.urls import host_of

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 2
HOST_BACKOFF_STATUSES = frozenset({401, 403, 429})

# fetch_page(url, outlet_domain) -> FetchResult
PageFetcher = Callable[[str, str], FetchResult]


@dataclass
class PubDateSummary:
    checked: int = 0
    found: int = 0
    no_date: int = 0
    robots_disallowed: int = 0
    failed: int = 0
    hosts_skipped: int = 0


def refresh_pub_dates(
    conn: sqlite3.Connection,
    fetch_page: PageFetcher,
    robots: Robots,
    now: datetime,
    *,
    limit: int = 150,
    max_age: timedelta = timedelta(days=3),
    min_interval: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> PubDateSummary:
    summary = PubDateSummary()
    rows = conn.execute(
        "SELECT a.id, a.url, a.published_at, o.domain FROM articles a"
        " JOIN outlets o ON o.id = a.outlet_id"
        " WHERE a.outlet_published_at IS NULL AND a.pubdate_attempts < ?"
        " AND a.published_at >= ?"
        " AND (a.pubdate_checked_at IS NULL OR a.pubdate_checked_at < ?)"
        " ORDER BY a.published_at DESC LIMIT ?",
        (MAX_ATTEMPTS, ts(now - max_age), ts(now - timedelta(hours=1)), limit),
    ).fetchall()
    skip_hosts: set[str] = set()
    last_request: float | None = None
    stamp = ts(now)

    def record(article_id: int, attempts: str, when: str | None, method: str | None) -> None:
        conn.execute(
            f"UPDATE articles SET pubdate_attempts = {attempts},"  # noqa: S608 - fixed SQL
            " pubdate_checked_at = ?, outlet_published_at = ?, pubdate_method = ?"
            " WHERE id = ?",
            (stamp, when, method, article_id),
        )

    for row in rows:
        host = host_of(row["url"])
        if host in skip_hosts:
            continue
        if not robots.allowed(row["url"]):
            summary.robots_disallowed += 1
            record(row["id"], str(MAX_ATTEMPTS), None, "robots.txt disallows")
            continue
        if last_request is not None:
            wait = min_interval - (clock() - last_request)
            if wait > 0:
                sleep(wait)
        last_request = clock()
        summary.checked += 1
        try:
            page = fetch_page(row["url"], row["domain"])
        except FetchBlocked as exc:
            summary.failed += 1
            record(row["id"], "pubdate_attempts + 1", None, None)
            if exc.status in HOST_BACKOFF_STATUSES or (exc.status or 0) >= 500:
                skip_hosts.add(host)
                summary.hosts_skipped += 1
                log.info("leaving host alone this run", extra={"host": host, "status": exc.status})
            continue
        found = extract(page.body.decode("utf-8", errors="replace"), parse_ts(row["published_at"]))
        if found:
            summary.found += 1
            record(row["id"], "pubdate_attempts + 1", ts(found.when), found.method)
        else:
            summary.no_date += 1  # the page has no usable date: don't ask again
            record(row["id"], str(MAX_ATTEMPTS), None, "no date in page metadata")
    log.info("publication dates checked", extra=vars(summary))
    return summary
