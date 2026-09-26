"""GDELT 15-minute GKG files: format, filtering, slot handling. No live downloads: the
zips are built here in the GKG 2.1 layout (27 tab-separated columns)."""

from __future__ import annotations

import io
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from newsroom.net.http import ApiClient
from newsroom.services.ingest import run_ingest, sync_outlets
from newsroom.services.tagging import Tagger
from newsroom.sources.gdelt_files import (
    DomainMatcher,
    GkgFilesSource,
    parse_lines,
    read_zip,
    slot_url,
    slots_between,
)
from tests.helpers import OUTLETS, TAGS, make_db

NOW = datetime(2026, 9, 24, 18, 3, tzinfo=UTC)
DOMAINS = ["cbc.ca", "nytimes.com", "radio-canada.ca"]


def gkg_line(
    url: str,
    title: str = "A headline",
    date: str = "20260924174500",
    collection: str = "1",
    translation: str = "",
    image: str = "",
    n: int = 1,
    v2themes: str = "",
) -> str:
    cols = [""] * 27
    cols[0] = f"{date}-{n}"
    cols[1] = date
    cols[2] = collection
    cols[3] = "whatever.example"
    cols[4] = url
    cols[7] = "THEME_A;THEME_B" * 50  # GKG lines are long; make sure that's fine
    cols[8] = v2themes
    cols[18] = image
    cols[25] = translation
    cols[26] = f"<PAGE_LINKS>x</PAGE_LINKS><PAGE_TITLE>{title}</PAGE_TITLE>" if title else ""
    return "\t".join(cols)


def gkg_zip(lines: list[str], name: str = "20260924174500.gkg.csv") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(name, "\n".join(lines) + "\n")
    return buf.getvalue()


def test_slots_are_15_minute_marks_after_start() -> None:
    start = datetime(2026, 9, 24, 17, 10, tzinfo=UTC)
    assert slots_between(start, NOW) == [
        datetime(2026, 9, 24, 17, 15, tzinfo=UTC),
        datetime(2026, 9, 24, 17, 30, tzinfo=UTC),
        datetime(2026, 9, 24, 17, 45, tzinfo=UTC),
        datetime(2026, 9, 24, 18, 0, tzinfo=UTC),
    ]
    on_mark = datetime(2026, 9, 24, 17, 45, tzinfo=UTC)
    assert slots_between(on_mark, NOW)[0] == datetime(2026, 9, 24, 18, 0, tzinfo=UTC)
    assert slot_url(on_mark) == "https://data.gdeltproject.org/gdeltv2/20260924174500.gkg.csv.zip"
    assert slot_url(on_mark, ".translation").endswith("20260924174500.translation.gkg.csv.zip")


def test_domain_matching_is_exact_with_subdomains() -> None:
    match = DomainMatcher(DOMAINS)
    assert match("www.cbc.ca") == "cbc.ca"
    assert match("ici.radio-canada.ca") == "radio-canada.ca"
    assert match("notcbc.ca") is None  # the DOC API's domain: would have matched this
    assert match("cbc.ca.evil.com") is None


def test_parse_keeps_only_our_outlets_with_clean_titles() -> None:
    lines = [
        gkg_line("https://www.cbc.ca/news/1", "Budget &amp; housing: what&#39;s new", n=1),
        gkg_line("https://notcbc.ca/scam", n=2),
        gkg_line("https://www.nytimes.com/2", "", n=3),  # no title
        gkg_line("https://www.nytimes.com/3", collection="2", n=4),  # not a web document
        gkg_line("javascript:alert(1)", n=5),
        gkg_line("https://www.nytimes.com/4", image="https://static.nyt.com/i.jpg", n=6),
        "garbage line without tabs",
        gkg_line("https://www.cbc.ca/5", date="not-a-date", n=7),
    ]
    records, count = parse_lines(iter(lines), DomainMatcher(DOMAINS), translated=False)
    assert count == len(lines)
    assert [r.url for r in records] == ["https://www.cbc.ca/news/1", "https://www.nytimes.com/4"]
    assert records[0].title == "Budget & housing: what's new"
    assert records[0].domain == "cbc.ca"
    assert records[0].language == "en"
    assert records[0].published_at == datetime(2026, 9, 24, 17, 45, tzinfo=UTC)
    assert records[1].image_url == "https://static.nyt.com/i.jpg"


def test_translated_articles_get_their_source_language() -> None:
    line = gkg_line(
        "https://ici.radio-canada.ca/nouvelle/1",
        "Élection partielle à Montréal",
        translation="srclc:fra;eng:GT-FRA 1.0",
    )
    [record] = parse_lines(iter([line]), DomainMatcher(DOMAINS), translated=True)[0]
    assert record.language == "fr" and record.domain == "radio-canada.ca"


def test_zip_is_streamed_and_bad_zips_are_rejected() -> None:
    data = gkg_zip([gkg_line("https://www.cbc.ca/1")])
    assert len(list(read_zip(io.BytesIO(data)))) == 1
    with pytest.raises(zipfile.BadZipFile):
        list(read_zip(io.BytesIO(b"not a zip")))


