"""Read-only view of the ownership graph, shared by the web pages and the CLI.

The graph is small (hundreds of rows), so it is loaded whole per request.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

RELATION_LABELS = {"owned_by": "Owned by", "parent_org": "Parent organization"}


@dataclass(frozen=True)
class Node:
    id: int
    qid: str
    name: str
    description: str
    kind: str
    country: str
    website: str | None
    source_url: str
    retrieved_at: str


@dataclass(frozen=True)
class Edge:
    child: int
    parent: int
    relation: str
    share: float | None
    start_date: str | None
    source: str
    source_url: str
    retrieved_at: str

    @property
    def label(self) -> str:
        return RELATION_LABELS[self.relation]


@dataclass
class Step:
    """One edge in an ownership chain, with the parent's own chain nested below."""

    edge: Edge
    parent: Node
    above: list[Step] = field(default_factory=list)
    cycle: bool = False


@dataclass
class Summary:
    """What the article card shows for an outlet."""

    entity: Node | None
    direct: list[tuple[Edge, Node]]
    ultimate: list[Node]


class Graph:
    def __init__(self, nodes: dict[int, Node], edges: list[Edge]) -> None:
        self.nodes = nodes
        self.up: dict[int, list[Edge]] = {}
        self.down: dict[int, list[Edge]] = {}
        for e in edges:
            self.up.setdefault(e.child, []).append(e)
            self.down.setdefault(e.parent, []).append(e)
        for lst in (*self.up.values(), *self.down.values()):
            lst.sort(key=lambda e: (e.relation != "owned_by", self.nodes[e.parent].name))

    @classmethod
    def load(cls, conn: sqlite3.Connection) -> Graph:
        nodes = {
            r["id"]: Node(
                r["id"],
                r["qid"],
                r["name"],
                r["description"],
                r["kind"],
                r["country"],
                r["website"],
                r["source_url"],
                r["retrieved_at"],
            )
            for r in conn.execute("SELECT * FROM entities")
        }
        edges = [
            Edge(
                r["child_entity_id"],
                r["parent_entity_id"],
                r["relation"],
                r["share"],
                r["start_date"],
                r["source"],
                r["source_url"],
                r["retrieved_at"],
            )
            for r in conn.execute("SELECT * FROM ownership_edges ORDER BY id")
        ]
        return cls(nodes, edges)

    def by_qid(self, qid: str) -> Node | None:
        return next((n for n in self.nodes.values() if n.qid == qid), None)

    def chain(self, entity_id: int, _seen: frozenset[int] = frozenset()) -> list[Step]:
        seen = _seen | {entity_id}
        steps = []
        for e in self.up.get(entity_id, []):
            parent = self.nodes[e.parent]
            if e.parent in seen:
                steps.append(Step(e, parent, cycle=True))
            else:
                steps.append(Step(e, parent, self.chain(e.parent, seen)))
        return steps

    def ultimate(self, entity_id: int) -> list[Node]:
        """Top-most entities reachable upward (those with no recorded parent)."""
        roots: dict[int, Node] = {}
        seen = {entity_id}
        stack = [entity_id]
        while stack:
            current = stack.pop()
            parents = self.up.get(current, [])
            if not parents and current != entity_id:
                roots[current] = self.nodes[current]
            for e in parents:
                if e.parent not in seen:
                    seen.add(e.parent)
                    stack.append(e.parent)
        return sorted(roots.values(), key=lambda n: n.name)

    def descendants(self, entity_id: int) -> dict[int, list[int]]:
        """Every entity below `entity_id`, mapped to one path of entity ids from it."""
        paths: dict[int, list[int]] = {}
        queue = [(entity_id, [entity_id])]
        while queue:
            current, path = queue.pop(0)
            for e in self.down.get(current, []):
                if e.child not in paths and e.child != entity_id:
                    paths[e.child] = [*path, e.child]
                    queue.append((e.child, [*path, e.child]))
        return paths

    def summary(self, entity_id: int | None) -> Summary:
        if entity_id is None or entity_id not in self.nodes:
            return Summary(None, [], [])
        direct = [(e, self.nodes[e.parent]) for e in self.up.get(entity_id, [])]
        ultimate = self.ultimate(entity_id)
        direct_ids = {n.id for _, n in direct}
        if {n.id for n in ultimate} <= direct_ids:
            ultimate = []  # nothing more to say beyond the direct owner
        return Summary(self.nodes[entity_id], direct, ultimate)
