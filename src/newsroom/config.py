"""Loading and validating the hand-edited YAML files in config/."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import yaml

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
        seen.add(domain)
        outlets.append(OutletConfig(domain, name, country, language))
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
