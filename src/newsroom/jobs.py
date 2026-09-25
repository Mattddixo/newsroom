"""Job bodies shared by the worker schedule and the CLI."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from newsroom.config import ConfigError, load_curated_funding, load_outlets, load_tags
from newsroom.db import connect
from newsroom.net.http import ApiClient
from newsroom.net.safe_fetch import FetchBlocked, FetchResult, safe_fetch
from newsroom.services import funding, ingest, ownership, pubdates
from newsroom.services.coverage import build_report as coverage_report
from newsroom.services.ingest import parse_ts, ts
from newsroom.services.tagging import Tagger, retag_all, sync_tags
from newsroom.settings import Settings
from newsroom.sources import funding as funding_sources
from newsroom.sources.gdelt import GdeltSource
from newsroom.sources.gdelt_files import GkgFilesSource, scan_hosts, slots_between
from newsroom.sources.pubdate import (
    EARLIEST,
    FUTURE_SKEW,
    Robots,
    candidates,
    extract,
    parse_timestamp,
)
from newsroom.sources.wikidata import WikidataSource
from newsroom.urls import host_matches, host_of, is_http_url

log = logging.getLogger(__name__)


def sync_config(settings: Settings) -> Tagger:
    """Load outlets.yaml and tags.yaml into the database. Raises ConfigError if invalid."""
    outlets = load_outlets(settings.config_dir / "outlets.yaml")
    tags = load_tags(settings.config_dir / "tags.yaml")
    conn = connect(settings.db_path)
    try:
        ingest.sync_outlets(conn, outlets, datetime.now(UTC))
        conn.execute("BEGIN IMMEDIATE")
        sync_tags(conn, tags)
        conn.execute("COMMIT")
    finally:
        conn.close()
    return Tagger(tags)


def ingest_articles(settings: Settings, *, wait: float = 0) -> ingest.RunSummary:
    with ingest.ingest_lock(settings.lock_path("ingest"), wait, name="ingest"):
        tagger = sync_config(settings)
        conn = connect(settings.db_path)
        catch_up = timedelta(hours=settings.ingest_catchup_hours)
        backfill = timedelta(hours=settings.ingest_backfill_hours)
        source: GkgFilesSource | GdeltSource
        if settings.ingest_source == "doc":
            # DOC search API. Its limiter stays closed for a minute or more after a 429 and
            # retrying inside that window keeps it closed, so a refusal ends the run.
            client = ApiClient(
                settings.user_agent,
                min_interval=settings.gdelt_min_interval,
                max_retries=2,
                retry_throttled=False,
            )
            source = GdeltSource(client, group_size=settings.gdelt_group_size)
        else:
            # 15-minute GKG files: static downloads, no per-request quota. Every file
            # covers every outlet, so a new outlet's backfill is limited to the catch-up
            # window (48 hours would be ~400 files).
            client = ApiClient(settings.user_agent, timeout=120.0, min_interval=1.0, max_retries=2)
            source = GkgFilesSource(client, settings.tmp_dir, ingest.outlet_domains(conn))
            backfill = min(backfill, catch_up)
        try:
            return ingest.run_ingest(conn, source, tagger, backfill=backfill, catch_up=catch_up)
        finally:
            client.close()
            conn.close()


HTML_TYPES = frozenset({"text/html", "application/xhtml+xml"})
_ROBOTS_CACHE: dict = {}  # robots.txt rules per host, kept for the worker's lifetime (1-day TTL)


def _publication_dates(
    settings: Settings, conn: sqlite3.Connection, limit: int | None = None
) -> pubdates.PubDateSummary | None:
    """Read publication dates for recent articles (see services/pubdates.py for limits)."""
    if not settings.pubdate_fetch:
        return None
    if not settings.contact_email:
        log.warning("publication dates skipped: CONTACT_EMAIL is not set")
        return None
    robots_text, page = _page_fetchers(settings, ingest.outlet_domains(conn))
    return pubdates.refresh_pub_dates(
        conn,
        page,
        Robots(robots_text, cache=_ROBOTS_CACHE),
        datetime.now(UTC).replace(microsecond=0),
        limit=limit or settings.pubdate_per_run,
        max_age=timedelta(days=settings.pubdate_max_age_days),
    )


def _page_fetchers(
    settings: Settings, outlets: dict[str, list[str]]
) -> tuple[Callable[[str], str], Callable[[str, str], FetchResult]]:
    """robots.txt and article-page fetchers: SSRF-guarded, outlet sites only (an outlet's
    page fetch is limited to its own domains), size-capped."""
    all_domains = [d for main, others in outlets.items() for d in (main, *others)]

    def robots_text(url: str) -> str:
        page = safe_fetch(
            url,
            allowlist=all_domains,
            allowed_types={"text/plain"},
            max_bytes=500_000,
            timeout=10,
            user_agent=settings.user_agent,
            truncate=True,
        )
        return page.body.decode("utf-8", errors="replace")

    def page(url: str, domain: str) -> FetchResult:
        return safe_fetch(
            url,
            allowlist=[domain, *outlets.get(domain, [])],
            allowed_types=HTML_TYPES,
            max_bytes=1_500_000,
            timeout=15,
            user_agent=settings.user_agent,
            truncate=True,
        )

    return robots_text, page


def explain_date(settings: Settings, url: str) -> list[str]:
    """What the date check would do with one article page, step by step (for diagnosis).
    Same fetchers and limits as the real check; only outlets' own sites."""
    if not is_http_url(url):
        raise ValueError("not an http(s) link")
    conn = connect(settings.db_path)
    try:
        outlets = ingest.outlet_domains(conn)
        seen_row = conn.execute(
            "SELECT published_at FROM articles WHERE url = ?", (url,)
        ).fetchone()
    finally:
        conn.close()
    host = host_of(url)
    domain = next(
        (
            main
            for main, others in outlets.items()
            if any(host_matches(host, d) for d in (main, *others))
        ),
        None,
    )
    if domain is None:
        raise LookupError(f"{host} is not one of the outlets in outlets.yaml")
    seen = parse_ts(seen_row[0]) if seen_row else datetime.now(UTC)
    lines = [
        f"outlet: {domain}",
        f"GDELT saw it: {ts(seen) if seen_row else 'not in the database (using now)'}",
    ]
    robots_text, page = _page_fetchers(settings, outlets)
    verdict = Robots(robots_text).check(url)
    lines.append(f"robots.txt: {verdict}")
    if verdict != "allowed":
        return lines
    try:
        result = page(url, domain)
    except FetchBlocked as exc:
        lines.append(f"page: could not be read ({exc})")
        return lines
    lines.append(f"page: read {len(result.body):,} bytes")
    found = extract(result.body.decode("utf-8", errors="replace"), seen)
    listed = False
    for method, raw in candidates(result.body.decode("utf-8", errors="replace")):
        listed = True
        when = parse_timestamp(raw)
        if when is None:
            why = "rejected: not a full date-time with a time zone"
        elif when > seen + FUTURE_SKEW:
            why = "rejected: later than GDELT saw the article"
        elif when < EARLIEST:
            why = "rejected: before 1995"
        elif found and found.method == method and found.when == when:
            why = "USED"
        else:
            why = "valid (a higher-priority tag was used)"
        lines.append(f"  {method}: {str(raw)[:60]!r} -> {why}")
    if not listed:
        lines.append("  no date tags in the page's metadata")
    lines.append(f"result: {ts(found.when) + ' (' + found.method + ')' if found else 'no date'}")
    return lines


