"""Every combination of feed filters, sorts and pages, checked against a plain-Python model.

The model knows nothing about the SQL: it filters and sorts a list of dicts the way the
feed is documented to behave. Any difference is a bug in the queries.
"""

from __future__ import annotations

import itertools
import random
import re
import sqlite3
import unicodedata
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from newsroom.services.ingest import run_ingest
from newsroom.services.tagging import Tagger
from newsroom.sources.base import QueryResult
from newsroom.web import queries
from tests.helpers import OUTLETS, TAGS, FakeSource, make_db, rec

TZ = ZoneInfo("America/Toronto")
NOW = datetime(2026, 9, 24, 16, 0, tzinfo=UTC)
WORDS = [
    "council",
    "housing",
    "rent",
    "election",
    "élection",
    "interest",
    "rate",
    "inflation",
    "wildfire",
    "transit",
    "budget",
    "Montréal",
    "nurses",
    "strike",
    "report",
]
NAMES = {o.domain: o.name for o in OUTLETS}
COUNTRY = {o.domain: o.country for o in OUTLETS}


def fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> tuple[sqlite3.Connection, list[dict]]:
    rng = random.Random(7)
    path = Path(tmp_path_factory.mktemp("matrix")) / "db.sqlite3"
    conn = make_db(path, NOW)
    domains = [o.domain for o in OUTLETS]
    records, meta = [], []
    burst_at = NOW - timedelta(hours=5, minutes=50)
    for i in range(360):
        d = rng.choice(domains)
        title = " ".join(rng.sample(WORDS, 3)) + f" {i}"
        seen = NOW - timedelta(minutes=rng.randint(0, 10 * 24 * 60))
        if i >= 300:  # one busy outlet publishing a lot within a couple of hours
            d, seen = "cbc.ca", burst_at + timedelta(minutes=rng.randint(0, 100))
        # some pages give a publication time before GDELT saw them; ties on purpose
        published = (
            seen - timedelta(minutes=rng.choice([0, 5, 90, 600])) if rng.random() < 0.6 else None
        )
        host = "ici.radio-canada.ca" if d == "radio-canada.ca" else d
        records.append(rec(f"https://{host}/a/{i}", title, seen, d))
        meta.append(
            {
                "url": f"https://{host}/a/{i}",
                "title": title,
                "domain": d,
                "seen": seen,
                "published": published,
            }
        )
    run_ingest(conn, FakeSource([QueryResult("q", records)]), Tagger(TAGS), now=NOW)
    for m in meta:
        row = conn.execute("SELECT id FROM articles WHERE url = ?", (m["url"],)).fetchone()
        m["id"] = row["id"]
        if m["published"]:
            conn.execute(
                "UPDATE articles SET outlet_published_at = ? WHERE id = ?",
                (m["published"].strftime("%Y-%m-%dT%H:%M:%SZ"), m["id"]),
            )
        m["shown"] = m["published"] or m["seen"]
        m["tags"] = set(Tagger(TAGS).match(m["title"]))

    # Ownership: Q900 owns cbc.ca directly and radio-canada.ca through Q903.
    stamp = "2026-09-24T00:00:00Z"
    for qid, name in [
        ("Q900", "Top Owner"),
        ("Q901", "CBC item"),
        ("Q902", "RC item"),
        ("Q903", "Middle Co"),
        ("Q904", "NYT item"),
    ]:
        conn.execute(
            "INSERT INTO entities (qid, name, source, source_url, retrieved_at)"
            " VALUES (?, ?, 'test', 'https://example.org', ?)",
            (qid, name, stamp),
        )
    eid = {r["qid"]: r["id"] for r in conn.execute("SELECT id, qid FROM entities")}
    for child, parent in [("Q901", "Q900"), ("Q902", "Q903"), ("Q903", "Q900")]:
        conn.execute(
            "INSERT INTO ownership_edges (child_entity_id, parent_entity_id, relation,"
            " source, source_url, retrieved_at) VALUES (?, ?, 'owned_by', 'test',"
            " 'https://example.org', ?)",
            (eid[child], eid[parent], stamp),
        )
    for domain, qid in [("cbc.ca", "Q901"), ("radio-canada.ca", "Q902"), ("nytimes.com", "Q904")]:
        conn.execute("UPDATE outlets SET entity_id = ? WHERE domain = ?", (eid[qid], domain))
    return conn, meta


OWNED = {"Q900": {"cbc.ca", "radio-canada.ca"}, "Q903": {"radio-canada.ca"}}
CAP = 3


def matches_query(title: str, q: str) -> bool:
    words = re.findall(r"\w+", fold(title))
    tokens = re.findall(r"\w+", fold(q))
    if not tokens:
        return False
    *whole, last = tokens
    return all(t in words for t in whole) and any(w.startswith(last) for w in words)


