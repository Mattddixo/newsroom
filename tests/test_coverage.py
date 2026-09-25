"""Coverage check: which outlets GDELT's files contain, and look-alike addresses."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from newsroom.net.http import ApiClient
from newsroom.services.coverage import build_report, name_tokens
from newsroom.sources.gdelt_files import scan_hosts, slot_url
from tests.test_gdelt_files import Files, gkg_line, gkg_zip

OUTLETS = {
    "ms.now": ("MS NOW", ["msnbc.com"]),
    "washingtonpost.com": ("The Washington Post", []),
    "cbc.ca": ("CBC News", []),
    "thechronicleherald.ca": ("The Chronicle Herald", []),
}


def test_name_tokens() -> None:
    assert name_tokens("washingtonpost.com") == {"washingtonpost"}
    assert name_tokens("abcnews.go.com") == {"abcnews"}  # "go" is too short to be useful
    assert name_tokens("radio-canada.ca") == {"radio-canada"}
    assert name_tokens("cbc.ca") == set()  # too short: would match unrelated hosts
    assert name_tokens("ms.now", ["msnbc.com"]) == {"msnbc"}


def test_report_says_what_to_do_for_each_outlet() -> None:
    hosts = {
        "www.ms.now": 5,
        "www.msnbc.com": 1,
        "www.cbc.ca": 10,
        "notcbc.ca": 2,  # ignored: "cbc" is too short to look for look-alikes
        "www.saltwire.com": 3,  # not named like the Herald, so not suggested
        "washingtonpost-live.example": 4,
        "www.nytimes.com": 7,  # not one of these outlets
    }
    rows = {r.domain: r for r in build_report(OUTLETS, hosts, {"cbc.ca": 50})}
    assert rows["ms.now"].verdict == "carried"
    assert rows["ms.now"].in_gdelt == 6
    assert rows["ms.now"].hosts == {"www.ms.now": 5, "www.msnbc.com": 1}
    assert rows["cbc.ca"].in_gdelt == 10 and rows["cbc.ca"].stored_7d == 50
    assert rows["washingtonpost.com"].verdict == "check look-alikes"
    assert rows["washingtonpost.com"].lookalikes == [("washingtonpost-live.example", 4)]
    assert rows["thechronicleherald.ca"].verdict == "not in GDELT's files"
    order = [r.domain for r in build_report(OUTLETS, hosts, {})]
    assert order[:2] == ["thechronicleherald.ca", "washingtonpost.com"]  # problems first


def test_scan_counts_hosts_and_skips_missing_files(tmp_path: Path) -> None:
    first = datetime(2026, 9, 24, 17, 45, tzinfo=UTC)
    second = first + timedelta(minutes=15)
    files = Files(
        {
            slot_url(first): gkg_zip(
                [
                    gkg_line("https://www.cbc.ca/1", n=1),
                    gkg_line("https://www.cbc.ca/2", n=2),
                    gkg_line("https://www.ms.now/3", n=3),
                    gkg_line("https://www.cbc.ca/print", collection="2", n=4),  # not web
                ]
            ),
            slot_url(first, ".translation"): gkg_zip(
                [gkg_line("https://ici.radio-canada.ca/4", n=1)]
            ),
            # the second slot is missing entirely (not published yet)
        }
    )
    client = ApiClient("ua", transport=httpx.MockTransport(files), sleep=lambda s: None)
    counts, read = scan_hosts(client, tmp_path / "tmp", [first, second])
    assert read == 2
    assert counts == {"www.cbc.ca": 2, "www.ms.now": 1, "ici.radio-canada.ca": 1}


def test_coverage_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from newsroom import jobs
    from newsroom.settings import Settings

    settings = Settings(data_dir=tmp_path, config_dir=Path("config"))
    monkeypatch.setattr(jobs, "scan_hosts", lambda c, d, slots: ({"www.ms.now": 3}, 2 * len(slots)))
    from newsroom.db import connect
    from newsroom.migrate import migrate

    conn = connect(settings.db_path)
    migrate(conn)
    conn.close()
    rows, files, slots = jobs.coverage(settings, 2)
    assert slots in (8, 9) and files == 2 * slots  # 2 hours of 15-minute slots, 2 streams
    carried = [r for r in rows if r.verdict == "carried"]
    assert [r.domain for r in carried] == ["ms.now"]
    with pytest.raises(ValueError):
        jobs.coverage(settings, 0)
