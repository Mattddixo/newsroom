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


@dataclass(frozen=True)
class TagConfig:
    slug: str
    label: str
    keywords: tuple[str, ...]


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
        seen.add(domain)
        seen.update(also)
        outlets.append(OutletConfig(domain, name, country, language, also, feeds, note))
    return outlets


def load_tags(path: Path) -> list[TagConfig]:
    data = _load(path)
    items = data.get("tags") if isinstance(data, dict) else None
    if not isinstance(items, dict):
        raise ConfigError(f"{path}: expected a top-level 'tags:' mapping")
    tags: list[TagConfig] = []
    for slug, body in items.items():
        slug = str(slug)
        if not SLUG_RE.match(slug):
            raise ConfigError(f"{path.name}: invalid tag slug {slug!r}")
        if not isinstance(body, dict):
            raise ConfigError(f"{path.name} ({slug}): expected label and keywords")
        label = str(body.get("label", "")).strip()
        keywords = body.get("keywords")
        if not label or not isinstance(keywords, list) or not keywords:
            raise ConfigError(f"{path.name} ({slug}): label and a non-empty keywords list required")
        cleaned = tuple(k for k in (normalize_text(str(k)) for k in keywords) if k)
        tags.append(TagConfig(slug, label, cleaned))
    return tags


def normalize_text(text: str) -> str:
    """Casefold, strip accents, collapse whitespace. Used for keyword matching."""
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
