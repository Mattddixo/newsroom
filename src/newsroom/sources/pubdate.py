"""Publication dates from an article page's own metadata.

GDELT only says when it first *saw* an article. The outlet's publication time is in the
page's metadata; this module reads it, and nothing else, from the start of the page.

Sources, in priority order (the first valid one wins):
  1. schema.org JSON-LD `datePublished` on an Article-type object (NewsArticle, ...)
  2. <meta property="article:published_time"> (Open Graph)
  3. <meta itemprop="datePublished"> (schema.org microdata)
  4. other common tags: parsely-pub-date, sailthru.date, pubdate, publishdate,
     dc.date.issued, dcterms.created
  5. JSON-LD `datePublished` on any object

A value is only accepted if it has a time zone (otherwise the instant is ambiguous),
is not after GDELT saw the article (+1 h for clock skew), and is not before 1995.
"""

from __future__ import annotations

import contextlib
import json
import re
import time
import urllib.robotparser
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser

from newsroom.net.safe_fetch import FetchBlocked
from newsroom.urls import host_of

EARLIEST = datetime(1995, 1, 1, tzinfo=UTC)
FUTURE_SKEW = timedelta(hours=1)
ARTICLE_TYPES = re.compile(r"(Article|BlogPosting|Report)$")
META_NAMES = (
    "parsely-pub-date",
    "sailthru.date",
    "pubdate",
    "publishdate",
    "dc.date.issued",
    "dcterms.created",
)
_OFFSET_NO_COLON = re.compile(r"([+-]\d{2})(\d{2})$")


class _MetaParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.jsonld: list[str] = []
        self._in_jsonld = False
        self._buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "meta":
            key = (a.get("property") or a.get("name") or a.get("itemprop") or "").lower()
            if key and "content" in a:
                self.meta.setdefault(key, a["content"])
            if a.get("itemprop", "").lower() == "datepublished" and "content" in a:
                self.meta.setdefault("itemprop:datepublished", a["content"])
        elif tag == "script" and a.get("type", "").lower() == "application/ld+json":
            self._in_jsonld = True
            self._buf = []

    def handle_data(self, data: str) -> None:
        if self._in_jsonld:
            self._buf.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._in_jsonld:
            self.jsonld.append("".join(self._buf))
            self._in_jsonld = False


def parse_timestamp(value: object) -> datetime | None:
    """ISO 8601 with a time zone, or None."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}", text):
        return None  # date-only or free text: no reliable instant
    text = _OFFSET_NO_COLON.sub(r"\1:\2", text.replace("Z", "+00:00"))
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else None


def _walk(node: object) -> Iterator[dict]:
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def _types(obj: dict) -> list[str]:
    t = obj.get("@type", [])
    return [t] if isinstance(t, str) else [x for x in t if isinstance(x, str)]


def candidates(html: str) -> Iterator[tuple[str, str]]:
    """(method, raw value) pairs in priority order."""
    parser = _MetaParser()
    with contextlib.suppress(Exception):  # malformed markup: use whatever was collected
        parser.feed(html)
        parser.close()
    objects: list[dict] = []
    for block in parser.jsonld:
        try:
            objects.extend(_walk(json.loads(block, strict=False)))
        except ValueError:
            continue
    for obj in objects:
        if "datePublished" in obj and any(ARTICLE_TYPES.search(t) for t in _types(obj)):
            yield "schema.org datePublished", obj["datePublished"]
    if "article:published_time" in parser.meta:
        yield "article:published_time", parser.meta["article:published_time"]
    if "itemprop:datepublished" in parser.meta:
        yield "itemprop datePublished", parser.meta["itemprop:datepublished"]
    for name in META_NAMES:
        if name in parser.meta:
            yield f"meta {name}", parser.meta[name]
    for obj in objects:
        if "datePublished" in obj:
            yield "schema.org datePublished (untyped)", obj["datePublished"]


@dataclass(frozen=True)
class PublishedDate:
    when: datetime  # UTC
    method: str


def extract(html: str, seen: datetime) -> PublishedDate | None:
    """The first candidate that is a plausible publication time for an article GDELT saw
    at `seen`."""
    for method, raw in candidates(html):
        when = parse_timestamp(raw)
        if when and EARLIEST <= when <= seen + FUTURE_SKEW:
            return PublishedDate(when, method)
    return None


# ------------------------------------------------------------------ robots.txt


class Robots:
    """robots.txt per host, cached for a day, following RFC 9309:
    200 -> obey the rules; 4xx -> no rules (allowed); 5xx or unreachable -> disallowed."""

    TTL = 24 * 3600

    def __init__(
        self,
        fetch_text: Callable[[str], str],
        agent: str = "newsroom",
        clock: Callable[[], float] = time.monotonic,
        cache: dict[str, tuple[float, urllib.robotparser.RobotFileParser | bool]] | None = None,
    ) -> None:
        self._fetch = fetch_text
        self.agent = agent
        self._clock = clock
        # Pass a shared dict to keep rules across runs (the worker is long-lived).
        self._cache = cache if cache is not None else {}

    def allowed(self, url: str) -> bool:
        host = host_of(url)
        cached = self._cache.get(host)
        if cached is None or self._clock() - cached[0] > self.TTL:
            cached = (self._clock(), self._load(host))
            self._cache[host] = cached
        rules = cached[1]
        if isinstance(rules, bool):
            return rules
        return rules.can_fetch(self.agent, url)

    def _load(self, host: str) -> urllib.robotparser.RobotFileParser | bool:
        try:
            text = self._fetch(f"https://{host}/robots.txt")
        except FetchBlocked as exc:
            client_error = exc.status is not None and 400 <= exc.status < 500
            # Something other than text/plain (often an HTML page) holds no usable rules.
            not_robots = exc.status is None and "content-type" in str(exc)
            return client_error or not_robots
        parser = urllib.robotparser.RobotFileParser()
        parser.parse(text.splitlines())
        return parser
