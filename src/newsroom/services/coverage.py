"""Coverage check: does GDELT carry each outlet, and under which web addresses?

Reads a few hours of GDELT's 15-minute files and counts articles per host. For each
outlet it reports the articles on its own domains, and "look-alike" hosts: addresses
containing the outlet's name that no outlet in outlets.yaml claims (how a move such as
msnbc.com -> ms.now shows up). Nothing is changed; it's a report for editing outlets.yaml.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from newsroom.sources.gdelt_files import DomainMatcher

GENERIC_LABELS = {"www", "ici", "m", "amp", "edition", "news", "en", "fr"}
MIN_TOKEN = 4  # shorter names (cnn, npr, wsj) match too many unrelated hosts
LOOKALIKES_SHOWN = 5


@dataclass
class OutletCoverage:
    domain: str
    name: str
    in_gdelt: int = 0  # articles on the outlet's own domains in the files read
    hosts: dict[str, int] = field(default_factory=dict)  # which of its hosts, how many
    lookalikes: list[tuple[str, int]] = field(default_factory=list)
    stored_7d: int = 0  # articles already stored, last 7 days

    @property
    def verdict(self) -> str:
        if self.in_gdelt:
            return "carried"
        if self.lookalikes:
            return "check look-alikes"
        return "not in GDELT's files"


def name_tokens(domain: str, others: Sequence[str] = ()) -> set[str]:
    """Distinctive name parts of an outlet's domains: washingtonpost.com -> washingtonpost."""
    tokens = set()
    for d in (domain, *others):
        labels = [part for part in d.split(".")[:-1] if part not in GENERIC_LABELS]
        tokens.update(label for label in labels if len(label) >= MIN_TOKEN)
    return tokens


def build_report(
    outlets: Mapping[str, tuple[str, Sequence[str]]],
    host_counts: Mapping[str, int],
    stored_7d: Mapping[str, int],
) -> list[OutletCoverage]:
    """`outlets`: domain -> (name, other domains). Sorted: problems first, then by name."""
    aliases = {d: list(others) for d, (_, others) in outlets.items()}
    match = DomainMatcher(list(outlets), aliases)
    report = {
        d: OutletCoverage(d, name, stored_7d=stored_7d.get(d, 0))
        for d, (name, _) in outlets.items()
    }
    unclaimed: dict[str, int] = {}
    for host, n in host_counts.items():
        owner = match(host)
        if owner:
            report[owner].in_gdelt += n
            report[owner].hosts[host] = report[owner].hosts.get(host, 0) + n
        else:
            unclaimed[host] = n
    for d, (_, others) in outlets.items():
        tokens = name_tokens(d, others)
        # The name must be a whole part of the address (politico.eu, lesoleil.sn), not a
        # fragment of another word (latimes in manilatimes, time in timesofindia).
        similar = [(h, n) for h, n in unclaimed.items() if tokens & set(h.split("."))]
        similar.sort(key=lambda hn: (-hn[1], hn[0]))
        report[d].lookalikes = similar[:LOOKALIKES_SHOWN]
    order = {"not in GDELT's files": 0, "check look-alikes": 1, "carried": 2}
    return sorted(report.values(), key=lambda r: (order[r.verdict], r.name.casefold()))
