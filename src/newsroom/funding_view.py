"""Read-only funding queries, shared by the web pages and the CLI."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Sequence

KIND_ORDER = {
    "government_appropriation": 0,
    "nonprofit_revenue": 1,
    "grant": 2,
    "public_filing": 3,
    "charity_registration": 4,
}


def records(conn: sqlite3.Connection, entity_ids: Sequence[int]) -> list[sqlite3.Row]:
    """Funding rows for these entities, grouped in the given order (outlet first, then owners)."""
    if not entity_ids:
        return []
    marks = ",".join("?" * len(entity_ids))
    rows = conn.execute(
        f"SELECT * FROM funding_records WHERE entity_id IN ({marks})",  # noqa: S608
        list(entity_ids),
    ).fetchall()
    position = {eid: i for i, eid in enumerate(entity_ids)}
    return sorted(
        rows,
        key=lambda r: (
            position[r["entity_id"]],
            -_year(r["period"]),
            KIND_ORDER.get(r["kind"], 9),
            r["label"],
        ),
    )


def _year(period: str | None) -> int:
    """Newest first: sort by the first 4-digit year in the period text."""
    m = re.search(r"\d{4}", period or "")
    return int(m.group(0)) if m else 0
