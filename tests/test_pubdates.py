"""Publication dates from article pages: extraction, plausibility, robots.txt, politeness."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from newsroom.net.safe_fetch import FetchBlocked, FetchResult
from newsroom.services.ingest import run_ingest
from newsroom.services.pubdates import ZONE_CONFLICT, audit_dates, refresh_pub_dates
from newsroom.services.tagging import Tagger
from newsroom.settings import Settings
from newsroom.sources.base import QueryResult
from newsroom.sources.pubdate import DateConflict, Robots, extract, parse_timestamp
from newsroom.web.app import create_app
from tests.helpers import TAGS, FakeSource, make_db, rec

PAGES = Path(__file__).parent / "fixtures" / "pages"
SEEN = datetime(2026, 9, 24, 3, 0, tzinfo=UTC)  # when GDELT saw the article


def page(name: str) -> str:
    return (PAGES / name).read_text()


# ------------------------------------------------------------------ extraction


def test_schema_org_article_date_wins() -> None:
    found = extract(page("jsonld_newsarticle.html"), SEEN)
    assert found is not None
    assert found.when == datetime(2026, 9, 24, 0, 14, tzinfo=UTC)  # 20:14 -04:00
    assert found.method == "schema.org datePublished"
    # the WebSite object's 2001 date and dateModified are not used


def test_open_graph_date() -> None:
    found = extract(page("og_only.html"), SEEN)
    assert found and found.when == datetime(2026, 9, 23, 14, 2, 11, tzinfo=UTC)
    assert found.method == "article:published_time"


def test_ambiguous_and_implausible_dates_are_skipped() -> None:
    found = extract(page("naive_and_future.html"), SEEN)
    # no time zone -> skipped; year 2031 is after GDELT saw it -> skipped;
    # the microdata value with a +0200 offset is the first plausible one
    assert found and found.method == "itemprop datePublished"
    assert found.when == datetime(2026, 9, 22, 7, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    "markup",
    [
        '<time itemprop="datePublished" datetime="2026-09-23T10:00:00-04:00">Sept. 23</time>',
        '<span itemprop="datePublished" content="2026-09-23T14:00:00Z">Sept. 23</span>',
        '<meta property="og:article:published_time" content="2026-09-23T14:00:00+00:00">',
    ],
)
def test_other_standard_date_markup(markup: str) -> None:
    found = extract(f"<html><head></head><body>{markup}</body></html>", SEEN)
    assert found is not None and found.when == datetime(2026, 9, 23, 14, tzinfo=UTC)


def test_no_usable_date() -> None:
    assert extract(page("no_date.html"), SEEN) is None  # "September 23, 2026" is not ISO


def test_broken_markup_still_yields_valid_tags() -> None:
    found = extract(page("broken.html"), SEEN)
    assert found and found.method == "meta parsely-pub-date"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-09-23T20:14:00Z", datetime(2026, 9, 23, 20, 14, tzinfo=UTC)),
        ("2026-09-23T20:14:00.123-04:00", datetime(2026, 9, 24, 0, 14, 0, 123000, tzinfo=UTC)),
        ("2026-09-23 20:14:00+0000", datetime(2026, 9, 23, 20, 14, tzinfo=UTC)),
        ("2026-09-23T20:14:00", None),  # no zone
        ("2026-09-23", None),  # date only
        ("Sept 23", None),
        (None, None),
        (12345, None),
    ],
)
def test_parse_timestamp(value: object, expected: datetime | None) -> None:
    assert parse_timestamp(value) == expected


def test_publication_just_after_seen_is_allowed_for_clock_skew() -> None:
    html = '<meta property="article:published_time" content="2026-09-24T03:40:00Z">'
    assert extract(html, SEEN) is not None
    html = '<meta property="article:published_time" content="2026-09-24T04:30:00Z">'
    assert extract(html, SEEN) is None


def test_tags_that_disagree_by_whole_hours_mean_no_date() -> None:
    # The same wall-clock time in two zones: one tag gets the time zone wrong.
    html = (
        '<meta property="article:published_time" content="2026-09-23T18:50:00-04:00">'
        '<meta name="parsely-pub-date" content="2026-09-23T18:50:00Z">'
    )
    found = extract(html, SEEN)
    assert isinstance(found, DateConflict)
    assert {found.first[0], found.second[0]} == {"article:published_time", "meta parsely-pub-date"}


@pytest.mark.parametrize(
    "second",
    [
        '<meta name="parsely-pub-date" content="2026-09-23T22:50:00Z">',  # same instant
        '<meta name="parsely-pub-date" content="2026-09-23T22:53:00Z">',  # minutes apart
        # the untyped fallback can be anything on the page (a related article, the site)
        '<script type="application/ld+json">{"datePublished": "2026-09-23T20:50:00Z"}</script>',
    ],
)
def test_agreeing_or_unrelated_tags_are_not_a_conflict(second: str) -> None:
    html = f'<meta property="article:published_time" content="2026-09-23T18:50:00-04:00">{second}'
    found = extract(html, SEEN)
    assert not isinstance(found, DateConflict)
    assert found and found.when == datetime(2026, 9, 23, 22, 50, tzinfo=UTC)


# ------------------------------------------------------------------ robots.txt


def test_robots_rules_and_rfc_fallbacks() -> None:
    texts = {
        "https://a.ca/robots.txt": "User-agent: *\nDisallow: /private/\n",
        "https://b.ca/robots.txt": "User-agent: newsroom\nDisallow: /\n",
    }
    fetched: list[str] = []

    def fetch(url: str) -> str:
        fetched.append(url)
        if url in texts:
            return texts[url]
        if "c.ca" in url:
            raise FetchBlocked("unexpected status 404", status=404)
        if "d.ca" in url:
            raise FetchBlocked("unexpected status 503", status=503)
        raise FetchBlocked("content-type not allowed: text/html")

    robots = Robots(fetch)
    assert robots.allowed("https://a.ca/news/1")
    assert not robots.allowed("https://a.ca/private/x")
    assert not robots.allowed("https://b.ca/news/1")  # rules for our agent name
    assert robots.allowed("https://c.ca/news/1")  # 404: no rules
    assert not robots.allowed("https://d.ca/news/1")  # 5xx: stay away
    assert robots.allowed("https://e.ca/news/1")  # HTML instead of rules
    robots.allowed("https://a.ca/news/2")
    assert fetched.count("https://a.ca/robots.txt") == 1  # cached


@pytest.mark.parametrize(
    ("text", "url", "allowed"),
    [
        # the longest (most specific) matching rule wins, wherever it is in the file
        ("User-agent: *\nDisallow: /\nAllow: /news/\n", "https://x.ca/news/1", True),
        ("User-agent: *\nDisallow: /\nAllow: /news/\n", "https://x.ca/sports/1", False),
        (
            "User-agent: *\nAllow: /news/\nDisallow: /news/private/\n",
            "https://x.ca/news/private/a",
            False,
        ),
        # on a tie, Allow wins
        ("User-agent: *\nDisallow: /a\nAllow: /a\n", "https://x.ca/a", True),
        # wildcards and end anchors
        ("User-agent: *\nDisallow: /*.pdf$\n", "https://x.ca/doc.pdf", False),
        ("User-agent: *\nDisallow: /*.pdf$\n", "https://x.ca/doc.pdf?x=1", True),
        ("User-agent: *\nDisallow: /*?share=\n", "https://x.ca/news/1?share=fb", False),
        ("User-agent: *\nDisallow: /*?share=\n", "https://x.ca/news/1", True),
        # a group naming us replaces "*"; groups naming us are combined
        ("User-agent: *\nDisallow: /\n\nUser-agent: newsroom\nAllow: /\n", "https://x.ca/n", True),
        (
            "User-agent: newsroom\nDisallow: /a/\nUser-agent: other\nDisallow: /\n"
            "User-agent: NewsRoom\nDisallow: /b/\n",
            "https://x.ca/b/1",
            False,
        ),
        # other bots' groups don't apply to us
        ("User-agent: GPTBot\nDisallow: /\n", "https://x.ca/news/1", True),
        # an empty Disallow allows everything; comments and stray rules are ignored
        ("User-agent: *\nDisallow:\n", "https://x.ca/anything", True),
        (
            "Disallow: /\nUser-agent: * # everyone\nDisallow: /x # not news\n",
            "https://x.ca/n",
            True,
        ),
        # several user-agent lines share one group
        ("User-agent: a\nUser-agent: *\nDisallow: /\n", "https://x.ca/n", False),
        # robots.txt itself is always allowed
        ("User-agent: *\nDisallow: /\n", "https://x.ca/robots.txt", True),
    ],
)
def test_robots_rules_follow_rfc_9309(text: str, url: str, allowed: bool) -> None:
    assert Robots(lambda u: text).allowed(url) is allowed


def test_unreachable_robots_is_not_a_verdict_and_is_asked_again_soon() -> None:
    now = {"t": 0.0}
    calls: list[str] = []

    def fetch(url: str) -> str:
        calls.append(url)
        if len(calls) == 1:
            raise FetchBlocked("request failed: ReadTimeout")
        return "User-agent: *\nDisallow:\n"

    robots = Robots(fetch, clock=lambda: now["t"])
    assert robots.check("https://a.ca/x") == "unavailable"
    now["t"] = 30 * 60
    assert robots.check("https://a.ca/x") == "unavailable"  # not hammered
    now["t"] = 61 * 60
    assert robots.check("https://a.ca/x") == "allowed"  # asked again after an hour
    assert len(calls) == 2


def test_unavailable_robots_leaves_articles_unmarked(conn: sqlite3.Connection) -> None:
    def fetch(url: str) -> str:
        raise FetchBlocked("unexpected status 503", status=503)

    requested: list[str] = []
    s = refresh_pub_dates(
        conn, lambda u, d: requested.append(u), Robots(fetch), NOW, sleep=lambda x: None
    )
    assert requested == [] and s.robots_disallowed == 0
    r = row(conn, "https://cbc.ca/news/1")
    assert r["pubdate_method"] is None and r["pubdate_attempts"] == 0  # retried next pass


def test_robots_cache_expires_after_a_day() -> None:
    now = {"t": 0.0}
    calls: list[str] = []
    robots = Robots(lambda u: calls.append(u) or "", clock=lambda: now["t"])
    robots.allowed("https://a.ca/x")
    now["t"] = 23 * 3600
    robots.allowed("https://a.ca/x")
    now["t"] = 25 * 3600
    robots.allowed("https://a.ca/x")
    assert len(calls) == 2


# ------------------------------------------------------------------ service

NOW = SEEN + timedelta(minutes=30)


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    c = make_db(Settings(data_dir=tmp_path).db_path, NOW)
    records = [
        rec("https://cbc.ca/news/1", "One", SEEN),
        rec("https://cbc.ca/news/2", "Two", SEEN - timedelta(minutes=5)),
        rec("https://www.nytimes.com/3", "Three", SEEN - timedelta(minutes=10), "nytimes.com"),
        rec("https://www.nytimes.com/4", "Four", SEEN - timedelta(minutes=15), "nytimes.com"),
        rec("https://cbc.ca/news/old", "Old", SEEN - timedelta(days=5)),
    ]
    run_ingest(c, FakeSource([QueryResult("q", records)]), Tagger(TAGS), now=NOW)
    return c


def html_result(url: str, body: str) -> FetchResult:
    return FetchResult(url=url, content_type="text/html", body=body.encode())


def row(conn: sqlite3.Connection, url: str) -> sqlite3.Row:
    return conn.execute("SELECT * FROM articles WHERE url = ?", (url,)).fetchone()


def test_fills_dates_politely(conn: sqlite3.Connection) -> None:
    requested: list[str] = []
    sleeps: list[float] = []

    def fetch(url: str, domain: str) -> FetchResult:
        requested.append(url)
        if url.endswith("/1"):
            return html_result(url, page("jsonld_newsarticle.html"))
        if url.endswith("/2"):
            return html_result(url, page("no_date.html"))
        raise FetchBlocked("unexpected status 429", status=429)

    clock = {"t": 0.0}
    s = refresh_pub_dates(
        conn,
        fetch,
        Robots(lambda u: ""),
        NOW,
        sleep=lambda x: (sleeps.append(x), clock.__setitem__("t", clock["t"] + x)),
        clock=lambda: clock["t"],
    )
    # the 5-day-old article is out of scope; after nytimes answered 429, its second
    # article was not requested at all
    assert requested == [
        "https://cbc.ca/news/1",
        "https://cbc.ca/news/2",
        "https://www.nytimes.com/3",
    ]
    assert sleeps == [1.0, 1.0]  # >= 1 s between requests
    assert (s.found, s.no_date, s.failed, s.hosts_skipped) == (1, 1, 1, 1)

    one = row(conn, "https://cbc.ca/news/1")
    assert one["outlet_published_at"] == "2026-09-24T00:14:00Z"
    assert one["pubdate_method"] == "schema.org datePublished"
    assert one["published_at"] == "2026-09-24T03:00:00Z"  # GDELT's time is kept
    assert row(conn, "https://cbc.ca/news/2")["pubdate_attempts"] == 2  # no date: done
    assert row(conn, "https://www.nytimes.com/3")["pubdate_attempts"] == 1  # retry later
    assert row(conn, "https://www.nytimes.com/4")["pubdate_attempts"] == 0


def test_retry_waits_an_hour_and_stops_after_two_attempts(conn: sqlite3.Connection) -> None:
    def failing(url: str, domain: str) -> FetchResult:
        raise FetchBlocked("request failed: ConnectTimeout")

    def sleep(x: float) -> None:
        pass

    refresh_pub_dates(conn, failing, Robots(lambda u: ""), NOW, sleep=sleep)
    again = refresh_pub_dates(
        conn, failing, Robots(lambda u: ""), NOW + timedelta(minutes=15), sleep=sleep
    )
    assert again.checked == 0  # checked less than an hour ago
    refresh_pub_dates(conn, failing, Robots(lambda u: ""), NOW + timedelta(hours=2), sleep=sleep)
    last = refresh_pub_dates(
        conn, failing, Robots(lambda u: ""), NOW + timedelta(hours=4), sleep=sleep
    )
    assert last.checked == 0  # two attempts each: never asked again


def test_robots_disallow_is_respected(conn: sqlite3.Connection) -> None:
    requested: list[str] = []
    robots = Robots(lambda u: "User-agent: *\nDisallow: /\n")
    s = refresh_pub_dates(conn, lambda u, d: requested.append(u), robots, NOW, sleep=lambda x: None)
    assert requested == []
    assert s.robots_disallowed == 4
    assert row(conn, "https://cbc.ca/news/1")["pubdate_method"] == "robots.txt disallows"


def test_per_run_limit(conn: sqlite3.Connection) -> None:
    s = refresh_pub_dates(
        conn,
        lambda u, d: html_result(u, page("og_only.html")),
        Robots(lambda u: ""),
        NOW,
        limit=2,
        sleep=lambda x: None,
    )
    assert s.checked == 2


# ------------------------------------------------------------------ web


def test_cards_say_published_or_seen(conn: sqlite3.Connection, tmp_path: Path) -> None:
    refresh_pub_dates(
        conn,
        lambda u, d: html_result(
            u, page("jsonld_newsarticle.html" if u.endswith("/1") else "no_date.html")
        ),
        Robots(lambda u: ""),
        NOW,
        sleep=lambda x: None,
    )
    client = TestClient(create_app(Settings(data_dir=tmp_path, rate_limit="1000/minute")))
    html = client.get("/").text
    # 00:14 UTC = 20:14 Toronto on the 23rd; the card uses the outlet's time, not GDELT's
    published = '<time datetime="2026-09-23T20:14:00-04:00">Sep 23, 8:14 p.m. EDT</time>'
    assert f"Published {published}" in html
    assert "Seen <time" in html  # articles whose page gave no date
    # sorted by the date shown: "One" (published 20:14 the day before) now sits below
    # "Two" (seen 22:55), although GDELT saw "One" later
    assert html.index(">Two<") < html.index(">One<")


def test_job_wiring_limits_each_request(
    conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from newsroom import jobs

    calls: list[dict] = []

    def fake_safe_fetch(url: str, **kw):  # type: ignore[no-untyped-def]
        calls.append({"url": url, **kw})
        if url.endswith("/robots.txt"):
            return FetchResult(url=url, content_type="text/plain", body=b"")
        return html_result(url, page("og_only.html"))

    monkeypatch.setattr(jobs, "safe_fetch", fake_safe_fetch)
    monkeypatch.setattr(jobs, "_ROBOTS_CACHE", {})
    settings = Settings(data_dir=tmp_path, contact_email="x@example.org", pubdate_per_run=3)
    summary = jobs._publication_dates(settings, conn)
    assert summary is not None and summary.found == 3  # per-run cap
    pages = [c for c in calls if not c["url"].endswith("/robots.txt")]
    robots = [c for c in calls if c["url"].endswith("/robots.txt")]
    assert {c["url"] for c in robots} == {
        "https://cbc.ca/robots.txt",
        "https://www.nytimes.com/robots.txt",
    }
    for c in pages:
        assert c["allowlist"] in (["cbc.ca"], ["nytimes.com"])  # only the outlet's own site
        assert c["truncate"] is True and c["max_bytes"] == 1_500_000
        assert "text/html" in c["allowed_types"]
        assert "x@example.org" in c["user_agent"]
    assert all(c["allowed_types"] == {"text/plain"} for c in robots)

    off = Settings(data_dir=tmp_path, contact_email="x@example.org", pubdate_fetch=False)
    assert jobs._publication_dates(off, conn) is None
    no_contact = Settings(data_dir=tmp_path)
    assert jobs._publication_dates(no_contact, conn) is None


def test_explain_shows_each_step(
    conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from newsroom import jobs

    def fake_safe_fetch(url: str, **kw):  # type: ignore[no-untyped-def]
        if url.endswith("/robots.txt"):
            return FetchResult(
                url=url, content_type="text/plain", body=b"User-agent: *\nDisallow: /private/\n"
            )
        return html_result(url, page("naive_and_future.html"))

    monkeypatch.setattr(jobs, "safe_fetch", fake_safe_fetch)
    settings = Settings(data_dir=tmp_path, contact_email="x@example.org")
    lines = jobs.explain_date(settings, "https://cbc.ca/news/1")
    text = "\n".join(lines)
    assert "outlet: cbc.ca" in text and "robots.txt: allowed" in text
    assert "rejected: not a full date-time with a time zone" in text  # the naive value
    assert "rejected: later than GDELT saw the article" in text
    assert "result:" in text

    blocked = jobs.explain_date(settings, "https://cbc.ca/private/x")
    assert blocked[-1] == "robots.txt: disallowed"  # stops there: the page isn't read
    with pytest.raises(LookupError):
        jobs.explain_date(settings, "https://example.com/not-an-outlet")
    with pytest.raises(ValueError):
        jobs.explain_date(settings, "javascript:alert(1)")


def test_conflicting_page_is_left_undated(conn: sqlite3.Connection) -> None:
    html = (
        '<meta property="article:published_time" content="2026-09-23T22:50:00-04:00">'
        '<meta name="sailthru.date" content="2026-09-23T22:50:00Z">'
    )
    s = refresh_pub_dates(
        conn,
        lambda url, d: html_result(url, html),
        Robots(lambda u: ""),
        NOW,
        limit=1,
        sleep=lambda x: None,
    )
    assert s.conflicting == 1 and s.found == 0
    one = row(conn, "https://cbc.ca/news/1")
    assert one["outlet_published_at"] is None and one["pubdate_method"] == ZONE_CONFLICT
    assert one["pubdate_attempts"] == 2  # the page won't change its mind: done


def test_date_audit_flags_dates_hours_before_first_seen(conn: sqlite3.Connection) -> None:
    # nytimes: every date 4 h before GDELT saw it (local time labelled as UTC)
    conn.execute(
        "UPDATE articles SET outlet_published_at ="
        " strftime('%Y-%m-%dT%H:%M:%SZ', published_at, '-4 hours', '-10 minutes'),"
        " pubdate_method = 'article:published_time'"
        " WHERE url LIKE 'https://www.nytimes.com/%'"
    )
    conn.execute(
        "UPDATE articles SET outlet_published_at ="
        " strftime('%Y-%m-%dT%H:%M:%SZ', published_at, '-12 minutes'),"
        " pubdate_method = 'feed pubdate' WHERE url = 'https://cbc.ca/news/1'"
    )
    conn.execute(
        "UPDATE articles SET pubdate_method = ? WHERE url = 'https://cbc.ca/news/2'",
        (ZONE_CONFLICT,),
    )
    rows = {(a.domain, a.origin): a for a in audit_dates(conn, NOW)}
    ny = rows[("nytimes.com", "page")]
    assert ny.dated == 2 and ny.early == 2 and ny.median_lag == timedelta(hours=4, minutes=10)
    assert not ny.suspect  # too few articles to judge
    cbc_feed = rows[("cbc.ca", "feed")]
    assert cbc_feed.median_lag == timedelta(minutes=12) and cbc_feed.early == 0
    assert rows[("cbc.ca", "page")].zone_conflicts == 1  # the old article is outside 7 days
