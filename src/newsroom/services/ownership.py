"""Ownership resolution: outlet domain -> Wikidata item -> owner chain.

All network calls happen first; the database is then updated in one transaction,
so a failed refresh never leaves a half-written chain behind.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from newsroom.net.safe_fetch import FetchBlocked, FetchResult
from newsroom.services.ingest import parse_ts, ts
from newsroom.sources.wikidata import (
    IDENTIFIER_PROPERTIES,
    QID_RE,
    EntityData,
    WikidataSource,
    commons_file_page,
    commons_thumb_url,
    entity_url,
)
from newsroom.urls import is_http_url

log = logging.getLogger(__name__)

MAX_DEPTH = 10
AUTO_STATUSES = ("unmatched", "auto", "ambiguous")
LOGO_HOSTS = ("commons.wikimedia.org", "upload.wikimedia.org")
LOGO_MAX_AGE = timedelta(days=30)
_MAGIC = {
    b"\x89PNG\r\n\x1a\n": "png",
    b"\xff\xd8\xff": "jpg",
    b"GIF87a": "gif",
    b"GIF89a": "gif",
}


@dataclass
class ResolveSummary:
    outlets: int = 0
    matched: int = 0
    ambiguous: int = 0
    unmatched: int = 0
    entities: int = 0
    edges: int = 0


def due_outlets(
    conn: sqlite3.Connection,
    now: datetime,
    refresh: timedelta,
    domains: Sequence[str] | None = None,
) -> list[sqlite3.Row]:
    """Active outlets never checked or checked longer ago than `refresh`.
    With explicit `domains`, those outlets regardless of age."""
    if domains:
        marks = ",".join("?" * len(domains))
        return conn.execute(
            f"SELECT * FROM outlets WHERE domain IN ({marks}) ORDER BY domain",  # noqa: S608
            list(domains),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM outlets WHERE active = 1 AND"
        " (ownership_checked_at IS NULL OR ownership_checked_at < ?) ORDER BY domain",
        (ts(now - refresh),),
    ).fetchall()


def match_outlets(
    conn: sqlite3.Connection, source: WikidataSource, outlets: Sequence[sqlite3.Row], now: datetime
) -> ResolveSummary:
    """Match domains to Wikidata items by official website (P856).
    Confirmed and manual matches are never touched."""
    summary = ResolveSummary()
    todo = [o for o in outlets if o["match_status"] in AUTO_STATUSES]
    if not todo:
        return summary
    found = source.match_domains([o["domain"] for o in todo])
    stamp = ts(now)
    conn.execute("BEGIN IMMEDIATE")
    try:
        for o in todo:
            candidates = found.get(o["domain"], [])
            conn.execute("DELETE FROM outlet_match_candidates WHERE outlet_id = ?", (o["id"],))
            if len(candidates) == 1:
                qid = candidates[0].qid
                conn.execute(
                    "UPDATE outlets SET wikidata_qid = ?, match_status = 'auto',"
                    " match_source_url = ?, matched_at = ? WHERE id = ?",
                    (qid, entity_url(qid, "P856"), stamp, o["id"]),
                )
                summary.matched += 1
            else:
                status = "ambiguous" if candidates else "unmatched"
                conn.execute(
                    "UPDATE outlets SET wikidata_qid = NULL, match_status = ?,"
                    " match_source_url = NULL, matched_at = ? WHERE id = ?",
                    (status, stamp, o["id"]),
                )
                conn.executemany(
                    "INSERT INTO outlet_match_candidates"
                    " (outlet_id, qid, label, description, website, retrieved_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        (o["id"], c.qid, c.label, c.description, c.website, stamp)
                        for c in candidates
                    ],
                )
                if candidates:
                    summary.ambiguous += 1
                else:
                    summary.unmatched += 1
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return summary


def _manual_parents(conn: sqlite3.Connection) -> dict[str, set[str]]:
    rows = conn.execute(
        "SELECT c.qid AS child, p.qid AS parent FROM ownership_edges e"
        " JOIN entities c ON c.id = e.child_entity_id"
        " JOIN entities p ON p.id = e.parent_entity_id WHERE e.source = 'manual'"
    )
    out: dict[str, set[str]] = {}
    for r in rows:
        out.setdefault(r["child"], set()).add(r["parent"])
    return out


def fetch_chains(
    source: WikidataSource,
    roots: set[str],
    now: datetime,
    manual: dict[str, set[str]] | None = None,
    max_depth: int = MAX_DEPTH,
) -> tuple[dict[str, EntityData], set[str]]:
    """Breadth-first walk up owned-by / parent-org. Returns (entities, examined):
    `examined` are entities whose parents were followed; entities first reached at
    the depth limit are fetched for their name only."""
    manual = manual or {}
    fetched: dict[str, EntityData] = {}
    examined: set[str] = set()
    frontier = set(roots)
    depth = 0
    while frontier:
        new = source.get_entities(frontier - fetched.keys(), now)
        fetched.update(new)
        if depth >= max_depth:
            break
        nxt: set[str] = set()
        for qid in frontier:
            data = fetched.get(qid)
            if data is None:
                continue
            examined.add(qid)
            nxt.update(p.qid for p in data.parents)
            nxt.update(manual.get(qid, ()))
        frontier = nxt - fetched.keys()  # visited set: cycles terminate here
        depth += 1
    return fetched, examined


def _upsert_entity(
    conn: sqlite3.Connection, data: EntityData, labels: dict[str, str], stamp: str
) -> int:
    kind = "; ".join(labels.get(q, q) for q in data.instance_of[:3])
    country = "; ".join(labels.get(q, q) for q in data.country)
    conn.execute(
        "INSERT INTO entities (qid, name, description, kind, country, website, source,"
        " source_url, retrieved_at) VALUES (?, ?, ?, ?, ?, ?, 'wikidata', ?, ?)"
        " ON CONFLICT (qid) DO UPDATE SET name = excluded.name,"
        " description = excluded.description, kind = excluded.kind,"
        " country = excluded.country, website = excluded.website,"
        " source_url = excluded.source_url, retrieved_at = excluded.retrieved_at",
        (
            data.qid,
            data.label,
            data.description,
            kind,
            country,
            data.website,
            entity_url(data.qid),
            stamp,
        ),
    )
    return conn.execute("SELECT id FROM entities WHERE qid = ?", (data.qid,)).fetchone()[0]


def resolve_ownership(
    conn: sqlite3.Connection,
    source: WikidataSource,
    outlets: Sequence[sqlite3.Row],
    now: datetime,
    max_depth: int = MAX_DEPTH,
) -> ResolveSummary:
    summary = ResolveSummary(outlets=len(outlets))
    # Re-read: matching may have just changed the QIDs.
    ids = [o["id"] for o in outlets]
    if not ids:
        return summary
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT id, domain, wikidata_qid, logo_file FROM outlets WHERE id IN ({marks})",  # noqa: S608
        ids,
    ).fetchall()
    roots = {r["wikidata_qid"] for r in rows if r["wikidata_qid"]}
    fetched, examined = fetch_chains(source, roots, now, _manual_parents(conn), max_depth)
    labels = source.get_labels(
        {q for d in fetched.values() for q in (*d.instance_of[:3], *d.country)}
    )

    stamp = ts(now)
    conn.execute("BEGIN IMMEDIATE")
    try:
        entity_ids = {qid: _upsert_entity(conn, d, labels, stamp) for qid, d in fetched.items()}
        summary.entities = len(entity_ids)
        for qid in examined:
            data = fetched[qid]
            child = entity_ids[qid]
            conn.execute(
                "DELETE FROM ownership_edges WHERE child_entity_id = ? AND source = 'wikidata'",
                (child,),
            )
            for p in data.parents:
                parent = entity_ids.get(p.qid)
                if parent is None:  # parent item missing or deleted on Wikidata
                    continue
                conn.execute(
                    "INSERT OR IGNORE INTO ownership_edges (child_entity_id, parent_entity_id,"
                    " relation, share, start_date, source, source_url, retrieved_at)"
                    " VALUES (?, ?, ?, ?, ?, 'wikidata', ?, ?)",
                    (child, parent, p.relation, p.share, p.start, entity_url(qid, p.prop), stamp),
                )
                summary.edges += 1
            conn.execute(
                "DELETE FROM entity_identifiers WHERE entity_id = ? AND source = 'wikidata'",
                (child,),
            )
            for scheme, values in data.identifiers.items():
                prop = next(k for k, v in IDENTIFIER_PROPERTIES.items() if v == scheme)
                conn.executemany(
                    "INSERT OR IGNORE INTO entity_identifiers"
                    " (entity_id, scheme, value, source, source_url, retrieved_at)"
                    " VALUES (?, ?, ?, 'wikidata', ?, ?)",
                    [(child, scheme, v, entity_url(qid, prop), stamp) for v in values],
                )
        for r in rows:
            qid = r["wikidata_qid"]
            data = fetched.get(qid) if qid else None
            if qid and data is None:
                log.warning("wikidata item not found", extra={"domain": r["domain"], "qid": qid})
            logo_file = data.logo_file if data else None
            logo_changed = logo_file != r["logo_file"]
            conn.execute(
                "UPDATE outlets SET entity_id = ?, ownership_checked_at = ?, logo_file = ?"
                " WHERE id = ?",
                (entity_ids.get(qid) if qid else None, stamp, logo_file, r["id"]),
            )
            if logo_changed:
                conn.execute(
                    "UPDATE outlets SET logo_path = NULL, logo_retrieved_at = NULL,"
                    " logo_source_url = NULL WHERE id = ?",
                    (r["id"],),
                )
        _collect_orphans(conn)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return summary


def _collect_orphans(conn: sqlite3.Connection) -> int:
    """Delete entities no longer reachable from any outlet (manual edges are kept)."""
    parents: dict[int, set[int]] = {}
    for r in conn.execute("SELECT child_entity_id, parent_entity_id FROM ownership_edges"):
        parents.setdefault(r[0], set()).add(r[1])
    keep = {r[0] for r in conn.execute("SELECT entity_id FROM outlets WHERE entity_id IS NOT NULL")}
    keep |= {
        v
        for r in conn.execute(
            "SELECT child_entity_id, parent_entity_id FROM ownership_edges WHERE source = 'manual'"
        )
        for v in r
    }
    stack = list(keep)
    while stack:
        for p in parents.get(stack.pop(), ()):
            if p not in keep:
                keep.add(p)
                stack.append(p)
    all_ids = {r[0] for r in conn.execute("SELECT id FROM entities")}
    orphans = all_ids - keep
    conn.executemany("DELETE FROM entities WHERE id = ?", [(i,) for i in orphans])
    return len(orphans)


# ------------------------------------------------------------------ manual curation


def set_qid(conn: sqlite3.Connection, domain: str, qid: str | None, now: datetime) -> None:
    """Pin an outlet to a QID (or to 'no item' with None). Auto-matching skips it afterwards."""
    if qid is not None and not QID_RE.match(qid):
        raise ValueError(f"not a QID: {qid}")
    cur = conn.execute(
        "UPDATE outlets SET wikidata_qid = ?, match_status = 'manual', match_source_url = ?,"
        " matched_at = ?, ownership_checked_at = NULL WHERE domain = ?",
        (qid, entity_url(qid) if qid else None, ts(now), domain),
    )
    if cur.rowcount == 0:
        raise LookupError(f"unknown outlet: {domain}")
    conn.execute(
        "DELETE FROM outlet_match_candidates WHERE outlet_id ="
        " (SELECT id FROM outlets WHERE domain = ?)",
        (domain,),
    )


def confirm(conn: sqlite3.Connection, domains: Sequence[str] | None) -> int:
    """Mark automatic matches as confirmed so re-matching never changes them."""
    if domains:
        marks = ",".join("?" * len(domains))
        cur = conn.execute(
            f"UPDATE outlets SET match_status = 'confirmed' WHERE match_status = 'auto'"  # noqa: S608
            f" AND domain IN ({marks})",
            list(domains),
        )
    else:
        cur = conn.execute(
            "UPDATE outlets SET match_status = 'confirmed' WHERE match_status = 'auto'"
        )
    return cur.rowcount


def add_manual_edge(
    conn: sqlite3.Connection,
    source: WikidataSource,
    child_qid: str,
    parent_qid: str,
    relation: str,
    source_url: str,
    now: datetime,
) -> None:
    if relation not in ("owned_by", "parent_org"):
        raise ValueError("relation must be owned_by or parent_org")
    if not is_http_url(source_url):
        raise ValueError("a source URL (http/https) is required for every manual edge")
    if child_qid == parent_qid or not (QID_RE.match(child_qid) and QID_RE.match(parent_qid)):
        raise ValueError("two different QIDs are required")
    fetched = source.get_entities([child_qid, parent_qid], now)
    missing = {child_qid, parent_qid} - fetched.keys()
    if missing:
        raise LookupError(f"not found on Wikidata: {', '.join(sorted(missing))}")
    labels = source.get_labels(
        {q for d in fetched.values() for q in (*d.instance_of[:3], *d.country)}
    )
    stamp = ts(now)
    conn.execute("BEGIN IMMEDIATE")
    try:
        ids = {q: _upsert_entity(conn, d, labels, stamp) for q, d in fetched.items()}
        conn.execute(
            "INSERT INTO ownership_edges (child_entity_id, parent_entity_id, relation,"
            " source, source_url, retrieved_at) VALUES (?, ?, ?, 'manual', ?, ?)"
            " ON CONFLICT (child_entity_id, parent_entity_id, relation, source)"
            " DO UPDATE SET source_url = excluded.source_url, retrieved_at = excluded.retrieved_at",
            (ids[child_qid], ids[parent_qid], relation, source_url, stamp),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def remove_manual_edge(conn: sqlite3.Connection, child_qid: str, parent_qid: str) -> int:
    cur = conn.execute(
        "DELETE FROM ownership_edges WHERE source = 'manual'"
        " AND child_entity_id = (SELECT id FROM entities WHERE qid = ?)"
        " AND parent_entity_id = (SELECT id FROM entities WHERE qid = ?)",
        (child_qid, parent_qid),
    )
    return cur.rowcount


# ------------------------------------------------------------------ logos

Fetcher = Callable[[str], FetchResult]


def refresh_logos(conn: sqlite3.Connection, fetch: Fetcher, logo_dir: Path, now: datetime) -> int:
    """Cache outlet logos from Wikimedia Commons (PNG renders), via the SSRF-guarded fetcher."""
    logo_dir.mkdir(parents=True, exist_ok=True)
    rows = conn.execute(
        "SELECT o.id, o.domain, o.logo_file, o.logo_path, o.logo_retrieved_at, e.qid"
        " FROM outlets o JOIN entities e ON e.id = o.entity_id"
        " WHERE o.logo_file IS NOT NULL"
    ).fetchall()
    updated = 0
    for r in rows:
        fresh = r["logo_retrieved_at"] and parse_ts(r["logo_retrieved_at"]) > now - LOGO_MAX_AGE
        if r["logo_path"] and fresh and (logo_dir / r["logo_path"]).exists():
            continue
        try:
            result = fetch(commons_thumb_url(r["logo_file"]))
        except FetchBlocked as exc:
            log.warning("logo fetch failed", extra={"domain": r["domain"], "error": str(exc)})
            continue
        ext = next((e for magic, e in _MAGIC.items() if result.body.startswith(magic)), None)
        if ext is None and result.body[:4] == b"RIFF" and result.body[8:12] == b"WEBP":
            ext = "webp"
        if ext is None:
            log.warning("logo is not a recognised image", extra={"domain": r["domain"]})
            continue
        name = f"{r['qid']}.{ext}"
        tmp = logo_dir / f".{name}.tmp"
        tmp.write_bytes(result.body)
        tmp.replace(logo_dir / name)
        conn.execute(
            "UPDATE outlets SET logo_path = ?, logo_source_url = ?, logo_retrieved_at = ?"
            " WHERE id = ?",
            (name, commons_file_page(r["logo_file"]), ts(now), r["id"]),
        )
        updated += 1
    # Remove files no outlet points at any more.
    used = {r[0] for r in conn.execute("SELECT logo_path FROM outlets WHERE logo_path IS NOT NULL")}
    for path in logo_dir.iterdir():
        if path.is_file() and path.name not in used and not path.name.startswith("."):
            path.unlink()
    return updated
