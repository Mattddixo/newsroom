"""Feed page: date grouping, filters, search, pagination, escaping."""

from __future__ import annotations

import re
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
        ),
        rec("https://cbc.ca/b", "Byelection called in Toronto riding", NOW - timedelta(hours=3)),
        # 03:30 UTC on the 24th is 23:30 on the 23rd in Toronto: must group under the 23rd
        rec(
            "https://cbc.ca/c", "Late night council vote", datetime(2026, 9, 24, 3, 30, tzinfo=UTC)
        ),
        rec(
            "https://ici.radio-canada.ca/d",
            "Élection partielle à Montréal",
            NOW - timedelta(days=2),
            "radio-canada.ca",
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
    by_day = {d.day.isoformat(): [a.title for a in d.articles] for d in page.days}
    assert "Late night council vote" in by_day["2026-09-23"]
    assert list(by_day) == sorted(by_day, reverse=True)


def test_article_links_are_safe(client: TestClient) -> None:
    html = client.get("/").text
    assert 'href="https://cbc.ca/a" target="_blank" rel="noopener noreferrer"' in html
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


def test_pagination(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(queries, "PAGE_SIZE", 2)
    client = TestClient(create_app(settings))
    seen: list[str] = []
    url = "/"
    for _ in range(5):
        html = client.get(url).text
        seen += titles(html)
        m = re.search(r'href="(/\?[^"]*before=[^"]+)" rel="next"', html)
        if not m:
            break
        url = m.group(1).replace("&amp;", "&")
    assert len(seen) == 5
    assert len(set(seen)) == 5


def test_tag_and_outlet_links_keep_filters(client: TestClient) -> None:
    html = client.get("/", params={"country": "CA"}).text
    assert 'href="/?tag=housing&amp;country=CA"' in html
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
    assert "No data yet" in resp.text


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
