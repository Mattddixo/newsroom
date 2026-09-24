"""GDELT DOC 2.0 API adapter (https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/).

Free, no key. Notes that shape this adapter:
- Every call needs a query and returns at most 250 articles, so outlets are
  queried in small OR-groups and a saturated time window is split in half.
- GDELT asks for no more than one request every 5 seconds; we pace at 6.
- Errors and rate-limit notices arrive as HTTP 200 with a plain-text body.
- "seendate" is when GDELT first saw the article (usually minutes after publication).
"""

from __future__ import annotations

import html
import json
import logging
import re
import unicodedata
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import httpx

from newsroom.net.http import ApiClient, ApiError, RateLimited
from newsroom.sources.base import ArticleRecord, QueryResult
from newsroom.urls import host_matches, host_of, is_http_url

log = logging.getLogger(__name__)

ENDPOINT = "https://api.gdeltproject.org/api/v2/doc/doc"
MAX_RECORDS = 250
MIN_SPLIT = timedelta(minutes=15)
MAX_TITLE = 500
_DOMAIN_RE = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)+$")

LANGUAGES = {
    "english": "en",
    "french": "fr",
    "spanish": "es",
    "german": "de",
    "portuguese": "pt",
    "italian": "it",
    "chinese": "zh",
    "arabic": "ar",
}


def build_query(domains: Sequence[str]) -> str:
    for d in domains:
        if not _DOMAIN_RE.match(d):
            raise ValueError(f"invalid domain for GDELT query: {d!r}")
    terms = [f"domain:{d}" for d in domains]
    return terms[0] if len(terms) == 1 else "(" + " OR ".join(terms) + ")"


def build_url(query: str, start: datetime, end: datetime) -> str:
    params = {
        "query": query,
        "mode": "artlist",
        "format": "json",
        "maxrecords": MAX_RECORDS,
        "sort": "datedesc",
        "startdatetime": start.astimezone(UTC).strftime("%Y%m%d%H%M%S"),
        "enddatetime": end.astimezone(UTC).strftime("%Y%m%d%H%M%S"),
    }
    return f"{ENDPOINT}?{urlencode(params)}"


def check_response(response: httpx.Response) -> None:
    """GDELT reports throttling as a 200 with a text message."""
    head = response.text[:300].lower()
    if "limit requests" in head or "rate limit" in head:
        raise RateLimited(response.text[:120].strip())


def parse_seendate(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return None


def clean_title(value: object) -> str:
    if not isinstance(value, str):
        return ""
    text = html.unescape(value)
    text = "".join(" " if unicodedata.category(c).startswith("C") else c for c in text)
    title = " ".join(text.split())
    return title[:MAX_TITLE]


def parse_articles(body: str, domains: Sequence[str]) -> tuple[list[ArticleRecord], int]:
    """Parse an artlist JSON body. Returns (valid records, raw article count).

    Raises ApiError if the body is not the expected JSON (GDELT error text).
    """
    text = body.strip()
    if not text.startswith("{"):
        raise ApiError(f"GDELT returned non-JSON: {text[:160]!r}")
    try:
        data = json.loads(text, strict=False)  # GDELT sometimes emits raw control chars
    except json.JSONDecodeError as exc:
        raise ApiError(f"GDELT returned malformed JSON: {exc}") from exc
    raw = data.get("articles") or []
    if not isinstance(raw, list):
        raise ApiError("GDELT 'articles' is not a list")

    records: list[ArticleRecord] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        if not isinstance(url, str) or not is_http_url(url):
            continue
        host = host_of(url)
        domain = next((d for d in domains if host_matches(host, d)), None)
        if domain is None:  # "domain:" is a substring match; keep only exact outlets
            continue
        title = clean_title(item.get("title"))
        seen = parse_seendate(item.get("seendate", ""))
        if not title or seen is None:
            continue
        image = item.get("socialimage")
        language = item.get("language")
        records.append(
            ArticleRecord(
                url=url,
                title=title,
                domain=domain,
                published_at=seen,
                language=LANGUAGES.get(language.lower()) if isinstance(language, str) else None,
                image_url=image if isinstance(image, str) and is_http_url(image) else None,
            )
        )
    return records, len(raw)


class GdeltSource:
    name = "gdelt"

    def __init__(self, client: ApiClient, group_size: int = 8) -> None:
        self.client = client
        self.group_size = group_size

    def fetch(
        self, domains: Sequence[str], start: datetime, end: datetime
    ) -> Iterator[QueryResult]:
        ordered = sorted(domains)
        for i in range(0, len(ordered), self.group_size):
            group = ordered[i : i + self.group_size]
            yield from self._window(group, start, end)

    def _window(
        self, group: Sequence[str], start: datetime, end: datetime, depth: int = 0
    ) -> Iterator[QueryResult]:
        url = build_url(build_query(group), start, end)
        try:
            body = self.client.get(url, check=check_response).text
            records, raw_count = parse_articles(body, group)
        except ApiError as exc:
            log.warning("gdelt query failed", extra={"domains": list(group), "error": str(exc)})
            yield QueryResult(source_url=url, error=str(exc))
            return
        if raw_count >= MAX_RECORDS and end - start > MIN_SPLIT and depth < 8:
            mid = start + (end - start) / 2
            yield from self._window(group, start, mid, depth + 1)
            yield from self._window(group, mid, end, depth + 1)
            return
        if raw_count >= MAX_RECORDS:
            log.warning(
                "gdelt window still saturated; some articles may be missed",
                extra={"domains": list(group), "start": start.isoformat()},
            )
        yield QueryResult(source_url=url, records=records)
