"""Job bodies shared by the worker schedule and the CLI."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from newsroom.config import (
    ConfigError,
    load_curated_funding,
    load_outlets,
    load_ownership_corrections,
    load_tags,
)
from newsroom.db import connect
from newsroom.net.http import ApiClient, ApiError
from newsroom.net.safe_fetch import FetchBlocked, FetchResult, safe_fetch
from newsroom.services import feed_ingest, funding, ingest, ownership, pubdates
from newsroom.services.coverage import build_report as coverage_report
from newsroom.services.ingest import parse_ts, ts
from newsroom.services.tagging import Tagger, retag_all, sync_tags
from newsroom.settings import Settings
from newsroom.sources import funding as funding_sources
from newsroom.sources.feeds import (
    COMMON_FEED_PATHS,
    FEED_TYPES,
    MAX_FEED_BYTES,
    discover_feeds,
    feed_records,
    parse_feed,
)
from newsroom.sources.gdelt import GdeltSource
from newsroom.sources.gdelt_files import GkgFilesSource, scan_hosts, slots_between
from newsroom.sources.pubdate import (
    EARLIEST,
    FUTURE_SKEW,
    DateConflict,
    Robots,
    candidates,
    extract,
    parse_timestamp,
)
from newsroom.sources.wikidata import WikidataSource
from newsroom.urls import canonical_key, host_matches, host_of, is_http_url

log = logging.getLogger(__name__)


def sync_config(settings: Settings) -> Tagger:
    """Load outlets.yaml and tags.yaml into the database. Raises ConfigError if invalid."""
    outlets = load_outlets(settings.config_dir / "outlets.yaml")
    tags = load_tags(settings.config_dir / "tags.yaml")
    conn = connect(settings.db_path)
    try:
        ingest.sync_outlets(conn, outlets, datetime.now(UTC))
        conn.execute("BEGIN IMMEDIATE")
        sync_tags(conn, tags)
        conn.execute("COMMIT")
    finally:
        conn.close()
    return Tagger(tags)


def ingest_articles(settings: Settings, *, wait: float = 0) -> ingest.RunSummary:
    with ingest.ingest_lock(settings.lock_path("ingest"), wait, name="ingest"):
        tagger = sync_config(settings)
        conn = connect(settings.db_path)
        catch_up = timedelta(hours=settings.ingest_catchup_hours)
        backfill = timedelta(hours=settings.ingest_backfill_hours)
        source: GkgFilesSource | GdeltSource
        if settings.ingest_source == "doc":
            # DOC search API. Its limiter stays closed for a minute or more after a 429 and
            # retrying inside that window keeps it closed, so a refusal ends the run.
            client = ApiClient(
                settings.user_agent,
                min_interval=settings.gdelt_min_interval,
                max_retries=2,
                retry_throttled=False,
            )
            source = GdeltSource(client, group_size=settings.gdelt_group_size)
        else:
            # 15-minute GKG files: static downloads, no per-request quota. Every file
            # covers every outlet, so a new outlet's backfill is limited to the catch-up
            # window (48 hours would be ~400 files).
            client = ApiClient(settings.user_agent, timeout=120.0, min_interval=1.0, max_retries=2)
            source = GkgFilesSource(client, settings.tmp_dir, ingest.outlet_domains(conn))
            backfill = min(backfill, catch_up)
        try:
            return ingest.run_ingest(conn, source, tagger, backfill=backfill, catch_up=catch_up)
        finally:
            client.close()
            conn.close()


def ingest_feeds(settings: Settings, *, wait: float = 0) -> ingest.RunSummary:
    """Read outlets' own RSS/Atom feeds (outlets.yaml `feeds:`). Same lock as ingestion."""
    with ingest.ingest_lock(settings.lock_path("ingest"), wait, name="ingest"):
        tagger = sync_config(settings)
        conn = connect(settings.db_path)
        try:
            return feed_ingest.run_feeds(
                conn,
                _feed_fetcher(settings),
                Robots(_robots_fetcher(settings, conn), cache=_ROBOTS_CACHE),
                tagger,
                max_age=timedelta(hours=settings.ingest_backfill_hours),
            )
        finally:
            conn.close()


