"""Read-only view of the ownership graph, shared by the web pages and the CLI.

The graph is small (hundreds of rows), so it is loaded whole per request.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

RELATION_LABELS = {"owned_by": "Owned by", "parent_org": "Parent organization"}
# In a running sentence: "Owned by X, whose parent organization is Y".
RELATION_WHOSE = {"owned_by": "whose owner is", "parent_org": "whose parent organization is"}
# Kinds that say what an owner *is* in ownership terms (a Crown corporation, a public
# company, a co-operative...) rather than what it does (a broadcaster). Checked in order.
LEGAL_FORM_WORDS = (
    "crown corporation",
    "public company",
    "privately held",
    "company",
    "corporation",
    "cooperative",
    "co-operative",
    "nonprofit",
    "non-profit",
    "foundation",
    "trust",
    "department",
    "ministry",
    "agency",
    "government",
    "partnership",
)
SUMMARY_DEPTH = 3  # owners named in the one-line summary beyond the direct one


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

    @property
    def kinds(self) -> list[str]:
        return [k.strip() for k in self.kind.split(";") if k.strip()]

    @property
    def type_label(self) -> str:
        """The one kind that best says what this owner is, e.g. "Crown corporation" out of
        "broadcaster; production company; Crown corporation". As Wikidata words it."""
        kinds = self.kinds
        if "human" in kinds:
            return "person"
        for word in LEGAL_FORM_WORDS:
            for k in kinds:
                if word in k.lower():
                    return k
        return kinds[0] if kinds else ""


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

    @property
    def whose(self) -> str:
        return RELATION_WHOSE[self.relation]


@dataclass
class Step:
    """One edge in an ownership chain, with the parent's own chain nested below."""

    edge: Edge
    parent: Node
    above: list[Step] = field(default_factory=list)
    cycle: bool = False


@dataclass
class Summary:
    """What the article card shows for an outlet: its direct owners, and the chain above
    the first of them (first recorded owner at each step, up to SUMMARY_DEPTH), each with
    the relation Wikidata states."""

    entity: Node | None
    direct: list[tuple[Edge, Node]]
    above: list[tuple[Edge, Node]] = field(default_factory=list)
    more: bool = False  # the chain branches or goes on beyond `above` (shown as "…")


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

    def lineage(self, entity_id: int | None) -> list[int]:
        """The entity followed by everything above it, nearest first."""
        if entity_id is None or entity_id not in self.nodes:
            return []
        order, queue, seen = [], [entity_id], {entity_id}
        while queue:
            current = queue.pop(0)
            order.append(current)
            for e in self.up.get(current, []):
                if e.parent not in seen:
                    seen.add(e.parent)
                    queue.append(e.parent)
        return order

    def summary(self, entity_id: int | None) -> Summary:
        if entity_id is None or entity_id not in self.nodes:
            return Summary(None, [])
        direct = [(e, self.nodes[e.parent]) for e in self.up.get(entity_id, [])]
        above: list[tuple[Edge, Node]] = []
        more = False
        if direct:
            seen = {entity_id, direct[0][1].id}
            current = direct[0][1].id
            while self.up.get(current):
                if len(self.up[current]) > 1:
                    more = True  # other owners at this step: in the full chain, not the line
                e = self.up[current][0]
                if e.parent in seen:
                    break  # the records loop back
                if len(above) == SUMMARY_DEPTH:
                    more = True
                    break
                above.append((e, self.nodes[e.parent]))
                seen.add(e.parent)
                current = e.parent
        return Summary(self.nodes[entity_id], direct, above, more)


def summary_text(summary: Summary) -> str:
    """The card's one-line summary as plain text (same wording as the web page)."""
    if not summary.direct:
        return "No owner listed on Wikidata" if summary.entity else "Owner: no record found"
    parts = []
    for i, (edge, node) in enumerate(summary.direct):
        label = edge.label if i == 0 else edge.label.lower()
        colon = ":" if edge.relation == "parent_org" else ""
        share = f" ({edge.share * 100:.4g}%)" if edge.share else ""
        text = f"{label}{colon} {node.name}{share}"
        if i == 0:
            text += "".join(f", {e.whose} {n.name}" for e, n in summary.above)
            text += ", …" if summary.more else ""
        parts.append(text)
    return "; ".join(parts)
