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


@dataclass
class QueryResult:
    """One API request's worth of records. Ingestion commits each result separately,
    so a failure part-way through a run keeps everything fetched before it."""

    source_url: str
    records: list[ArticleRecord] = field(default_factory=list)
    error: str | None = None


class ArticleSource(Protocol):
    name: str

    def fetch(
        self, domains: Sequence[str], start: datetime, end: datetime
    ) -> Iterator[QueryResult]:
        """Yield results for articles from `domains` first seen in [start, end)."""
        ...
