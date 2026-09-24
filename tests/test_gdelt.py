"""GDELT adapter against recorded-format fixtures. No live API calls."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from newsroom.net.http import ApiClient, ApiError
from newsroom.sources.gdelt import (
    MAX_RECORDS,
    GdeltSource,
    build_query,
    parse_articles,
)

FIXTURES = Path(__file__).parent / "fixtures" / "gdelt"
DOMAINS = ["cbc.ca", "ctvnews.ca", "radio-canada.ca"]
START = datetime(2026, 9, 22, 12, tzinfo=UTC)
END = datetime(2026, 9, 23, 18, tzinfo=UTC)


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def client_for(handler, sleeps: list[float] | None = None) -> ApiClient:  # type: ignore[no-untyped-def]
    return ApiClient(
        "test-agent",
        transport=httpx.MockTransport(handler),
        sleep=(sleeps.append if sleeps is not None else lambda s: None),
        min_interval=0,
    )


def test_parse_artlist_fixture() -> None:
    records, raw = parse_articles(fixture("artlist.json"), DOMAINS)
    assert raw == 8
    urls = [r.url for r in records]
    # lookalike domain, javascript: URL, missing date and blank title are dropped
    assert "https://fakecbc.ca/scam" not in urls
    assert all(u.startswith("https://") for u in urls)
    assert len(records) == 4  # includes the utm duplicate; dedup happens at insert

    first = records[0]
    assert first.title == "Federal budget promises new housing money & tax changes"
    assert first.domain == "cbc.ca"
    assert first.published_at == datetime(2026, 9, 23, 14, 15, tzinfo=UTC)
    assert first.language == "en"
    assert first.image_url == "https://i.cbc.ca/1.7412345.jpg"

    french = next(r for r in records if r.domain == "radio-canada.ca")
    assert french.language == "fr"
    assert french.image_url is None  # javascript: image URL rejected

    ctv = next(r for r in records if r.domain == "ctvnews.ca")
    assert ctv.title == "Wildfire evacuation order lifted in northern town"  # control char gone


def test_parse_empty_and_errors() -> None:
    assert parse_articles(fixture("empty.json"), DOMAINS) == ([], 0)
    with pytest.raises(ApiError, match="non-JSON"):
        parse_articles(fixture("error.txt"), DOMAINS)
    with pytest.raises(ApiError, match="malformed"):
        parse_articles('{"articles": [', DOMAINS)


def test_build_query() -> None:
    assert build_query(["cbc.ca"]) == "domain:cbc.ca"
    assert build_query(["a.ca", "b.com"]) == "(domain:a.ca OR domain:b.com)"
    with pytest.raises(ValueError):
        build_query(["cbc.ca) OR (sourcelang:x"])


def test_fetch_groups_and_params() -> None:
    seen: list[dict[str, list[str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(parse_qs(urlsplit(str(request.url)).query))
        assert request.headers["user-agent"] == "test-agent"
        return httpx.Response(200, text=fixture("empty.json"))

    source = GdeltSource(client_for(handler), group_size=2)
    results = list(source.fetch(DOMAINS, START, END))
    assert len(results) == 2  # 3 domains in groups of 2
    assert seen[0]["query"] == ["(domain:cbc.ca OR domain:ctvnews.ca)"]
    assert seen[1]["query"] == ["domain:radio-canada.ca"]
    assert seen[0]["mode"] == ["artlist"]
    assert seen[0]["maxrecords"] == [str(MAX_RECORDS)]
    assert seen[0]["startdatetime"] == ["20260922120000"]
    assert seen[0]["enddatetime"] == ["20260923180000"]
    assert all(r.error is None for r in results)


def test_saturated_window_is_split() -> None:
    windows: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        q = parse_qs(urlsplit(str(request.url)).query)
        windows.append((q["startdatetime"][0], q["enddatetime"][0]))
        n = MAX_RECORDS if len(windows) == 1 else 3
        articles = [
            {
                "url": f"https://www.cbc.ca/n/{len(windows)}-{i}",
                "title": f"Story {i}",
                "seendate": "20260923T100000Z",
                "language": "English",
            }
            for i in range(n)
        ]
        return httpx.Response(200, text=json.dumps({"articles": articles}))

    results = list(GdeltSource(client_for(handler)).fetch(["cbc.ca"], START, END))
    assert len(windows) == 3
    assert windows[1] == ("20260922120000", "20260923030000")
    assert windows[2] == ("20260923030000", "20260923180000")
    assert [len(r.records) for r in results] == [3, 3]
    # oldest half first; each result says how far the group is now covered
    assert [r.window_end for r in results] == [START + (END - START) / 2, END]


def test_rate_limit_text_triggers_backoff_then_succeeds() -> None:
    calls = {"n": 0}
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        body = fixture("ratelimit.txt") if calls["n"] == 1 else fixture("artlist.json")
        return httpx.Response(200, text=body)

    client = client_for(handler, sleeps)
    results = list(GdeltSource(client).fetch(["cbc.ca"], START, END))
    assert calls["n"] == 2
    assert sleeps[0] == 30.0  # throttled: a long wait, not the 10 s used for errors
    assert client.min_interval == 10.0  # and it paces itself more slowly from now on
    assert results[0].error is None
    assert len(results[0].records) == 2


def test_persistent_failure_is_reported_not_raised() -> None:
    sleeps: list[float] = []
    source = GdeltSource(client_for(lambda r: httpx.Response(503), sleeps))
    results = list(source.fetch(["cbc.ca"], START, START + timedelta(hours=1)))
    assert len(results) == 1
    assert results[0].error and "HTTP 503" in results[0].error
    assert sleeps == [10.0, 20.0, 40.0]  # exponential backoff, 4 attempts
    assert not results[0].throttled and results[0].window_end is None


def test_error_body_is_reported() -> None:
    source = GdeltSource(client_for(lambda r: httpx.Response(200, text=fixture("error.txt"))))
    results = list(source.fetch(["cbc.ca"], START, END))
    assert results[0].error and "non-JSON" in results[0].error


def test_retry_after_header_respected() -> None:
    calls = {"n": 0}
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "42"})
        return httpx.Response(200, text="{}")

    list(GdeltSource(client_for(handler, sleeps)).fetch(["cbc.ca"], START, END))
    assert sleeps[0] == 42.0  # Retry-After wins


def test_client_paces_requests() -> None:
    now = {"t": 100.0}
    sleeps: list[float] = []

    def sleep(s: float) -> None:
        sleeps.append(s)
        now["t"] += s

    client = ApiClient(
        "ua",
        transport=httpx.MockTransport(lambda r: httpx.Response(200, text="{}")),
        min_interval=6.0,
        sleep=sleep,
        clock=lambda: now["t"],
    )
    client.get("https://api.gdeltproject.org/a")
    now["t"] += 1.0
    client.get("https://api.gdeltproject.org/b")
    assert sleeps == [5.0]


def test_backoff_log_names_the_host(caplog: pytest.LogCaptureFixture) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503) if calls["n"] == 1 else httpx.Response(200, text="{}")

    with caplog.at_level("WARNING", logger="newsroom.net.http"):
        list(GdeltSource(client_for(handler)).fetch(["cbc.ca"], START, END))
    [record] = [r for r in caplog.records if r.msg == "request failed, backing off"]
    assert record.host == "api.gdeltproject.org"  # type: ignore[attr-defined]


def test_repeated_429_keeps_slowing_down() -> None:
    sleeps: list[float] = []
    client = client_for(lambda r: httpx.Response(429), sleeps)
    results = list(GdeltSource(client).fetch(["cbc.ca"], START, END))
    assert results[0].error and "429" in results[0].error
    assert results[0].throttled  # ingestion stops the run instead of trying other groups
    assert [s for s in sleeps if s in (30.0, 60.0, 120.0)] == [30.0, 60.0, 120.0]
    assert client.min_interval == 60.0  # 10 -> 20 -> 40 -> capped at 60


def test_pacing_counts_from_when_the_last_request_finished() -> None:
    """GDELT measures its gap from the end of the previous request, so a slow response
    must not eat into the wait."""
    now = {"t": 100.0}
    sleeps: list[float] = []

    def slow(request: httpx.Request) -> httpx.Response:
        now["t"] += 8.0  # the response takes 8 s
        return httpx.Response(200, text="{}")

    def sleep(s: float) -> None:
        sleeps.append(s)
        now["t"] += s

    client = ApiClient(
        "ua",
        transport=httpx.MockTransport(slow),
        min_interval=10.0,
        sleep=sleep,
        clock=lambda: now["t"],
    )
    client.get("https://api.gdeltproject.org/a")
    client.get("https://api.gdeltproject.org/b")
    assert sleeps == [10.0]  # the full gap after the 8 s response, not 10 - 8


def test_gdelt_refusal_is_final_for_the_run() -> None:
    """No retrying into GDELT's block: one refusal ends it (the next run resumes)."""
    calls: list[str] = []
    sleeps: list[float] = []

    def refuse(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(429, text="Please limit requests to one every 5 seconds")

    client = ApiClient(
        "ua",
        transport=httpx.MockTransport(refuse),
        sleep=sleeps.append,
        retry_throttled=False,
    )
    results = list(GdeltSource(client).fetch(["cbc.ca"], START, END))
    assert len(calls) == 1 and sleeps == []
    assert results[0].throttled and results[0].error

    def refuse_text(request: httpx.Request) -> httpx.Response:  # the 200-with-text variant
        calls.append(str(request.url))
        return httpx.Response(200, text=fixture("ratelimit.txt"))

    calls.clear()
    client = ApiClient(
        "ua",
        transport=httpx.MockTransport(refuse_text),
        sleep=sleeps.append,
        retry_throttled=False,
    )
    results = list(GdeltSource(client).fetch(["cbc.ca"], START, END))
    assert len(calls) == 1 and results[0].throttled
