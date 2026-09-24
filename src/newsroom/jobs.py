"""Job bodies shared by the worker schedule and the CLI."""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime, timedelta

from newsroom.config import ConfigError, load_curated_funding, load_outlets, load_tags
from newsroom.db import connect
from newsroom.net.http import ApiClient
from newsroom.net.safe_fetch import FetchResult, safe_fetch
from newsroom.services import funding, ingest, ownership, pubdates
from newsroom.services.tagging import Tagger, retag_all, sync_tags
from newsroom.settings import Settings
from newsroom.sources import funding as funding_sources
from newsroom.sources.gdelt import GdeltSource
from newsroom.sources.pubdate import Robots
from newsroom.sources.wikidata import WikidataSource

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
        client = ApiClient(settings.user_agent, min_interval=settings.gdelt_min_interval)
        try:
            source = GdeltSource(client, group_size=settings.gdelt_group_size)
            summary = ingest.run_ingest(
                conn,
                source,
                tagger,
                backfill=timedelta(hours=settings.ingest_backfill_hours),
                catch_up=timedelta(hours=settings.ingest_catchup_hours),
            )
            return summary
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
    outlet_domains = [r[0] for r in conn.execute("SELECT domain FROM outlets WHERE active = 1")]

    def robots_text(url: str) -> str:
        page = safe_fetch(
            url,
            allowlist=outlet_domains,
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
            allowlist=[domain],
            allowed_types=HTML_TYPES,
            max_bytes=1_500_000,
            timeout=15,
            user_agent=settings.user_agent,
            truncate=True,
        )

    return pubdates.refresh_pub_dates(
        conn,
        page,
        Robots(robots_text, cache=_ROBOTS_CACHE),
        datetime.now(UTC).replace(microsecond=0),
        limit=limit or settings.pubdate_per_run,
        max_age=timedelta(days=settings.pubdate_max_age_days),
    )


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
