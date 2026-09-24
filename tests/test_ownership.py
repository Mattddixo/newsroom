"""Matching, chain resolution, curation, logos and the ownership pages."""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from newsroom.config import OutletConfig
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
    assert [n.qid for n in s.ultimate] == ["Q1003"]  # the only entity with no recorded parent

    # Nothing we were told to ignore made it in
    qids = {r[0] for r in conn.execute("SELECT qid FROM entities")}
    assert not qids & {"Q1005", "Q1009", "Q1010"}


def test_unknown_owner_and_unmatched_are_not_disclosed(conn: sqlite3.Connection) -> None:
    resolve_all(conn, FakeWikidata())
    graph = Graph.load(conn)
    weekly = outlet(conn, "unknownowner.com")
    s = graph.summary(weekly["entity_id"])
    assert s.entity is not None and s.direct == [] and s.ultimate == []
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
    assert "Owned by Example Media Group · ultimately Example Holdings Inc." in html
    assert "Owner: Not publicly disclosed" in html
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
    assert "Matched automatically by official website" in html
    assert "Housing news" in html
    assert "Wikimedia Commons" in html
    unmatched = client.get("/outlet/nomatch.org").text
    assert '<a href="/about#not-disclosed">Not publicly disclosed</a>' in unmatched
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
    assert "Example Daily" in html and "Not publicly disclosed" in html
    assert 'id="not-disclosed"' in client.get("/about").text


def test_tables_and_funding_link_to_owner_pages(client: TestClient) -> None:
    outlets = client.get("/outlets").text
    assert 'Owned by <a href="/owner/Q1002">Example Media Group</a>' in outlets
    assert 'ultimately <a href="/owner/Q1003">Example Holdings Inc.</a>' in outlets
    assert 'Owner: <a href="/about#not-disclosed">Not publicly disclosed</a>' in outlets
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
