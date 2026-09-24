"""Read-only queries behind the feed. All user input arrives here already validated."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from itertools import groupby
from zoneinfo import ZoneInfo

from newsroom.ownership_view import Graph
from newsroom.services.ingest import parse_ts, ts
from newsroom.sources.wikidata import QID_RE

PAGE_SIZE = 50
MAX_QUERY_LENGTH = 200
_TOKEN = re.compile(r"\w+", re.UNICODE)
_DOMAIN = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)+$")
_SLUG = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_CURSOR = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)_(\d{1,12})$")


@dataclass(frozen=True)
class FeedFilters:
    q: str = ""
    tag: str = ""
    outlet: str = ""
    country: str = ""
    owner: str = ""
    date_from: date | None = None
    date_to: date | None = None
    before: tuple[str, int] | None = None

    @classmethod
    def parse(cls, params: dict[str, str]) -> FeedFilters:
        """Build filters from query params, silently dropping anything invalid."""
        q = " ".join(params.get("q", "").split())[:MAX_QUERY_LENGTH]
        tag = params.get("tag", "").strip().lower()
        outlet = params.get("outlet", "").strip().lower()
        country = params.get("country", "").strip().upper()
        owner = params.get("owner", "").strip().upper()
        before = None
        if m := _CURSOR.match(params.get("before", "")):
            before = (m.group(1), int(m.group(2)))
        return cls(
            q=q,
            tag=tag if _SLUG.match(tag) else "",
            outlet=outlet if _DOMAIN.match(outlet) else "",
            country=country if re.fullmatch(r"[A-Z]{2}", country) else "",
            owner=owner if QID_RE.match(owner) else "",
            date_from=_date(params.get("from", "")),
            date_to=_date(params.get("to", "")),
            before=before,
        )

    def params(self, **overrides: object) -> dict[str, str]:
        """Current filters as query params (for links), minus the cursor."""
        values = {
            "q": self.q,
            "tag": self.tag,
            "outlet": self.outlet,
            "country": self.country,
            "owner": self.owner,
            "from": self.date_from.isoformat() if self.date_from else "",
            "to": self.date_to.isoformat() if self.date_to else "",
        }
        values.update({k: str(v) for k, v in overrides.items()})
        return {k: v for k, v in values.items() if v}

    @property
    def cursor(self) -> str:
        return f"{self.before[0]}_{self.before[1]}" if self.before else ""

    @property
    def active(self) -> bool:
        return bool(self.params())


def _date(value: str) -> date | None:
    try:
        return date.fromisoformat(value.strip()) if value else None
    except ValueError:
        return None


def fts_query(text: str) -> str:
    """Turn free text into a safe FTS5 query: every word quoted, all required,
    the last one as a prefix so partial words match while typing."""
    tokens = _TOKEN.findall(text)[:20]
    if not tokens:
        return ""
    quoted = [f'"{t}"' for t in tokens]
    quoted[-1] += "*"
    return " ".join(quoted)


@dataclass
class Article:
    id: int
    url: str
    title: str
    published: datetime  # local time
    outlet_name: str
    outlet_domain: str
    outlet_entity_id: int | None = None
    logo_path: str | None = None
    tags: list[tuple[str, str]] = field(default_factory=list)  # (slug, label)


@dataclass
class Day:
    day: date
    articles: list[Article]


@dataclass
class FeedPage:
    days: list[Day]
    next_cursor: str | None
    count: int


def feed(conn: sqlite3.Connection, f: FeedFilters, tz: ZoneInfo) -> FeedPage:
    where: list[str] = []
    args: list[object] = []
    if f.q:
        match = fts_query(f.q)
        if not match:
            return FeedPage([], None, 0)
        where.append("a.id IN (SELECT rowid FROM articles_fts WHERE articles_fts MATCH ?)")
        args.append(match)
    if f.outlet:
        where.append("o.domain = ?")
        args.append(f.outlet)
    if f.country:
        where.append("o.country = ?")
        args.append(f.country)
    if f.owner:
        where.append(
            "o.entity_id IN (WITH RECURSIVE below(id) AS ("
            " SELECT id FROM entities WHERE qid = ?"
            " UNION SELECT e.child_entity_id FROM ownership_edges e"
            " JOIN below ON e.parent_entity_id = below.id) SELECT id FROM below)"
        )
        args.append(f.owner)
    if f.tag:
        where.append(
            "EXISTS (SELECT 1 FROM article_tags at JOIN tags t ON t.id = at.tag_id"
            " WHERE at.article_id = a.id AND t.slug = ?)"
        )
        args.append(f.tag)
    if f.date_from:
        where.append("a.published_at >= ?")
        args.append(ts(datetime.combine(f.date_from, time.min, tz)))
    if f.date_to:
        where.append("a.published_at < ?")
        args.append(ts(datetime.combine(f.date_to + timedelta(days=1), time.min, tz)))
    if f.before:
        where.append("(a.published_at, a.id) < (?, ?)")
        args.extend(f.before)

    # `where` holds only the fixed clauses above; every value is a bound parameter.
    clause = " WHERE " + " AND ".join(where) if where else ""
    sql = (
        "SELECT a.id, a.url, a.title, a.published_at, o.display_name, o.domain,"  # noqa: S608
        " o.entity_id, o.logo_path"
        f" FROM articles a JOIN outlets o ON o.id = a.outlet_id{clause}"
        " ORDER BY a.published_at DESC, a.id DESC LIMIT ?"
    )
    rows = conn.execute(sql, [*args, PAGE_SIZE + 1]).fetchall()
    more = len(rows) > PAGE_SIZE
    rows = rows[:PAGE_SIZE]

    articles = [
        Article(
            id=r["id"],
            url=r["url"],
            title=r["title"],
            published=parse_ts(r["published_at"]).astimezone(tz),
            outlet_name=r["display_name"],
            outlet_domain=r["domain"],
            outlet_entity_id=r["entity_id"],
            logo_path=r["logo_path"],
        )
        for r in rows
    ]
    _attach_tags(conn, articles)
    days = [Day(d, list(items)) for d, items in groupby(articles, key=lambda a: a.published.date())]
    cursor = None
    if more and rows:
        last = rows[-1]
        cursor = f"{last['published_at']}_{last['id']}"
    return FeedPage(days, cursor, len(articles))


def _attach_tags(conn: sqlite3.Connection, articles: list[Article]) -> None:
    if not articles:
        return
    by_id = {a.id: a for a in articles}
    placeholders = ",".join("?" * len(by_id))
    rows = conn.execute(
        "SELECT at.article_id, t.slug, t.label FROM article_tags at"  # noqa: S608
        f" JOIN tags t ON t.id = at.tag_id WHERE at.article_id IN ({placeholders})"
        " ORDER BY t.label",
        list(by_id),
    )
    for r in rows:
        by_id[r["article_id"]].tags.append((r["slug"], r["label"]))


def filter_options(conn: sqlite3.Connection) -> dict[str, list[sqlite3.Row]]:
    return {
        "outlets": conn.execute(
            "SELECT domain, display_name FROM outlets WHERE active = 1 ORDER BY display_name"
        ).fetchall(),
        "tags": conn.execute("SELECT slug, label FROM tags ORDER BY label").fetchall(),
        "countries": conn.execute(
            "SELECT DISTINCT country FROM outlets WHERE active = 1 ORDER BY country"
        ).fetchall(),
    }


def last_ingest(conn: sqlite3.Connection) -> datetime | None:
    row = conn.execute(
        "SELECT max(finished_at) FROM ingest_runs WHERE status IN ('ok', 'partial')"
    ).fetchone()
    return parse_ts(row[0]) if row and row[0] else None


# ---------------------------------------------------------------- outlets & owners


def article_counts(conn: sqlite3.Connection, now: datetime) -> dict[int, tuple[int, int]]:
    """outlet id -> (all articles, articles in the last 30 days)."""
    cutoff = ts(now - timedelta(days=30))
    return {
        r[0]: (r[1], r[2])
        for r in conn.execute(
            "SELECT outlet_id, count(*), sum(published_at >= ?) FROM articles GROUP BY outlet_id",
            (cutoff,),
        )
    }


def outlets(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM outlets WHERE active = 1 ORDER BY display_name").fetchall()


def outlet(conn: sqlite3.Connection, domain: str) -> sqlite3.Row | None:
    if not _DOMAIN.match(domain):
        return None
    return conn.execute("SELECT * FROM outlets WHERE domain = ?", (domain,)).fetchone()


@dataclass
class OwnedOutlet:
    outlet: sqlite3.Row
    via: list[str]  # names of intermediate entities, top-down
    total: int
    recent: int


def owned_outlets(
    graph: Graph, entity_id: int, rows: list[sqlite3.Row], counts: dict[int, tuple[int, int]]
) -> list[OwnedOutlet]:
    paths = graph.descendants(entity_id)
    out = []
    for o in rows:
        eid = o["entity_id"]
        if eid is None:
            continue
        if eid == entity_id:
            path: list[int] | None = [entity_id]
        else:
            path = paths.get(eid)
        if path is None:
            continue
        via = [graph.nodes[i].name for i in path[1:-1]]
        total, recent = counts.get(o["id"], (0, 0))
        out.append(OwnedOutlet(o, via, total, recent or 0))
    out.sort(key=lambda x: (-x.recent, -x.total, x.outlet["display_name"]))
    return out


@dataclass
class OwnerRow:
    qid: str
    name: str
    kind: str
    country: str
    outlets: int
    recent: int


def owners(
    graph: Graph, rows: list[sqlite3.Row], counts: dict[int, tuple[int, int]]
) -> list[OwnerRow]:
    """Top-level owners (no recorded parent) of at least one outlet other than themselves."""
    result = []
    for node in graph.nodes.values():
        if graph.up.get(node.id):
            continue
        owned = [
            o
            for o in owned_outlets(graph, node.id, rows, counts)
            if o.outlet["entity_id"] != node.id
        ]
        if owned:
            result.append(
                OwnerRow(
                    node.qid,
                    node.name,
                    node.kind,
                    node.country,
                    len(owned),
                    sum(o.recent for o in owned),
                )
            )
    result.sort(key=lambda r: (-r.outlets, -r.recent, r.name))
    return result


def owner_options(graph: Graph, rows: list[sqlite3.Row]) -> list[tuple[str, str]]:
    """Entities that own at least one outlet (directly or indirectly), for the feed filter."""
    outlet_entities = {o["entity_id"] for o in rows if o["entity_id"]}
    options = {}
    for eid in outlet_entities:
        stack, seen = [eid], {eid}
        while stack:
            for e in graph.up.get(stack.pop(), []):
                if e.parent not in seen:
                    seen.add(e.parent)
                    stack.append(e.parent)
                    node = graph.nodes[e.parent]
                    options[node.qid] = node.name
    return sorted(options.items(), key=lambda kv: kv[1].casefold())


def recent_articles(conn: sqlite3.Connection, outlet_id: int, limit: int = 20) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT url, title, published_at FROM articles WHERE outlet_id = ?"
        " ORDER BY published_at DESC, id DESC LIMIT ?",
        (outlet_id, limit),
    ).fetchall()
