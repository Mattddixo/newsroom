"""Which items in the ownership records an article names.

GDELT lists the people and organizations each article names. When the article is
collected, each name is compared with the names of every item in the ownership records
(Wikidata labels and specific aliases, see newsroom.names) and exact matches are stored.
Whether a named item is in the outlet's own ownership chain is worked out when the page
is shown (OwnerMention), so a change of ownership is reflected at once.

Names the outlet itself goes by are skipped: CBC News naming "CBC" is the outlet
referring to itself, not a report about the corporation that owns it.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field

from newsroom.names import alias_keys, name_key


@dataclass
class NameIndex:
    by_key: dict[str, set[str]] = field(default_factory=dict)  # name key -> QIDs
    own: dict[str, set[str]] = field(default_factory=dict)  # outlet domain -> its own keys

    @classmethod
    def load(cls, conn: sqlite3.Connection) -> NameIndex:
        aliases: dict[int, list[str]] = {}
        for r in conn.execute("SELECT entity_id, alias FROM entity_aliases"):
            aliases.setdefault(r["entity_id"], []).append(r["alias"])
        index = cls()
        every_key: dict[int, set[str]] = {}
        for r in conn.execute("SELECT id, qid, name FROM entities"):
            names = aliases.get(r["id"], [])
            for key in alias_keys(r["name"], names):
                index.by_key.setdefault(key, set()).add(r["qid"])
            every_key[r["id"]] = {k for k in map(name_key, [r["name"], *names]) if k}
        for r in conn.execute("SELECT domain, display_name, entity_id FROM outlets"):
            own = every_key.get(r["entity_id"], set()) | {
                name_key(r["display_name"]),
                name_key(r["domain"].split(".")[0]),
            }
            own.discard("")
            index.own[r["domain"]] = own
        return index

    def match(self, domain: str, names: Iterable[tuple[str, int]]) -> dict[str, int]:
        """{QID: mentions} for the items named in an article from `domain`."""
        own = self.own.get(domain, set())
        found: dict[str, int] = {}
        for name, mentions in names:
            key = name_key(name)
            if not key or key in own:
                continue
            qids = self.by_key.get(key, set())
            if len(qids) == 1:  # two items by the same name: can't tell which is meant
                qid = next(iter(qids))
                found[qid] = found.get(qid, 0) + mentions
        return found


def store(conn: sqlite3.Connection, article_id: int, found: dict[str, int]) -> None:
    conn.executemany(
        "INSERT INTO article_mentions (article_id, qid, mentions) VALUES (?, ?, ?)"
        " ON CONFLICT (article_id, qid) DO NOTHING",
        [(article_id, qid, n) for qid, n in found.items()],
    )