def _feed_fetcher(settings: Settings) -> Callable[[str, list[str]], bytes]:
    """A feed comes from the host its URL names (set in outlets.yaml), or redirects within
    the outlet's own domains (HuffPost's feed moves to chaski.huffpost.com), nowhere else."""

    def fetch(url: str, outlet_domains: list[str]) -> bytes:
        return safe_fetch(
            url,
            allowlist=[host_of(url), *outlet_domains],
            allowed_types=FEED_TYPES | {"text/plain"},
            max_bytes=MAX_FEED_BYTES,
            timeout=20,
            user_agent=settings.user_agent,
        ).body

    return fetch


def _robots_fetcher(settings: Settings, conn: sqlite3.Connection) -> Callable[[str], str]:
    """robots.txt from outlets' sites and their feed hosts."""
    hosts = [d for main, others in ingest.outlet_domains(conn).items() for d in (main, *others)]
    hosts += [
        host_of(url)
        for (feeds,) in conn.execute("SELECT feeds FROM outlets WHERE active = 1")
        for url in feeds.split()
    ]

    def robots_text(url: str) -> str:
        page = safe_fetch(
            url,
            allowlist=hosts,
            allowed_types={"text/plain"},
            max_bytes=500_000,
            timeout=10,
            user_agent=settings.user_agent,
            truncate=True,
        )
        return page.body.decode("utf-8", errors="replace")

    return robots_text


HTML_TYPES = frozenset({"text/html", "application/xhtml+xml"})
_ROBOTS_CACHE: dict = {}  # robots.txt rules per host, kept for the worker's lifetime (1-day TTL)


def _publication_dates(
    settings: Settings, conn: sqlite3.Connection, limit: int | None = None
) -> pubdates.PubDateSummary | None:
    """Read publication dates for recent articles (see services/pubdates.py for limits)."""
    if not settings.pubdate_fetch:
        return None
    if not settings.contact_email:
        log.warning("publication dates skipped: CONTACT_EMAIL is not set")
        return None
    robots_text, page = _page_fetchers(settings, ingest.outlet_domains(conn))
    return pubdates.refresh_pub_dates(
        conn,
        page,
        Robots(robots_text, cache=_ROBOTS_CACHE),
        datetime.now(UTC).replace(microsecond=0),
        limit=limit or settings.pubdate_per_run,
        max_age=timedelta(days=settings.pubdate_max_age_days),
        tagger=Tagger(load_tags(settings.config_dir / "tags.yaml")),
    )


def _page_fetchers(
    settings: Settings, outlets: dict[str, list[str]]
) -> tuple[Callable[[str], str], Callable[[str, str], FetchResult]]:
    """robots.txt and article-page fetchers: SSRF-guarded, outlet sites only (an outlet's
    page fetch is limited to its own domains), size-capped."""
    all_domains = [d for main, others in outlets.items() for d in (main, *others)]

    def robots_text(url: str) -> str:
        page = safe_fetch(
            url,
            allowlist=all_domains,
            allowed_types={"text/plain"},
            max_bytes=500_000,
            timeout=10,
            user_agent=settings.user_agent,
            truncate=True,
        )
        return page.body.decode("utf-8", errors="replace")

    def page(url: str, domain: str) -> FetchResult:
        return safe_fetch(
            url,
            allowlist=[domain, *outlets.get(domain, [])],
            allowed_types=HTML_TYPES,
            max_bytes=1_500_000,
            timeout=15,
            user_agent=settings.user_agent,
            truncate=True,
        )

    return robots_text, page


