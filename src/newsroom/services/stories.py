"""Which articles from different outlets cover the same story.

Two articles are linked when what they say they're about overlaps strongly: the words
of their headlines and the people and organizations GDELT found named in them (at least
twice each). Terms are weighted by how rare they are among recent articles, so sharing
"Carney" and "steel" counts for more than sharing "Trump" on a day he's in every
headline. The measure is the cosine similarity of the two sets of weighted terms, and
the bar is set high (LINK_RULES): a missed link is better than a wrong one.

A new article joins the story of the most similar earlier article from another outlet,
if any is similar enough; otherwise it starts a story of its own. Rules that keep
groups tight:
  * only articles from different outlets are compared (an outlet's own headline
    formulas, "LIVE:" or "Opinion |", never link its articles to each other);
  * a story takes new articles for STORY_SPAN after its first one, so a long-running
    subject doesn't drift into one endless group;
  * an article is assigned once, when first seen, and never moved.
Each link records the terms it rests on, and the site shows them.
"""

from __future__ import annotations

import logging
import math
import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from newsroom.config import normalize_text
from newsroom.names import display_name, name_key
from newsroom.places import COUNTRIES, GROUPS
from newsroom.services.ingest import parse_ts, ts

log = logging.getLogger(__name__)

POOL = timedelta(hours=48)  # recent articles considered (and counted for rarity)
STORY_SPAN = timedelta(hours=24)  # a story takes articles this long after its first
# Similar enough: at least 3 terms in common and a similarity of 0.3, or 2 terms and 0.4.
# Missing a link is better than a wrong one.
LINK_RULES = ((3, 0.3), (2, 0.4))  # (terms in common, similarity)
COMMON_SHARE = 0.03  # a term in more than this share of recent articles doesn't find links
SHOW_SHARED = 4  # terms kept to show why an article was linked

_WORD = re.compile(r"\w+", re.UNICODE)
# Words that say nothing about which story it is (compared without case or accents).
_STOP_EN = """
the and for but not are was were been being has have had its his her hers him she they
them their our your you who whom what when where why how which that this these those than
then there here from into onto over under with without about after before amid among
against between during since until upon via per will would could should can may might
must shall does did doing done just also only more most much many some any all each every
other another such very new news says said say saying tells told report reports reported
update updates updated live watch video videos photos photo opinion analysis review first
last next year years week weeks day days today tonight yesterday tomorrow time times back
out off one two three four five get gets got make makes made take takes took know
"""
_STOP_FR = """
les des une uns pour dans sur par avec sans sous vers chez entre selon après avant qui que
quoi dont est sont sera seront été être avoir fait faire font aux leur leurs ses son cette
ces cet comme mais plus moins pas très tout tous toute toutes nous vous ils elles lui elle
deux trois quatre cinq année années jour jours semaine aujourd hui hier demain nouveau
nouvelle nouvelles direct vidéo analyse chronique
"""
STOPWORDS = frozenset(normalize_text(w) for w in (_STOP_EN + _STOP_FR).split())
PLACE_NAMES = frozenset(normalize_text(n) for n in COUNTRIES.values()) | frozenset(
    normalize_text(name) for name, _ in GROUPS.values()
)


def headline_terms(title: str) -> dict[str, str]:
    """{term: word as written} for a headline's meaningful words. Plurals are folded
    ("tariffs" and "tariff" are one term)."""
    out: dict[str, str] = {}
    for word in _WORD.findall(title):
        w = normalize_text(word)
        if len(w) < 3 or w.isdigit() or w in STOPWORDS:
            continue
        if len(w) > 4 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.setdefault(w, word)
    return out


def article_terms(title: str, names: str, about: str) -> dict[str, str]:
    """An article's terms: its headline words, and the names it mentions that the
    headline doesn't ("Carney" in a headline stands for Mark Carney, as it would in
    another outlet's headline), so one person isn't counted twice."""
    words = headline_terms(title)
    named = {t: n for t, n in name_terms(names, about).items() if t.rsplit(" ", 1)[-1] not in words}
    return {**words, **named}


def name_terms(names: str, about: str) -> dict[str, str]:
    """{term: name as written} for the people and organizations an article names at
    least twice (`names`), or, for articles collected before those were kept, the
    people in `about` (which also lists countries: those are left out, since whole
    countries say little about which story it is)."""
    listed = [n for n in names.split(" · ") if n] or [
        p for p in about.split(" · ") if p and normalize_text(p) not in PLACE_NAMES
    ]
    out: dict[str, str] = {}
    for name in listed:
        key = name_key(name)
        if key:
            out.setdefault("@" + key, display_name(name))
    return out


