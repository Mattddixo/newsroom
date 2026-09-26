"""Loading and validating the hand-edited YAML files in config/."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import yaml

from newsroom.urls import is_http_url

DOMAIN_RE = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)+$")
SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
THEME_RE = re.compile(r"^[A-Z0-9_]+$")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class OutletConfig:
    domain: str
    name: str
    country: str
    language: str
    # Other domains the outlet publishes articles on (a rebrand, a second site). Articles
    # on these count as this outlet's; subdomains are included as for `domain`.
    also: tuple[str, ...] = ()
    # The outlet's own RSS/Atom feeds: a second article source (for outlets GDELT doesn't
    # carry). Only items linking to the outlet's own domains are kept.
    feeds: tuple[str, ...] = ()
    # Shown on the outlet page, e.g. why no articles are available. Plain text.
    note: str = ""
    # Pins the outlet's Wikidata item (a QID, or "none" if it has no item) instead of
    # matching automatically. For outlets the automatic match gets wrong or can't find.
    wikidata: str = ""


@dataclass(frozen=True)
class TagConfig:
    slug: str
    label: str
    # GDELT GKG theme names (exact, e.g. ENV_CLIMATECHANGE): GDELT's coding of the full
    # article text. The tag applies when these themes are mentioned `min_mentions` times.
    gdelt: tuple[str, ...] = ()
    # The outlet's own section / topic names (normalized): used for articles GDELT's
    # themes give no tag.
    sections: tuple[str, ...] = ()
    min_mentions: int = 3


def _load(path: Path) -> object:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"{path} not found") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from exc


def load_outlets(path: Path) -> list[OutletConfig]:
    data = _load(path)
    items = data.get("outlets") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ConfigError(f"{path}: expected a top-level 'outlets:' list")
    outlets: list[OutletConfig] = []
    seen: set[str] = set()
    for i, item in enumerate(items, 1):
        where = f"{path.name} entry {i}"
        if not isinstance(item, dict):
            raise ConfigError(f"{where}: expected a mapping")
        domain = str(item.get("domain", "")).strip().lower()
        name = str(item.get("name", "")).strip()
        country = str(item.get("country", "")).strip().upper()
        language = str(item.get("language", "")).strip().lower()
        if not DOMAIN_RE.match(domain):
            raise ConfigError(f"{where}: invalid domain {domain!r}")
        if domain in seen:
            raise ConfigError(f"{where}: duplicate domain {domain}")
        if not name:
            raise ConfigError(f"{where} ({domain}): name is required")
        if not re.fullmatch(r"[A-Z]{2}", country):
            raise ConfigError(f"{where} ({domain}): country must be a 2-letter code")
        if not re.fullmatch(r"[a-z]{2}", language):
            raise ConfigError(f"{where} ({domain}): language must be a 2-letter code")
        raw_also = item.get("also", [])
        if not isinstance(raw_also, list):
            raise ConfigError(f"{where} ({domain}): 'also' must be a list of domains")
        also = tuple(str(a).strip().lower() for a in raw_also)
        for alias in also:
            if not DOMAIN_RE.match(alias):
                raise ConfigError(f"{where} ({domain}): invalid domain in 'also': {alias!r}")
            # `seen` holds every domain listed so far, main or not, so a domain can't
            # belong to two outlets whichever order they're in.
            if alias in seen or alias == domain or also.count(alias) > 1:
                raise ConfigError(f"{where} ({domain}): {alias} is listed twice")
        raw_feeds = item.get("feeds", [])
        if not isinstance(raw_feeds, list):
            raise ConfigError(f"{where} ({domain}): 'feeds' must be a list of URLs")
        feeds = tuple(str(f).strip() for f in raw_feeds)
        for feed in feeds:
            if not is_http_url(feed):
                raise ConfigError(f"{where} ({domain}): invalid feed URL {feed!r}")
        note = " ".join(str(item.get("note", "") or "").split())
        if len(note) > 300:
            raise ConfigError(f"{where} ({domain}): note is longer than 300 characters")
        wikidata = str(item.get("wikidata", "") or "").strip()
        if wikidata and wikidata != "none" and not re.fullmatch(r"Q[1-9]\d{0,11}", wikidata):
            raise ConfigError(f"{where} ({domain}): wikidata must be a QID like Q12345 or 'none'")
        seen.add(domain)
        seen.update(also)
        outlets.append(OutletConfig(domain, name, country, language, also, feeds, note, wikidata))
    return outlets


def load_tags(path: Path) -> list[TagConfig]:
    data = _load(path)
    items = data.get("tags") if isinstance(data, dict) else None
    if not isinstance(items, dict):
        raise ConfigError(f"{path}: expected a top-level 'tags:' mapping")
    default_min = data.get("gdelt_min_mentions", 3) if isinstance(data, dict) else 3
    tags: list[TagConfig] = []
    for slug, body in items.items():
        slug = str(slug)
        where = f"{path.name} ({slug})"
        if not SLUG_RE.match(slug):
            raise ConfigError(f"{path.name}: invalid tag slug {slug!r}")
        if not isinstance(body, dict):
            raise ConfigError(f"{where}: expected label, gdelt and sections")
        if "keywords" in body:
            raise ConfigError(f"{where}: 'keywords' is no longer used; use gdelt and sections")
        label = str(body.get("label", "")).strip()
        gdelt = body.get("gdelt", [])
        sections = body.get("sections", [])
        min_mentions = body.get("gdelt_min_mentions", default_min)
        if not label:
            raise ConfigError(f"{where}: label required")
        if not isinstance(gdelt, list) or not isinstance(sections, list):
            raise ConfigError(f"{where}: gdelt and sections must be lists")
        if not gdelt and not sections:
            raise ConfigError(f"{where}: needs gdelt themes or sections")
        themes = tuple(str(t).strip() for t in gdelt)
        bad = [t for t in themes if not THEME_RE.match(t)]
        if bad:
            raise ConfigError(f"{where}: not a GDELT theme name: {bad[0]!r}")
        if not isinstance(min_mentions, int) or isinstance(min_mentions, bool) or min_mentions < 1:
            raise ConfigError(f"{where}: gdelt_min_mentions must be a whole number >= 1")
        cleaned = tuple(k for k in (normalize_text(str(k)) for k in sections) if k)
        tags.append(TagConfig(slug, label, themes, cleaned, min_mentions))
    return tags


def normalize_text(text: str) -> str:
    """Casefold, strip accents, collapse whitespace. Used for section matching."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(stripped.split())