def explain_date(settings: Settings, url: str) -> list[str]:
    """Where one article's date comes from, step by step (for diagnosis): what is stored,
    what the outlet's feed states, and what the date check finds on the page. Same
    fetchers and limits as the real thing; only outlets' own sites and feeds."""
    if not is_http_url(url):
        raise ValueError("not an http(s) link")
    tz = ZoneInfo(settings.timezone)
    local = lambda when: f"{when.astimezone(tz):%Y-%m-%d %H:%M %Z}"  # noqa: E731
    conn = connect(settings.db_path)
    try:
        outlets = ingest.outlet_domains(conn)
        stored = conn.execute(
            "SELECT published_at, source, outlet_published_at, pubdate_method FROM articles"
            " WHERE url_key = ?",
            (canonical_key(url),),
        ).fetchone()
        host = host_of(url)
        domain = next(
            (
                main
                for main, others in outlets.items()
                if any(host_matches(host, d) for d in (main, *others))
            ),
            None,
        )
        if domain is None:
            raise LookupError(f"{host} is not one of the outlets in outlets.yaml")
        feeds_row = conn.execute("SELECT feeds FROM outlets WHERE domain = ?", (domain,)).fetchone()
        feed_robots = _robots_fetcher(settings, conn)
    finally:
        conn.close()
    lines = [f"outlet: {domain}"]
    if stored:
        seen = parse_ts(stored["published_at"])
        lines.append(f"first seen: {ts(seen)} ({local(seen)}), via {stored['source']}")
        if stored["outlet_published_at"]:
            when = parse_ts(stored["outlet_published_at"])
            lines.append(
                f"stored publication date: {ts(when)}, shown as {local(when)}"
                f" (from {stored['pubdate_method']})"
            )
        else:
            lines.append(
                f"stored publication date: none ({stored['pubdate_method'] or 'not checked yet'})"
            )
    else:
        seen = datetime.now(UTC)
        lines.append("not in the database (the checks below use now as first seen)")

    # What the outlet's own feed says about this article, raw.
    feeds = feeds_row["feeds"].split() if feeds_row else []
    key = canonical_key(url)
    fetch_feed = _feed_fetcher(settings)
    robots = Robots(feed_robots)
    for feed in feeds:
        if robots.check(feed) != "allowed":
            lines.append(f"feed {feed}: not read (robots.txt)")
            continue
        try:
            items = parse_feed(fetch_feed(feed, [domain, *outlets.get(domain, [])]), feed)
        except (FetchBlocked, ApiError) as exc:
            lines.append(f"feed {feed}: could not be read ({exc})")
            continue
        item = next((i for i in items if i.link and canonical_key(i.link) == key), None)
        if item is None:
            lines.append(f"feed {feed}: article not in it now")
        elif item.published:
            lines.append(
                f"feed {feed}: {item.date_field} {item.raw_date!r} -> {ts(item.published)}"
                f" = {local(item.published)}"
            )
        else:
            lines.append(f"feed {feed}: no usable date ({item.raw_date!r})")

    robots_text, page = _page_fetchers(settings, outlets)
    verdict = Robots(robots_text).check(url)
    lines.append(f"page robots.txt: {verdict}")
    if verdict != "allowed":
        return lines
    try:
        result = page(url, domain)
    except FetchBlocked as exc:
        lines.append(f"page: could not be read ({exc})")
        return lines
    lines.append(f"page: read {len(result.body):,} bytes")
    found = extract(result.body.decode("utf-8", errors="replace"), seen)
    listed = False
    for method, raw in candidates(result.body.decode("utf-8", errors="replace")):
        listed = True
        when = parse_timestamp(raw)
        if when is None:
            why = "rejected: not a full date-time with a time zone"
        elif when > seen + FUTURE_SKEW:
            why = "rejected: later than the article was first seen"
        elif when < EARLIEST:
            why = "rejected: before 1995"
        elif isinstance(found, DateConflict):
            why = f"valid on its own ({ts(when)})"
        elif found and found.method == method and found.when == when:
            why = "USED"
        else:
            why = "valid (a higher-priority tag was used)"
        shown = f" [{local(when)}]" if when else ""
        lines.append(f"  {method}: {str(raw)[:60]!r}{shown} -> {why}")
    if not listed:
        lines.append("  no date tags in the page's metadata")
    if isinstance(found, DateConflict):
        (m1, t1), (m2, t2) = found.first, found.second
        hours = abs(t1 - t2) // timedelta(hours=1)
        lines.append(
            f"page result: no date. {m1} ({ts(t1)}) and {m2} ({ts(t2)}) are {hours} h apart,"
            " so the page gets a time zone wrong and neither can be trusted"
        )
    else:
        lines.append(
            f"page result: {local(found.when) + ' (' + found.method + ')' if found else 'no date'}"
        )
    return lines