def publication_dates(
    settings: Settings, limit: int | None = None, *, wait: float = 0
) -> pubdates.PubDateSummary | None:
    """Run the publication-date pass (worker every 15 min, or the CLI). None when turned
    off (PUBDATE_FETCH) or CONTACT_EMAIL is not set."""
    with ingest.ingest_lock(settings.lock_path("pubdates"), wait, name="publication-date"):
        conn = connect(settings.db_path)
        try:
            summary = _publication_dates(settings, conn, limit)
        finally:
            conn.close()
    return summary


def retag(settings: Settings) -> int:
    with ingest.ingest_lock(settings.lock_path("ingest"), name="ingest"):
        tagger = sync_config(settings)
        conn = connect(settings.db_path)
        try:
            return retag_all(conn, tagger)
        finally:
            conn.close()


def prune(settings: Settings) -> int:
    conn = connect(settings.db_path)
    try:
        removed = ingest.prune(conn, settings.retention_days)
    finally:
        conn.close()
    log.info("pruned old articles", extra={"removed": removed})
    return removed


def wikidata_client(settings: Settings) -> ApiClient:
    """Wikimedia's User-Agent policy requires contact details; refuse to run without them."""
    if not settings.contact_email:
        raise ConfigError("CONTACT_EMAIL must be set in .env before querying Wikidata")
    # 65 s: the query service's own limit is 60 s, so don't give up before it does.
    return ApiClient(settings.user_agent, timeout=65.0, min_interval=settings.wikidata_min_interval)


