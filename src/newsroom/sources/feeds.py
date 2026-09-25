"""Outlets' own RSS / Atom feeds: a second article source, for outlets GDELT doesn't carry.

Feeds are the publisher's own, machine-readable list of recent headlines. We take only
what the rest of the app takes from GDELT: headline, link, and time. A feed's
publication time is the outlet's own statement, so it's used as the article's
publication date (no page visit needed), with the same plausibility checks.

Formats: RSS 2.0 (<item>), RSS 1.0 / RDF (<item>, dc:date) and Atom (<entry>).
Parsing uses defusedxml: no entity expansion, no external entities, no DTD tricks.
Only items that link to the outlet's own site (its domain or `also:` domains) are kept.
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import urljoin
from xml.etree.ElementTree import Element

import defusedxml.ElementTree as SafeET
from defusedxml import DefusedXmlException

from newsroom.net.http import ApiError
from newsroom.sources.base import ArticleRecord
from newsroom.sources.gdelt import clean_title
from newsroom.sources.gdelt_files import DomainMatcher
from newsroom.sources.pubdate import EARLIEST, FUTURE_SKEW, parse_timestamp
from newsroom.urls import host_of, is_http_url

FEED_TYPES = frozenset(
    {
        "application/rss+xml",
        "application/atom+xml",
        "application/rdf+xml",
        "application/xml",
        "text/xml",
    }
)
MAX_FEED_BYTES = 5 * 1024 * 1024
MAX_ITEMS = 300
_TAGS = re.compile(r"<[^>]+>")
_BARE_AMP = re.compile(rb"&(?!(?:#[0-9]+|#x[0-9A-Fa-f]+|[A-Za-z][A-Za-z0-9]*);)")
# Tried when a homepage declares no feed: the usual addresses on common news platforms
# (WordPress, Arc XP, ...). A guess only counts if it serves a current feed of the
# outlet's own articles.
COMMON_FEED_PATHS = (
    "/feed/",
    "/rss",
    "/rss.xml",
    "/feed",
    "/rss/",
    "/feeds/rss.xml",
    "/index.xml",
    "/arc/outboundfeeds/rss/?outputType=xml",
)


def _local(tag: object) -> str:
    """Element name without its namespace: {http://www.w3.org/2005/Atom}entry -> entry."""
    return tag.rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""


def _child(el: Element, name: str) -> Element | None:
    return next((c for c in el if _local(c.tag) == name), None)


def _text(el: Element | None) -> str:
    return "".join(el.itertext()).strip() if el is not None else ""


def parse_feed_date(value: str) -> datetime | None:
    """RFC 822 (RSS pubDate) or ISO 8601 (Atom, dc:date), with a time zone; else None."""
    value = value.strip()
    if not value:
        return None
    iso = parse_timestamp(value)
    if iso:
        return iso
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if parsed is None or parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


@dataclass
class FeedItem:
    title: str
    link: str
    published: datetime | None
    date_field: str | None  # which element the date came from


def _item_link(el: Element, atom: bool, base: str) -> str:
    if atom:
        links = [c for c in el if _local(c.tag) == "link"]
        for c in links:
            if c.get("rel", "alternate") == "alternate" and c.get("href"):
                return urljoin(base, c.get("href", "").strip())
        return ""
    link = _text(_child(el, "link"))
    if not link:  # fall back to a permalink GUID
        guid = _child(el, "guid")
        if guid is not None and guid.get("isPermaLink", "true").lower() != "false":
            link = _text(guid)
    return urljoin(base, link) if link else ""


def _item_date(el: Element, atom: bool) -> tuple[datetime | None, str | None]:
    fields = ("published", "updated") if atom else ("pubdate", "date", "published")
    for name in fields:
        when = parse_feed_date(_text(_child(el, name)))
        if when:
            return when, name
    return None, None


def parse_feed(body: bytes, base_url: str) -> list[FeedItem]:
    """All items in an RSS/Atom document (at most MAX_ITEMS). Raises ApiError if the body
    isn't a feed."""
    try:
        root = SafeET.fromstring(body, forbid_dtd=False)
    except SafeET.ParseError as exc:
        # The most common feed bug: a bare "&" in a title or URL. Escaping bare ones (and
        # nothing else) is what lenient feed readers do; entities stay forbidden.
        repaired = _BARE_AMP.sub(b"&amp;", body)
        if repaired == body:
            raise ApiError(f"not a readable feed: {exc}") from exc
        try:
            root = SafeET.fromstring(repaired, forbid_dtd=False)
        except (SafeET.ParseError, DefusedXmlException, ValueError) as exc2:
            raise ApiError(f"not a readable feed: {exc}") from exc2
    except (DefusedXmlException, ValueError) as exc:
        raise ApiError(f"not a readable feed: {exc}") from exc
    kind = _local(root.tag)
    if kind == "feed":
        atom, entries = True, [c for c in root if _local(c.tag) == "entry"]
    elif kind in ("rss", "rdf"):
        atom = False
        entries = [e for e in root.iter() if _local(e.tag) == "item"]
    else:
        raise ApiError(f"not an RSS or Atom feed (root element <{kind}>)")
    items = []
    for el in entries[:MAX_ITEMS]:
        title = clean_title(_strip_tags(_text(_child(el, "title"))))
        link = _item_link(el, atom, base_url)
        when, field = _item_date(el, atom)
        items.append(FeedItem(title, link, when, field))
    return items


class _Unescape(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _strip_tags(text: str) -> str:
    """Some feeds put HTML in titles (<b>, &amp;). Keep only the text."""
    if "<" not in text and "&" not in text:
        return text
    parser = _Unescape()
    parser.feed(_TAGS.sub(" ", text))
    parser.close()
    return "".join(parser.parts)


@dataclass
class FeedResult:
    records: list[ArticleRecord]
    items: int  # items in the feed
    off_site: int  # items linking somewhere other than the outlet's site
    undated: int  # items without a usable date (they get one from the page later)
    too_old: int  # items older than `max_age`
    newest: datetime | None


def feed_records(
    items: Sequence[FeedItem],
    domain: str,
    aliases: Sequence[str],
    language: str | None,
    now: datetime,
    max_age: timedelta,
) -> FeedResult:
    """Turn feed items into article records for one outlet. The record's first-seen time is
    now; the feed's own time, when plausible, is the publication date."""
    match = DomainMatcher([domain], {domain: list(aliases)})
    records: list[ArticleRecord] = []
    off_site = undated = too_old = 0
    newest: datetime | None = None
    for item in items:
        if not item.title or not is_http_url(item.link):
            continue
        try:
            host = host_of(item.link)
        except ValueError:
            continue
        if match(host) is None:
            off_site += 1
            continue
        published = item.published
        if published and not (EARLIEST <= published <= now + FUTURE_SKEW):
            published = None
        if published is None:
            undated += 1
        elif now - published > max_age:
            too_old += 1
            continue
        if published and (newest is None or published > newest):
            newest = published
        records.append(
            ArticleRecord(
                url=item.link,
                title=item.title,
                domain=domain,
                published_at=now,
                language=language,
                image_url=None,
                outlet_published_at=published,
                pubdate_method=f"feed {item.date_field}" if published else None,
            )
        )
    return FeedResult(records, len(items), off_site, undated, too_old, newest)


# ------------------------------------------------------------------ autodiscovery


class _AlternateLinks(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []  # (href, title)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "link":
            return
        a = {k.lower(): (v or "") for k, v in attrs}
        rels = a.get("rel", "").lower().split()
        kind = a.get("type", "").lower()
        if "alternate" in rels and kind in FEED_TYPES and a.get("href"):
            self.links.append((a["href"], a.get("title", "")))


def discover_feeds(html: str, page_url: str) -> list[tuple[str, str]]:
    """Feeds a page declares for itself (<link rel="alternate" type="application/rss+xml">),
    as absolute (url, title) pairs in page order."""
    parser = _AlternateLinks()
    with contextlib.suppress(Exception):  # malformed markup: use what was collected
        parser.feed(html)
        parser.close()
    out: list[tuple[str, str]] = []
    for href, title in parser.links:
        url = urljoin(page_url, href.strip())
        if _is_comments_feed(url, title):
            continue
        if is_http_url(url) and url not in [u for u, _ in out]:
            out.append((url, title))
    return out


def _is_comments_feed(url: str, title: str) -> bool:
    """WordPress and others declare a comments feed next to the articles one."""
    return "/comments/" in url or "comments feed" in title.lower()