def publication_dates(
    settings: Settings, limit: int | None = None, *, wait: float = 0
) -> pubdates.PubDateSummary | None:
    """Run the publication-date pass (worker every 15 min, or the CLI). None when turned
    off (PUBDATE_FETCH) or CONTACT_EMAIL is not set."""
    with ingest.ingest_lock(settings.lock_path("pubdates"), wait, name="publication-date"):
        conn = connect(settings.db_path)
        try:
            summary = _publication_dates(settings, conn, limit)
        finally:
            conn.close()
    return summary


def date_audit(settings: Settings, days: int = 7) -> list[pubdates.DateAudit]:
    conn = connect(settings.db_path)
    try:
        return pubdates.audit_dates(conn, datetime.now(UTC), days)
    finally:
        conn.close()


def retag(settings: Settings) -> int:
    with ingest.ingest_lock(settings.lock_path("ingest"), name="ingest"):
        tagger = sync_config(settings)
        conn = connect(settings.db_path)
        try:
            return retag_all(conn, tagger)
        finally:
            conn.close()


def prune(settings: Settings) -> int:
    conn = connect(settings.db_path)
    try:
        removed = ingest.prune(conn, settings.retention_days)
    finally:
        conn.close()
    log.info("pruned old articles", extra={"removed": removed})
    return removed


def wikidata_client(settings: Settings) -> ApiClient:
    """Wikimedia's User-Agent policy requires contact details; refuse to run without them."""
    if not settings.contact_email:
        raise ConfigError("CONTACT_EMAIL must be set in .env before querying Wikidata")
    # 65 s: the query service's own limit is 60 s, so don't give up before it does.
    return ApiClient(settings.user_agent, timeout=65.0, min_interval=settings.wikidata_min_interval)


def resolve_ownership(
    settings: Settings,
    domains: list[str] | None = None,
    *,
    rematch: bool = True,
    wait: float = 0,
) -> ownership.ResolveSummary:
    """Match and resolve outlets that are due (or the given domains), then refresh logos."""
    now = datetime.now(UTC).replace(microsecond=0)
    client = wikidata_client(settings)
    with ingest.ingest_lock(settings.lock_path("records"), wait, name="ownership/funding"):
        sync_config(settings)
        conn = connect(settings.db_path)
        try:
            source = WikidataSource(client)
            outlets = ownership.due_outlets(
                conn, now, timedelta(days=settings.ownership_refresh_days), domains
            )
            if domains and len(outlets) != len(set(domains)):
                known = {o["domain"] for o in outlets}
                raise LookupError(f"unknown outlet(s): {', '.join(sorted(set(domains) - known))}")
            matched = ownership.match_outlets(conn, source, outlets, now) if rematch else None
            corrections = load_ownership_corrections(settings.config_dir / "ownership.yaml")
            summary = ownership.resolve_ownership(
                conn, source, outlets, now, corrections=corrections
            )
            if matched:
                summary.matched = matched.matched
                summary.ambiguous = matched.ambiguous
                summary.unmatched = matched.unmatched
            logos = ownership.refresh_logos(
                conn,
                lambda url: safe_fetch(
                    url, allowlist=ownership.LOGO_HOSTS, user_agent=settings.user_agent
                ),
                settings.logo_dir,
                now,
            )
        finally:
            client.close()
            conn.close()
    log.info("ownership resolved", extra={**vars(summary), "logos_updated": logos})
    return summary