class Files:
    """A fake data.gdeltproject.org: {url: bytes | status code}; unknown URLs are 404."""

    def __init__(self, files: dict[str, bytes | int]) -> None:
        self.files = files
        self.requested: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requested.append(url)
        found = self.files.get(url, 404)
        if isinstance(found, int):
            return httpx.Response(found)
        return httpx.Response(200, content=found)


def source_for(files: Files, tmp_path: Path) -> GkgFilesSource:
    client = ApiClient("ua", transport=httpx.MockTransport(files), sleep=lambda s: None)
    return GkgFilesSource(client, tmp_path / "tmp")


def slot(h: int, m: int) -> datetime:
    return datetime(2026, 9, 24, h, m, tzinfo=UTC)


def test_fetch_reads_both_streams_per_slot_oldest_first(tmp_path: Path) -> None:
    files = Files(
        {
            slot_url(slot(17, 45)): gkg_zip([gkg_line("https://www.cbc.ca/a", "A")]),
            slot_url(slot(17, 45), ".translation"): gkg_zip(
                [gkg_line("https://ici.radio-canada.ca/b", "B", translation="srclc:fra;")]
            ),
            slot_url(slot(18, 0)): gkg_zip([gkg_line("https://www.nytimes.com/c", "C")]),
            slot_url(slot(18, 0), ".translation"): gkg_zip([]),
        }
    )
    results = list(source_for(files, tmp_path).fetch(DOMAINS, slot(17, 30), NOW))
    assert [r.window_end for r in results] == [slot(17, 45), slot(18, 0)]
    assert [[x.title for x in r.records] for r in results] == [["A", "B"], ["C"]]
    assert all(r.error is None for r in results)
    assert not any((tmp_path / "tmp").iterdir())  # temp files cleaned up


def test_unpublished_slot_waits_for_the_next_run(tmp_path: Path) -> None:
    files = Files(
        {slot_url(slot(17, 45)): gkg_zip([]), slot_url(slot(17, 45), ".translation"): gkg_zip([])}
    )
    results = list(source_for(files, tmp_path).fetch(DOMAINS, slot(17, 30), NOW))
    # 18:00 isn't out yet: progress stops at 17:45 and it isn't an error
    assert [r.window_end for r in results] == [slot(17, 45), slot(17, 45)]
    assert all(r.error is None for r in results)


def test_old_missing_slot_is_skipped_and_errors_stop_the_run(tmp_path: Path) -> None:
    empty = gkg_zip([])
    files = Files(
        {
            # 16:00 was never published (404); 16:15 is fine; 16:30 is a server error
            slot_url(slot(16, 15)): empty,
            slot_url(slot(16, 15), ".translation"): empty,
            slot_url(slot(16, 30)): 503,
        }
    )
    results = list(source_for(files, tmp_path).fetch(DOMAINS, slot(15, 45), NOW))
    assert results[0].window_end == slot(16, 15)  # the 16:00 gap is passed over
    assert results[-1].error and "503" in results[-1].error
    assert len(results) == 2  # nothing after the failure


def test_run_ingest_with_files_advances_to_the_last_published_slot(tmp_path: Path) -> None:
    conn = make_db(tmp_path / "db.sqlite3", NOW)
    sync_outlets(conn, OUTLETS, NOW)
    files = Files(
        {
            slot_url(slot(17, 45)): gkg_zip([gkg_line("https://www.cbc.ca/a", "Housing plan")]),
            slot_url(slot(17, 45), ".translation"): gkg_zip([]),
        }
    )
    source = source_for(files, tmp_path)
    s = run_ingest(conn, source, Tagger(TAGS), now=NOW, backfill=timedelta(minutes=30))
    assert s.status == "ok" and s.inserted == 1
    cursors = {r[0] for r in conn.execute("SELECT window_end FROM ingest_cursors")}
    assert cursors == {"2026-09-24T17:45:00Z"}  # not 18:03: the 18:00 file wasn't out yet
    row = conn.execute("SELECT source_url, published_at FROM articles").fetchone()
    assert row[0] == slot_url(slot(17, 45))  # each article links to the file it came from
    assert row[1] == "2026-09-24T17:45:00Z"

    files.requested.clear()
    files.files[slot_url(slot(18, 0))] = gkg_zip([])
    files.files[slot_url(slot(18, 0), ".translation")] = gkg_zip([])
    run_ingest(conn, source, Tagger(TAGS), now=NOW + timedelta(minutes=15))
    assert files.requested[0] == slot_url(slot(18, 0))  # picks up where it left off
    conn.close()


def test_missing_files_are_never_skipped_blindly(tmp_path: Path) -> None:
    """If the file host answers 404 for everything, progress must not move past it."""
    files = Files({})
    results = list(source_for(files, tmp_path).fetch(DOMAINS, slot(17, 45), slot(20, 0)))
    assert len(files.requested) == 4  # gives up after 4 missing in a row
    assert [r.window_end for r in results] == [slot(17, 45)]  # stays where it was
    assert all(r.error is None for r in results)


