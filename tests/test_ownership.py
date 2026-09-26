"""Matching, chain resolution, curation, logos and the ownership pages."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from newsroom.config import ConfigError, OutletConfig
from newsroom.net.safe_fetch import FetchBlocked, FetchResult
from newsroom.ownership_view import Graph
from newsroom.services import ownership
from newsroom.services.ingest import run_ingest, sync_outlets
from newsroom.services.tagging import Tagger
from newsroom.settings import Settings
from newsroom.sources.base import QueryResult
from newsroom.web.app import create_app
from tests.helpers import TAGS, FakeSource, make_db, rec
from tests.wd_fake import FakeWikidata

NOW = datetime(2026, 9, 24, 12, tzinfo=UTC)
OUTLETS = [
    OutletConfig("exampledaily.ca", "Example Daily", "CA", "en"),
    OutletConfig("twonames.ca", "Two Names", "CA", "en"),
    OutletConfig("unknownowner.com", "Unknown Owner Weekly", "US", "en"),
    OutletConfig("nomatch.org", "No Match", "US", "en"),
]
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 20


def resolve_all(conn: sqlite3.Connection, fake: FakeWikidata, when: datetime = NOW) -> None:
    source = fake.source()
    due = ownership.due_outlets(conn, when, timedelta(days=7))
    ownership.match_outlets(conn, source, due, when)
    ownership.resolve_ownership(conn, source, due, when)


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    c = make_db(tmp_path / "db.sqlite3", NOW)
    sync_outlets(c, OUTLETS, NOW)
    return c


def outlet(conn: sqlite3.Connection, domain: str) -> sqlite3.Row:
    return conn.execute("SELECT * FROM outlets WHERE domain = ?", (domain,)).fetchone()


def test_matching_statuses(conn: sqlite3.Connection) -> None:
    fake = FakeWikidata()
    summary = ownership.match_outlets(
        conn, fake.source(), ownership.due_outlets(conn, NOW, timedelta(days=7)), NOW
    )
    assert (summary.matched, summary.ambiguous, summary.unmatched) == (2, 1, 1)
    daily = outlet(conn, "exampledaily.ca")
    assert daily["match_status"] == "auto"
    assert daily["wikidata_qid"] == "Q1001"
    assert daily["match_source_url"] == "https://www.wikidata.org/wiki/Q1001#P856"
    two = outlet(conn, "twonames.ca")
    assert two["match_status"] == "ambiguous"
    assert two["wikidata_qid"] is None
    cands = conn.execute(
        "SELECT qid FROM outlet_match_candidates WHERE outlet_id = ? ORDER BY qid", (two["id"],)
    ).fetchall()
    assert [c[0] for c in cands] == ["Q2001", "Q2002"]
    assert outlet(conn, "nomatch.org")["match_status"] == "unmatched"


def test_chain_resolution_with_sources(conn: sqlite3.Connection) -> None:
    resolve_all(conn, FakeWikidata())
    graph = Graph.load(conn)
    daily = graph.by_qid("Q1001")
    assert daily is not None
    assert outlet(conn, "exampledaily.ca")["entity_id"] == daily.id

    chain = graph.chain(daily.id)
    assert [(s.edge.relation, s.parent.qid) for s in chain] == [("owned_by", "Q1002")]
    group = chain[0]
    assert group.edge.source == "wikidata"
    assert group.edge.source_url == "https://www.wikidata.org/wiki/Q1001#P127"
    assert group.edge.retrieved_at == "2026-09-24T12:00:00Z"
    above = {s.parent.qid: s for s in group.above}
    assert set(above) == {"Q1003", "Q1004"}
    assert above["Q1003"].edge.share == pytest.approx(0.6)
    assert above["Q1003"].edge.source_url.endswith("Q1002#P749")
    # Q1004 owns Q1002 which owns ... Q1004: the loop is cut and flagged, not followed forever
    assert above["Q1004"].above[0].parent.qid == "Q1002"
    assert above["Q1004"].above[0].cycle

    holdings = graph.by_qid("Q1003")
    assert holdings is not None
    assert holdings.kind == "public company"
    assert holdings.country == "United States"
    assert holdings.website is None
    cik = conn.execute(
        "SELECT value, source_url FROM entity_identifiers WHERE scheme = 'sec_cik'"
    ).fetchone()
    assert tuple(cik) == ("0000123456", "https://www.wikidata.org/wiki/Q1003#P5531")

    s = graph.summary(daily.id)
    assert [n.qid for _, n in s.direct] == ["Q1002"]
    # the line follows the first owner at each step; the branch (Q1003) is flagged as "more"
    assert [(e.relation, n.qid) for e, n in s.above] == [("owned_by", "Q1004")]
    assert s.more

    # Nothing we were told to ignore made it in
    qids = {r[0] for r in conn.execute("SELECT qid FROM entities")}
    assert not qids & {"Q1005", "Q1009", "Q1010"}


def test_unknown_owner_and_unmatched_are_not_disclosed(conn: sqlite3.Connection) -> None:
    resolve_all(conn, FakeWikidata())
    graph = Graph.load(conn)
    weekly = outlet(conn, "unknownowner.com")
    s = graph.summary(weekly["entity_id"])
    assert s.entity is not None and s.direct == [] and s.above == []
    assert graph.summary(outlet(conn, "nomatch.org")["entity_id"]).entity is None


def test_depth_limit(conn: sqlite3.Connection) -> None:
    fake = FakeWikidata()
    source = fake.source()
    due = ownership.due_outlets(conn, NOW, timedelta(days=7))
    ownership.match_outlets(conn, source, due, NOW)
    ownership.resolve_ownership(conn, source, due, NOW, max_depth=1)
    graph = Graph.load(conn)
    daily = graph.by_qid("Q1001")
    assert daily is not None
    chain = graph.chain(daily.id)
    assert chain[0].parent.qid == "Q1002"  # fetched for its name
    assert chain[0].above == []  # but not followed further


def test_refresh_is_idempotent_and_replaces_edges(conn: sqlite3.Connection) -> None:
    fake = FakeWikidata()
    resolve_all(conn, fake)
    edges_before = conn.execute("SELECT count(*) FROM ownership_edges").fetchone()[0]
    # Wikidata changes: Example Daily is no longer owned by anyone.
    fake.entities["Q1001"]["claims"].pop("P127")
    later = NOW + timedelta(days=8)
    resolve_all(conn, fake, later)
    graph = Graph.load(conn)
    daily = graph.by_qid("Q1001")
    assert daily is not None and graph.chain(daily.id) == []
    # The old chain is unreachable from any outlet now, so it is cleaned up.
    assert {n.qid for n in graph.nodes.values()} == {"Q1001", "Q1008"}
    assert edges_before > 0
    assert conn.execute("SELECT count(*) FROM ownership_edges").fetchone()[0] == 0


def test_not_due_outlets_are_skipped(conn: sqlite3.Connection) -> None:
    resolve_all(conn, FakeWikidata())
    assert ownership.due_outlets(conn, NOW + timedelta(days=1), timedelta(days=7)) == []
    assert len(ownership.due_outlets(conn, NOW + timedelta(days=8), timedelta(days=7))) == 4


def test_manual_qid_is_never_overwritten(conn: sqlite3.Connection) -> None:
    fake = FakeWikidata()
    ownership.set_qid(conn, "twonames.ca", "Q1008", NOW)
    ownership.set_qid(conn, "nomatch.org", None, NOW)
    resolve_all(conn, fake)
    assert outlet(conn, "twonames.ca")["wikidata_qid"] == "Q1008"
    assert outlet(conn, "twonames.ca")["match_status"] == "manual"
    assert outlet(conn, "nomatch.org")["match_status"] == "manual"
    with pytest.raises(ValueError):
        ownership.set_qid(conn, "twonames.ca", "12345", NOW)
    with pytest.raises(LookupError):
        ownership.set_qid(conn, "nope.example", "Q1", NOW)


def test_confirm(conn: sqlite3.Connection) -> None:
    resolve_all(conn, FakeWikidata())
    assert ownership.confirm(conn, ["exampledaily.ca"]) == 1
    assert outlet(conn, "exampledaily.ca")["match_status"] == "confirmed"
    # a confirmed match survives re-matching even if Wikidata's website data changes
    fake = FakeWikidata()
    fake.sparql["results"]["bindings"] = []
    resolve_all(conn, fake, NOW + timedelta(days=8))
    assert outlet(conn, "exampledaily.ca")["wikidata_qid"] == "Q1001"


def test_manual_edge_requires_source_and_survives_refresh(conn: sqlite3.Connection) -> None:
    fake = FakeWikidata()
    resolve_all(conn, fake)
    source = fake.source()
    with pytest.raises(ValueError, match="source URL"):
        ownership.add_manual_edge(conn, source, "Q1008", "Q1003", "owned_by", "", NOW)
    with pytest.raises(ValueError):
        ownership.add_manual_edge(conn, source, "Q1008", "Q1008", "owned_by", "https://x.org", NOW)
    ownership.add_manual_edge(
        conn, source, "Q1008", "Q1003", "owned_by", "https://example.org/filing.pdf", NOW
    )
    resolve_all(conn, fake, NOW + timedelta(days=8))
    graph = Graph.load(conn)
    weekly = graph.by_qid("Q1008")
    assert weekly is not None
    chain = graph.chain(weekly.id)
    assert [(s.parent.qid, s.edge.source) for s in chain] == [("Q1003", "manual")]
    assert chain[0].edge.source_url == "https://example.org/filing.pdf"
    assert ownership.remove_manual_edge(conn, "Q1008", "Q1003") == 1


def test_failed_fetch_leaves_database_unchanged(conn: sqlite3.Connection) -> None:
    fake = FakeWikidata()
    resolve_all(conn, fake)
    before = conn.execute("SELECT count(*) FROM ownership_edges").fetchone()[0]

    class Broken(FakeWikidata):
        def handler(self, request):  # type: ignore[no-untyped-def]
            import httpx

            return httpx.Response(503)

    with pytest.raises(Exception, match="giving up"):
        resolve_all(conn, Broken(), NOW + timedelta(days=8))
    assert conn.execute("SELECT count(*) FROM ownership_edges").fetchone()[0] == before


def test_logos(conn: sqlite3.Connection, tmp_path: Path) -> None:
    resolve_all(conn, FakeWikidata())
    fetched: list[str] = []

    def fetch(url: str) -> FetchResult:
        fetched.append(url)
        return FetchResult(url=url, content_type="image/png", body=PNG)

    logo_dir = tmp_path / "logos"
    assert ownership.refresh_logos(conn, fetch, logo_dir, NOW) == 1
    row = outlet(conn, "exampledaily.ca")
    assert row["logo_path"] == "Q1001.png"
    assert row["logo_source_url"].startswith(
        "https://commons.wikimedia.org/wiki/File:Example_Daily"
    )
    assert (logo_dir / "Q1001.png").read_bytes() == PNG
    assert fetched[0].startswith("https://commons.wikimedia.org/wiki/Special:FilePath/")
    # fresh: not fetched again
    assert ownership.refresh_logos(conn, fetch, logo_dir, NOW + timedelta(days=1)) == 0

    def bad(url: str) -> FetchResult:
        raise FetchBlocked("nope")

    assert ownership.refresh_logos(conn, bad, logo_dir, NOW + timedelta(days=40)) == 0

    def not_image(url: str) -> FetchResult:
        return FetchResult(url=url, content_type="image/png", body=b"<html>")

    conn.execute("UPDATE outlets SET logo_path = NULL")
    assert ownership.refresh_logos(conn, not_image, logo_dir, NOW) == 0
    assert not (logo_dir / "Q1001.png").exists()  # unreferenced file removed


# ------------------------------------------------------------------ web


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    settings = Settings(data_dir=tmp_path, rate_limit="1000/minute")
    c = make_db(settings.db_path, NOW)
    sync_outlets(c, OUTLETS, NOW)
    resolve_all(c, FakeWikidata())
    ownership.refresh_logos(c, lambda u: FetchResult(u, "image/png", PNG), settings.logo_dir, NOW)
    run_ingest(
        c,
        FakeSource(
            [
                QueryResult(
                    "q",
                    [
                        rec(
                            "https://exampledaily.ca/1",
                            "Housing news",
                            NOW - timedelta(hours=1),
                            "exampledaily.ca",
                        ),
                        rec(
                            "https://nomatch.org/2",
                            "Other news",
                            NOW - timedelta(hours=2),
                            "nomatch.org",
                        ),
                    ],
                )
            ]
        ),
        Tagger(TAGS),
        now=NOW,
    )
    c.close()
    return TestClient(create_app(settings))


def test_card_ownership_line(client: TestClient) -> None:
    html = client.get("/").text
    assert "Owned by Example Media Group, whose owner is Example Family Trust, …" in html
    assert "Owner: no record found" in html
    assert 'hx-get="/fragments/ownership/exampledaily.ca"' in html
    assert '<img class="logo" src="/logos/Q1001.png"' in html


def test_ownership_fragment_links_every_claim_to_source(client: TestClient) -> None:
    html = client.get("/fragments/ownership/exampledaily.ca").text
    assert 'href="https://www.wikidata.org/wiki/Q1001#P127"' in html
    assert 'href="https://www.wikidata.org/wiki/Q1002#P749"' in html
    assert "(60%)" in html
    assert "since 2019-03" in html
    assert "retrieved 2026-09-24" in html
    assert "<html" not in html  # a fragment, not a page


def test_outlet_page(client: TestClient) -> None:
    html = client.get("/outlet/exampledaily.ca").text
    assert "Example Media Group" in html
    assert "Matched by its official website" in html
    assert "Housing news" in html
    assert "Wikimedia Commons" in html
    unmatched = client.get("/outlet/nomatch.org").text
    assert '<a href="/about#no-record">no record found</a>' in unmatched
    assert client.get("/outlet/not-an-outlet.com").status_code == 404
    assert client.get("/outlet/..%2Fetc").status_code == 404


def test_owner_pages(client: TestClient) -> None:
    owners = client.get("/owners").text
    assert 'href="/owner/Q1003"' in owners
    owner = client.get("/owner/Q1003").text
    assert "Example Daily" in owner
    assert "Example Media Group" in owner  # held through
    assert 'href="/?owner=Q1003"' in owner
    assert client.get("/owner/Q999999").status_code == 404
    assert client.get("/owner/DROP").status_code == 404


def test_owner_filter(client: TestClient) -> None:
    html = client.get("/", params={"owner": "Q1003"}).text
    assert "Housing news" in html
    assert "Other news" not in html


def test_logo_route(client: TestClient) -> None:
    resp = client.get("/logos/Q1001.png")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert resp.content == PNG
    for bad in ("../db/newsroom.sqlite3", "Q1001.svg", "x.png", "Q1002.png"):
        assert client.get(f"/logos/{bad}").status_code == 404


def test_outlets_and_about_pages(client: TestClient) -> None:
    html = client.get("/outlets").text
    assert "Example Daily" in html and "no record found" in html
    assert 'id="no-record"' in client.get("/about").text


def test_tables_and_funding_link_to_owner_pages(client: TestClient) -> None:
    outlets = client.get("/outlets").text
    assert 'Owned by <a href="/owner/Q1002">Example Media Group</a>' in outlets
    assert 'whose owner is <a href="/owner/Q1004">Example Family Trust</a>' in outlets
    assert 'Owner: <a href="/about#no-record">no record found</a>' in outlets
    assert 'href="/?country=CA"' in outlets
    owner = client.get("/owner/Q1003").text
    assert '<a href="/owner/Q1002">Example Media Group</a>' in owner  # held through


def test_panel_links_are_not_captured_by_the_panel(client: TestClient) -> None:
    """The <details> loads its panel with hx-target/hx-swap. Without hx-disinherit, the
    (boosted) links inside the panel inherit that target, find nothing and do nothing."""
    html = client.get("/").text
    for tag in re.findall(r"<details class=\"own\"[^>]*>", html):
        assert 'hx-disinherit="*"' in tag
    for tag in re.findall(r"<div class=\"new-articles\"[^>]*>", html):
        assert 'hx-disinherit="*"' in tag


def test_every_internal_link_opens(client: TestClient) -> None:
    """Crawl every page and panel; each same-site link must load, and #anchors must exist."""
    start = ["/", "/outlets", "/owners", "/about"]
    start += [f"/fragments/ownership/{o.domain}" for o in OUTLETS]
    seen: set[str] = set()
    queue = list(start)
    ids: dict[str, set[str]] = {}
    anchors: list[tuple[str, str, str]] = []
    while queue:
        url = queue.pop()
        if url in seen:
            continue
        seen.add(url)
        resp = client.get(url)
        assert resp.status_code == 200, url
        if "text/html" not in resp.headers["content-type"]:
            continue
        html = resp.text
        ids[url] = set(re.findall(r'\bid="([^"]+)"', html))
        for href in re.findall(r'href="(/[^"]*)"', html):
            href = href.replace("&amp;", "&")
            path, _, anchor = href.partition("#")
            if anchor:
                anchors.append((url, path or url, anchor))
            if path and not path.startswith("/static/"):
                queue.append(path)
        assert len(seen) < 400
    for page, target, anchor in anchors:
        assert anchor in ids.get(target, set()), f"{page} links to missing {target}#{anchor}"
    assert any(u.startswith("/owner/") for u in seen) and any(
        u.startswith("/outlet/") for u in seen
    )


