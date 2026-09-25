"""Common interface for source adapters. One module per source."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class ArticleRecord:
    url: str
    title: str
    domain: str
    published_at: datetime  # timezone-aware UTC
    language: str | None
    image_url: str | None
    # The outlet's own publication time, when the source states it (e.g. a feed's pubDate).
    outlet_published_at: datetime | None = None
    pubdate_method: str | None = None
    # GDELT's theme coding of the article text: (theme, mentions). Empty for other sources.
    themes: tuple[tuple[str, int], ...] = ()


@dataclass
class QueryResult:
    """One API request's worth of records. Ingestion commits each result separately,
    so a failure part-way through a run keeps everything fetched before it."""

    source_url: str
    records: list[ArticleRecord] = field(default_factory=list)
    error: str | None = None
    # Everything up to here is now in, for this group (sources fetch oldest-first).
    window_end: datetime | None = None
    throttled: bool = False  # the API kept refusing: stop the run, try again next time


class ArticleSource(Protocol):
    name: str

    def groups(self, domains: Sequence[str]) -> list[list[str]]:
        """How domains are batched into requests. Ingestion tracks progress per group."""
        ...

    def fetch(
        self, domains: Sequence[str], start: datetime, end: datetime
    ) -> Iterator[QueryResult]:
        """Yield results for articles from `domains` first seen in [start, end), oldest
        time slice first. Ingestion stops a group at its first error."""
        ...