CURATED_KINDS = ("government_appropriation", "grant", "nonprofit_revenue", "public_filing")


@dataclass(frozen=True)
class CuratedFunding:
    outlet: str | None  # outlet domain, or
    qid: str | None  # a Wikidata item
    kind: str
    label: str
    amount: float | None
    currency: str | None
    period: str | None
    funder: str | None
    source_url: str
    retrieved: str  # YYYY-MM-DD: when you checked the source


def load_curated_funding(path: Path) -> list[CuratedFunding]:
    """Hand-curated funding figures (e.g. public broadcasters). Missing file = none."""
    if not path.exists():
        return []
    data = _load(path)
    items = data.get("funding") if isinstance(data, dict) else None
    if items is None:
        return []
    if not isinstance(items, list):
        raise ConfigError(f"{path.name}: 'funding:' must be a list")
    out: list[CuratedFunding] = []
    for i, item in enumerate(items, 1):
        where = f"{path.name} entry {i}"
        if not isinstance(item, dict):
            raise ConfigError(f"{where}: expected a mapping")
        outlet = str(item["outlet"]).strip().lower() if item.get("outlet") else None
        qid = str(item["qid"]).strip().upper() if item.get("qid") else None
        if bool(outlet) == bool(qid):
            raise ConfigError(f"{where}: give exactly one of 'outlet' or 'qid'")
        if outlet and not DOMAIN_RE.match(outlet):
            raise ConfigError(f"{where}: invalid outlet domain {outlet!r}")
        if qid and not re.fullmatch(r"Q[1-9]\d{0,11}", qid):
            raise ConfigError(f"{where}: invalid qid {qid!r}")
        kind = str(item.get("kind", ""))
        if kind not in CURATED_KINDS:
            raise ConfigError(f"{where}: kind must be one of {', '.join(CURATED_KINDS)}")
        label = str(item.get("label", "")).strip()
        if not label:
            raise ConfigError(f"{where}: label is required")
        amount = item.get("amount")
        if amount is not None and (
            isinstance(amount, bool) or not isinstance(amount, int | float) or amount < 0
        ):
            raise ConfigError(f"{where}: amount must be a non-negative number (no commas)")
        currency = str(item["currency"]).strip().upper() if item.get("currency") else None
        if amount is not None and not (currency and re.fullmatch(r"[A-Z]{3}", currency)):
            raise ConfigError(f"{where}: a 3-letter currency is required with an amount")
        source_url = str(item.get("source_url", "")).strip()
        if not is_http_url(source_url):
            raise ConfigError(f"{where}: source_url (http/https) is required")
        try:
            retrieved = date.fromisoformat(str(item.get("retrieved", ""))).isoformat()
        except ValueError as exc:
            raise ConfigError(f"{where}: retrieved must be a date (YYYY-MM-DD)") from exc
        out.append(
            CuratedFunding(
                outlet=outlet,
                qid=qid,
                kind=kind,
                label=label,
                amount=float(amount) if amount is not None else None,
                currency=currency,
                period=str(item["period"]).strip() if item.get("period") else None,
                funder=str(item["funder"]).strip() if item.get("funder") else None,
                source_url=source_url,
                retrieved=retrieved,
            )
        )
    return out