def _item(qid: str, label: str, website: str) -> dict:
    return {
        "id": qid,
        "labels": {"en": {"value": label}},
        "claims": {
            "P856": [
                {
                    "rank": "normal",
                    "mainsnak": {"snaktype": "value", "datavalue": {"value": website}},
                }
            ]
        },
    }


def test_several_items_with_the_website_pick_the_news_outlet(conn: sqlite3.Connection) -> None:
    fake = FakeWikidata()
    fake.news = {"Q2001"}  # "Two Names News" is a newspaper; Q2002 is the company
    due = ownership.due_outlets(conn, NOW, timedelta(days=7))
    ownership.match_outlets(conn, fake.source(), due, NOW)
    two = outlet(conn, "twonames.ca")
    assert (two["match_status"], two["wikidata_qid"]) == ("auto", "Q2001")
    assert "only news outlet among 2" in two["match_note"]
    fake.news = {"Q2001", "Q2002"}  # both news outlets: no guess
    conn.execute("UPDATE outlets SET match_status = 'unmatched'")
    ownership.match_outlets(conn, fake.source(), due, NOW)
    assert outlet(conn, "twonames.ca")["match_status"] == "ambiguous"


def test_name_search_accepts_only_the_outlets_own_website(conn: sqlite3.Connection) -> None:
    fake = FakeWikidata()
    fake.entities.update(
        {
            "Q3001": _item("Q3001", "No Match (radio)", "https://sports.nomatch.org/"),
            "Q3002": _item("Q3002", "No Match", "https://www.nomatch.org/news"),
            "Q3003": _item("Q3003", "No Match Band", "https://nomatch-band.com/"),
        }
    )
    fake.search_results = {"No Match": ["Q3003", "Q3001", "Q3002"]}
    due = ownership.due_outlets(conn, NOW, timedelta(days=7))
    ownership.match_outlets(conn, fake.source(), due, NOW)
    o = outlet(conn, "nomatch.org")
    # the item on the outlet's own domain wins over a subdomain; the band is ignored
    assert (o["match_status"], o["wikidata_qid"]) == ("auto", "Q3002")
    assert o["match_note"] == "a search for its name; its official website checked"

    fake.search_results = {"No Match": ["Q3003"]}  # only an unrelated item: nothing
    conn.execute("UPDATE outlets SET match_status = 'unmatched' WHERE domain = 'nomatch.org'")
    ownership.match_outlets(conn, fake.source(), due, NOW)
    assert outlet(conn, "nomatch.org")["match_status"] == "unmatched"


