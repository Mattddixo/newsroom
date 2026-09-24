"""Funding records: fetched per public identifier, plus the curated YAML file.

Like ownership, each identifier's data is fetched first and then written in one
transaction that replaces that entity's rows from that source.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from newsroom.config import CuratedFunding
from newsroom.net.http import ApiError
from newsroom.services.ingest import ts
from newsroom.sources.funding import FundingRecord, normalize_id, registry_url

log = logging.getLogger(__name__)

SCHEME_SOURCE = {"sec_cik": "sec_edgar", "us_ein": "propublica", "ca_bn": "cra"}

# scheme -> function(identifier value) -> records
Fetchers = dict[str, Callable[[str], list[FundingRecord]]]


@dataclass
class FundingSummary:
    checked: int = 0
    failed: int = 0
    records: int = 0
    curated: int = 0
    curated_skipped: int = 0


def _replace(
    conn: sqlite3.Connection,
    entity_id: int,
    source: str,
    records: Sequence[FundingRecord],
    stamp: str,
) -> None:
    conn.execute(
        "DELETE FROM funding_records WHERE entity_id = ? AND source = ?", (entity_id, source)
    )
    conn.executemany(
        "INSERT INTO funding_records (entity_id, kind, label, amount, currency, period, funder,"
        " source, source_url, retrieved_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                entity_id,
                r.kind,
                r.label,
                r.amount,
                r.currency,
                r.period,
                r.funder,
                r.source,
                r.source_url,
                stamp,
            )
            for r in records
        ],
    )


def refresh_funding(
    conn: sqlite3.Connection,
    fetchers: Fetchers,
    now: datetime,
    refresh: timedelta,
    force: bool = False,
) -> FundingSummary:
    summary = FundingSummary()
    stamp = ts(now)
    due = conn.execute(
        "SELECT entity_id, scheme, value FROM entity_identifiers"
        " WHERE ? OR funding_checked_at IS NULL OR funding_checked_at < ?"
        " ORDER BY entity_id, scheme",
        (int(force), ts(now - refresh)),
    ).fetchall()
    for row in due:
        fetch = fetchers.get(row["scheme"])
        if fetch is None:
            continue
        try:
            records = fetch(row["value"])
        except (ApiError, ValueError) as exc:
            summary.failed += 1
            log.warning(
                "funding lookup failed",
                extra={"scheme": row["scheme"], "value": row["value"], "error": str(exc)},
            )
            continue
        conn.execute("BEGIN IMMEDIATE")
        try:
            _replace(conn, row["entity_id"], SCHEME_SOURCE[row["scheme"]], records, stamp)
            conn.execute(
                "UPDATE entity_identifiers SET funding_checked_at = ?"
                " WHERE entity_id = ? AND scheme = ? AND value = ?",
                (stamp, row["entity_id"], row["scheme"], row["value"]),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        summary.checked += 1
        summary.records += len(records)
    # Records whose identifier has since been removed are dropped.
    conn.execute(
        "DELETE FROM funding_records WHERE source IN ('sec_edgar', 'propublica', 'cra')"
        " AND NOT EXISTS (SELECT 1 FROM entity_identifiers i WHERE i.entity_id ="
        " funding_records.entity_id AND ("
        " (i.scheme = 'sec_cik' AND funding_records.source = 'sec_edgar') OR"
        " (i.scheme = 'us_ein' AND funding_records.source = 'propublica') OR"
        " (i.scheme = 'ca_bn' AND funding_records.source = 'cra')))"
    )
    return summary


def sync_curated(
    conn: sqlite3.Connection, entries: Sequence[CuratedFunding], summary: FundingSummary
) -> None:
    """Make the curated rows mirror config/public_funding.yaml."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM funding_records WHERE source = 'curated'")
        for e in entries:
            if e.qid:
                row = conn.execute("SELECT id FROM entities WHERE qid = ?", (e.qid,)).fetchone()
            else:
                row = conn.execute(
                    "SELECT entity_id AS id FROM outlets WHERE domain = ?", (e.outlet,)
                ).fetchone()
            if row is None or row["id"] is None:
                summary.curated_skipped += 1
                log.warning(
                    "curated funding not attached: outlet/item has no resolved Wikidata entity",
                    extra={"outlet": e.outlet, "qid": e.qid},
                )
                continue
            conn.execute(
                "INSERT INTO funding_records (entity_id, kind, label, amount, currency, period,"
                " funder, source, source_url, retrieved_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 'curated', ?, ?)",
                (
                    row["id"],
                    e.kind,
                    e.label,
                    e.amount,
                    e.currency,
                    e.period,
                    e.funder,
                    e.source_url,
                    f"{e.retrieved}T00:00:00Z",
                ),
            )
            summary.curated += 1
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def set_identifier(
    conn: sqlite3.Connection, qid: str, scheme: str, value: str, now: datetime
) -> str:
    """Record an identifier by hand (e.g. a CRA business number). Its source is the
    registry's own public page for that identifier."""
    if scheme not in SCHEME_SOURCE:
        raise ValueError(f"scheme must be one of {', '.join(SCHEME_SOURCE)}")
    value = normalize_id(scheme, value)
    row = conn.execute("SELECT id FROM entities WHERE qid = ?", (qid,)).fetchone()
    if row is None:
        raise LookupError(f"{qid} is not in the database (resolve its outlet's ownership first)")
    conn.execute(
        "INSERT INTO entity_identifiers (entity_id, scheme, value, source, source_url,"
        " retrieved_at) VALUES (?, ?, ?, 'manual', ?, ?)"
        " ON CONFLICT (entity_id, scheme, value) DO UPDATE SET funding_checked_at = NULL",
        (row["id"], scheme, value, registry_url(scheme, value), ts(now)),
    )
    return value


def remove_identifier(conn: sqlite3.Connection, qid: str, scheme: str, value: str) -> int:
    value = normalize_id(scheme, value)
    return conn.execute(
        "DELETE FROM entity_identifiers WHERE source = 'manual' AND scheme = ? AND value = ?"
        " AND entity_id = (SELECT id FROM entities WHERE qid = ?)",
        (scheme, value, qid),
    ).rowcount