def expected(meta: list[dict], f: queries.FeedFilters) -> list[dict]:
    rows = []
    for m in meta:
        if f.q and not matches_query(m["title"], f.q):
            continue
        if f.tag and f.tag not in m["tags"]:
            continue
        if f.outlet and m["domain"] != f.outlet:
            continue
        if f.country and COUNTRY[m["domain"]] != f.country:
            continue
        if f.owner and m["domain"] not in OWNED.get(f.owner, set()):
            continue
        local_day = m["shown"].astimezone(TZ).date()
        if f.date_from and local_day < f.date_from:
            continue
        if f.date_to and local_day > f.date_to:
            continue
        rows.append(m)
    if f.mix == "balanced" and not f.outlet:
        # each outlet's newest CAP per UTC hour, among the matching articles
        kept, per_hour = [], {}
        for m in sorted(rows, key=lambda m: (m["shown"], m["id"]), reverse=True):
            key = (m["domain"], m["shown"].astimezone(UTC).strftime("%Y-%m-%dT%H"))
            per_hour[key] = per_hour.get(key, 0) + 1
            if per_hour[key] <= CAP:
                kept.append(m)
        rows = kept
    if f.sort == "oldest":
        rows.sort(key=lambda m: (m["shown"], m["id"]))
    elif f.sort == "outlet":
        rows.sort(key=lambda m: (NAMES[m["domain"]].casefold(), -m["shown"].timestamp(), -m["id"]))
    else:  # newest; relevance is compared as a set
        rows.sort(key=lambda m: (m["shown"], m["id"]), reverse=True)
    return rows


FROM_TO = [
    (None, None),
    (date(2026, 9, 20), date(2026, 9, 22)),
    (date(2026, 9, 24), None),
    (None, date(2026, 9, 16)),
]
COMBOS = list(
    itertools.product(
        ["", "housing", "elect", "interest rate", "montreal"],
        ["", "housing", "economy"],
        ["", "cbc.ca"],
        ["", "US"],
        ["", "Q900", "Q903"],
        FROM_TO,
        ["newest", "oldest", "outlet", "relevance"],
        ["balanced", "all"],
    )
)


def test_every_filter_and_sort_combination(world: tuple[sqlite3.Connection, list[dict]]) -> None:
    conn, meta = world
    checked = 0
    for q, tag, outlet, country, owner, (d_from, d_to), sort, mix in COMBOS:
        if sort == "relevance" and not q:
            continue
        params = {
            "q": q,
            "tag": tag,
            "outlet": outlet,
            "country": country,
            "owner": owner,
            "from": d_from.isoformat() if d_from else "",
            "to": d_to.isoformat() if d_to else "",
            "sort": sort,
            "mix": mix,
            "per": "25",
        }
        f = queries.FeedFilters.parse(params)
        want = expected(meta, f)
        got_ids: list[int] = []
        page = queries.feed(conn, f, TZ, CAP)
        assert page.total == len(want), params
        if f.balanced:
            assert page.hidden == len(expected(meta, replace(f, mix="all"))) - len(want), params
        for n in range(1, page.pages + 1):
            fp = queries.FeedFilters.parse({**params, "page": str(n)})
            p = queries.feed(conn, fp, TZ, CAP)
            ids = [a.id for g in p.groups for a in g.articles]
            assert len(ids) == (p.last - p.first + 1 if p.total else 0), params
            got_ids += ids
        if sort == "relevance":
            assert sorted(got_ids) == sorted(m["id"] for m in want), params
        else:
            assert got_ids == [m["id"] for m in want], params
        checked += 1
    assert checked > 2000


def test_balanced_mix_caps_a_busy_outlet(world: tuple[sqlite3.Connection, list[dict]]) -> None:
    conn, meta = world
    everything = queries.FeedFilters(mix="all", per=100)
    balanced = queries.FeedFilters(per=100)
    assert queries.feed(conn, balanced, TZ, CAP).hidden > 40  # the burst is capped
    assert queries.feed(conn, everything, TZ, CAP).hidden == 0
    one_outlet = queries.FeedFilters(outlet="cbc.ca", per=100)
    assert queries.feed(conn, one_outlet, TZ, CAP).hidden == 0  # an outlet's own feed: all
    long_ago = datetime(2000, 1, 1, tzinfo=UTC)  # page drawn before anything arrived
    new_all = queries.count_new(conn, everything, TZ, long_ago, CAP)
    new_balanced = queries.count_new(conn, balanced, TZ, long_ago, CAP)
    assert new_all == len(meta) and new_balanced == len(expected(meta, balanced))


def test_dates_shown_and_grouping_follow_the_shown_date(
    world: tuple[sqlite3.Connection, list[dict]],
) -> None:
    conn, meta = world
    by_id = {m["id"]: m for m in meta}
    for n in range(1, 5):
        page = queries.feed(conn, queries.FeedFilters(page=n, per=100), TZ)
        for group in page.groups:
            for a in group.articles:
                m = by_id[a.id]
                assert a.published == m["shown"].astimezone(TZ)
                assert a.date_kind == ("published" if m["published"] else "seen")
                assert group.day == a.published.date()  # grouped under its own local day


def test_reversed_date_range_is_swapped(world: tuple[sqlite3.Connection, list[dict]]) -> None:
    conn, _ = world
    forward = queries.FeedFilters.parse({"from": "2026-09-20", "to": "2026-09-22"})
    backward = queries.FeedFilters.parse({"from": "2026-09-22", "to": "2026-09-20"})
    assert backward.date_from == forward.date_from and backward.date_to == forward.date_to
    assert queries.feed(conn, backward, TZ).total == queries.feed(conn, forward, TZ).total > 0


def test_new_count_matches_what_the_balanced_feed_would_add(
    world: tuple[sqlite3.Connection, list[dict]],
) -> None:
    """The notice counts articles the (capped) feed would show now but didn't before."""
    conn, _ = world
    f = queries.FeedFilters(per=100)
    after = datetime.now(UTC) + timedelta(minutes=5)  # a page drawn after all of them
    assert queries.count_new(conn, f, TZ, after, CAP, now=after) == 0
    before = datetime(2000, 1, 1, tzinfo=UTC)
    shown = queries.feed(conn, f, TZ, CAP).total
    assert queries.count_new(conn, f, TZ, before, CAP) == shown