def test_outlets_yaml_pin(conn: sqlite3.Connection) -> None:
    pinned = [replace(OUTLETS[0], wikidata="Q1011"), replace(OUTLETS[3], wikidata="none")]
    sync_outlets(conn, [*pinned, *OUTLETS[1:3]], NOW)
    daily, none = outlet(conn, "exampledaily.ca"), outlet(conn, "nomatch.org")
    assert (daily["match_status"], daily["wikidata_qid"]) == ("manual", "Q1011")
    assert daily["match_note"] == "set in outlets.yaml"
    assert (none["match_status"], none["wikidata_qid"]) == ("manual", None)
    resolve_all(conn, FakeWikidata())  # automatic matching leaves pins alone
    assert outlet(conn, "exampledaily.ca")["wikidata_qid"] == "Q1011"
    sync_outlets(conn, OUTLETS, NOW)  # the line removed: automatic again
    back = outlet(conn, "exampledaily.ca")
    assert (back["match_status"], back["wikidata_qid"]) == ("unmatched", None)


@pytest.mark.parametrize(
    ("kind", "shown"),
    [
        ("broadcaster; production company; Crown corporation", "Crown corporation"),
        ("newspaper; public company", "public company"),
        ("human", "person"),
        ("television network", "television network"),  # nothing better: the first
        ("", ""),
    ],
)
def test_owner_type_is_the_most_telling_kind(kind: str, shown: str) -> None:
    from newsroom.ownership_view import Node

    assert Node(1, "Q1", "X", "", kind, "", None, "u", "t").type_label == shown