def resolve_ownership(
    settings: Settings,
    domains: list[str] | None = None,
    *,
    rematch: bool = True,
    wait: float = 0,
) -> ownership.ResolveSummary:
    """Match and resolve outlets that are due (or the given domains), then refresh logos."""
    now = datetime.now(UTC).replace(microsecond=0)
    client = wikidata_client(settings)
    with ingest.ingest_lock(settings.lock_path("records"), wait, name="ownership/funding"):
        sync_config(settings)
        conn = connect(settings.db_path)
        try:
            source = WikidataSource(client)
            outlets = ownership.due_outlets(
                conn, now, timedelta(days=settings.ownership_refresh_days), domains
            )
            if domains and len(outlets) != len(set(domains)):
                known = {o["domain"] for o in outlets}
                raise LookupError(f"unknown outlet(s): {', '.join(sorted(set(domains) - known))}")
            matched = ownership.match_outlets(conn, source, outlets, now) if rematch else None
            summary = ownership.resolve_ownership(conn, source, outlets, now)
            if matched:
                summary.matched = matched.matched
                summary.ambiguous = matched.ambiguous
                summary.unmatched = matched.unmatched
            logos = ownership.refresh_logos(
                conn,
                lambda url: safe_fetch(
                    url, allowlist=ownership.LOGO_HOSTS, user_agent=settings.user_agent
                ),
                settings.logo_dir,
                now,
            )
        finally:
            client.close()
            conn.close()
    log.info("ownership resolved", extra={**vars(summary), "logos_updated": logos})
    return summary


def refresh_funding(
    settings: Settings, *, force: bool = False, wait: float = 0
) -> funding.FundingSummary:
    """Look up funding records for identifiers that are due, and sync the curated file."""
    if not settings.contact_email:
        raise ConfigError("CONTACT_EMAIL must be set in .env before querying SEC or ProPublica")
    curated = load_curated_funding(settings.config_dir / "public_funding.yaml")
    now = datetime.now(UTC).replace(microsecond=0)
    sec = ApiClient(settings.user_agent, min_interval=0.2)  # SEC allows 10 req/s
    propublica = ApiClient(settings.user_agent, min_interval=1.0)
    fetchers: funding.Fetchers = {
        "sec_cik": lambda v: funding_sources.fetch_sec(sec, v),
        "us_ein": lambda v: funding_sources.fetch_propublica(propublica, v),
        "ca_bn": funding_sources.cra_records,
    }
    with ingest.ingest_lock(settings.lock_path("records"), wait, name="ownership/funding"):
        conn = connect(settings.db_path)
        try:
            summary = funding.refresh_funding(
                conn, fetchers, now, timedelta(days=settings.ownership_refresh_days), force
            )
            funding.sync_curated(conn, curated, summary)
        finally:
            conn.close()
            sec.close()
            propublica.close()
    log.info("funding refreshed", extra=vars(summary))
    return summary


def coverage(settings: Settings, hours: int) -> tuple[list, int, int]:
    """Survey the last `hours` of GDELT's 15-minute files: which outlets are there, under
    which addresses. Returns (report rows, files read, slots asked for)."""
    if not 1 <= hours <= 48:
        raise ValueError("hours must be between 1 and 48")
    sync_config(settings)
    conn = connect(settings.db_path)
    try:
        domains = ingest.outlet_domains(conn)
        names = dict(conn.execute("SELECT domain, display_name FROM outlets WHERE active = 1"))
        week = ts(datetime.now(UTC) - timedelta(days=7))
        stored = dict(
            conn.execute(
                "SELECT o.domain, count(a.id) FROM outlets o JOIN articles a"
                " ON a.outlet_id = o.id AND a.published_at >= ? WHERE o.active = 1"
                " GROUP BY o.id",
                (week,),
            )
        )
    finally:
        conn.close()
    now = datetime.now(UTC)
    slots = slots_between(now - timedelta(hours=hours), now)
    client = ApiClient(settings.user_agent, timeout=120.0, min_interval=1.0, max_retries=2)
    try:
        counts, files = scan_hosts(client, settings.tmp_dir, slots)
    finally:
        client.close()
    outlets = {d: (names.get(d, d), others) for d, others in domains.items()}
    return coverage_report(outlets, counts, stored), files, len(slots)
