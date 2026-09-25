"""Publication dates from an article page's own metadata.

GDELT only says when it first *saw* an article. The outlet's publication time is in the
page's metadata; this module reads it, and nothing else, from the start of the page.

Sources, in priority order (the first valid one wins):
  1. schema.org JSON-LD `datePublished` on an Article-type object (NewsArticle, ...)
  2. <meta property="article:published_time"> (Open Graph)
  3. itemprop="datePublished" on <meta content> or <time datetime> (schema.org microdata)
  4. other common tags: parsely-pub-date, sailthru.date, pubdate, publishdate,
     dc.date.issued, dcterms.created
  5. JSON-LD `datePublished` on any object

A value is only accepted if it has a time zone (otherwise the instant is ambiguous),
is not after GDELT saw the article (+1 h for clock skew), and is not before 1995.

If the page's own tags for the article disagree by a whole number of hours (18:50-04:00
in one, 18:50Z in another), the page gets a time zone wrong and there's no telling which
tag is right, so the page counts as giving no date.
"""

from __future__ import annotations

import contextlib
import json
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from urllib.parse import urlsplit

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
        if a.get("itemprop", "").lower() == "datepublished":
            # schema.org microdata on any element: <meta content>, <time datetime>, ...
            value = a.get("content") or a.get("datetime")
            if value:
                self.meta.setdefault("itemprop:datepublished", value)
        if tag == "meta":
            key = (a.get("property") or a.get("name") or "").lower()
            if key and "content" in a:
                self.meta.setdefault(key, a["content"])
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
    for key in ("article:published_time", "og:article:published_time"):
        if key in parser.meta:
            yield "article:published_time", parser.meta[key]
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


@dataclass(frozen=True)
class DateConflict:
    """Two of the page's tags for the article differ by whole hours: a time zone error."""

    first: tuple[str, datetime]
    second: tuple[str, datetime]


# Tags that describe the article itself. The untyped JSON-LD fallback is left out: it can
# belong to anything on the page (a related article, the site).
_OWN_TAGS = frozenset(
    {"schema.org datePublished", "article:published_time", "itemprop datePublished"}
    | {f"meta {name}" for name in META_NAMES}
)
MAX_ZONE_OFFSET = timedelta(hours=14)


def zone_conflict(values: list[tuple[str, datetime]]) -> DateConflict | None:
    """The first pair of values that differ by a whole number of hours (up to 14, the
    largest UTC offset): the same wall-clock time read in two different time zones."""
    for i, (method_a, a) in enumerate(values):
        for method_b, b in values[i + 1 :]:
            gap = abs(a - b)
            if timedelta(0) < gap <= MAX_ZONE_OFFSET and gap % timedelta(hours=1) == timedelta(0):
                return DateConflict((method_a, a), (method_b, b))
    return None


def extract(html: str, seen: datetime) -> PublishedDate | DateConflict | None:
    """The first candidate that is a plausible publication time for an article GDELT saw
    at `seen`; a DateConflict if the page's own tags contradict each other."""
    found: PublishedDate | None = None
    own: dict[str, datetime] = {}  # first value of each of the article's own tags
    for method, raw in candidates(html):
        when = parse_timestamp(raw)
        if when is None:
            continue
        if method in _OWN_TAGS:
            own.setdefault(method, when)
        if found is None and EARLIEST <= when <= seen + FUTURE_SKEW:
            found = PublishedDate(when, method)
    if found is None:
        return None
    return zone_conflict(list(own.items())) or found


# ------------------------------------------------------------------ robots.txt


class RobotsRules:
    """One site's robots.txt, matched as RFC 9309 says: use the group(s) naming our
    product token, else the "*" group(s); the longest matching rule wins; on a tie Allow
    wins; "*" matches any characters and a trailing "$" anchors the end.
    (Python's urllib.robotparser uses the first matching rule and has no wildcards.)"""

    def __init__(self, text: str, agent: str) -> None:
        agent = agent.lower()
        groups: list[tuple[list[str], list[tuple[bool, str]]]] = []
        agents: list[str] = []
        rules: list[tuple[bool, str]] = []
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            key, value = (part.strip() for part in line.split(":", 1))
            key = key.lower()
            if key == "user-agent":
                if rules:  # a user-agent line after rules starts a new group
                    groups.append((agents, rules))
                    agents, rules = [], []
                agents.append(value.lower())
            elif key in ("allow", "disallow") and agents:
                if value:  # an empty Disallow means "nothing disallowed"
                    rules.append((key == "allow", value))
        if agents:
            groups.append((agents, rules))
        mine = [r for a, r in groups if agent in a]
        chosen = mine or [r for a, r in groups if "*" in a]
        self.rules = [rule for group in chosen for rule in group]

    @staticmethod
    def _matches(pattern: str, path: str) -> bool:
        anchored = pattern.endswith("$")
        body = pattern[:-1] if anchored else pattern
        regex = ".*".join(re.escape(part) for part in body.split("*"))
        return re.match(regex + ("$" if anchored else ""), path) is not None

    def allows(self, url: str) -> bool:
        parts = urlsplit(url)
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        if path == "/robots.txt":
            return True
        best: tuple[int, bool] | None = None  # (pattern length, allow)
        for allow, pattern in self.rules:
            if self._matches(pattern, path):
                candidate = (len(pattern), allow)
                if best is None or candidate > best:  # longer wins; Allow wins ties
                    best = candidate
        return best is None or best[1]


ALLOWED, DISALLOWED, UNAVAILABLE = "allowed", "disallowed", "unavailable"


class Robots:
    """robots.txt per host, following RFC 9309. 200: obey the rules (cached a day).
    4xx, or something that isn't a robots file: no rules, allowed (cached a day).
    5xx or unreachable: "unavailable" - stay away for now, but that's not a verdict on
    the articles; asked again after an hour."""

    TTL = 24 * 3600
    RETRY_UNAVAILABLE = 3600

    def __init__(
        self,
        fetch_text: Callable[[str], str],
        agent: str = "newsroom",
        clock: Callable[[], float] = time.monotonic,
        cache: dict[str, tuple[float, RobotsRules | str]] | None = None,
    ) -> None:
        self._fetch = fetch_text
        self.agent = agent
        self._clock = clock
        # Pass a shared dict to keep rules across runs (the worker is long-lived).
        self._cache = cache if cache is not None else {}

    def check(self, url: str) -> str:
        """ALLOWED, DISALLOWED, or UNAVAILABLE (robots.txt couldn't be read just now)."""
        host = host_of(url)
        cached = self._cache.get(host)
        if cached is not None:
            ttl = self.RETRY_UNAVAILABLE if cached[1] == UNAVAILABLE else self.TTL
            if self._clock() - cached[0] > ttl:
                cached = None
        if cached is None:
            cached = (self._clock(), self._load(host))
            self._cache[host] = cached
        rules = cached[1]
        if isinstance(rules, str):
            return rules
        return ALLOWED if rules.allows(url) else DISALLOWED

    def allowed(self, url: str) -> bool:
        return self.check(url) == ALLOWED

    def _load(self, host: str) -> RobotsRules | str:
        try:
            text = self._fetch(f"https://{host}/robots.txt")
        except FetchBlocked as exc:
            if exc.status is not None and 400 <= exc.status < 500:
                return ALLOWED  # no robots.txt: no rules
            if exc.status is None and "content-type" in str(exc):
                return ALLOWED  # an HTML page or similar: holds no rules
            return UNAVAILABLE
        return RobotsRules(text, self.agent)
