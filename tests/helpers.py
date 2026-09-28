from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from datetime import datetime
from pathlib import Path

from newsroom.config import OutletConfig, TagConfig, normalize_text
from newsroom.db import connect
from newsroom.migrate import migrate
from newsroom.services.ingest import sync_outlets
from newsroom.services.tagging import sync_tags
from newsroom.sources.base import ArticleRecord, QueryResult

OUTLETS = [
    OutletConfig("cbc.ca", "CBC News", "CA", "en"),
    OutletConfig("radio-canada.ca", "Radio-Canada", "CA", "fr"),
    OutletConfig("nytimes.com", "The New York Times", "US", "en"),
]

TAGS = [
    TagConfig("housing", "Housing", ("ECON_HOUSING_PRICES",), ("housing", "logement")),
    TagConfig("elections", "Elections", ("ELECTION",), ("elections",)),
    TagConfig("economy", "Economy", ("ECON_INFLATION",), (normalize_text("économie"),)),
]


def make_db(path: Path, now: datetime) -> sqlite3.Connection:
    conn = connect(path)
    migrate(conn)
    sync_outlets(conn, OUTLETS, now)
    conn.execute("BEGIN")
    sync_tags(conn, TAGS)
    conn.execute("COMMIT")
    return conn


ENTITY_STAMP = "2026-09-24T12:00:00Z"


def add_entity(conn: sqlite3.Connection, qid: str, name: str, *aliases: str, kind: str = "") -> int:
    conn.execute(
        "INSERT INTO entities (qid, name, kind, source, source_url, retrieved_at)"
        " VALUES (?, ?, ?, 'wikidata', ?, ?)",
        (qid, name, kind, f"https://www.wikidata.org/wiki/{qid}", ENTITY_STAMP),
    )
    eid = conn.execute("SELECT id FROM entities WHERE qid = ?", (qid,)).fetchone()[0]
    conn.executemany(
        "INSERT INTO entity_aliases (entity_id, alias) VALUES (?, ?)", [(eid, a) for a in aliases]
    )
    return eid


def add_edge(conn: sqlite3.Connection, child: int, parent: int, source: str = "wikidata") -> None:
    conn.execute(
        "INSERT INTO ownership_edges (child_entity_id, parent_entity_id, relation, source,"
        " source_url, retrieved_at) VALUES (?, ?, 'owned_by', ?, 'https://example.org', ?)",
        (child, parent, source, ENTITY_STAMP),
    )


def rec(
    url: str,
    title: str,
    when: datetime,
    domain: str = "cbc.ca",
    themes: tuple[tuple[str, int], ...] = (),
) -> ArticleRecord:
    return ArticleRecord(
        url=url,
        title=title,
        domain=domain,
        published_at=when,
        language="en",
        image_url=None,
        themes=themes,
    )


class FakeSource:
    name = "fake"

    def __init__(
        self,
        results: list[QueryResult] | None = None,
        explode: bool = False,
        group_size: int | None = None,
    ) -> None:
        self.results = results or []
        self.explode = explode
        self.group_size = group_size
        self.calls: list[tuple[datetime, datetime]] = []

    def groups(self, domains: Sequence[str]) -> list[list[str]]:
        ordered = sorted(domains)
        size = self.group_size or len(ordered) or 1
        return [ordered[i : i + size] for i in range(0, len(ordered), size)]

    def fetch(
        self, domains: Sequence[str], start: datetime, end: datetime
    ) -> Iterator[QueryResult]:
        self.calls.append((start, end))
        for i, r in enumerate(self.results):
            if self.explode and i == 1:
                raise RuntimeError("boom")
            yield r
