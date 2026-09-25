"""Feed page: date grouping, filters, search, pagination, escaping."""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from newsroom.services.ingest import run_ingest
from newsroom.services.tagging import Tagger
from newsroom.settings import Settings
from newsroom.sources.base import QueryResult
from newsroom.web import queries
from newsroom.web.app import create_app
from tests.helpers import TAGS, FakeSource, make_db, rec

TZ = ZoneInfo("America/Toronto")
NOW = datetime(2026, 9, 24, 16, tzinfo=UTC)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    s = Settings(data_dir=tmp_path, rate_limit="1000/minute", search_rate_limit="1000/minute")
    conn = make_db(s.db_path, NOW)
    records = [
        rec(
            "https://cbc.ca/a",
            "Housing starts climb as interest rate falls",
            NOW - timedelta(hours=1),
            themes=(("ECON_HOUSING_PRICES", 3), ("ECON_INFLATION", 3)),
        ),
        rec(
            "https://cbc.ca/b",
            "Byelection called in Toronto riding",
            NOW - timedelta(hours=3),
            themes=(("ELECTION", 5),),
        ),
        # 03:30 UTC on the 24th is 23:30 on the 23rd in Toronto: must group under the 23rd
        rec(
            "https://cbc.ca/c", "Late night council vote", datetime(2026, 9, 24, 3, 30, tzinfo=UTC)
        ),
        rec(
            "https://ici.radio-canada.ca/d",
            "Élection partielle à Montréal",
            NOW - timedelta(days=2),
            "radio-canada.ca",
            themes=(("ELECTION", 4),),
        ),
        rec(
            "https://nytimes.com/e",
            "<script>alert('x')</script> & friends",
            NOW - timedelta(days=3),
            "nytimes.com",
        ),
    ]
    run_ingest(conn, FakeSource([QueryResult("https://api/q", records)]), Tagger(TAGS), now=NOW)
    conn.close()
    return s


@pytest.fixture
def client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings))


def titles(html: str) -> list[str]:
    return re.findall(r'class="title" href="[^"]+"[^>]*>([^<]+)</a>', html)


def test_feed_lists_newest_first(client: TestClient) -> None:
    html = client.get("/").text
    assert titles(html)[0] == "Housing starts climb as interest rate falls"
    assert len(titles(html)) == 5


def test_date_grouping_uses_local_timezone(settings: Settings) -> None:
    from newsroom.db import connect_readonly

    conn = connect_readonly(settings.db_path)
    page = queries.feed(conn, queries.FeedFilters(), TZ)
    by_day = {g.day.isoformat(): [a.title for a in g.articles] for g in page.groups}
    assert "Late night council vote" in by_day["2026-09-23"]
    assert list(by_day) == sorted(by_day, reverse=True)


def test_article_links_are_safe(client: TestClient) -> None:
    html = client.get("/").text
    assert '<a class="title" href="https://cbc.ca/a" rel="noopener noreferrer">' in html
    assert (
        '<a class="open-new" href="https://cbc.ca/a" target="_blank" rel="noopener noreferrer"'
        in html
    )
    assert "<script>alert" not in html
    assert "&lt;script&gt;alert(&#39;x&#39;)&lt;/script&gt; &amp; friends" in html


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("housing", ["Housing starts climb as interest rate falls"]),
        ("HOUSING starts", ["Housing starts climb as interest rate falls"]),
        ("hous", ["Housing starts climb as interest rate falls"]),  # prefix while typing
        ("election", ["Élection partielle à Montréal"]),  # accent-insensitive
        ("byelection toronto", ["Byelection called in Toronto riding"]),
        ('"unbalanced quote OR NEAR( *', []),  # FTS syntax is neutralised
        ("zzzz", []),
    ],
)
def test_search(client: TestClient, query: str, expected: list[str]) -> None:
    resp = client.get("/", params={"q": query})
    assert resp.status_code == 200
    assert titles(resp.text) == expected


def test_filters(client: TestClient) -> None:
    assert titles(client.get("/", params={"outlet": "nytimes.com"}).text) == [
        "<script>alert('x')</script> & friends".replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("'", "&#39;")
    ]
    assert len(titles(client.get("/", params={"country": "US"}).text)) == 1
    assert len(titles(client.get("/", params={"country": "CA"}).text)) == 4
    housing = titles(client.get("/", params={"tag": "housing"}).text)
    assert housing == ["Housing starts climb as interest rate falls"]
    ranged = titles(client.get("/", params={"from": "2026-09-23", "to": "2026-09-23"}).text)
    assert ranged == ["Late night council vote"]


