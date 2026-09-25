"""Outlets' own RSS/Atom feeds: parsing, safety, storage and the feed check."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from newsroom.config import OutletConfig
from newsroom.net.http import ApiError
from newsroom.net.safe_fetch import FetchBlocked, FetchResult
from newsroom.services.feed_ingest import run_feeds
from newsroom.services.ingest import run_ingest, sync_outlets
from newsroom.services.tagging import Tagger
from newsroom.settings import Settings
from newsroom.sources.base import QueryResult
from newsroom.sources.feeds import discover_feeds, feed_records, parse_feed, parse_feed_date
from newsroom.sources.pubdate import Robots
from tests.helpers import TAGS, FakeSource, make_db, rec

NOW = datetime(2026, 9, 25, 14, 0, tzinfo=UTC)

RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/">
<channel><title>Example</title>
  <item><title>Council passes &lt;b&gt;housing&lt;/b&gt; plan &amp; budget</title>
    <link>https://www.example.ca/news/1</link>
    <pubDate>Fri, 25 Sep 2026 09:30:00 -0400</pubDate></item>
  <item><title>Sponsored</title><link>https://ads.elsewhere.com/x</link>
    <pubDate>Fri, 25 Sep 2026 12:00:00 GMT</pubDate></item>
  <item><title>No date here</title><guid>https://example.ca/news/2</guid></item>
  <item><title>Naive date</title><link>https://example.ca/news/3</link>
    <pubDate>2026-09-25 11:00</pubDate></item>
  <item><title>From the future</title><link>https://example.ca/news/4</link>
    <pubDate>Mon, 28 Sep 2026 12:00:00 GMT</pubDate></item>
  <item><title>Old news</title><link>https://example.ca/news/5</link>
    <pubDate>Mon, 01 Jan 2024 12:00:00 GMT</pubDate></item>
  <item><title>dc date</title><link>https://example.ca/news/6</link>
    <dc:date>2026-09-25T13:00:00Z</dc:date></item>
</channel></rss>"""

ATOM = b"""<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry><title type="html">Atom &amp;amp; story</title>
    <link rel="alternate" href="/news/a"/><link rel="enclosure" href="/img.jpg"/>
    <published>2026-09-25T09:00:00-04:00</published><updated>2026-09-25T10:00:00Z</updated>
  </entry>
</feed>"""

RDF = b"""<?xml version="1.0"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns="http://purl.org/rss/1.0/"
  xmlns:dc="http://purl.org/dc/elements/1.1/">
  <item><title>RDF story</title><link>https://example.ca/rdf/1</link>
    <dc:date>2026-09-25T08:00:00+00:00</dc:date></item>
</rdf:RDF>"""


def records(body: bytes, base: str = "https://example.ca/feed", aliases=()):  # type: ignore[no-untyped-def]
    items = parse_feed(body, base)
    return feed_records(items, "example.ca", list(aliases), "en", NOW, timedelta(hours=48))


def test_rss_items_dates_and_filtering() -> None:
    got = records(RSS)
    assert got.items == 7
    by_title = {r.title: r for r in got.records}
    story = by_title["Council passes housing plan & budget"]  # HTML stripped, entity decoded
    assert story.url == "https://www.example.ca/news/1"
    assert story.outlet_published_at == datetime(2026, 9, 25, 13, 30, tzinfo=UTC)
    assert story.pubdate_method == "feed pubdate"
    assert story.published_at == NOW  # first seen now; the feed's time is the publication date
    assert got.off_site == 1  # the ad link to another site is dropped
    assert by_title["No date here"].url == "https://example.ca/news/2"  # permalink guid
    assert by_title["No date here"].outlet_published_at is None
    assert by_title["Naive date"].outlet_published_at is None  # no time zone: not used
    assert by_title["From the future"].outlet_published_at is None  # implausible
    assert "Old news" not in by_title and got.too_old == 1
    assert by_title["dc date"].pubdate_method == "feed date"
    assert got.undated == 3
    assert got.newest == datetime(2026, 9, 25, 13, 30, tzinfo=UTC)


def test_atom_and_rdf() -> None:
    [atom] = records(ATOM).records
    assert atom.url == "https://example.ca/news/a"  # relative link resolved; enclosure ignored
    assert atom.title == "Atom & story"
    assert atom.outlet_published_at == datetime(2026, 9, 25, 13, tzinfo=UTC)  # published wins
    [rdf] = records(RDF).records
    assert rdf.title == "RDF story" and rdf.outlet_published_at.hour == 8  # type: ignore[union-attr]


