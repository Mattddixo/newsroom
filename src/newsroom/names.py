"""Comparing names of people and organizations across sources.

GDELT writes the names it finds in an article its own way ("bce inc", "Mark Carney");
Wikidata has an item's label and aliases ("BCE Inc.", "BCE"). A name matches only when
both reduce to the same key: case, accents and punctuation ignored, and a trailing legal
form dropped ("Inc.", "Ltd", "Corporation"), nothing more. No partial or fuzzy matches.
"""

from __future__ import annotations

import re

from newsroom.config import normalize_text

_WORD = re.compile(r"\w+", re.UNICODE)
# Endings that only name a legal form: "BCE Inc." is "BCE". Not "Company" or "Group",
# which are often part of the name: The New York Times Company isn't The New York Times.
LEGAL_FORMS = frozenset(
    {"inc", "incorporated", "ltd", "limited", "llc", "plc", "corp", "corporation", "co",
     "sa", "ag", "gmbh", "lp", "llp", "ltee"}
)  # fmt: skip
MIN_KEY_LENGTH = 3  # shorter keys ("ap", "cp") are too ambiguous to match on


def name_key(name: str) -> str:
    """The form two names are compared in, or '' when too short to compare safely."""
    words = _WORD.findall(normalize_text(name))
    if words and words[0] == "the":
        words = words[1:]
    while len(words) > 1 and words[-1] in LEGAL_FORMS:
        words.pop()
    key = " ".join(words)
    return key if len(key.replace(" ", "")) >= MIN_KEY_LENGTH else ""


def is_acronym(name: str) -> bool:
    """ "BCE", "CN2i": a short name in capitals, which stands for one organization."""
    compact = name.replace(".", "").strip()
    return 3 <= len(compact) <= 8 and compact.isalnum() and sum(c.isupper() for c in compact) >= 2


def alias_keys(label: str, aliases: list[str]) -> set[str]:
    """Keys an item can be recognized by: its label, and those of its aliases that are
    specific enough (several words, or an acronym). A one-word alias such as "Rogers"
    names too many things to count as a mention of Rogers Communications."""
    keys = {name_key(label)}
    for alias in aliases:
        if " " in alias.strip() or is_acronym(alias):
            keys.add(name_key(alias))
    keys.discard("")
    return keys


def display_name(name: str) -> str:
    """A GDELT name for display: GDELT lower-cases organizations ("world health
    organization", "nato"). Short one-word names are read as acronyms."""
    if not name.islower():
        return name
    if " " not in name and len(name) <= 4:
        return name.upper()
    return name.title()