def _claim(prop_value: str) -> dict:
    return {
        "rank": "normal",
        "mainsnak": {"snaktype": "value", "datavalue": {"value": {"id": prop_value}}},
    }


def test_a_person_who_has_died_is_not_a_current_owner(conn: sqlite3.Connection) -> None:
    fake = FakeWikidata()
    founder = {
        "id": "Q9001",
        "labels": {"en": {"value": "Late Founder"}},
        "claims": {
            "P31": [_claim("Q5")],
            "P570": [
                {
                    "rank": "normal",
                    "mainsnak": {
                        "snaktype": "value",
                        "datavalue": {"value": {"time": "+2012-03-01T00:00:00Z", "precision": 11}},
                    },
                }
            ],
        },
    }
    fake.entities["Q9001"] = founder
    daily = fake.entities["Q1001"]
    daily["claims"]["P127"] = [*daily["claims"]["P127"], _claim("Q9001")]
    resolve_all(conn, fake)
    graph = Graph.load(conn)
    s = graph.summary(outlet(conn, "exampledaily.ca")["entity_id"])
    # the statement has no end date, but its owner died in 2012: left out
    assert [n.qid for _, n in s.direct] == ["Q1002"]


def test_ownership_report(conn: sqlite3.Connection, tmp_path: Path, capsys, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from newsroom import cli

    resolve_all(conn, FakeWikidata())
    monkeypatch.setattr(cli, "get_settings", lambda: Settings(data_dir=tmp_path))
    monkeypatch.setattr(cli, "connect", lambda _: conn)
    assert cli.cmd_ownership_report(None) == 0  # type: ignore[arg-type]
    out = capsys.readouterr().out
    assert "CHECK nomatch.org" in out and "flags: no Wikidata item" in out
    assert "CHECK unknownowner.com" in out and "no owner listed" in out
    assert "shown: Owned by Example Media Group, whose owner is Example Family Trust, …" in out


def test_cited_corrections_add_and_set_aside(conn: sqlite3.Connection, tmp_path: Path) -> None:
    from newsroom.config import load_ownership_corrections

    path = tmp_path / "ownership.yaml"
    path.write_text(
        "corrections:\n"
        "  - outlet: exampledaily.ca\n"
        "    remove_owner: Example Media Group\n"
        "    add_owner: Q1011\n"
        "    source: https://example.org/sold\n"
        "    checked: 2026-09-20\n"
        "  - outlet: nomatch.org\n"  # no Wikidata item yet: skipped, not an error
        "    add_owner: Q1011\n"
        "    source: https://example.org/x\n"
        "    checked: 2026-09-20\n"
    )
    fixes = load_ownership_corrections(path)
    fake = FakeWikidata()
    source = fake.source()
    due = ownership.due_outlets(conn, NOW, timedelta(days=7))
    ownership.match_outlets(conn, source, due, NOW)
    resolve_all(conn, fake)  # Example Media Group is now a known name
    ownership.resolve_ownership(conn, source, due, NOW, corrections=fixes)
    graph = Graph.load(conn)
    daily = outlet(conn, "exampledaily.ca")
    s = graph.summary(daily["entity_id"])
    assert [(e.source, n.qid) for e, n in s.direct] == [("correction", "Q1011")]
    aside = graph.set_aside[daily["entity_id"]]
    assert [graph.nodes[e.parent].qid for e in aside] == ["Q1002"]
    assert aside[0].source_url == "https://example.org/sold"
    settings = Settings(data_dir=tmp_path / "site", rate_limit="1000/minute")
    settings.db_path.parent.mkdir(parents=True)
    copy = sqlite3.connect(settings.db_path)
    conn.backup(copy)
    copy.close()
    client = TestClient(create_app(settings))
    page = client.get("/outlet/exampledaily.ca").text
    assert "Set aside as out of date:" in page and "owned by Example Media Group" in page
    assert ">correction</span>" in page


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("corrections:\n  - outlet: a.ca\n    add_owner: X\n    checked: 2026-01-01\n", "source"),
        (
            "corrections:\n  - outlet: a.ca\n    add_owner: X\n    source: https://x.org\n",
            "checked",
        ),
        (
            "corrections:\n  - outlet: a.ca\n    source: https://x.org\n    checked: 2026-01-01\n",
            "needs one of",
        ),
        (
            "corrections:\n  - entity: X\n    outlet: a.ca\n    add_owner: Y\n"
            "    source: https://x.org\n    checked: 2026-01-01\n",
            "exactly one",
        ),
    ],
)
def test_correction_validation(tmp_path: Path, body: str, message: str) -> None:
    from newsroom.config import load_ownership_corrections

    path = tmp_path / "ownership.yaml"
    path.write_text(body)
    with pytest.raises(ConfigError, match=message):
        load_ownership_corrections(path)


