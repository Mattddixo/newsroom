"""Job bodies shared by the worker schedule and the CLI."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from newsroom.config import load_outlets, load_tags
from newsroom.db import connect
from newsroom.net.http import ApiClient
from newsroom.services import ingest
from newsroom.services.tagging import Tagger, retag_all, sync_tags
from newsroom.settings import Settings
from newsroom.sources.gdelt import GdeltSource

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
