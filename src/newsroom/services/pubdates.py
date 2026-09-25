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
from datetime import UTC, datetime, timedelta

from newsroom.net.safe_fetch import FetchBlocked, FetchResult
from newsroom.services.ingest import parse_ts, ts
from newsroom.sources.pubdate import DISALLOWED, UNAVAILABLE, DateConflict, Robots, extract
from newsroom.urls import host_of

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 2
HOST_BACKOFF_STATUSES = frozenset({401, 403, 429})
ZONE_CONFLICT = "time zones disagree in page metadata"

# fetch_page(url, outlet_domain) -> FetchResult
PageFetcher = Callable[[str, str], FetchResult]


@dataclass
class PubDateSummary:
    checked: int = 0
    found: int = 0
    no_date: int = 0
    conflicting: int = 0
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
    wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> PubDateSummary:
    """Check up to `limit` recent undated articles, newest first. Each article is stamped
    with the moment it was checked (the feed shows new articles from then on)."""
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

    def record(article_id: int, attempts: str, when: str | None, method: str | None) -> None:
        conn.execute(
            f"UPDATE articles SET pubdate_attempts = {attempts},"  # noqa: S608 - fixed SQL
            " pubdate_checked_at = ?, outlet_published_at = ?, pubdate_method = ?"
            " WHERE id = ?",
            (ts(wall_clock()), when, method, article_id),
        )

    for row in rows:
        host = host_of(row["url"])
        if host in skip_hosts:
            continue
        verdict = robots.check(row["url"])
        if verdict == UNAVAILABLE:
            # robots.txt couldn't be read just now: leave the site alone this pass and
            # record nothing (it's not a verdict on the article); tried again next pass.
            skip_hosts.add(host)
            summary.hosts_skipped += 1
            log.info("robots.txt unavailable; leaving host alone this run", extra={"host": host})
            continue
        if verdict == DISALLOWED:
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
        if isinstance(found, DateConflict):
            # The page's tags disagree about the time zone: no telling which is right.
            summary.conflicting += 1
            record(row["id"], str(MAX_ATTEMPTS), None, ZONE_CONFLICT)
        elif found:
            summary.found += 1
            record(row["id"], "pubdate_attempts + 1", ts(found.when), found.method)
        else:
            summary.no_date += 1  # the page has no usable date: don't ask again
            record(row["id"], str(MAX_ATTEMPTS), None, "no date in page metadata")
    log.info("publication dates checked", extra=vars(summary))
    return summary


# ------------------------------------------------------------------ audit

SUSPECT_LAG = timedelta(hours=3)


@dataclass
class DateAudit:
    domain: str
    origin: str  # "feed" (the outlet's RSS feed) or "page" (the article page's metadata)
    dated: int
    median_lag: timedelta  # first seen minus stated publication time
    early: int  # stated more than SUSPECT_LAG before first seen
    zone_conflicts: int  # pages whose own tags disagree by whole hours

    @property
    def suspect(self) -> bool:
        """Most of the outlet's dates are hours before it was seen: typical of a time
        zone error (local time labelled as UTC), though a slow source looks the same."""
        return self.dated >= 5 and self.median_lag >= SUSPECT_LAG


def audit_dates(conn: sqlite3.Connection, now: datetime, days: int = 7) -> list[DateAudit]:
    """How each outlet's stated publication times compare with when articles were first
    seen, over the last `days`. Read-only."""
    rows = conn.execute(
        "SELECT o.domain, a.published_at, a.outlet_published_at, a.pubdate_method"
        " FROM articles a JOIN outlets o ON o.id = a.outlet_id"
        " WHERE a.published_at >= ?"
        " AND (a.outlet_published_at IS NOT NULL OR a.pubdate_method = ?)",
        (ts(now - timedelta(days=days)), ZONE_CONFLICT),
    ).fetchall()
    lags: dict[tuple[str, str], list[timedelta]] = {}
    conflicts: dict[str, int] = {}
    for r in rows:
        if r["outlet_published_at"] is None:
            conflicts[r["domain"]] = conflicts.get(r["domain"], 0) + 1
            continue
        origin = "feed" if (r["pubdate_method"] or "").startswith("feed ") else "page"
        lag = parse_ts(r["published_at"]) - parse_ts(r["outlet_published_at"])
        lags.setdefault((r["domain"], origin), []).append(lag)
    out = [
        DateAudit(
            domain,
            origin,
            len(values),
            sorted(values)[len(values) // 2],
            sum(v > SUSPECT_LAG for v in values),
            conflicts.pop(domain, 0) if origin == "page" else 0,
        )
        for (domain, origin), values in lags.items()
    ]
    out += [DateAudit(d, "page", 0, timedelta(0), 0, n) for d, n in conflicts.items()]
    return sorted(out, key=lambda a: (not a.suspect, -a.zone_conflicts, a.domain, a.origin))
