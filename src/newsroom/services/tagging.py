"""Keyword tags from config/tags.yaml, matched against article titles."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Sequence

from newsroom.config import TagConfig, normalize_text


class Tagger:
    def __init__(self, tags: Sequence[TagConfig]) -> None:
        self._patterns: list[tuple[str, re.Pattern[str]]] = []
        for tag in tags:
            alternatives = sorted(tag.keywords, key=len, reverse=True)
            body = "|".join(re.escape(k).replace(r"\ ", r"\s+") for k in alternatives)
            self._patterns.append((tag.slug, re.compile(rf"(?<!\w)(?:{body})(?!\w)")))

    def match(self, title: str) -> dict[str, str]:
        """Return {tag slug: matched keyword} for a title."""
        text = normalize_text(title)
        found: dict[str, str] = {}
        for slug, pattern in self._patterns:
            m = pattern.search(text)
            if m:
                found[slug] = m.group(0)
        return found


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
    conn: sqlite3.Connection, tagger: Tagger, article_id: int, title: str, tag_ids: dict[str, int]
) -> None:
    for slug, keyword in tagger.match(title).items():
        tag_id = tag_ids.get(slug)
        if tag_id is not None:
            conn.execute(
                "INSERT OR IGNORE INTO article_tags (article_id, tag_id, matched) VALUES (?, ?, ?)",
                (article_id, tag_id, keyword),
            )


def tag_ids(conn: sqlite3.Connection) -> dict[str, int]:
    return {row["slug"]: row["id"] for row in conn.execute("SELECT id, slug FROM tags")}


def retag_all(conn: sqlite3.Connection, tagger: Tagger, batch: int = 2000) -> int:
    """Recompute every article's tags (after editing tags.yaml). Returns articles scanned."""
    ids = tag_ids(conn)
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM article_tags")
        count = 0
        cursor = conn.execute("SELECT id, title FROM articles")
        while rows := cursor.fetchmany(batch):
            for row in rows:
                tag_article(conn, tagger, row["id"], row["title"], ids)
            count += len(rows)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return count
