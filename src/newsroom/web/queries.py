"""Read-only queries behind the feed. All user input arrives here already validated."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from itertools import groupby
from zoneinfo import ZoneInfo

from newsroom.ownership_view import Graph, Node
from newsroom.services.ingest import parse_ts, ts
from newsroom.sources.wikidata import QID_RE

PAGE_SIZES = (25, 50, 100)
DEFAULT_PAGE_SIZE = 50
MAX_PAGE = 100_000
SORTS = {
    "newest": "Newest first",
    "oldest": "Oldest first",
    "outlet": "Outlet name",
    "relevance": "Best match",  # only with a search
}
MAX_QUERY_LENGTH = 200
_TOKEN = re.compile(r"\w+", re.UNICODE)
_DOMAIN = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)+$")
_SLUG = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


# The date the site shows, sorts and filters by: the outlet's own publication time when the
# article page gave one, otherwise when GDELT first saw it. Matches the articles_effective_date
# index expression.
SHOWN_AT = "coalesce(a.outlet_published_at, a.published_at)"


@dataclass(frozen=True)
class FeedFilters:
    q: str = ""
    tag: str = ""
    outlet: str = ""
    country: str = ""
    owner: str = ""
    date_from: date | None = None
    date_to: date | None = None
    sort: str = "newest"
    page: int = 1
    per: int = DEFAULT_PAGE_SIZE

    @classmethod
    def parse(cls, params: dict[str, str]) -> FeedFilters:
        """Build filters from query params, silently dropping anything invalid."""
        q = " ".join(params.get("q", "").split())[:MAX_QUERY_LENGTH]
        tag = params.get("tag", "").strip().lower()
        outlet = params.get("outlet", "").strip().lower()
        country = params.get("country", "").strip().upper()
        owner = params.get("owner", "").strip().upper()
        sort = params.get("sort", "newest")
        if sort not in SORTS or (sort == "relevance" and not q):
            sort = "newest"
        date_from, date_to = _date(params.get("from", "")), _date(params.get("to", ""))
        if date_from and date_to and date_from > date_to:  # picked the wrong way round
            date_from, date_to = date_to, date_from
        page = _int(params.get("page", ""), 1)
        per = _int(params.get("per", ""), DEFAULT_PAGE_SIZE)
        return cls(
            q=q,
            tag=tag if _SLUG.match(tag) else "",
            outlet=outlet if _DOMAIN.match(outlet) else "",
            country=country if re.fullmatch(r"[A-Z]{2}", country) else "",
            owner=owner if QID_RE.match(owner) else "",
            date_from=date_from,
            date_to=date_to,
            sort=sort,
            page=page if 1 <= page <= MAX_PAGE else 1,
            per=per if per in PAGE_SIZES else DEFAULT_PAGE_SIZE,
        )

    def params(self, **overrides: object) -> dict[str, str]:
        """Current state as query params for links. Defaults are left out (clean URLs);
        pass page=..., sort=... etc. to change one thing."""
        values = {
            key: "" for key in ("q", "tag", "outlet", "country", "owner", "from", "to")
        }  # fixed order, so URLs read the same however they were built
        values.update(self.filter_params())
        values.update({"sort": self.sort, "per": str(self.per), "page": str(self.page)})
        values.update({k: str(v) for k, v in overrides.items()})
        defaults = {"sort": "newest", "per": str(DEFAULT_PAGE_SIZE), "page": "1"}
        return {k: v for k, v in values.items() if v and defaults.get(k) != v}

    def filter_params(self) -> dict[str, str]:
        """Just the filters (what 'Clear filters' clears)."""
        values = {
            "q": self.q,
            "tag": self.tag,
            "outlet": self.outlet,
            "country": self.country,
            "owner": self.owner,
            "from": self.date_from.isoformat() if self.date_from else "",
            "to": self.date_to.isoformat() if self.date_to else "",
        }
        return {k: v for k, v in values.items() if v}

    @property
    def active(self) -> bool:
        return bool(self.filter_params())

    @property
    def sort_options(self) -> dict[str, str]:
        return {k: v for k, v in SORTS.items() if k != "relevance" or self.q}


def _int(value: str, default: int) -> int:
    value = value.strip()
    return int(value) if value.isdigit() and len(value) <= 7 else default


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
    published: datetime  # local time: the outlet's publication time if known, else first seen
    outlet_name: str
    outlet_domain: str
    outlet_entity_id: int | None = None
    logo_path: str | None = None
    language: str | None = None
    date_kind: str = "seen"  # "published" (from the article page) | "seen" (GDELT)
    tags: list[tuple[str, str]] = field(default_factory=list)  # (slug, label)


@dataclass
class Group:
    """A run of articles under one heading: a day, an outlet, or none (best match)."""

    kind: str  # day | outlet | none
    articles: list[Article]
    day: date | None = None
    label: str = ""


@dataclass
class FeedPage:
    groups: list[Group]
    total: int
    page: int
    per: int

    @property
    def pages(self) -> int:
        return max(1, -(-self.total // self.per))

    @property
    def first(self) -> int:
        return (self.page - 1) * self.per + 1 if self.total else 0

    @property
    def last(self) -> int:
        return min(self.page * self.per, self.total)

    @property
    def page_links(self) -> list[int | None]:
        """Page numbers to show: first, last and two either side of the current one;
        None marks a gap."""
        wanted = {1, self.pages, *range(self.page - 2, self.page + 3)}
        numbers = sorted(n for n in wanted if 1 <= n <= self.pages)
        out: list[int | None] = []
        for n in numbers:
            if out and n - (out[-1] or 0) > 1:
                out.append(None)
            out.append(n)
        return out


def _conditions(
    f: FeedFilters, tz: ZoneInfo, with_query: bool = True
) -> tuple[list[str], list[object]] | None:
    """SQL conditions for the filters (fixed strings, values bound). None = matches nothing."""
    where: list[str] = []
    args: list[object] = []
    if f.q and with_query:
        match = fts_query(f.q)
        if not match:
            return None
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
        where.append(f"{SHOWN_AT} >= ?")
        args.append(ts(datetime.combine(f.date_from, time.min, tz)))
    if f.date_to:
        where.append(f"{SHOWN_AT} < ?")
        args.append(ts(datetime.combine(f.date_to + timedelta(days=1), time.min, tz)))
    return where, args


def latest_id(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT coalesce(max(id), 0) FROM articles").fetchone()[0]


def count_new(conn: sqlite3.Connection, f: FeedFilters, tz: ZoneInfo, since_id: int) -> int:
    """Articles added after `since_id` (insertion order) that match the filters."""
    cond = _conditions(f, tz)
    if cond is None:
        return 0
    where, args = cond
    clause = " AND ".join(["a.id > ?", *where])
    sql = (
        "SELECT count(*) FROM articles a JOIN outlets o ON o.id = a.outlet_id"  # noqa: S608
        f" WHERE {clause}"
    )
    return conn.execute(sql, [since_id, *args]).fetchone()[0]


ORDERS = {
    "newest": f"{SHOWN_AT} DESC, a.id DESC",
    "oldest": f"{SHOWN_AT} ASC, a.id ASC",
    "outlet": f"o.display_name COLLATE NOCASE ASC, {SHOWN_AT} DESC, a.id DESC",
    "relevance": f"articles_fts.rank, {SHOWN_AT} DESC, a.id DESC",
}
SELECT_COLUMNS = (
    f"a.id, a.url, a.title, {SHOWN_AT} AS shown_at, a.outlet_published_at,"
    " o.display_name, o.domain,"
    " o.entity_id, o.logo_path, a.language"
)


def feed(conn: sqlite3.Connection, f: FeedFilters, tz: ZoneInfo) -> FeedPage:
    relevance = f.sort == "relevance" and bool(f.q)
    cond = _conditions(f, tz, with_query=not relevance)
    match = fts_query(f.q) if relevance else ""
    if cond is None or (relevance and not match):
        return FeedPage([], 0, 1, f.per)
    where, args = cond
    if relevance:
        # Ranked search: FTS5's bm25 `rank` needs the FTS table in the FROM clause.
        source = (
            " FROM articles_fts JOIN articles a ON a.id = articles_fts.rowid"
            " JOIN outlets o ON o.id = a.outlet_id"
        )
        where = ["articles_fts MATCH ?", *where]
        args = [match, *args]
    else:
        source = " FROM articles a JOIN outlets o ON o.id = a.outlet_id"
    # `where`, `source` and ORDERS hold only fixed SQL; every value is a bound parameter.
    clause = " WHERE " + " AND ".join(where) if where else ""
    total = conn.execute(f"SELECT count(*){source}{clause}", args).fetchone()[0]
    page = min(f.page, max(1, -(-total // f.per)))
    rows = conn.execute(
        f"SELECT {SELECT_COLUMNS}{source}{clause} ORDER BY {ORDERS[f.sort]} LIMIT ? OFFSET ?",
        [*args, f.per, (page - 1) * f.per],
    ).fetchall()

    articles = [
        Article(
            id=r["id"],
            url=r["url"],
            title=r["title"],
            published=parse_ts(r["shown_at"]).astimezone(tz),
            date_kind="published" if r["outlet_published_at"] else "seen",
            outlet_name=r["display_name"],
            outlet_domain=r["domain"],
            outlet_entity_id=r["entity_id"],
            logo_path=r["logo_path"],
            language=r["language"],
        )
        for r in rows
    ]
    _attach_tags(conn, articles)
    if f.sort == "outlet":
        groups = [
            Group("outlet", list(items), label=name)
            for name, items in groupby(articles, key=lambda a: a.outlet_name)
        ]
    elif f.sort == "relevance":
        groups = [Group("none", articles)] if articles else []
    else:
        groups = [
            Group("day", list(items), day=d)
            for d, items in groupby(articles, key=lambda a: a.published.date())
        ]
    return FeedPage(groups, total, page, f.per)


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
    via: list[Node]  # intermediate entities, top-down
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
        via = [graph.nodes[i] for i in path[1:-1]]
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
        "SELECT url, title, language, outlet_published_at,"
        " coalesce(outlet_published_at, published_at) AS shown_at"
        " FROM articles WHERE outlet_id = ?"
        " ORDER BY coalesce(outlet_published_at, published_at) DESC, id DESC LIMIT ?",
        (outlet_id, limit),
    ).fetchall()