def test_other_domains_are_the_outlets_own() -> None:
    body = RSS.replace(b"ads.elsewhere.com", b"www.example-old.ca")
    got = records(body, aliases=["example-old.ca"])
    assert got.off_site == 0
    assert all(r.domain == "example.ca" for r in got.records)


@pytest.mark.parametrize(
    "body",
    [
        # billion laughs
        b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;&a;">]>'
        b"<rss><channel><item><title>&b;</title></item></channel></rss>",
        # external entity (file read)
        b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
        b"<rss><channel><item><title>&x;</title></item></channel></rss>",
        b"<html><body>not a feed</body></html>",
        b"not xml at all",
    ],
)
def test_hostile_or_broken_documents_are_rejected(body: bytes) -> None:
    with pytest.raises(ApiError):
        parse_feed(body, "https://example.ca/feed")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Fri, 25 Sep 2026 12:30:00 -0400", datetime(2026, 9, 25, 16, 30, tzinfo=UTC)),
        ("Fri, 25 Sep 2026 12:30:00 GMT", datetime(2026, 9, 25, 12, 30, tzinfo=UTC)),
        ("2026-09-25T12:30:00Z", datetime(2026, 9, 25, 12, 30, tzinfo=UTC)),
        ("Fri, 25 Sep 2026 12:30:00", None),  # no zone
        ("yesterday", None),
        ("", None),
    ],
)
def test_parse_feed_date(value: str, expected: datetime | None) -> None:
    assert parse_feed_date(value) == expected


def test_autodiscovery() -> None:
    html = """<html><head>
      <link rel="alternate" type="application/rss+xml" title="Top stories" href="/rss/top.xml">
      <link rel="alternate" type="application/atom+xml" href="https://example.ca/atom">
      <link rel="alternate" hreflang="fr" href="https://example.ca/fr/">
      <link rel="stylesheet" type="text/css" href="/s.css">
      <link rel="alternate" type="application/rss+xml" href="javascript:alert(1)">
    </head></html>"""
    assert discover_feeds(html, "https://www.example.ca/") == [
        ("https://www.example.ca/rss/top.xml", "Top stories"),
        ("https://example.ca/atom", ""),
    ]


# ------------------------------------------------------------------ ingestion


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    c = make_db(tmp_path / "db.sqlite3", NOW)
    sync_outlets(
        c,
        [
            OutletConfig(
                "example.ca",
                "Example",
                "CA",
                "en",
                feeds=("https://feeds.example.ca/top.xml", "https://example.ca/broken.xml"),
            ),
            OutletConfig("nofeed.ca", "No Feed", "CA", "en"),
        ],
        NOW,
    )
    return c


def fetcher(bodies: dict[str, bytes]):  # type: ignore[no-untyped-def]
    requested: list[str] = []

    def fetch(url: str) -> bytes:
        requested.append(url)
        if url not in bodies:
            raise FetchBlocked("unexpected status 404", status=404)
        return bodies[url]

    return fetch, requested


def test_run_feeds_stores_dated_articles(conn: sqlite3.Connection) -> None:
    fetch, requested = fetcher({"https://feeds.example.ca/top.xml": RSS})
    s = run_feeds(conn, fetch, Robots(lambda u: ""), Tagger(TAGS), now=NOW, sleep=lambda x: None)
    assert requested == ["https://feeds.example.ca/top.xml", "https://example.ca/broken.xml"]
    assert s.status == "partial" and s.queries == 2 and s.query_errors == 1
    assert s.inserted == 5  # 7 items - 1 off-site - 1 too old
    row = conn.execute(
        "SELECT source, outlet_published_at, pubdate_method, pubdate_checked_at, source_url"
        " FROM articles WHERE url = 'https://www.example.ca/news/1'"
    ).fetchone()
    assert row["source"] == "feeds" and row["source_url"] == "https://feeds.example.ca/top.xml"
    assert row["outlet_published_at"] == "2026-09-25T13:30:00Z"
    assert row["pubdate_method"] == "feed pubdate"
    assert row["pubdate_checked_at"] is not None  # dated by its source: shown straight away
    undated = conn.execute(
        "SELECT pubdate_checked_at FROM articles WHERE url = 'https://example.ca/news/2'"
    ).fetchone()
    assert undated[0] is None  # the date check will read its page
    tags = conn.execute(
        "SELECT count(*) FROM article_tags at JOIN articles a ON a.id = at.article_id"
        " WHERE a.url = 'https://www.example.ca/news/1'"
    ).fetchone()[0]
    assert tags >= 1  # "housing" keyword tagged, as for GDELT articles
    again = run_feeds(
        conn, fetch, Robots(lambda u: ""), Tagger(TAGS), now=NOW, sleep=lambda x: None
    )
    assert again.inserted == 0  # no duplicates


