"""Topic tags (config/tags.yaml), from two sources recorded with each article:

1. GDELT's theme coding of the full article text (GKG V2Themes), for articles GDELT
   collected. A tag applies when its themes are mentioned `min_mentions` times, so a
   passing mention doesn't count.
2. The outlet's own section and topic labels from the article page's metadata
   (article:section, article:tag, schema.org articleSection/keywords, news_keywords),
   for articles GDELT's themes give no tag, and always for tags GDELT has no themes
   for (sports, arts). Matched as whole labels.

Each tag records its evidence (which themes, or which section), shown on hover. Only
theme names and labels are stored, never article text, so `newsroom retag` can recompute
tags after tags.yaml changes. Only themes some tag uses are stored: a theme added to
tags.yaml later applies to new articles.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable, Mapping, Sequence

from newsroom.config import TagConfig, normalize_text

MAX_SECTIONS = 30
_SEGMENTS = re.compile(r"\s*(?:[/>|,;]|\s[-\u2013]\s)\s*")


class Tagger:
    def __init__(self, tags: Sequence[TagConfig]) -> None:
        self.tags = list(tags)
        self.themes = frozenset(t for tag in self.tags for t in tag.gdelt)
        self.sections_only = frozenset(tag.slug for tag in self.tags if not tag.gdelt)

    def from_themes(self, counts: Mapping[str, int]) -> dict[str, str]:
        """{slug: evidence} for tags whose GDELT themes are mentioned often enough."""
        found: dict[str, str] = {}
        for tag in self.tags:
            hits = sorted(
                ((t, counts[t]) for t in tag.gdelt if counts.get(t)), key=lambda h: (-h[1], h[0])
            )
            if hits and sum(n for _, n in hits) >= tag.min_mentions:
                found[tag.slug] = "GDELT themes: " + ", ".join(f"{t} ({n})" for t, n in hits[:3])
        return found

    def from_sections(self, labels: Iterable[str]) -> dict[str, str]:
        """{slug: evidence} for tags naming one of the outlet's own section/topic labels.
        "News > Politics" or "Canada/Health" count as each of their parts."""
        parts: dict[str, str] = {}
        for label in labels:
            for part in _SEGMENTS.split(label):
                key = normalize_text(part)
                if key:
                    parts.setdefault(key, part.strip())
        found: dict[str, str] = {}
        for tag in self.tags:
            hit = next((parts[s] for s in tag.sections if s in parts), None)
            if hit:
                found[tag.slug] = f"Outlet's section: {hit}"
        return found

    def match(self, themes: Mapping[str, int], sections: Iterable[str]) -> dict[str, str]:
        """GDELT's themes first; the outlet's own sections only if they give no tag. Tags
        GDELT has no themes for (sports, arts) always come from the sections."""
        by_sections = self.from_sections(sections)
        by_themes = self.from_themes(themes)
        if not by_themes:
            return by_sections
        return by_themes | {s: e for s, e in by_sections.items() if s in self.sections_only}


# ------------------------------------------------------------------ storage


def encode_themes(counts: Mapping[str, int], keep: frozenset[str]) -> str:
    """'ELECTION:5 ENV_CLIMATECHANGE:2' for the themes some tag uses."""
    return " ".join(f"{t}:{n}" for t, n in sorted(counts.items()) if t in keep and n > 0)


def decode_themes(text: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for item in text.split():
        name, _, n = item.rpartition(":")
        if name and n.isdigit():
            out[name] = int(n)
    return out


def encode_sections(labels: Iterable[str]) -> str:
    seen: list[str] = []
    for label in labels:
        label = " ".join(label.split())[:100]
        if label and label not in seen:
            seen.append(label)
    return "\n".join(seen[:MAX_SECTIONS])


def sync_tags(conn: sqlite3.Connection, tags: Sequence[TagConfig]) -> None:
    """Make the tags table mirror tags.yaml. Removed tags lose their assignments."""
    slugs = [t.slug for t in tags]
    for tag in tags:
        conn.execute(
            "INSERT INTO tags (slug, label) VALUES (?, ?) "
            "ON CONFLICT (slug) DO UPDATE SET label = excluded.label",
            (tag.slug, tag.label),
        )
    placeholders = ",".join("?" * len(slugs)) or "''"
    conn.execute(f"DELETE FROM tags WHERE slug NOT IN ({placeholders})", slugs)  # noqa: S608


def tag_article(
    conn: sqlite3.Connection,
    tagger: Tagger,
    article_id: int,
    themes: str,
    sections: str,
    tag_ids: dict[str, int],
) -> None:
    """(Re)compute one article's tags from its stored themes and sections."""
    conn.execute("DELETE FROM article_tags WHERE article_id = ?", (article_id,))
    found = tagger.match(decode_themes(themes), sections.splitlines())
    for slug, evidence in found.items():
        tag_id = tag_ids.get(slug)
        if tag_id is not None:
            conn.execute(
                "INSERT INTO article_tags (article_id, tag_id, matched) VALUES (?, ?, ?)",
                (article_id, tag_id, evidence),
            )


def tag_ids(conn: sqlite3.Connection) -> dict[str, int]:
    return {row["slug"]: row["id"] for row in conn.execute("SELECT id, slug FROM tags")}


def retag_all(conn: sqlite3.Connection, tagger: Tagger, batch: int = 2000) -> int:
    """Recompute every article's tags (after editing tags.yaml). Returns articles scanned."""
    ids = tag_ids(conn)
    conn.execute("BEGIN IMMEDIATE")
    try:
        count = 0
        cursor = conn.execute("SELECT id, gdelt_themes, sections FROM articles")
        while rows := cursor.fetchmany(batch):
            for row in rows:
                tag_article(conn, tagger, row["id"], row["gdelt_themes"], row["sections"], ids)
            count += len(rows)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return count
