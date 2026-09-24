"""Wikidata adapter against recorded-format fixtures. No live API calls."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from newsroom.net.http import ApiClient, ApiError
from newsroom.sources.wikidata import (
    WikidataSource,
    build_match_query,
    commons_thumb_url,
    parse_entity,
    parse_match_results,
    website_variants,
)
from tests.wd_fake import FakeWikidata, load

NOW = datetime(2026, 9, 24, tzinfo=UTC)
ENTITIES = load("entities.json")["entities"]


def test_parse_entity_current_statements_only() -> None:
    d = parse_entity(ENTITIES["Q1001"], NOW)
    assert d is not None
    assert d.label == "Example Daily"
    assert d.instance_of == ["Q11032"]
    assert d.country == ["Q16"]
    assert d.website == "https://exampledaily.ca/"
    assert d.logo_file == "Example Daily logo.svg"
    # deprecated (Q1009) and ended-in-2015 (Q1010) statements are ignored
    assert [(p.qid, p.relation) for p in d.parents] == [("Q1002", "owned_by")]


def test_preferred_rank_wins_and_qualifiers() -> None:
    d = parse_entity(ENTITIES["Q1002"], NOW)
    assert d is not None
    parents = {p.qid: p for p in d.parents}
    assert set(parents) == {"Q1004", "Q1003"}  # Q1005 (normal) loses to preferred Q1004
    assert parents["Q1003"].relation == "parent_org"
    assert parents["Q1003"].prop == "P749"
    assert parents["Q1003"].share == pytest.approx(0.6)
    assert parents["Q1003"].start == "2019-03"  # month precision kept
    assert parents["Q1004"].share is None  # never invented


def test_identifiers_and_unsafe_website() -> None:
    d = parse_entity(ENTITIES["Q1003"], NOW)
    assert d is not None
    assert d.identifiers == {"sec_cik": ["0000123456"]}
    assert d.website is None  # javascript: URL rejected
    q1008 = parse_entity(ENTITIES["Q1008"], NOW)
    assert q1008 is not None
    assert q1008.parents == []  # "unknown value" owner is not an owner
    assert q1008.identifiers == {"us_ein": ["12-3456789"]}


def test_future_end_date_is_still_current() -> None:
    d = parse_entity(ENTITIES["Q1011"], NOW)
    assert d is not None
    assert [p.qid for p in d.parents] == ["Q1012"]
    assert parse_entity(ENTITIES["Q1011"], datetime(3000, 1, 1, tzinfo=UTC)).parents == []  # type: ignore[union-attr]


def test_missing_entity() -> None:
    assert parse_entity({"id": "Q1007", "missing": ""}, NOW) is None
    assert parse_entity({"id": "not-a-qid"}, NOW) is None


def test_match_query_and_results() -> None:
    assert "https://www.cbc.ca/" in website_variants("cbc.ca")
    assert len(website_variants("cbc.ca")) == 8
    query = build_match_query(["cbc.ca"])
    assert "<http://cbc.ca>" in query and "DeprecatedRank" in query
    # statements are looked up by value, in written order (no full scan of P856)
    assert 'hint:optimizer "None"' in query
    assert (
        query.index("VALUES ?site")
        < query.index("?st ps:P856 ?site")
        < query.index("?item p:P856 ?st")
    )
    result = parse_match_results(
        load("sparql_match.json"), ["exampledaily.ca", "twonames.ca", "nothing.ca"]
    )
    assert [c.qid for c in result["exampledaily.ca"]] == ["Q1001"]  # deduplicated
    assert [c.qid for c in result["twonames.ca"]] == ["Q2001", "Q2002"]
    assert result["nothing.ca"] == []


def test_client_batches_and_follows_redirects() -> None:
    fake = FakeWikidata()
    source = fake.source()
    got = source.get_entities(["Q1001", "Q1006", "Q1007", "bogus"], NOW)
    assert set(got) == {"Q1001", "Q1006"}
    assert got["Q1006"].qid == "Q1003"  # merged item, returned under the requested id
    params = fake.requests[0]
    assert params["action"] == ["wbgetentities"]
    assert params["ids"] == ["Q1001|Q1006|Q1007"]
    assert params["maxlag"] == ["5"]
    labels = source.get_labels(["Q16", "Q11032"])
    assert labels == {"Q16": "Canada", "Q11032": "newspaper"}


def test_maxlag_is_retried() -> None:
    calls = {"n": 0}
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                200,
                json={"error": {"code": "maxlag", "info": "lagged"}},
                headers={"retry-after": "5"},
            )
        return httpx.Response(200, json={"entities": {"Q1": {"id": "Q1", "labels": {}}}})

    client = ApiClient("ua", transport=httpx.MockTransport(handler), sleep=sleeps.append)
    assert WikidataSource(client).get_labels(["Q1"]) == {"Q1": "Q1"}
    assert calls["n"] == 2
    assert sleeps == [10.0]


def test_api_error_is_raised() -> None:
    client = ApiClient(
        "ua",
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"error": {"code": "no-such-entity"}})
        ),
        sleep=lambda s: None,
    )
    with pytest.raises(ApiError, match="no-such-entity"):
        WikidataSource(client).get_labels(["Q1"])


def test_commons_thumb_url() -> None:
    assert commons_thumb_url("Example Daily logo.svg") == (
        "https://commons.wikimedia.org/wiki/Special:FilePath/Example_Daily_logo.svg?width=64"
    )
