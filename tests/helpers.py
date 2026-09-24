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
    TagConfig("housing", "Housing", tuple(map(normalize_text, ["housing", "logement", "rent"]))),
    TagConfig("elections", "Elections", tuple(map(normalize_text, ["election", "élection"]))),
    TagConfig("economy", "Economy", tuple(map(normalize_text, ["interest rate", "inflation"]))),
]


def make_db(path: Path, now: datetime) -> sqlite3.Connection:
    conn = connect(path)
    migrate(conn)
    sync_outlets(conn, OUTLETS, now)
    conn.execute("BEGIN")
    sync_tags(conn, TAGS)
    conn.execute("COMMIT")
    return conn


def rec(url: str, title: str, when: datetime, domain: str = "cbc.ca") -> ArticleRecord:
    return ArticleRecord(
        url=url, title=title, domain=domain, published_at=when, language="en", image_url=None
    )


class FakeSource:
    name = "fake"

    def __init__(self, results: list[QueryResult] | None = None, explode: bool = False) -> None:
        self.results = results or []
        self.explode = explode
        self.calls: list[tuple[datetime, datetime]] = []

    def fetch(
        self, domains: Sequence[str], start: datetime, end: datetime
    ) -> Iterator[QueryResult]:
        self.calls.append((start, end))
        for i, r in enumerate(self.results):
            if self.explode and i == 1:
                raise RuntimeError("boom")
            yield r