@dataclass
class _Item:
    id: int
    outlet_id: int
    seen: datetime
    terms: dict[str, str]  # term -> as written
    story_id: int | None = None
    weights: dict[str, float] = field(default_factory=dict)
    norm: float = 0.0


def similarity(a: _Item, b: _Item) -> tuple[float, list[str]]:
    """Cosine similarity of two articles' weighted terms, and the shared terms, rarest
    first."""
    shared = sorted(a.weights.keys() & b.weights.keys(), key=lambda t: (-a.weights[t], t))
    if not shared or not a.norm or not b.norm:
        return 0.0, []
    dot = sum(a.weights[t] * b.weights[t] for t in shared)
    return dot / (a.norm * b.norm), shared


def link_stories(conn: sqlite3.Connection, now: datetime) -> int:
    """Assign every recent article that has no story yet. Returns how many were linked
    to another outlet's article (the rest start stories of their own)."""
    rows = conn.execute(
        "SELECT a.id, a.outlet_id, a.title, a.names, a.about, a.published_at, s.story_id"
        " FROM articles a LEFT JOIN article_stories s ON s.article_id = a.id"
        " WHERE a.published_at >= ? ORDER BY a.published_at, a.id",
        (ts(now - POOL),),
    ).fetchall()
    items = [
        _Item(
            r["id"],
            r["outlet_id"],
            parse_ts(r["published_at"]),
            article_terms(r["title"], r["names"], r["about"]),
            r["story_id"],
        )
        for r in rows
    ]
    if not any(i.story_id is None for i in items):
        return 0
    df: dict[str, int] = {}
    for item in items:
        for t in item.terms:
            df[t] = df.get(t, 0) + 1
    n = len(items)
    for item in items:
        item.weights = {t: math.log((1 + n) / (1 + df[t])) + 1 for t in item.terms}
        item.norm = math.sqrt(sum(w * w for w in item.weights.values()))
    common = max(10, COMMON_SHARE * n)

    started: dict[int, datetime] = {}  # story -> its first article's time (in the pool)
    postings: dict[str, list[_Item]] = {}
    for item in items:
        if item.story_id is not None:
            _index(item, postings, started, df, common)
    linked = 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        for item in items:
            if item.story_id is not None:
                continue
            match, shared = _best_match(item, postings, started)
            if match:
                item.story_id = match.story_id
                linked += 1
            else:
                item.story_id = item.id
            conn.execute(
                "INSERT INTO article_stories (article_id, story_id, linked_to, shared)"
                " VALUES (?, ?, ?, ?) ON CONFLICT (article_id) DO NOTHING",
                (
                    item.id,
                    item.story_id,
                    match.id if match else None,
                    " · ".join(item.terms[t] for t in shared[:SHOW_SHARED]),
                ),
            )
            _index(item, postings, started, df, common)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    log.info("stories linked", extra={"articles": n, "linked": linked})
    return linked


def _index(
    item: _Item,
    postings: dict[str, list[_Item]],
    started: dict[int, datetime],
    df: dict[str, int],
    common: float,
) -> None:
    story = item.story_id if item.story_id is not None else item.id
    started[story] = min(started.get(story, item.seen), item.seen)
    for t in item.terms:
        if df[t] <= common:
            postings.setdefault(t, []).append(item)


def _best_match(
    item: _Item, postings: dict[str, list[_Item]], started: dict[int, datetime]
) -> tuple[_Item | None, list[str]]:
    """The most similar earlier-assigned article from another outlet whose story is
    still open to this one, if similar enough."""
    best: tuple[float, int] | None = None
    found: tuple[_Item | None, list[str]] = (None, [])
    for other in _candidates(item, postings):
        if other.outlet_id == item.outlet_id or other.story_id is None:
            continue
        if abs(item.seen - started[other.story_id]) > STORY_SPAN:
            continue
        score, shared = similarity(item, other)
        if not any(len(shared) >= n and score >= least for n, least in LINK_RULES):
            continue
        key = (score, -other.id)
        if best is None or key > best:
            best, found = key, (other, shared)
    return found


def _candidates(item: _Item, postings: dict[str, list[_Item]]) -> Iterable[_Item]:
    seen: set[int] = set()
    for t in item.terms:
        for other in postings.get(t, ()):
            if other.id not in seen:
                seen.add(other.id)
                yield other