def test_missing_translation_file_counts_as_empty(tmp_path: Path) -> None:
    files = Files({slot_url(slot(17, 45)): gkg_zip([gkg_line("https://www.cbc.ca/a", "A")])})
    results = list(source_for(files, tmp_path).fetch(DOMAINS, slot(17, 30), slot(17, 50)))
    assert [len(r.records) for r in results] == [1]
    assert results[0].window_end == slot(17, 45)


def test_reading_is_bounded_whatever_the_zip_claims() -> None:
    from newsroom.net.http import ApiError

    long_line = "x" * 5000
    data = gkg_zip([gkg_line("https://www.cbc.ca/1"), long_line, gkg_line("https://www.cbc.ca/2")])
    lines = list(read_zip(io.BytesIO(data), max_line=4000))
    assert len(lines) == 2  # the over-long line is skipped, the rest still read
    with pytest.raises(ApiError):
        list(read_zip(io.BytesIO(data), max_bytes=1000))


def test_download_refuses_redirects_to_other_hosts(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "data.gdeltproject.org":
            return httpx.Response(302, headers={"location": "https://evil.example/x.zip"})
        return httpx.Response(200, content=gkg_zip([]))

    results = list(source_for(handler, tmp_path).fetch(DOMAINS, slot(17, 30), slot(17, 50)))
    assert results[0].error and "redirect" in results[0].error


def test_other_domains_count_for_their_outlet(tmp_path: Path) -> None:
    match = DomainMatcher(["ms.now", "cbc.ca"], {"ms.now": ["msnbc.com"], "gone.ca": ["x.ca"]})
    assert match("www.msnbc.com") == "ms.now"
    assert match("www.ms.now") == "ms.now"
    assert match("x.ca") is None  # an alias of an outlet that isn't in this group

    files = Files(
        {
            slot_url(slot(17, 45)): gkg_zip(
                [
                    gkg_line("https://www.msnbc.com/old", "Old"),
                    gkg_line("https://www.ms.now/new", "New"),
                ]
            ),
            slot_url(slot(17, 45), ".translation"): gkg_zip([]),
        }
    )
    client = ApiClient("ua", transport=httpx.MockTransport(files), sleep=lambda s: None)
    source = GkgFilesSource(client, tmp_path / "tmp", {"ms.now": ["msnbc.com"]})
    [result] = list(source.fetch(["ms.now"], slot(17, 30), slot(17, 50)))
    assert [(r.title, r.domain) for r in result.records] == [("Old", "ms.now"), ("New", "ms.now")]


def test_sync_stores_other_domains(tmp_path: Path) -> None:
    from newsroom.config import OutletConfig
    from newsroom.services.ingest import outlet_domains

    conn = make_db(tmp_path / "db.sqlite3", NOW)
    sync_outlets(conn, [OutletConfig("ms.now", "MS NOW", "US", "en", ("msnbc.com",))], NOW)
    assert outlet_domains(conn)["ms.now"] == ["msnbc.com"]
    sync_outlets(conn, [OutletConfig("ms.now", "MS NOW", "US", "en")], NOW)
    assert outlet_domains(conn)["ms.now"] == []  # removed from the list, removed here
    conn.close()


def test_gdelt_themes_are_counted_per_mention() -> None:
    # V2Themes: one "THEME,character offset" entry per mention in the article text
    v2 = "ELECTION,120;ENV_CLIMATECHANGE,300;ELECTION,845;ELECTION,1290;bad theme,5;;"
    line = gkg_line("https://www.cbc.ca/news/vote", v2themes=v2)
    [record] = parse_lines(iter([line]), DomainMatcher(DOMAINS), translated=False)[0]
    assert record.themes == (("ELECTION", 3), ("ENV_CLIMATECHANGE", 1))


def test_main_places_and_people() -> None:
    from newsroom.sources.gdelt_files import main_people, main_places

    # one entry per mention: type#name#country#adm1#adm2#lat#long#feature#offset
    loc = ";".join(
        [
            "4#Ottawa, Ontario, Canada#CA#CA08##45.4#-75.7#-570760#100",
            "1#Canada#CA#CA##60#-95#CA#300",
            "1#Canada#CA#CA##60#-95#CA#900",
            "2#Washington, United States#US#USDC##38.9#-77#531871#400",
            "2#Washington, United States#US#USDC##38.9#-77#531871#1200",
            "4#Paris, France#FR#FR11##48.8#2.3#-1456928#700",  # one passing mention
        ]
    )
    assert main_places(loc) == (("CA", 3), ("US", 2))
    assert main_places("") == ()
    people = "Mark Carney,50;Mark Carney,900;Donald Trump,200;Donald Trump,300;Trump,5;Jane Doe,70"
    # full names mentioned twice or more; a lone word or a single mention doesn't count
    assert main_people(people) == (("Donald Trump", 2), ("Mark Carney", 2))
    assert main_people("mark carney,1;mark carney,9") == (("Mark Carney", 2),)
