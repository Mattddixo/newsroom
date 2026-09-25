"""GDELT 2.0 raw 15-minute files (Global Knowledge Graph), for continuous collection.

GDELT publishes everything it processed in each 15-minute window as static files at
predictable addresses (https://data.gdeltproject.org/gdeltv2/YYYYMMDDHHMMSS.gkg.csv.zip,
plus a ".translation" twin for articles GDELT translated into English). These are plain
file downloads with no per-request quota, unlike the DOC 2.0 search API, which refuses
repeated queries from one address. One run downloads only the slots it hasn't seen.

The GKG is tab-separated, one article per line, 27 columns (GKG 2.1 codebook). Used here:
  1 DATE (YYYYMMDDHHMMSS, the 15-minute batch)   2 SourceCollectionIdentifier (1 = web)
  4 DocumentIdentifier (the article URL)         18 SharingImage
  25 TranslationInfo ("srclc:fra;..." when translated)
  26 Extras (XML; the page title is in <PAGE_TITLE>)
Only lines whose URL belongs to one of our outlets are kept; everything else is skipped
without being split into columns.
"""

from __future__ import annotations

import html
import logging
import re
import tempfile
import zipfile
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import IO

from newsroom.net.http import ApiClient, ApiError, NotFound
from newsroom.sources.base import ArticleRecord, QueryResult
from newsroom.sources.gdelt import clean_title
from newsroom.urls import host_of, is_http_url

log = logging.getLogger(__name__)

BASE = "https://data.gdeltproject.org/gdeltv2/"
SLOT = timedelta(minutes=15)
STREAMS = ("", ".translation")  # English-language sources, then translated ones
MAX_DOWNLOAD = 200 * 1024 * 1024  # compressed; real files are far smaller
MAX_UNZIPPED = 2 * 1024 * 1024 * 1024  # stop reading past this (zip bomb guard)
MAX_LINE = 4 * 1024 * 1024  # a GKG line is a few KB; longer ones are skipped
SPOOL_IN_MEMORY = 16 * 1024 * 1024  # larger downloads spill to a temp file on disk
UNPUBLISHED_GRACE = timedelta(hours=1)  # missing files older than this get a warning
MAX_MISSING_IN_A_ROW = 4  # then stop asking; the host may be having trouble
COLUMNS = 27
_TITLE = re.compile(r"<PAGE_TITLE>(.*?)</PAGE_TITLE>", re.S)
_SOURCE_LANG = re.compile(r"srclc:([a-z]{3})")
LANGUAGES = {  # ISO 639-2 (GDELT) -> 639-1 (what the rest of the app uses)
    "fra": "fr",
    "spa": "es",
    "deu": "de",
    "por": "pt",
    "ita": "it",
    "zho": "zh",
    "ara": "ar",
    "rus": "ru",
    "jpn": "ja",
    "kor": "ko",
}


def slot_url(slot: datetime, stream: str = "") -> str:
    return f"{BASE}{slot.astimezone(UTC):%Y%m%d%H%M%S}{stream}.gkg.csv.zip"


