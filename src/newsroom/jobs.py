"""Job bodies shared by the worker schedule and the CLI."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from newsroom.config import ConfigError, load_curated_funding, load_outlets, load_tags
from newsroom.db import connect
from newsroom.net.http import ApiClient
from newsroom.net.safe_fetch import safe_fetch
from newsroom.services import funding, ingest, ownership
from newsroom.services.tagging import Tagger, retag_all, sync_tags
from newsroom.settings import Settings
from newsroom.sources import funding as funding_sources
from newsroom.sources.gdelt import GdeltSource
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


def ingest_articles(settings: Settings) -> ingest.RunSummary:
    with ingest.ingest_lock(settings.lock_path):
        tagger = sync_config(settings)
        conn = connect(settings.db_path)
        client = ApiClient(settings.user_agent, min_interval=settings.gdelt_min_interval)
        try:
            source = GdeltSource(client, group_size=settings.gdelt_group_size)
            return ingest.run_ingest(
                conn,
                source,
                tagger,
                backfill=timedelta(hours=settings.ingest_backfill_hours),
            )
        finally:
            client.close()
            conn.close()


def retag(settings: Settings) -> int:
    with ingest.ingest_lock(settings.lock_path):
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
    return ApiClient(settings.user_agent, min_interval=settings.wikidata_min_interval)


def resolve_ownership(
    settings: Settings, domains: list[str] | None = None, *, rematch: bool = True
) -> ownership.ResolveSummary:
    """Match and resolve outlets that are due (or the given domains), then refresh logos."""
    now = datetime.now(UTC).replace(microsecond=0)
    client = wikidata_client(settings)
    with ingest.ingest_lock(settings.lock_path):
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


def refresh_funding(settings: Settings, *, force: bool = False) -> funding.FundingSummary:
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
    with ingest.ingest_lock(settings.lock_path):
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