def test_shareholders_and_public_companies_in_the_summary() -> None:
    from newsroom.ownership_view import Edge, Graph, Node

    def node(i: int, name: str, kind: str = "") -> Node:
        return Node(i, f"Q{i}", name, "", kind, "", None, "u", "t")

    def edge(child: int, parent: int, share: float | None = None) -> Edge:
        return Edge(child, parent, "owned_by", share, None, "wikidata", "u", "t")

    nodes = {
        1: node(1, "ABC News"),
        2: node(2, "ABC"),
        3: node(3, "Disney", "public company"),
        4: node(4, "BlackRock"),
    }
    graph = Graph(nodes, [edge(1, 2), edge(2, 3), edge(3, 4, 0.07)])
    s = graph.summary(1)
    # stops at the public company: it's owned by its shareholders
    assert [n.name for _, n in s.above] == ["Disney"] and s.more
    stake = graph.up[3][0]
    assert stake.minority and stake.label == "Shareholder"


def test_summary_names_an_owner_once() -> None:
    from newsroom.ownership_view import Edge, Graph, Node, summary_text

    nodes = {
        1: Node(1, "Q1", "CNN", "", "", "", None, "u", "t"),
        2: Node(2, "Q2", "Warner Bros. Discovery", "", "", "", None, "u", "t"),
    }
    edges = [
        Edge(1, 2, "owned_by", None, None, "wikidata", "u", "t"),
        Edge(1, 2, "parent_org", None, None, "wikidata", "u", "t"),
    ]
    assert summary_text(Graph(nodes, edges).summary(1)) == "Owned by Warner Bros. Discovery"