def test_feed_date_fills_in_an_article_gdelt_brought_first(conn: sqlite3.Connection) -> None:
    run_ingest(
        conn,
        FakeSource(
            [QueryResult("q", [rec("https://www.example.ca/news/1", "Council", NOW, "example.ca")])]
        ),
        Tagger(TAGS),
        now=NOW,
    )
    fetch, _ = fetcher({"https://feeds.example.ca/top.xml": RSS})
    run_feeds(conn, fetch, Robots(lambda u: ""), Tagger(TAGS), now=NOW, sleep=lambda x: None)
    row = conn.execute(
        "SELECT source, outlet_published_at FROM articles WHERE url = 'https://www.example.ca/news/1'"
    ).fetchone()
    assert row["source"] == "fake" and row["outlet_published_at"] == "2026-09-25T13:30:00Z"


def test_robots_txt_is_respected_for_feeds(conn: sqlite3.Connection) -> None:
    fetch, requested = fetcher({"https://feeds.example.ca/top.xml": RSS})
    robots = Robots(lambda u: "User-agent: *\nDisallow: /\n")
    s = run_feeds(conn, fetch, robots, Tagger(TAGS), now=NOW, sleep=lambda x: None)
    assert requested == [] and s.inserted == 0 and s.status == "failed"


def test_feed_fetches_are_paced(conn: sqlite3.Connection) -> None:
    fetch, _ = fetcher({})
    sleeps: list[float] = []
    clock = {"t": 0.0}
    run_feeds(
        conn,
        fetch,
        Robots(lambda u: ""),
        Tagger(TAGS),
        now=NOW,
        min_interval=1.0,
        sleep=lambda x: (sleeps.append(x), clock.__setitem__("t", clock["t"] + x)),
        clock=lambda: clock["t"],
    )
    assert sleeps == [1.0]


# ------------------------------------------------------------------ jobs and pages


def test_feed_fetcher_is_limited_to_the_feed_host(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from newsroom import jobs

    calls: list[dict] = []

    def fake(url: str, **kw):  # type: ignore[no-untyped-def]
        calls.append({"url": url, **kw})
        return FetchResult(url, "application/rss+xml", RSS)

    monkeypatch.setattr(jobs, "safe_fetch", fake)
    jobs._feed_fetcher(Settings(data_dir=tmp_path, contact_email="x@example.org"))(
        "https://feeds.example.ca/top.xml"
    )
    [c] = calls
    assert c["allowlist"] == ["feeds.example.ca"]
    assert "application/rss+xml" in c["allowed_types"] and c["max_bytes"] <= 5 * 1024 * 1024
    assert "x@example.org" in c["user_agent"]


def test_check_feeds_reports_and_discovers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from newsroom import jobs

    settings = Settings(data_dir=tmp_path, config_dir=tmp_path)
    (tmp_path / "outlets.yaml").write_text(
        "outlets:\n"
        "  - {domain: example.ca, name: Example, country: CA, language: en,"
        " feeds: [https://example.ca/broken.xml]}\n"
        "  - {domain: nofeed.ca, name: No Feed, country: CA, language: en}\n"
    )
    (tmp_path / "tags.yaml").write_text("tags:\n  x: {label: X, keywords: [x]}\n")
    make_db(settings.db_path, NOW).close()
    pages = {
        "https://example.ca/": b'<link rel="alternate" type="application/rss+xml" href="/rss.xml">',
        "https://example.ca/rss.xml": RSS,
    }

    def fake(url: str, **kw):  # type: ignore[no-untyped-def]
        if url.endswith("/robots.txt"):
            return FetchResult(url, "text/plain", b"")
        if url not in pages:
            raise FetchBlocked("unexpected status 404", status=404)
        kind = "text/html" if url.endswith("/") else "application/rss+xml"
        return FetchResult(url, kind, pages[url])

    monkeypatch.setattr(jobs, "safe_fetch", fake)
    report = {o.domain: o for o in jobs.check_feeds(settings, now=NOW)}
    configured, discovered = report["example.ca"].checks
    assert configured.error and "404" in configured.error
    assert discovered.origin == "discovered" and discovered.usable
    assert discovered.url == "https://example.ca/rss.xml" and discovered.on_site == 6
    assert report["nofeed.ca"].checks == []


def test_outlet_note_is_shown(tmp_path: Path) -> None:
    from newsroom.web.app import create_app

    settings = Settings(data_dir=tmp_path, rate_limit="1000/minute")
    c = make_db(settings.db_path, NOW)
    note = "No articles: GDELT doesn't carry this outlet, and it has no public feed."
    sync_outlets(c, [OutletConfig("example.ca", "Example", "CA", "en", note=note)], NOW)
    c.close()
    client = TestClient(create_app(settings))
    assert note.replace("'", "&#39;") in client.get("/outlet/example.ca").text
    assert note.replace("'", "&#39;") in client.get("/outlets").text


def test_bare_ampersands_are_repaired_but_entities_stay_forbidden() -> None:
    body = b"""<rss><channel><item><title>Q&A: taxes & you &amp; me</title>
      <link>https://example.ca/q?a=1&b=2</link></item></channel></rss>"""
    [item] = parse_feed(body, "https://example.ca/feed")
    assert item.title == "Q&A: taxes & you & me"
    assert item.link == "https://example.ca/q?a=1&b=2"
    evil = (
        b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x "y">]>'
        b"<rss><channel><item>&x; & z</item></channel></rss>"
    )
    with pytest.raises(ApiError):
        parse_feed(evil, "https://example.ca/feed")


