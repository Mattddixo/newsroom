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
    def minority(self) -> bool:
        """A stated stake under half: a shareholder, not an owner."""
        return self.relation == "owned_by" and self.share is not None and self.share < 0.5

    @property
    def label(self) -> str:
        return "Shareholder" if self.minority else RELATION_LABELS[self.relation]

    @property
    def whose(self) -> str:
        return RELATION_WHOSE[self.relation]


@dataclass(frozen=True)
class Source:
    url: str
    retrieved_at: str
    what: str  # where it comes from, shown on hover
    verb: str  # "retrieved" (Wikidata) or "checked" (a cited correction)


@dataclass
class Step:
    """One owner in an ownership chain: every statement linking the child to it (Wikidata
    often has "owned by" and "parent organization" for the same pair), with the owner's own
    chain nested below. An owner's chain is spelled out once per chain; later mentions are
    marked `listed` instead of repeating it."""

    edges: list[Edge]
    parent: Node
    above: list[Step] = field(default_factory=list)
    cycle: bool = False  # the owner is below itself: the records loop back
    listed: bool = False  # the owner's chain is already shown earlier in this chain

    @property
    def edge(self) -> Edge:
        return self.edges[0]  # owned_by first (see Graph.__init__)

    @property
    def minority(self) -> bool:
        return all(e.minority for e in self.edges)

    @property
    def label(self) -> str:
        labels = [e.label for e in self.edges if not (e.minority and not self.minority)]
        return " · ".join(dict.fromkeys(labels))

    @property
    def share(self) -> float | None:
        shares = [e.share for e in self.edges if e.share is not None]
        return max(shares) if shares else None

    @property
    def dates(self) -> list[str]:
        return sorted({e.start_date for e in self.edges if e.start_date})

    @property
    def corrected(self) -> bool:
        return any(e.source == "correction" for e in self.edges)

    @property
    def sources(self) -> list[Source]:
        out: dict[str, Source] = {}
        wikidata = [e for e in self.edges if e.source == "wikidata"]
        if wikidata:
            # One link: to the statement, or to the item's page when it makes several.
            url = wikidata[0].source_url
            url = url if len(wikidata) == 1 else url.split("#")[0]
            what = "Wikidata " + url.rsplit("/", 1)[-1]
            out[url] = Source(url, wikidata[0].retrieved_at, what, "retrieved")
        for e in self.edges:
            if e.source != "wikidata" and e.source_url not in out:
                what = (
                    f"Correction, from {e.source_url}" if e.source == "correction" else e.source_url
                )
                out[e.source_url] = Source(e.source_url, e.retrieved_at, what, "checked")
        return list(out.values())


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
        # Wikidata statements set aside as out of date by a cited correction: shown as
        # such on the outlet page, never as current ownership.
        self.set_aside: dict[int, list[Edge]] = {}
        for e in edges:
            if e.source == "set_aside":
                self.set_aside.setdefault(e.child, []).append(e)
                continue
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

    def chain(self, entity_id: int) -> list[Step]:
        """Everything above `entity_id`, one Step per owner. Each owner's own chain is
        spelled out the first time it appears; minority shareholders' chains are not."""
        expanded: set[int] = {entity_id}

        def walk(child: int, path: frozenset[int]) -> list[Step]:
            by_parent: dict[int, list[Edge]] = {}
            for e in self.up.get(child, []):
                by_parent.setdefault(e.parent, []).append(e)
            steps = []
            for parent_id, edges in by_parent.items():
                step = Step(edges, self.nodes[parent_id])
                if parent_id in path:
                    step.cycle = True
                elif parent_id in expanded:
                    step.listed = bool(self.up.get(parent_id))
                elif not step.minority:
                    expanded.add(parent_id)
                    step.above = walk(parent_id, path | {parent_id})
                steps.append(step)
            return steps

        return walk(entity_id, frozenset({entity_id}))

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
        direct: list[tuple[Edge, Node]] = []
        for e in self.up.get(entity_id, []):  # owned_by first (see __init__)
            if all(n.id != e.parent for _, n in direct):  # "owned by X; parent: X" says X twice
                direct.append((e, self.nodes[e.parent]))
        above: list[tuple[Edge, Node]] = []
        more = False
        if direct:
            seen = {entity_id, direct[0][1].id}
            current = direct[0][1].id
            while self.up.get(current):
                if "public company" in self.nodes[current].kinds:
                    # Owned by its shareholders: the line stops here (the outlet page lists
                    # any stakes Wikidata records).
                    more = True
                    break
                owners = [e for e in self.up[current] if not e.minority]
                if not owners:
                    more = True
                    break
                if len(self.up[current]) > 1:
                    more = True  # other owners at this step: in the full chain, not the line
                e = owners[0]
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


def ownership_lines(conn: sqlite3.Connection) -> dict[int, str]:
    """Each active outlet's ownership line, as the site shows it (for the change log)."""
    graph = Graph.load(conn)
    rows = conn.execute("SELECT id, entity_id FROM outlets WHERE active = 1").fetchall()
    return {r["id"]: summary_text(graph.summary(r["entity_id"])) for r in rows}