def test_corrections_retire_when_wikidata_agrees(conn: sqlite3.Connection) -> None:
    from newsroom.config import OwnershipCorrection as Fix

    def fix(outlet_: str, action: str, target: str) -> Fix:
        return Fix(outlet_, None, action, "owned_by", target, "https://x.org/s", "2024-01-01")

    fixes = [
        fix("exampledaily.ca", "add", "Q1002"),  # Wikidata already lists it
        fix("exampledaily.ca", "remove", "Q1011"),  # Wikidata doesn't list it (any more)
        fix("exampledaily.ca", "add", "Q1011"),  # a real gap: applied
        fix("nomatch.org", "add", "Q1011"),  # outlet without an item: not found
    ]
    fake = FakeWikidata()
    source = fake.source()
    due = ownership.due_outlets(conn, NOW, timedelta(days=7))
    ownership.match_outlets(conn, source, due, NOW)
    ownership.resolve_ownership(conn, source, due, NOW, corrections=fixes)
    states = dict(conn.execute("SELECT key, state FROM ownership_corrections").fetchall())
    assert states == {
        "exampledaily.ca add owner: Q1002": "retired",
        "exampledaily.ca remove owner: Q1011": "retired",
        "exampledaily.ca add owner: Q1011": "applied",
        "nomatch.org add owner: Q1011": "not_found",
    }
    sources = [
        r[0]
        for r in conn.execute(
            "SELECT e.source FROM ownership_edges e JOIN entities p ON p.id = e.parent_entity_id"
            " JOIN entities c ON c.id = e.child_entity_id WHERE c.qid = 'Q1001'"
        )
    ]
    assert sorted(sources) == ["correction", "wikidata"]  # Q1002 once, from Wikidata
    # a correction removed from the file drops out of the list
    ownership.resolve_ownership(conn, source, due, NOW, corrections=fixes[2:3])
    assert [r[0] for r in conn.execute("SELECT key FROM ownership_corrections")] == [
        "exampledaily.ca add owner: Q1011"
    ]