def test_comment_feeds_are_not_offered() -> None:
    html = """<link rel="alternate" type="application/rss+xml" title="Site » Feed" href="/feed/">
      <link rel="alternate" type="application/rss+xml" title="Site » Comments Feed"
            href="/comments/feed/">"""
    assert discover_feeds(html, "https://example.ca/") == [
        ("https://example.ca/feed/", "Site » Feed")
    ]


def _check_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pages: dict[str, tuple[str, bytes]]
):  # type: ignore[no-untyped-def]
    from newsroom import jobs

    settings = Settings(data_dir=tmp_path, config_dir=tmp_path)
    (tmp_path / "outlets.yaml").write_text(
        "outlets:\n  - {domain: example.ca, name: Example, country: CA, language: en}\n"
    )
    (tmp_path / "tags.yaml").write_text("tags:\n  x: {label: X, keywords: [x]}\n")
    make_db(settings.db_path, NOW).close()
    requested: list[str] = []

    def fake(url: str, **kw):  # type: ignore[no-untyped-def]
        requested.append(url)
        if url.endswith("/robots.txt"):
            return FetchResult(url, "text/plain", b"")
        if url not in pages:
            raise FetchBlocked("unexpected status 403", status=403)
        kind, body = pages[url]
        return FetchResult(url, kind, body)

    monkeypatch.setattr(jobs, "safe_fetch", fake)
    return jobs, settings, requested


def test_usual_feed_addresses_are_tried_when_none_is_declared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs, settings, requested = _check_setup(
        tmp_path,
        monkeypatch,
        {
            "https://example.ca/": ("text/html", b"<html>no feed links</html>"),
            "https://example.ca/rss.xml": ("application/rss+xml", RSS),
        },
    )
    [outlet] = jobs.check_feeds(settings, now=NOW)
    [probe] = outlet.checks
    assert probe.origin == "probed" and probe.url == "https://example.ca/rss.xml" and probe.usable
    assert "https://example.ca/rss.xml" in requested
    assert requested.index("https://example.ca/feed/") < requested.index(
        "https://example.ca/rss.xml"
    )


def test_blocked_homepage_is_explained(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    jobs, settings, _ = _check_setup(tmp_path, monkeypatch, {})
    [outlet] = jobs.check_feeds(settings, now=NOW)
    assert outlet.checks == []
    assert "homepage not read" in outlet.discovery and "403" in outlet.discovery


def test_stale_feed_is_not_suggested(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    old = RSS.replace(b"2026", b"2025")  # every item a year old
    jobs, settings, _ = _check_setup(
        tmp_path,
        monkeypatch,
        {
            "https://example.ca/": (
                "text/html",
                b'<link rel="alternate" type="application/rss+xml" href="/old.xml">',
            ),
            "https://example.ca/old.xml": ("application/rss+xml", old),
        },
    )
    [outlet] = jobs.check_feeds(settings, now=NOW)
    declared = outlet.checks[0]
    assert declared.stale and not declared.usable