def test_invalid_params_are_ignored(client: TestClient) -> None:
    resp = client.get(
        "/",
        params={
            "tag": "'; DROP TABLE x;--",
            "outlet": "<x>",
            "from": "yesterday",
            "country": "CAN",
            "before": "garbage",
        },
    )
    assert resp.status_code == 200
    assert len(titles(resp.text)) == 5


def add_articles(settings: Settings, n: int) -> None:
    from newsroom.db import connect

    conn = connect(settings.db_path)
    records = [
        rec(f"https://cbc.ca/bulk/{i}", f"Bulk story {i:03d}", NOW - timedelta(minutes=10 + i))
        for i in range(n)
    ]
    run_ingest(conn, FakeSource([QueryResult("q", records)]), Tagger(TAGS), now=NOW)
    conn.close()


def test_pagination_pages_and_links(settings: Settings) -> None:
    add_articles(settings, 55)  # 60 in total, most within one hour
    client = TestClient(create_app(replace(settings, feed_outlet_cap=1000)))
    first = client.get("/", params={"per": 25}).text
    assert "1\u201325 of 60 articles" in first
    assert "Page 1 of 3" in first
    assert 'href="/?per=25&amp;page=2" rel="next"' in first
    assert 'rel="prev"' not in first  # no previous on page 1
    assert 'aria-current="page" aria-label="Page 1, current"' in first

    seen: list[str] = []
    for n in (1, 2, 3):
        html = client.get("/", params={"per": 25, "page": n}).text
        seen += titles(html)
    assert len(seen) == 60 and len(set(seen)) == 60

    last = client.get("/", params={"per": 25, "page": 3}).text
    assert "51\u201360 of 60 articles" in last
    assert 'href="/?per=25&amp;page=2" rel="prev"' in last
    assert 'rel="next"' not in last


