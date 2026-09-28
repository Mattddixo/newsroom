"""Owners named in the news: name matching, storage at ingest, the card line, the filter."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from newsroom.names import alias_keys, display_name, name_key
from newsroom.services.ingest import run_ingest
from newsroom.services.mentions import NameIndex
from newsroom.services.tagging import Tagger
from newsroom.settings import Settings
from newsroom.sources.base import ArticleRecord, QueryResult
from newsroom.sources.gdelt_files import main_people, named
from newsroom.web.app import create_app
from tests.helpers import TAGS, FakeSource, add_edge, add_entity, make_db

NOW = datetime(2026, 9, 24, 18, 0, tzinfo=UTC)


def test_name_key_is_exact_up_to_case_accents_punctuation_and_legal_form() -> None:
    assert name_key("BCE Inc.") == name_key("bce inc") == name_key("BCE") == "bce"
    assert name_key("Québecor Média") == name_key("quebecor media")
    assert name_key("The Woodbridge Company Limited") == "woodbridge company"
    assert name_key("Nexstar Media Group, Inc.") == "nexstar media group"
    assert name_key("The New York Times Company") != name_key("The New York Times")
    assert name_key("A.G. Sulzberger") == name_key("A. G. Sulzberger")
    assert name_key("AP") == ""  # too short to match safely
    assert name_key("Bell Canada") != name_key("Bell Media")  # no partial matches


def test_alias_keys_keep_only_specific_aliases() -> None:
    keys = alias_keys("Rogers Communications", ["Rogers", "RCI", "Rogers Communications Inc."])
    assert keys == {"rogers communications", "rci"}  # "Rogers" alone names too much


def test_display_name() -> None:
    assert display_name("nato") == "NATO"
    assert display_name("world health organization") == "World Health Organization"
    assert display_name("Mark Carney") == "Mark Carney"


def test_gkg_names_parsing() -> None:
    persons = "Mark Carney,10;mark carney,90;Carney,120;Pierre Poilievre,200"
    orgs = "bce inc,5;bce inc,300;nato,40"
    assert named(persons, orgs) == (
        ("Mark Carney", 2),
        ("bce inc", 2),
        ("Carney", 1),
        ("Pierre Poilievre", 1),
        ("nato", 1),
    )
    assert main_people(persons) == (("Mark Carney", 2),)


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    """CBC News (cbc.ca) <- Canadian Broadcasting Corporation ("CBC"); The New York Times
    <- The New York Times Company <- A. G. Sulzberger; a set-aside owner for the Times."""
    c = make_db(Settings(data_dir=tmp_path).db_path, NOW)
    news = add_entity(c, "Q10", "CBC News")
    corp = add_entity(c, "Q11", "Canadian Broadcasting Corporation", "CBC", "CBC/Radio-Canada")
    nyt = add_entity(c, "Q20", "The New York Times")
    nytco = add_entity(c, "Q21", "The New York Times Company", "NYT Co.")
    person = add_entity(c, "Q22", "A. G. Sulzberger", "Arthur Gregg Sulzberger")
    old = add_entity(c, "Q23", "Old Owner Holdings")
    add_entity(c, "Q30", "Unrelated Holdings")
    add_edge(c, news, corp)
    add_edge(c, nyt, nytco)
    add_edge(c, nytco, person)
    add_edge(c, nyt, old, source="set_aside")
    c.execute("UPDATE outlets SET entity_id = ? WHERE domain = 'cbc.ca'", (news,))
    c.execute("UPDATE outlets SET entity_id = ? WHERE domain = 'nytimes.com'", (nyt,))
    return c


def test_index_skips_the_outlets_own_names(conn: sqlite3.Connection) -> None:
    index = NameIndex.load(conn)
    # CBC News naming "CBC" is the outlet talking about itself
    assert index.match("cbc.ca", [("cbc", 4), ("Canadian Broadcasting Corporation", 1)]) == {
        "Q11": 1
    }
    # the same name in the Times is a mention of the corporation
    assert index.match("nytimes.com", [("cbc", 4)]) == {"Q11": 4}
    assert index.match("nytimes.com", [("nyt co", 2), ("Arthur Gregg Sulzberger", 3)]) == {
        "Q21": 2,
        "Q22": 3,
    }


def _record(url: str, title: str, domain: str, names: tuple[tuple[str, int], ...]) -> ArticleRecord:
    return ArticleRecord(
        url=url,
        title=title,
        domain=domain,
        published_at=NOW - timedelta(hours=1),
        language="en",
        image_url=None,
        names=names,
    )


def _ingest(conn: sqlite3.Connection) -> None:
    records = [
        _record(
            "https://www.nytimes.com/1",
            "Times company reports earnings",
            "nytimes.com",
            (("the new york times company", 3), ("A.G. Sulzberger", 1), ("unrelated holdings", 2)),
        ),
        _record(
            "https://www.nytimes.com/2", "Old owner in court", "nytimes.com", (("old owner", 2),)
        ),
        _record(
            "https://www.nytimes.com/3",
            "Old owner holdings sued",
            "nytimes.com",
            (("old owner holdings", 2),),
        ),
        _record("https://www.cbc.ca/1", "CBC at the Olympics", "cbc.ca", (("cbc", 5),)),
    ]
    run_ingest(conn, FakeSource([QueryResult("u", records)]), Tagger(TAGS), now=NOW)


def test_ingest_stores_named_items(conn: sqlite3.Connection) -> None:
    _ingest(conn)
    rows = conn.execute(
        "SELECT a.url, m.qid, m.mentions FROM article_mentions m"
        " JOIN articles a ON a.id = m.article_id ORDER BY a.url, m.qid"
    ).fetchall()
    assert [tuple(r) for r in rows] == [
        ("https://www.nytimes.com/1", "Q21", 3),
        ("https://www.nytimes.com/1", "Q22", 1),
        ("https://www.nytimes.com/1", "Q30", 2),
        ("https://www.nytimes.com/3", "Q23", 2),
    ]  # "old owner" isn't "Old Owner Holdings": no partial matches
    names = conn.execute("SELECT names FROM articles WHERE url LIKE '%nytimes.com/1'").fetchone()
    assert names[0] == "the new york times company · unrelated holdings"


def test_card_names_owners_in_the_chain_only(conn: sqlite3.Connection, tmp_path: Path) -> None:
    _ingest(conn)
    client = TestClient(create_app(Settings(data_dir=tmp_path, rate_limit="1000/minute")))
    html = client.get("/?mix=all").text
    card = html[html.index("Times company reports earnings") :]
    card = card[: card.index("</li>")]
    assert 'href="/owner/Q21"' in card and 'href="/owner/Q22"' in card
    # the chain, on hover
    assert "A. G. Sulzberger → The New York Times Company → The New York Times" in card
    assert "The New York Times's ownership chain" in card
    assert "Unrelated Holdings" not in card  # named, but not in this outlet's chain
    # a set-aside (out-of-date) owner isn't in the chain
    card = html[html.index("Old owner holdings sued") :]
    assert "ownership chain" not in card[: card.index("</li>")]

    filtered = client.get("/?mentions=owner&mix=all").text
    assert "Times company reports earnings" in filtered
    assert "Old owner holdings sued" not in filtered
    assert "CBC at the Olympics" not in filtered
