"""Read-only queries behind the feed. All user input arrives here already validated."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from itertools import groupby
from zoneinfo import ZoneInfo

from newsroom.services.ingest import parse_ts, ts

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
        before = None
        if m := _CURSOR.match(params.get("before", "")):
            before = (m.group(1), int(m.group(2)))
        return cls(
            q=q,
            tag=tag if _SLUG.match(tag) else "",
            outlet=outlet if _DOMAIN.match(outlet) else "",
            country=country if re.fullmatch(r"[A-Z]{2}", country) else "",
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
        "SELECT a.id, a.url, a.title, a.published_at, o.display_name, o.domain"  # noqa: S608
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
