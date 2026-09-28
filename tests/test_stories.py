"""Stories covered by several outlets, and coverage by owner."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from newsroom.config import OutletConfig
from newsroom.ownership_view import Graph
from newsroom.services import stories
from newsroom.services.ingest import run_ingest, sync_outlets
from newsroom.services.tagging import Tagger
from newsroom.settings import Settings
from newsroom.sources.base import ArticleRecord, QueryResult
from newsroom.web import queries
from newsroom.web.app import create_app
from tests.helpers import OUTLETS, TAGS, FakeSource, add_edge, add_entity, make_db

NOW = datetime(2026, 9, 24, 18, 0, tzinfo=UTC)
MORE_OUTLETS = [
    *OUTLETS,
    OutletConfig("ctvnews.ca", "CTV News", "CA", "en"),
    OutletConfig("globalnews.ca", "Global News", "CA", "en"),
]


def test_headline_terms() -> None:
    terms = stories.headline_terms("Carney announces 25% tariffs on U.S. steel, says it's time")
    assert set(terms) == {"carney", "announce", "tariff", "steel"}
    assert terms["tariff"] == "tariffs"  # shown as written


def test_article_terms_count_a_person_once() -> None:
    terms = stories.article_terms("Carney hits back on steel", "Mark Carney · Dominic LeBlanc", "")
    assert "carney" in terms and "@mark carney" not in terms  # the headline word stands for him
    assert terms["@dominic leblanc"] == "Dominic LeBlanc"
    # before `names` was kept: the people in `about`, not its countries
    old = stories.article_terms("Budget day", "", "Chrystia Freeland · Canada · European Union")
    assert set(old) == {"budget", "@chrystia freeland"}


def _rec(url: str, title: str, minutes: int, names: str = "") -> ArticleRecord:
    domain = url.split("/")[2].removeprefix("www.")
    return ArticleRecord(
        url=url,
        title=title,
        domain=domain,
        published_at=NOW - timedelta(minutes=minutes),
        language="en",
        image_url=None,
        names=tuple((n, 2) for n in names.split(" · ") if n),
    )


BACKGROUND = [  # unrelated headlines, so word rarity is realistic
    "Toronto police investigate downtown shooting",
    "Ontario school boards warn of budget shortfall",
    "Stock markets rally as tech shares rebound",
    "Wildfire smoke prompts air quality warnings in Alberta",
    "Housing starts rise in Canada, CMHC says",
    "Trump signs executive order on artificial intelligence",
    "Blue Jays beat Yankees in extra innings",
    "Quebec announces new rules for daycare centres",
    "Air Canada pilots reach tentative deal",
    "Federal court strikes down firearms regulation",
    "Hurricane season forecast raised by meteorologists",
    "Bank of Canada holds interest rate steady",
]


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    c = make_db(Settings(data_dir=tmp_path).db_path, NOW)
    sync_outlets(c, MORE_OUTLETS, NOW)
    records = [
        _rec("https://www.cbc.ca/steel", "Carney announces 25% tariffs on U.S. steel", 300,
             "Mark Carney"),
        _rec("https://www.nytimes.com/steel",
             "Canada hits back with steel tariffs, Carney says", 240, "Mark Carney · Donald Trump"),
        _rec("https://www.ctvnews.ca/steel", "Steel tariffs: what Carney's move means", 200),
        _rec("https://www.cbc.ca/steel-2", "Steel tariffs take effect at midnight, Carney says",
             100, "Mark Carney"),  # a second CBC piece: joins through the other outlets
        _rec("https://www.globalnews.ca/other", "Carney visits flood-hit town in Manitoba", 90,
             "Mark Carney"),  # same person, different story
        *[
            _rec(f"https://www.globalnews.ca/bg{i}", title, 400 + i)
            for i, title in enumerate(BACKGROUND)
        ],
    ]  # fmt: skip
    run_ingest(c, FakeSource([QueryResult("u", records)]), Tagger(TAGS), now=NOW)
    return c


def _stories(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        r["url"]: r["story_id"]
        for r in conn.execute(
            "SELECT a.url, s.story_id FROM article_stories s JOIN articles a ON a.id = s.article_id"
        )
    }


def test_link_stories_groups_the_same_story_only(conn: sqlite3.Connection) -> None:
    linked = stories.link_stories(conn, NOW)
    by_url = _stories(conn)
    steel = {u: s for u, s in by_url.items() if u.endswith(("steel", "steel-2"))}
    assert len(set(steel.values())) == 1  # one story
    assert linked == 3
    first = conn.execute("SELECT id FROM articles WHERE url LIKE '%cbc.ca/steel'").fetchone()[0]
    assert set(steel.values()) == {first}  # named after its first article
    assert by_url["https://www.globalnews.ca/other"] != first  # same person, other story
    assert len(by_url) == 5 + len(BACKGROUND)  # every article has its (own) story
    shared = conn.execute(
        "SELECT s.shared FROM article_stories s JOIN articles a ON a.id = s.article_id"
        " WHERE a.url LIKE '%nytimes.com/steel'"
    ).fetchone()[0]
    assert set(shared.split(" · ")) >= {"steel", "tariffs", "Carney"}

    # assigned once: running again changes nothing
    assert stories.link_stories(conn, NOW + timedelta(minutes=15)) == 0
    assert _stories(conn) == by_url


def test_story_takes_articles_only_while_open(conn: sqlite3.Connection) -> None:
    stories.link_stories(conn, NOW)
    late = [
        _rec("https://www.globalnews.ca/steel-late",
             "Carney steel tariffs anniversary: what changed", -60 * 26, "Mark Carney"),
    ]  # fmt: skip
    later = NOW + timedelta(hours=27)
    run_ingest(conn, FakeSource([QueryResult("u", late)]), Tagger(TAGS), now=later)
    stories.link_stories(conn, later)
    by_url = _stories(conn)
    assert by_url["https://www.globalnews.ca/steel-late"] != by_url["https://www.cbc.ca/steel"]


def test_feed_and_story_page(conn: sqlite3.Connection, tmp_path: Path) -> None:
    stories.link_stories(conn, NOW)
    client = TestClient(create_app(Settings(data_dir=tmp_path, rate_limit="1000/minute")))
    html = client.get("/?mix=all").text
    card = html[html.index("Canada hits back with steel tariffs") :]
    card = card[: card.index("</li>")]
    assert "Also covered by" in card
    assert "CBC News and CTV News" in card  # other outlets, in the order they published
    story_id = _stories(conn)["https://www.cbc.ca/steel"]
    assert f'href="/story/{story_id}"' in card
    lone = html[html.index("Carney visits flood-hit town") :]
    assert "Also covered by" not in lone[: lone.index("</li>")]

    page = client.get(f"/story/{story_id}").text
    assert "4 articles from 3 outlets" in page
    assert page.index("Carney announces 25%") < page.index("Canada hits back")  # in order
    assert "Grouped by:" in page
    assert "Who&#39;s covering it" in page or "Who's covering it" in page
    assert client.get("/story/999999").status_code == 404


def test_coverage_by_owner(conn: sqlite3.Connection, tmp_path: Path) -> None:
    """CTV News <- Bell Media <- BCE (public company): counted under BCE. Global News has
    no owner recorded; the rest aren't matched to Wikidata."""
    ctv = add_entity(conn, "Q1", "CTV News")
    bell = add_entity(conn, "Q2", "Bell Media")
    bce = add_entity(conn, "Q3", "BCE Inc.", kind="public company")
    shareholder = add_entity(conn, "Q4", "Big Fund")
    add_edge(conn, ctv, bell)
    add_edge(conn, bell, bce)
    add_edge(conn, bce, shareholder)
    global_news = add_entity(conn, "Q5", "Global News")
    conn.execute("UPDATE outlets SET entity_id = ? WHERE domain = 'ctvnews.ca'", (ctv,))
    conn.execute("UPDATE outlets SET entity_id = ? WHERE domain = 'globalnews.ca'", (global_news,))

    graph = Graph.load(conn)
    assert [n.name for n in graph.owner_group(ctv)] == ["BCE Inc."]  # stops at a public company
    assert graph.owner_group(global_news) == ()

    f = queries.FeedFilters.parse({"mix": "all"})
    counts = queries.outlet_counts(conn, f, NOW.tzinfo)  # type: ignore[arg-type]
    shares = queries.by_owner(conn, graph, counts)
    assert [(tuple(n.name for n in s.owners), s.matched, s.articles) for s in shares] == [
        (("BCE Inc.",), True, 1),
        ((), True, 1 + len(BACKGROUND)),  # Global News: no owner recorded
        ((), False, 3),  # CBC News (2) and the Times (1): not matched
    ]
    assert sum(s.articles for s in shares) == sum(counts.values())

    client = TestClient(create_app(Settings(data_dir=tmp_path, rate_limit="1000/minute")))
    html = client.get("/fragments/by-owner?q=steel&mix=all").text
    assert "BCE Inc." in html and "?q=steel&amp;owner=Q3" in html
    assert "No majority owner recorded" not in html  # Global News has no steel story
    assert "No ownership record" in html