def test_ownership_changes_are_logged_and_reported(
    conn: sqlite3.Connection,
    tmp_path: Path,
    capsys,
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    from newsroom import cli
    from newsroom.ownership_view import ownership_lines

    resolve_all(conn, FakeWikidata())
    before = ownership_lines(conn)
    daily = outlet(conn, "exampledaily.ca")["id"]
    after = {**before, daily: "Owned by Someone Else"}
    assert ownership.log_changes(conn, before, after, NOW) == 1
    assert ownership.log_changes(conn, before, before, NOW) == 0
    conn.execute(
        "INSERT INTO ownership_corrections (key, state, detail, checked, updated_at) VALUES"
        " ('a.ca add owner: Q1', 'retired', 'Wikidata now lists it', '2026-01-01', 'x'),"
        " ('b.ca add owner: Q2', 'applied', '', '2020-01-01', 'x')"
    )
    monkeypatch.setattr(cli, "get_settings", lambda: Settings(data_dir=tmp_path))
    settings = cli.get_settings()
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    copy = sqlite3.connect(settings.db_path)
    conn.backup(copy)
    copy.close()
    monkeypatch.setattr(cli, "datetime", type("D", (), {"now": staticmethod(lambda tz=None: NOW)}))
    cli.cmd_status(None)  # type: ignore[arg-type]
    out = capsys.readouterr().out
    assert "changed in the last 7 days: 1" in out
    assert "->  Owned by Someone Else" in out
    assert "no longer needed (Wikidata now lists it; remove from ownership.yaml)" in out
    assert "source checked 2020-01-01, re-check it: b.ca add owner: Q2" in out