def refresh_funding(
    settings: Settings, *, force: bool = False, wait: float = 0
) -> funding.FundingSummary:
    """Look up funding records for identifiers that are due, and sync the curated file."""
    if not settings.contact_email:
        raise ConfigError("CONTACT_EMAIL must be set in .env before querying SEC or ProPublica")
    curated = load_curated_funding(settings.config_dir / "public_funding.yaml")
    now = datetime.now(UTC).replace(microsecond=0)
    sec = ApiClient(settings.user_agent, min_interval=0.2)  # SEC allows 10 req/s
    propublica = ApiClient(settings.user_agent, min_interval=1.0)
    fetchers: funding.Fetchers = {
        "sec_cik": lambda v: funding_sources.fetch_sec(sec, v),
        "us_ein": lambda v: funding_sources.fetch_propublica(propublica, v),
        "ca_bn": funding_sources.cra_records,
    }
    with ingest.ingest_lock(settings.lock_path("records"), wait, name="ownership/funding"):
        conn = connect(settings.db_path)
        try:
            summary = funding.refresh_funding(
                conn, fetchers, now, timedelta(days=settings.ownership_refresh_days), force
            )
            funding.sync_curated(conn, curated, summary)
        finally:
            conn.close()
            sec.close()
            propublica.close()
    log.info("funding refreshed", extra=vars(summary))
    return summary


def coverage(settings: Settings, hours: int) -> tuple[list, int, int]:
    """Survey the last `hours` of GDELT's 15-minute files: which outlets are there, under
    which addresses. Returns (report rows, files read, slots asked for)."""
    if not 1 <= hours <= 48:
        raise ValueError("hours must be between 1 and 48")
    sync_config(settings)
    conn = connect(settings.db_path)
    try:
        domains = ingest.outlet_domains(conn)
        names = dict(conn.execute("SELECT domain, display_name FROM outlets WHERE active = 1"))
        week = ts(datetime.now(UTC) - timedelta(days=7))
        stored = dict(
            conn.execute(
                "SELECT o.domain, count(a.id) FROM outlets o JOIN articles a"
                " ON a.outlet_id = o.id AND a.published_at >= ? WHERE o.active = 1"
                " GROUP BY o.id",
                (week,),
            )
        )
    finally:
        conn.close()
    now = datetime.now(UTC)
    slots = slots_between(now - timedelta(hours=hours), now)
    client = ApiClient(settings.user_agent, timeout=120.0, min_interval=1.0, max_retries=2)
    try:
        counts, files = scan_hosts(client, settings.tmp_dir, slots)
    finally:
        client.close()
    outlets = {d: (names.get(d, d), others) for d, others in domains.items()}
    return coverage_report(outlets, counts, stored), files, len(slots)


@dataclass
class FeedCheck:
    url: str
    origin: str  # "configured" | "discovered" (the homepage declares it) | "probed"
    title: str = ""
    error: str = ""
    items: int = 0
    on_site: int = 0
    dated: int = 0
    newest: datetime | None = None
    stale: bool = False  # newest item older than STALE_AFTER

    @property
    def usable(self) -> bool:
        return not self.error and self.on_site > 0 and not self.stale


@dataclass
class OutletFeeds:
    domain: str
    name: str
    gdelt_week: int
    checks: list[FeedCheck]
    discovery: str = ""  # why autodiscovery found nothing, when it didn't


LOW_GDELT_WEEK = 5  # below this many GDELT articles in 7 days, look for a feed
STALE_AFTER = timedelta(days=30)