# ------------------------------------------------------------------ ownership corrections

CORRECTION_ACTIONS = {
    "add_owner": ("add", "owned_by"),
    "add_parent": ("add", "parent_org"),
    "remove_owner": ("remove", "owned_by"),
    "remove_parent": ("remove", "parent_org"),
}


@dataclass(frozen=True)
class OwnershipCorrection:
    """One cited change to what Wikidata says about who owns something (ownership.yaml).
    `child` is an outlet domain or the name/QID of an owner; `target` a name or QID."""

    outlet: str | None  # the outlet's own Wikidata item
    entity: str | None  # or another item in the chain, by name or QID
    action: str  # add | remove
    relation: str  # owned_by | parent_org
    target: str  # the owner / parent, by name or QID
    source: str
    checked: str
    note: str = ""

    @property
    def key(self) -> str:
        """Stable name for this correction, e.g. "macleans.ca add owner: Q7589303"."""
        what = "owner" if self.relation == "owned_by" else "parent"
        return f"{self.outlet or self.entity} {self.action} {what}: {self.target}"


def load_ownership_corrections(path: Path) -> list[OwnershipCorrection]:
    """Cited corrections to Wikidata's ownership records. Missing file = none."""
    if not path.exists():
        return []
    data = _load(path)
    items = data.get("corrections") if isinstance(data, dict) else None
    if items is None:
        return []
    if not isinstance(items, list):
        raise ConfigError(f"{path.name}: 'corrections:' must be a list")
    out: list[OwnershipCorrection] = []
    for i, item in enumerate(items, 1):
        where = f"{path.name} entry {i}"
        if not isinstance(item, dict):
            raise ConfigError(f"{where}: expected a mapping")
        outlet = str(item.get("outlet") or "").strip().lower() or None
        entity = str(item.get("entity") or "").strip() or None
        if bool(outlet) == bool(entity):
            raise ConfigError(f"{where}: give exactly one of 'outlet' or 'entity'")
        if outlet and not DOMAIN_RE.match(outlet):
            raise ConfigError(f"{where}: invalid outlet domain {outlet!r}")
        source = str(item.get("source") or "").strip()
        if not is_http_url(source):
            raise ConfigError(f"{where}: every correction needs a source (http/https URL)")
        checked = str(item.get("checked") or "").strip()
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", checked):
            raise ConfigError(f"{where}: 'checked' must be the date you checked, YYYY-MM-DD")
        note = " ".join(str(item.get("note") or "").split())[:200]
        actions = [k for k in CORRECTION_ACTIONS if item.get(k)]
        if not actions:
            raise ConfigError(f"{where}: needs one of {', '.join(CORRECTION_ACTIONS)}")
        for key in actions:
            action, relation = CORRECTION_ACTIONS[key]
            out.append(
                OwnershipCorrection(
                    outlet, entity, action, relation, str(item[key]).strip(), source, checked, note
                )
            )
    return out