def test_page_past_the_end_redirects_to_last(settings: Settings, client: TestClient) -> None:
    resp = client.get("/", params={"page": 9}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"  # 5 articles: only page 1


def test_page_links_elide_the_middle() -> None:
    page = queries.FeedPage([], total=1000, page=10, per=50)  # 20 pages
    assert page.page_links == [1, None, 8, 9, 10, 11, 12, None, 20]
    assert queries.FeedPage([], total=120, page=1, per=50).page_links == [1, 2, 3]


@pytest.mark.parametrize(
    ("sort", "first_title"),
    [
        ("newest", "Housing starts climb as interest rate falls"),
        (
            "oldest",
            "<script>alert(&#39;x&#39;)</script> &amp; friends".replace("<", "&lt;").replace(
                ">", "&gt;"
            ),
        ),
    ],
)
def test_sort_by_date(client: TestClient, sort: str, first_title: str) -> None:
    assert titles(client.get("/", params={"sort": sort}).text)[0] == first_title


def test_sort_by_outlet_groups_under_outlet_headings(client: TestClient) -> None:
    html = client.get("/", params={"sort": "outlet"}).text
    headings = re.findall(r'<h2 id="group-\d+">([^<]+)</h2>', html)
    assert headings == ["CBC News", "Radio-Canada", "The New York Times"]


def test_best_match_only_with_a_search(settings: Settings, client: TestClient) -> None:
    assert "Best match" not in client.get("/").text
    # without a search, sort=relevance is dropped from the URL
    resp = client.get("/", params={"sort": "relevance"}, follow_redirects=False)
    assert resp.headers["location"] == "/"
    add_articles(settings, 3)
    html = client.get("/", params={"q": "housing", "sort": "relevance"}).text
    assert '<option value="relevance" selected>Best match</option>' in html
    assert titles(html) == ["Housing starts climb as interest rate falls"]
    assert '<h2 id="group-' not in html  # ranked list, no day headings


def test_htmx_request_gets_clean_push_url(client: TestClient) -> None:
    resp = client.get(
        "/",
        params={"q": "", "tag": "housing", "outlet": "", "sort": "newest", "per": "50"},
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert resp.status_code == 200
    assert resp.headers["HX-Push-Url"] == "/?tag=housing"


def test_changing_a_filter_resets_the_page(settings: Settings, client: TestClient) -> None:
    add_articles(settings, 55)
    # newest first, 25 per page: the tagged "Housing starts…" (1 h old) lands on page 3
    html = client.get("/", params={"per": 25, "page": 3, "mix": "all"}).text
    links = set(re.findall(r'href="(/\?[^"]*tag=housing[^"]*)"', html))
    assert links == {"/?tag=housing&amp;mix=all&amp;per=25"}  # keeps settings, back to page 1
    assert 'hx-include="closest form"' in html and 'name="page"' not in html


def test_outlets_and_owners_tables_sort(client: TestClient) -> None:
    by_name = client.get("/outlets").text
    assert by_name.index("CBC News") < by_name.index("Radio-Canada")
    assert 'aria-sort="ascending"' in by_name
    by_articles = client.get("/outlets", params={"sort": "articles"}).text
    assert 'aria-sort="descending"' in by_articles
    assert by_articles.index("CBC News") < by_articles.index("The New York Times")
    assert client.get("/outlets", params={"sort": "bogus"}).status_code == 200
    assert client.get("/owners", params={"sort": "name"}).status_code == 200


def test_tag_and_outlet_links_keep_filters(client: TestClient) -> None:
    html = client.get("/", params={"country": "CA"}).text
    assert 'href="/?tag=housing&amp;country=CA"' in html
    # each tag says where it came from
    assert 'title="GDELT themes: ECON_HOUSING_PRICES (3)">Housing</a>' in html
    assert 'href="/outlet/cbc.ca"' in html


def test_search_rate_limit(settings: Settings) -> None:
    s = Settings(data_dir=settings.data_dir, rate_limit="1000/minute", search_rate_limit="2/minute")
    client = TestClient(create_app(s))
    codes = [client.get("/", params={"q": "housing"}).status_code for _ in range(3)]
    assert codes == [200, 200, 429]
    assert client.get("/").status_code == 200  # browsing is unaffected


def test_empty_database_state(tmp_path: Path) -> None:
    client = TestClient(create_app(Settings(data_dir=tmp_path)))
    resp = client.get("/")
    assert resp.status_code == 200
    assert "No articles yet" in resp.text


def test_fts_query_builder() -> None:
    assert queries.fts_query("Hello, world!") == '"Hello" "world"*'
    assert queries.fts_query('" OR *') == '"OR"*'  # operator becomes a quoted literal
    assert queries.fts_query('" * ()') == ""
    assert queries.fts_query("") == ""


def test_empty_and_invalid_params_redirect_to_clean_url(client: TestClient) -> None:
    resp = client.get(
        "/", params={"q": "", "tag": "housing", "outlet": "", "from": "bad"}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/?tag=housing"
    assert client.get("/?tag=housing", follow_redirects=False).status_code == 200


def age_articles(settings: Settings, minutes: int = 60) -> None:
    """Pretend the articles so far were collected a while ago (retrieved_at is real time)."""
    from newsroom.db import connect

    conn = connect(settings.db_path)
    then = (datetime.now(UTC) - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute("UPDATE articles SET retrieved_at = ?", (then,))
    conn.close()


def test_new_articles_notice(settings: Settings, client: TestClient) -> None:
    age_articles(settings)
    html = client.get("/").text
    m = re.search(r'hx-get="/fragments/new\?since=(\d{14})"', html)
    assert m, "feed polls for new articles"
    assert 'hx-trigger="every 120s"' in html
    since = m.group(1)
    assert client.get(f"/fragments/new?since={since}").text.strip() == ""

    from newsroom.db import connect

    conn = connect(settings.db_path)
    run_ingest(
        conn,
        FakeSource(
            [
                QueryResult(
                    "q",
                    [
                        rec(
                            "https://cbc.ca/new1",
                            "Fresh housing story",
                            NOW,
                            themes=(("ECON_HOUSING_PRICES", 3),),
                        ),
                        rec("https://cbc.ca/new2", "Another fresh story", NOW),
                    ],
                )
            ]
        ),
        Tagger(TAGS),
        now=NOW + timedelta(minutes=15),
    )
    conn.close()

    frag = client.get(f"/fragments/new?since={since}").text
    assert '<a class="new-link" href="/">2 new articles · Show</a>' in frag
    tagged = client.get(f"/fragments/new?since={since}&tag=housing").text
    assert "1 new article · Show" in tagged and 'href="/?tag=housing"' in tagged
    assert client.get(f"/fragments/new?since={since}&country=US").text.strip() == ""
    for bad in ("", "abc", "-1", "9" * 20, "20261399999999"):
        assert client.get(f"/fragments/new?since={bad}").text.strip() == ""


def test_no_polling_on_search_or_older_pages(client: TestClient) -> None:
    assert "/fragments/new" not in client.get("/", params={"q": "housing"}).text
    html = client.get("/").text
    assert "every 15 minutes" in html  # meta note reflects the schedule


def test_article_date_on_every_card(client: TestClient) -> None:
    from newsroom.web.app import _article_date

    html = client.get("/", params={"sort": "outlet"}).text  # no day headings in this sort
    card = r"Seen <time datetime=\"2026-09-2\dT[^\"]+\">Sep 2\d, \d{1,2}:\d\d [ap]\.m\. EDT</time>"
    assert re.search(card, html)
    now = datetime(2026, 9, 24, tzinfo=TZ)
    assert _article_date(datetime(2026, 9, 3, 7, 5, tzinfo=TZ), now) == "Sep 3, 7:05 a.m. EDT"
    assert _article_date(datetime(2026, 9, 3, 18, 50, tzinfo=TZ), now) == "Sep 3, 6:50 p.m. EDT"
    assert _article_date(datetime(2026, 9, 3, 0, 5, tzinfo=TZ), now) == "Sep 3, 12:05 a.m. EDT"
    assert _article_date(datetime(2026, 9, 3, 12, 0, tzinfo=TZ), now) == "Sep 3, 12:00 p.m. EDT"
    # winter: standard time
    assert (
        _article_date(datetime(2025, 12, 31, 23, 59, tzinfo=TZ), now)
        == "Dec 31 2025, 11:59 p.m. EST"
    )


def test_filter_options_collapse_unless_a_filter_is_set(client: TestClient) -> None:
    html = client.get("/").text
    assert '<details class="more-filters">' in html  # collapsed
    assert "<summary>Filters</summary>" in html
    assert 'name="q"' in html.split('<details class="more-filters"')[0]  # search stays visible
    searched = client.get("/", params={"q": "housing"}).text
    assert '<details class="more-filters">' in searched  # a search alone isn't a filter
    filtered = client.get("/", params={"tag": "housing", "from": "2026-09-01"}).text
    assert '<details class="more-filters" open>' in filtered
    assert '<span class="badge">2 active</span>' in filtered


def test_balanced_mix_limits_busy_outlets(settings: Settings, client: TestClient) -> None:
    add_articles(settings, 55)  # cbc.ca: 55 more within about an hour
    html = client.get("/", params={"per": 100}).text
    shown = titles(html)
    bulk = [t for t in shown if t.startswith("Bulk story")]
    assert len(bulk) <= 3 * 2  # at most 3 per outlet per hour; the burst spans two hours
    assert "more hidden</a>" in html
    assert 'href="/?mix=all&amp;per=100"' in html
    assert '<select name="mix"' in html
    everything = client.get("/", params={"per": 100, "mix": "all"}).text
    assert len(titles(everything)) == 60 and "more hidden" not in everything
    one_outlet = client.get("/", params={"per": 100, "outlet": "cbc.ca"}).text
    assert len(titles(one_outlet)) == 58  # an outlet's own feed shows everything
    assert '<select name="mix"' not in one_outlet


def test_new_articles_wait_for_their_date_check(settings: Settings) -> None:
    """With date checking on, a new article appears once its publication date has been
    checked (or after the hold), and the new-articles notice counts it when it does."""
    from newsroom.db import connect

    age_articles(settings)
    hold = replace(settings, contact_email="x@example.org", pubdate_hold_minutes=15)
    client = TestClient(create_app(hold))
    html = client.get("/").text
    since = re.search(r"since=(\d{14})", html).group(1)  # type: ignore[union-attr]

    conn = connect(settings.db_path)
    run_ingest(
        conn,
        FakeSource([QueryResult("q", [rec("https://cbc.ca/fresh", "Fresh council vote", NOW)])]),
        Tagger(TAGS),
        now=NOW + timedelta(minutes=15),
    )
    assert "Fresh council vote" not in client.get("/").text  # waiting for its date check
    assert client.get(f"/fragments/new?since={since}").text.strip() == ""

    checked = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        "UPDATE articles SET pubdate_checked_at = ?, outlet_published_at = ?"
        " WHERE url = 'https://cbc.ca/fresh'",
        (checked, "2026-09-24T15:50:00Z"),
    )
    assert "Fresh council vote" in client.get("/").text  # shown, with its date
    assert "1 new article" in client.get(f"/fragments/new?since={since}").text

    # a page that can't be checked still shows up once the hold is over
    run_ingest(
        conn,
        FakeSource([QueryResult("q", [rec("https://cbc.ca/slow", "Slow site story", NOW)])]),
        Tagger(TAGS),
        now=NOW + timedelta(minutes=30),
    )
    assert "Slow site story" not in client.get("/").text
    old = (datetime.now(UTC) - timedelta(minutes=16)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute("UPDATE articles SET retrieved_at = ? WHERE url = 'https://cbc.ca/slow'", (old,))
    assert "Slow site story" in client.get("/").text
    conn.close()


def test_no_hold_when_dates_are_not_checked(settings: Settings, client: TestClient) -> None:
    add_articles(settings, 1)  # collected just now, never date-checked
    assert "Bulk story 000" in client.get("/", params={"mix": "all"}).text