def check_feeds(
    settings: Settings, discover_all: bool = False, now: datetime | None = None
) -> list[OutletFeeds]:
    """Test every configured feed; for outlets without a working one (and little from
    GDELT), find the feeds their homepage declares and test those. Changes nothing."""
    sync_config(settings)
    conn = connect(settings.db_path)
    try:
        domains = ingest.outlet_domains(conn)
        rows = conn.execute(
            "SELECT o.domain, o.display_name, o.feeds, o.language, (SELECT count(*) FROM"
            " articles a WHERE a.outlet_id = o.id AND a.source = 'gdelt' AND"
            " a.published_at >= ?) AS week FROM outlets o WHERE o.active = 1"
            " ORDER BY o.display_name COLLATE NOCASE",
            (ts(datetime.now(UTC) - timedelta(days=7)),),
        ).fetchall()
        robots = Robots(_robots_fetcher(settings, conn))
    finally:
        conn.close()
    fetch_feed = _feed_fetcher(settings)
    now = (now or datetime.now(UTC)).replace(microsecond=0)
    report = []
    for r in rows:
        aliases = domains.get(r["domain"], [])

        context = (r["domain"], aliases, r["language"], robots, fetch_feed, now)
        checks = [_test_feed(FeedCheck(url, "configured"), *context) for url in r["feeds"].split()]
        why = ""
        wants_discovery = discover_all or r["week"] < LOW_GDELT_WEEK
        if wants_discovery and not any(c.usable for c in checks):
            known = {c.url for c in checks}
            found, why, base = _discover(settings, r["domain"], aliases, robots)
            for url, title in found[:5]:
                if url not in known:
                    known.add(url)
                    checks.append(_test_feed(FeedCheck(url, "discovered", title), *context))
            if not any(c.usable for c in checks):
                # Nothing declared (or nothing working): try the platforms' usual addresses.
                for path in COMMON_FEED_PATHS:
                    url = urljoin(base, path)
                    if url in known:
                        continue
                    known.add(url)
                    probe = _test_feed(FeedCheck(url, "probed"), *context)
                    if probe.usable:
                        checks.append(probe)
                        break
        report.append(OutletFeeds(r["domain"], r["display_name"], r["week"], checks, why))
    return report


def _test_feed(
    check: FeedCheck,
    domain: str,
    aliases: list[str],
    language: str | None,
    robots: Robots,
    fetch_feed: Callable[[str, list[str]], bytes],
    now: datetime,
) -> FeedCheck:
    verdict = robots.check(check.url)
    if verdict == "disallowed":
        check.error = "robots.txt doesn't allow it"
        return check
    if verdict != "allowed":
        check.error = "robots.txt couldn't be read (the site was left alone; try again later)"
        return check
    try:
        items = parse_feed(fetch_feed(check.url, [domain, *aliases]), check.url)
    except (FetchBlocked, ApiError) as exc:
        check.error = str(exc)[:120]
        return check
    got = feed_records(items, domain, aliases, language, now, timedelta(days=36500))
    check.items, check.on_site = got.items, len(got.records)
    check.dated = len(got.records) - got.undated
    check.newest = got.newest
    check.stale = got.newest is not None and now - got.newest > STALE_AFTER
    return check


def _discover(
    settings: Settings, domain: str, aliases: list[str], robots: Robots
) -> tuple[list[tuple[str, str]], str, str]:
    """Feeds the outlet's homepage declares (RSS autodiscovery). Returns (feeds, why none
    were found, the homepage's final URL to resolve other addresses against)."""
    home = f"https://{domain}/"
    verdict = robots.check(home)
    if verdict != "allowed":
        return [], f"homepage not read: robots.txt {verdict}", home
    try:
        page = safe_fetch(
            home,
            allowlist=[domain, *aliases],
            allowed_types=HTML_TYPES,
            max_bytes=1_500_000,
            timeout=15,
            user_agent=settings.user_agent,
            truncate=True,
        )
    except FetchBlocked as exc:
        return [], f"homepage not read: {str(exc)[:100]}", home
    base = _without_default_port(page.url)
    found = discover_feeds(page.body.decode("utf-8", errors="replace"), base)
    return found, "" if found else "the homepage declares no feed", base


def _without_default_port(url: str) -> str:
    """https://www.x.ca:443/ -> https://www.x.ca/ (the fetcher reports the port it used)."""
    parts = urlsplit(url)
    default = {"https": 443, "http": 80}.get(parts.scheme)
    if parts.port is not None and parts.port == default and parts.hostname:
        return urlunsplit(parts._replace(netloc=parts.hostname))
    return url
