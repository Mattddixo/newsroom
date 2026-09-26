"""Funding adapters, refresh, curated file, and the funding panel. No live API calls."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from newsroom.config import ConfigError, load_curated_funding
from newsroom.net.http import ApiClient, ApiError
from newsroom.services import funding
from newsroom.services.ingest import run_ingest, sync_outlets
from newsroom.services.tagging import Tagger
from newsroom.settings import Settings
from newsroom.sources import funding as src
from newsroom.sources.base import QueryResult
from newsroom.web.app import create_app
from tests.helpers import TAGS, FakeSource, make_db, rec
from tests.test_ownership import OUTLETS, resolve_all
from tests.wd_fake import FakeWikidata

NOW = datetime(2026, 9, 24, 12, tzinfo=UTC)
FIX = Path(__file__).parent / "fixtures" / "funding"
ROOT = Path(__file__).resolve().parent.parent


def fixture(name: str) -> dict:
    return json.loads((FIX / name).read_text())


def client_for(handler) -> ApiClient:  # type: ignore[no-untyped-def]
    return ApiClient(
        "ua (x@example.org)", transport=httpx.MockTransport(handler), sleep=lambda s: None
    )


# ------------------------------------------------------------------ adapters


def test_sec_latest_annual_report_and_index() -> None:
    records = src.parse_sec_submissions(fixture("sec_submissions.json"), "123456")
    assert [r.kind for r in records] == ["public_filing", "public_filing"]
    annual, index = records
    assert annual.label == "Latest annual report (Form 40-F), filed 2026-02-20"
    assert annual.source_url == (
        "https://www.sec.gov/Archives/edgar/data/123456/000123456726000012/exh-20251231x40f.htm"
    )
    assert annual.period == "2025-12-31"
    assert annual.amount is None  # links only, no extracted figures
    assert "CIK=0000123456" in index.source_url


def test_sec_without_annual_report_gives_index_only() -> None:
    data = fixture("sec_submissions.json")
    data["filings"]["recent"]["form"] = ["10-Q", "8-K", "S-8", "10-Q"]
    assert [r.label for r in src.parse_sec_submissions(data, "123456")] == ["SEC filings (all)"]


def test_sec_rejects_odd_document_names() -> None:
    data = fixture("sec_submissions.json")
    data["filings"]["recent"]["primaryDocument"][2] = "../../evil"
    assert len(src.parse_sec_submissions(data, "123456")) == 1


def test_fetch_sec_uses_padded_cik() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json=fixture("sec_submissions.json"))

    src.fetch_sec(client_for(handler), "123456")
    assert seen == ["https://data.sec.gov/submissions/CIK0000123456.json"]


def test_propublica_latest_three_years() -> None:
    records = src.parse_propublica(fixture("propublica_org.json"), "12-3456789")
    got = [(r.kind, r.period, r.amount) for r in records]
    assert got == [
        ("nonprofit_revenue", "Tax year 2023", 45123456.0),
        ("grant", "Tax year 2023", 44000000.0),
        ("nonprofit_revenue", "Tax year 2022", 38500000.0),  # no grants field that year
        ("nonprofit_revenue", "Tax year 2021", 31000000.0),
        ("grant", "Tax year 2021", 30000000.0),
    ]
    assert all(r.currency == "USD" for r in records)
    assert (
        records[0].source_url
        == "https://projects.propublica.org/nonprofits/organizations/123456789"
    )


def test_propublica_404_means_no_records() -> None:
    assert src.fetch_propublica(client_for(lambda r: httpx.Response(404)), "12-3456789") == []
    with pytest.raises(ApiError):
        src.fetch_propublica(client_for(lambda r: httpx.Response(403)), "12-3456789")


@pytest.mark.parametrize(
    ("scheme", "raw", "normal"),
    [
        ("sec_cik", "123456", "0000123456"),
        ("us_ein", "123456789", "12-3456789"),
        ("us_ein", "12-3456789", "12-3456789"),
        ("ca_bn", "123456789rr0001", "123456789RR0001"),
    ],
)
def test_normalize_id(scheme: str, raw: str, normal: str) -> None:
    assert src.normalize_id(scheme, raw) == normal


@pytest.mark.parametrize(
    ("scheme", "raw"),
    [("sec_cik", "12a"), ("us_ein", "1234"), ("ca_bn", "123456789RC0001"), ("iban", "1")],
)
def test_normalize_id_rejects(scheme: str, raw: str) -> None:
    with pytest.raises(ValueError):
        src.normalize_id(scheme, raw)


def test_cra_link() -> None:
    [r] = src.cra_records("123456789RR0001")
    assert r.kind == "charity_registration"
    assert "selectedCharityBn=123456789RR0001" in r.source_url
    assert r.source_url.startswith("https://apps.cra-arc.gc.ca/")


# ------------------------------------------------------------------ curated file


def test_shipped_curated_file_is_valid() -> None:
    assert load_curated_funding(ROOT / "config" / "public_funding.yaml") == []


def test_curated_validation(tmp_path: Path) -> None:
    path = tmp_path / "f.yaml"
    good = (
        "funding:\n  - outlet: exampledaily.ca\n    kind: government_appropriation\n"
        "    label: Appropriation\n    amount: 1000\n    currency: cad\n    period: '2024-25'\n"
        "    source_url: https://example.org/ar.pdf\n    retrieved: 2026-09-01\n"
    )
    path.write_text(good)
    [entry] = load_curated_funding(path)
    assert (entry.currency, entry.amount, entry.retrieved) == ("CAD", 1000.0, "2026-09-01")
    for broken, message in [
        (good.replace("    source_url: https://example.org/ar.pdf\n", ""), "source_url"),
        (good.replace("amount: 1000", "amount: '1,000'"), "amount"),
        (good.replace("    currency: cad\n", ""), "currency"),
        (good.replace("kind: government_appropriation", "kind: opinion"), "kind"),
        (good.replace("retrieved: 2026-09-01", "retrieved: last week"), "retrieved"),
        (
            good.replace("outlet: exampledaily.ca", "outlet: exampledaily.ca\n    qid: Q1"),
            "exactly",
        ),
    ]:
        path.write_text(broken)
        with pytest.raises(ConfigError, match=message):
            load_curated_funding(path)
    assert load_curated_funding(tmp_path / "missing.yaml") == []


# ------------------------------------------------------------------ refresh


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    c = make_db(Settings(data_dir=tmp_path).db_path, NOW)
    sync_outlets(c, OUTLETS, NOW)
    resolve_all(c, FakeWikidata())  # gives Q1003 a CIK and Q1008 an EIN
    return c


def fetchers(calls: list[str], fail: set[str] | None = None) -> funding.Fetchers:
    def sec(v: str) -> list[src.FundingRecord]:
        calls.append(f"sec:{v}")
        if fail and "sec" in fail:
            raise ApiError("HTTP 503")
        return src.parse_sec_submissions(fixture("sec_submissions.json"), v)

    def pp(v: str) -> list[src.FundingRecord]:
        calls.append(f"pp:{v}")
        return src.parse_propublica(fixture("propublica_org.json"), v)

    return {"sec_cik": sec, "us_ein": pp, "ca_bn": src.cra_records}


def count(conn: sqlite3.Connection, source: str) -> int:
    return conn.execute(
        "SELECT count(*) FROM funding_records WHERE source = ?", (source,)
    ).fetchone()[0]


def test_refresh_due_and_idempotent(conn: sqlite3.Connection) -> None:
    calls: list[str] = []
    s = funding.refresh_funding(conn, fetchers(calls), NOW, timedelta(days=7))
    assert sorted(calls) == ["pp:12-3456789", "sec:0000123456"]
    assert (s.checked, s.failed) == (2, 0)
    assert count(conn, "sec_edgar") == 2 and count(conn, "propublica") == 5
    row = conn.execute(
        "SELECT * FROM funding_records WHERE source = 'propublica' LIMIT 1"
    ).fetchone()
    assert row["retrieved_at"] == "2026-09-24T12:00:00Z"

    calls.clear()
    funding.refresh_funding(conn, fetchers(calls), NOW + timedelta(days=1), timedelta(days=7))
    assert calls == []  # not due yet
    funding.refresh_funding(conn, fetchers(calls), NOW + timedelta(days=8), timedelta(days=7))
    assert len(calls) == 2
    assert count(conn, "propublica") == 5  # replaced, not duplicated


def test_failure_keeps_previous_records(conn: sqlite3.Connection) -> None:
    funding.refresh_funding(conn, fetchers([]), NOW, timedelta(days=7))
    s = funding.refresh_funding(
        conn, fetchers([], fail={"sec"}), NOW + timedelta(days=8), timedelta(days=7)
    )
    assert s.failed == 1 and s.checked == 1
    assert count(conn, "sec_edgar") == 2


def test_manual_identifier_and_removal(conn: sqlite3.Connection) -> None:
    value = funding.set_identifier(conn, "Q1001", "ca_bn", "123456789rr0001", NOW)
    assert value == "123456789RR0001"
    ident = conn.execute("SELECT * FROM entity_identifiers WHERE scheme = 'ca_bn'").fetchone()
    assert ident["source"] == "manual"
    assert "selectedCharityBn=123456789RR0001" in ident["source_url"]
    funding.refresh_funding(conn, fetchers([]), NOW, timedelta(days=7))
    assert count(conn, "cra") == 1
    with pytest.raises(ValueError):
        funding.set_identifier(conn, "Q1001", "ca_bn", "nope", NOW)
    with pytest.raises(LookupError):
        funding.set_identifier(conn, "Q999", "ca_bn", "123456789RR0001", NOW)
    assert funding.remove_identifier(conn, "Q1001", "ca_bn", "123456789RR0001") == 1
    funding.refresh_funding(conn, fetchers([]), NOW, timedelta(days=7))
    assert count(conn, "cra") == 0  # records follow their identifier


def test_sync_curated(conn: sqlite3.Connection, tmp_path: Path) -> None:
    path = tmp_path / "f.yaml"
    path.write_text(
        "funding:\n"
        "  - outlet: exampledaily.ca\n    kind: government_appropriation\n"
        "    label: Appropriation\n    amount: 1000\n    currency: CAD\n"
        "    source_url: https://example.org/ar.pdf\n    retrieved: 2026-09-01\n"
        "  - qid: Q1003\n    kind: grant\n    label: Grant\n"
        "    source_url: https://example.org/g\n    retrieved: 2026-09-01\n"
        "  - outlet: nomatch.org\n    kind: grant\n    label: Grant\n"
        "    source_url: https://example.org/g\n    retrieved: 2026-09-01\n"
    )
    summary = funding.FundingSummary()
    funding.sync_curated(conn, load_curated_funding(path), summary)
    assert (summary.curated, summary.curated_skipped) == (2, 1)
    funding.sync_curated(conn, load_curated_funding(path), funding.FundingSummary())
    assert count(conn, "curated") == 2  # mirrors the file, no duplicates
    funding.sync_curated(conn, [], funding.FundingSummary())
    assert count(conn, "curated") == 0


# ------------------------------------------------------------------ web


@pytest.fixture
def client(conn: sqlite3.Connection, tmp_path: Path) -> TestClient:
    funding.refresh_funding(conn, fetchers([]), NOW, timedelta(days=7))
    run_ingest(
        conn,
        FakeSource(
            [
                QueryResult(
                    "q",
                    [
                        rec(
                            "https://exampledaily.ca/1",
                            "Story",
                            NOW - timedelta(hours=1),
                            "exampledaily.ca",
                        ),
                        rec(
                            "https://unknownowner.com/1",
                            "Weekly",
                            NOW - timedelta(hours=2),
                            "unknownowner.com",
                        ),
                        rec(
                            "https://nomatch.org/1",
                            "Other",
                            NOW - timedelta(hours=3),
                            "nomatch.org",
                        ),
                    ],
                )
            ]
        ),
        Tagger(TAGS),
        now=NOW,
    )
    return TestClient(create_app(Settings(data_dir=tmp_path, rate_limit="1000/minute")))


def test_panel_shows_owner_funding_with_sources(client: TestClient) -> None:
    html = client.get("/fragments/ownership/exampledaily.ca").text
    who = '<a class="who" href="/owner/Q1003">Example Holdings Inc.</a>'
    assert f"{who}: Latest annual report (Form 40-F)" in html
    assert 'href="https://www.sec.gov/Archives/edgar/data/123456/' in html
    assert 'title="SEC EDGAR, retrieved 2026-09-24">source</a>' in html


def test_nonprofit_funding_amounts(client: TestClient) -> None:
    html = client.get("/outlet/unknownowner.com").text
    assert "Total revenue (IRS Form 990): <strong>US$45,123,456</strong> · Tax year 2023" in html
    assert "ProPublica Nonprofit Explorer" in html
    # matched, but Wikidata records no owner
    assert '<a href="/about#no-owner-listed">No owner listed on Wikidata</a>' in html


def test_wording_for_missing_records(client: TestClient) -> None:
    feed = client.get("/").text
    assert "No owner listed on Wikidata" in feed  # unknownowner.com card
    assert "Owner: no record found" in feed  # nomatch.org card
    assert "Not publicly disclosed" not in feed  # a claim about the outlet, not our sources
    html = client.get("/outlet/nomatch.org").text
    assert (
        'No funding record found in <a href="/about#funding">the sources this site checks</a>'
        in html
    )
    about = client.get("/about").text
    assert 'id="no-owner-listed"' in about and 'id="no-record"' in about and "ProPublica" in about


def test_owner_page_funding(client: TestClient) -> None:
    html = client.get("/owner/Q1003").text
    assert "Latest annual report (Form 40-F)" in html