def slots_between(start: datetime, end: datetime) -> list[datetime]:
    """15-minute slot times T with start < T <= end, oldest first. The file stamped T holds
    what GDELT processed in the 15 minutes before T."""
    epoch = datetime(2000, 1, 1, tzinfo=UTC)
    first = epoch + ((start - epoch) // SLOT + 1) * SLOT
    out = []
    t = first
    while t <= end:
        out.append(t)
        t += SLOT
    return out


class DomainMatcher:
    """Map a URL host to one of our outlet domains: the host itself or any parent domain
    (www.cbc.ca -> cbc.ca, ici.radio-canada.ca -> radio-canada.ca), including an outlet's
    other domains (`also:`), which map to the outlet's main one. Exact, not substring."""

    def __init__(
        self, domains: Sequence[str], aliases: Mapping[str, Sequence[str]] | None = None
    ) -> None:
        self.targets = {d: d for d in domains}
        for main, others in (aliases or {}).items():
            if main in self.targets:
                self.targets.update({alias: main for alias in others})

    def __call__(self, host: str) -> str | None:
        parts = host.split(".")
        for i in range(len(parts) - 1):
            main = self.targets.get(".".join(parts[i:]))
            if main:
                return main
        return None


def _column(line: str, index: int) -> tuple[str, int]:
    """Column `index` of a tab-separated line without splitting the whole (long) line."""
    start = 0
    for _ in range(index):
        start = line.find("\t", start) + 1
        if start == 0:
            return "", -1
    end = line.find("\t", start)
    return line[start : end if end != -1 else len(line)], start


def parse_lines(
    lines: Iterator[str], match: DomainMatcher, translated: bool
) -> tuple[list[ArticleRecord], int]:
    """Our outlets' articles from GKG lines. Returns (records, lines read)."""
    records: list[ArticleRecord] = []
    count = 0
    for line in lines:
        count += 1
        url, _ = _column(line, 4)
        if not url.startswith(("http://", "https://")):
            continue
        try:
            domain = match(host_of(url))
        except ValueError:  # malformed URL
            continue
        if domain is None:
            continue
        cols = line.rstrip("\r\n").split("\t")
        if len(cols) < COLUMNS or cols[2] != "1" or not is_http_url(url):
            continue
        found = _TITLE.search(cols[26])
        title = clean_title(html.unescape(found.group(1))) if found else ""
        try:
            seen = datetime.strptime(cols[1], "%Y%m%d%H%M%S").replace(tzinfo=UTC)
        except ValueError:
            continue
        if not title:
            continue
        language: str | None = "en"
        if translated:
            lang = _SOURCE_LANG.search(cols[25])
            language = LANGUAGES.get(lang.group(1)) if lang else None
        image = cols[18]
        records.append(
            ArticleRecord(
                url=url,
                title=title,
                domain=domain,
                published_at=seen,
                language=language,
                image_url=image if image and is_http_url(image) else None,
            )
        )
    return records, count


def read_zip(
    file: IO[bytes], max_bytes: int = MAX_UNZIPPED, max_line: int = MAX_LINE
) -> Iterator[str]:
    """Lines of the single CSV inside a GDELT zip, streamed (never fully decompressed).
    Bounded whatever the zip claims: at most `max_bytes` decompressed in total, and a line
    longer than `max_line` is skipped without being held in memory."""
    with zipfile.ZipFile(file) as archive:
        members = [m for m in archive.infolist() if m.filename.lower().endswith(".csv")]
        if len(members) != 1:
            raise ApiError(f"expected one CSV in the zip, found {len(members)}")
        if members[0].file_size > max_bytes:
            raise ApiError("zip member too large")
        total = 0
        with archive.open(members[0]) as raw:
            while True:
                line = raw.readline(max_line + 1)
                if not line:
                    return
                total += len(line)
                if total > max_bytes:
                    raise ApiError("zip member larger than it claimed")
                if len(line) > max_line and not line.endswith(b"\n"):
                    while True:  # skip the rest of an over-long line
                        rest = raw.readline(max_line)
                        total += len(rest)
                        if total > max_bytes:
                            raise ApiError("zip member larger than it claimed")
                        if not rest or rest.endswith(b"\n"):
                            break
                    continue
                yield line.decode("utf-8", errors="replace")


class GkgFilesSource:
    """ArticleSource over GDELT's 15-minute GKG files. All outlets form one group: every
    file covers every outlet, so they advance together."""

    name = "gdelt"
    overlap = timedelta(0)  # files never change once published: nothing to re-cover

    def __init__(
        self,
        client: ApiClient,
        tmp_dir: Path,
        aliases: Mapping[str, Sequence[str]] | None = None,
    ) -> None:
        self.client = client
        self.tmp_dir = tmp_dir
        self.aliases = dict(aliases or {})

    def groups(self, domains: Sequence[str]) -> list[list[str]]:
        return [sorted(domains)] if domains else []

    def fetch(
        self, domains: Sequence[str], start: datetime, end: datetime
    ) -> Iterator[QueryResult]:
        """One result per slot, oldest first. A missing (404) English file is only treated
        as a slot GDELT skipped once a later slot turns up; a run of missing files at the
        end means "not published yet" (or the host is having trouble), so progress stops
        at the last file read and the next run tries again."""
        match = DomainMatcher(domains, self.aliases)
        done = start
        missing: list[datetime] = []
        for slot in slots_between(start, end):
            try:
                records = self._read(slot_url(slot), match, translated=False)
            except NotFound:
                missing.append(slot)
                if len(missing) >= MAX_MISSING_IN_A_ROW:
                    break
                continue
            except ApiError as exc:
                yield self._failed(slot, exc)
                return
            try:
                records += self._read(slot_url(slot, ".translation"), match, translated=True)
            except NotFound:
                log.info("no translation file for slot", extra={"slot": slot.isoformat()})
            except ApiError as exc:
                yield self._failed(slot, exc)
                return
            for gap in missing:  # a later file exists, so these were never published
                log.warning("gdelt file missing; skipping slot", extra={"slot": gap.isoformat()})
            missing = []
            done = slot
            yield QueryResult(source_url=slot_url(slot), records=records, window_end=slot)
        if missing:
            if missing[0] <= end - UNPUBLISHED_GRACE:
                log.warning(
                    "gdelt files missing; will retry next run",
                    extra={"from": missing[0].isoformat(), "count": len(missing)},
                )
            yield QueryResult(source_url=slot_url(missing[0]), window_end=done)

    @staticmethod
    def _failed(slot: datetime, exc: ApiError) -> QueryResult:
        log.warning("gdelt file failed", extra={"slot": slot.isoformat(), "error": str(exc)})
        return QueryResult(source_url=slot_url(slot), error=str(exc))

    def _read(self, url: str, match: DomainMatcher, translated: bool) -> list[ArticleRecord]:
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.SpooledTemporaryFile(max_size=SPOOL_IN_MEMORY, dir=self.tmp_dir) as buf:
            size = self.client.download(url, buf, MAX_DOWNLOAD)
            try:
                records, lines = parse_lines(read_zip(buf), match, translated)
            except (zipfile.BadZipFile, EOFError, OSError) as exc:
                raise ApiError(f"unreadable zip {url}: {exc}") from exc
        log.info(
            "gdelt file read",
            extra={"url": url, "bytes": size, "lines": lines, "kept": len(records)},
        )
        return records


def count_hosts(lines: Iterator[str]) -> dict[str, int]:
    """Web articles per URL host in GKG lines (for the coverage check)."""
    counts: dict[str, int] = {}
    for line in lines:
        collection, _ = _column(line, 2)
        if collection != "1":
            continue
        url, _ = _column(line, 4)
        if not url.startswith(("http://", "https://")):
            continue
        try:
            host = host_of(url)
        except ValueError:
            continue
        if host:
            counts[host] = counts.get(host, 0) + 1
    return counts


def scan_hosts(
    client: ApiClient, tmp_dir: Path, slots: Sequence[datetime]
) -> tuple[dict[str, int], int]:
    """Article counts per host across the given slots (both streams). Returns (counts,
    files read). Missing files are skipped: this is a survey, not ingestion."""
    totals: dict[str, int] = {}
    files = 0
    tmp_dir.mkdir(parents=True, exist_ok=True)
    for slot in slots:
        for stream in STREAMS:
            url = slot_url(slot, stream)
            with tempfile.SpooledTemporaryFile(max_size=SPOOL_IN_MEMORY, dir=tmp_dir) as buf:
                try:
                    client.download(url, buf, MAX_DOWNLOAD)
                    counts = count_hosts(read_zip(buf))
                except NotFound:
                    continue
                except (zipfile.BadZipFile, EOFError, OSError) as exc:
                    raise ApiError(f"unreadable zip {url}: {exc}") from exc
            files += 1
            for host, n in counts.items():
                totals[host] = totals.get(host, 0) + n
    return totals, files
