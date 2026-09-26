"""Wikidata adapter: domain -> item matching (SPARQL) and entity data (wbgetentities).

Only statements Wikidata itself calls current are used:
  - deprecated statements are ignored
  - if a property has preferred-rank statements, only those are used ("best rank")
  - statements with an end time (P582) in the past are ignored
  - "unknown value" / "no value" snaks are ignored
  - a person (instance of human, Q5) has no owners: a person can't be owned, so an
    "owned by" / "parent organization" statement on one is a data error (for example a
    team a person has a stake in), and the chain stops there
  - a person's date of death (P570) is kept: someone who has died can't be a current
    owner, so an ownership statement pointing at them is out of date (see ownership)
Nothing is inferred: if a property is absent, the field is absent.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import quote

import httpx

from newsroom.net.http import ApiClient, ApiError, RateLimited
from newsroom.urls import host_of, is_http_url

log = logging.getLogger(__name__)

SPARQL_ENDPOINT = "https://query.wikidata.org/sparql"
API_ENDPOINT = "https://www.wikidata.org/w/api.php"
ENTITY_PAGE = "https://www.wikidata.org/wiki/"
COMMONS_FILE_PAGE = "https://commons.wikimedia.org/wiki/File:"
COMMONS_FILE_PATH = "https://commons.wikimedia.org/wiki/Special:FilePath/"

P_INSTANCE_OF = "P31"
Q_HUMAN = "Q5"
P_COUNTRY = "P17"
P_OWNED_BY = "P127"
P_PARENT_ORG = "P749"
P_WEBSITE = "P856"
P_LOGO = "P154"
P_START = "P580"
P_DIED = "P570"
P_END = "P582"
P_SHARE = "P1107"
IDENTIFIER_PROPERTIES = {"P5531": "sec_cik", "P1297": "us_ein"}
RELATIONS = {P_OWNED_BY: "owned_by", P_PARENT_ORG: "parent_org"}

QID_RE = re.compile(r"^Q[1-9]\d{0,11}$")
LANGUAGES = ("en", "fr")
BATCH = 50
MATCH_BATCH = 10
# "News media" and "mass media": an item that is an instance of one of their subclasses
# (newspaper, news website, TV channel, ...) is a news outlet by Wikidata's own
# classification, unlike the company that publishes it.
NEWS_TYPES = ("Q1193236", "Q11033")
SEARCH_LIMIT = 10


def entity_url(qid: str, prop: str | None = None) -> str:
    return f"{ENTITY_PAGE}{qid}" + (f"#{prop}" if prop else "")


@dataclass(frozen=True)
class Candidate:
    qid: str
    label: str
    description: str
    website: str


@dataclass(frozen=True)
class ParentClaim:
    qid: str
    relation: str  # owned_by | parent_org
    prop: str  # P127 | P749
    share: float | None = None  # 0..1, only when Wikidata states P1107
    start: str | None = None  # ISO date at the precision Wikidata gives


@dataclass
class EntityData:
    qid: str
    label: str
    description: str = ""
    instance_of: list[str] = field(default_factory=list)
    country: list[str] = field(default_factory=list)
    website: str | None = None
    websites: list[str] = field(default_factory=list)  # every current official website
    logo_file: str | None = None
    parents: list[ParentClaim] = field(default_factory=list)
    identifiers: dict[str, list[str]] = field(default_factory=dict)
    died: str | None = None  # a person's date of death, as precise as Wikidata gives it


# --------------------------------------------------------------------------- parsing


def website_variants(domain: str) -> list[str]:
    hosts = [domain, f"www.{domain}"]
    return [
        f"{scheme}://{h}{slash}"
        for scheme in ("https", "http")
        for h in hosts
        for slash in ("", "/")
    ]


def build_match_query(domains: Sequence[str]) -> str:
    values = " ".join(f"<{v}>" for d in domains for v in website_variants(d))
    # Join order matters on the Wikidata Query Service: left to its own planner it may
    # walk every P856 statement first (timeouts). With the optimizer off, it starts from
    # the handful of URLs, looks up statements *by value*, then their items.
    return (
        "SELECT ?item ?itemLabel ?itemDescription ?site WHERE {"
        ' hint:Query hint:optimizer "None" .'
        f" VALUES ?site {{ {values} }}"
        " ?st ps:P856 ?site ."
        " ?item p:P856 ?st ."
        " FILTER NOT EXISTS { ?st wikibase:rank wikibase:DeprecatedRank }"
        ' SERVICE wikibase:label { bd:serviceParam wikibase:language "en,fr,mul". }'
        " }"
    )


def parse_match_results(data: dict, domains: Sequence[str]) -> dict[str, list[Candidate]]:
    out: dict[str, dict[str, Candidate]] = {d: {} for d in domains}
    for b in data.get("results", {}).get("bindings", []):
        item = b.get("item", {}).get("value", "")
        qid = item.rsplit("/", 1)[-1]
        site = b.get("site", {}).get("value", "")
        if not QID_RE.match(qid):
            continue
        host = host_of(site).removeprefix("www.")
        if host not in out:
            continue
        label = b.get("itemLabel", {}).get("value", qid)
        out[host].setdefault(
            qid,
            Candidate(
                qid=qid,
                label=label,
                description=b.get("itemDescription", {}).get("value", ""),
                website=site,
            ),
        )
    return {d: sorted(c.values(), key=lambda c: int(c.qid[1:])) for d, c in out.items()}


def _label(entity: dict) -> str:
    labels = entity.get("labels") or {}
    for lang in (*LANGUAGES, "mul"):
        if lang in labels:
            return str(labels[lang]["value"])
    for value in labels.values():
        return str(value["value"])
    return str(entity.get("id", ""))


def _description(entity: dict) -> str:
    descs = entity.get("descriptions") or {}
    for lang in LANGUAGES:
        if lang in descs:
            return str(descs[lang]["value"])
    return ""


def _parse_time(snak: dict) -> tuple[str, datetime] | None:
    """Return (display string at stated precision, comparable datetime)."""
    try:
        value = snak["datavalue"]["value"]
        raw, precision = value["time"], int(value["precision"])
    except (KeyError, TypeError, ValueError):
        return None
    m = re.match(r"^([+-])(\d{1,16})-(\d{2})-(\d{2})T", raw)
    if not m or m.group(1) == "-":
        return None
    year, month, day = int(m.group(2)), int(m.group(3)), int(m.group(4))
    if year > 9999:
        return None
    when = datetime(year, max(month, 1), max(day, 1), tzinfo=UTC)
    if precision >= 11:
        text = f"{year:04d}-{month:02d}-{day:02d}"
    elif precision == 10:
        text = f"{year:04d}-{month:02d}"
    else:
        text = f"{year:04d}"
    return text, when


def _best_statements(claims: dict, prop: str, now: datetime) -> list[dict]:
    statements = [
        s
        for s in claims.get(prop, [])
        if s.get("rank") != "deprecated" and s.get("mainsnak", {}).get("snaktype") == "value"
    ]
    preferred = [s for s in statements if s.get("rank") == "preferred"]
    chosen = preferred or statements
    current = []
    for s in chosen:
        ended = False
        for q in s.get("qualifiers", {}).get(P_END, []):
            if q.get("snaktype") == "somevalue":
                ended = True  # ended at an unknown date
            parsed = _parse_time(q) if q.get("snaktype") == "value" else None
            if parsed and parsed[1] <= now:
                ended = True
        if not ended:
            current.append(s)
    return current


def _item_values(statements: Iterable[dict]) -> list[str]:
    out = []
    for s in statements:
        value = s["mainsnak"].get("datavalue", {}).get("value")
        qid = value.get("id") if isinstance(value, dict) else None
        if isinstance(qid, str) and QID_RE.match(qid) and qid not in out:
            out.append(qid)
    return out


def _string_values(statements: Iterable[dict]) -> list[str]:
    out = []
    for s in statements:
        value = s["mainsnak"].get("datavalue", {}).get("value")
        if isinstance(value, str) and value.strip() and value.strip() not in out:
            out.append(value.strip())
    return out


def _share(statement: dict) -> float | None:
    for q in statement.get("qualifiers", {}).get(P_SHARE, []):
        try:
            amount = float(q["datavalue"]["value"]["amount"])
        except (KeyError, TypeError, ValueError):
            continue
        if 0 < amount <= 1:
            return amount
        if 1 < amount <= 100:  # some statements record percent instead of a fraction
            return amount / 100
    return None


def parse_entity(entity: dict, now: datetime) -> EntityData | None:
    qid = entity.get("id")
    if not isinstance(qid, str) or not QID_RE.match(qid) or "missing" in entity:
        return None
    claims = entity.get("claims") or {}
    data = EntityData(qid=qid, label=_label(entity), description=_description(entity))
    data.instance_of = _item_values(_best_statements(claims, P_INSTANCE_OF, now))
    data.country = _item_values(_best_statements(claims, P_COUNTRY, now))
    websites = _string_values(_best_statements(claims, P_WEBSITE, now))
    data.websites = [w for w in websites if is_http_url(w)]
    data.website = data.websites[0] if data.websites else None
    logos = _string_values(_best_statements(claims, P_LOGO, now))
    data.logo_file = logos[0] if logos else None
    if Q_HUMAN in data.instance_of:
        for st in _best_statements(claims, P_DIED, now):
            parsed = _parse_time(st["mainsnak"])
            if parsed and parsed[1] <= now:
                data.died = parsed[0]
                break
    if Q_HUMAN not in data.instance_of:  # a person has no owners (see module docstring)
        for prop, relation in RELATIONS.items():
            for s in _best_statements(claims, prop, now):
                parent = _item_values([s])
                if not parent or parent[0] == qid:
                    continue
                start = None
                for q in s.get("qualifiers", {}).get(P_START, []):
                    parsed = _parse_time(q) if q.get("snaktype") == "value" else None
                    if parsed:
                        start = parsed[0]
                data.parents.append(ParentClaim(parent[0], relation, prop, _share(s), start))
    for prop, scheme in IDENTIFIER_PROPERTIES.items():
        values = _string_values(_best_statements(claims, prop, now))
        if values:
            data.identifiers[scheme] = values
    return data


# --------------------------------------------------------------------------- client


def _check_api(response: httpx.Response) -> None:
    try:
        body = response.json()
    except ValueError as exc:
        raise ApiError("Wikidata returned non-JSON") from exc
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        if error.get("code") == "maxlag":
            raise RateLimited("maxlag")
        raise ApiError(f"Wikidata API error: {error.get('code')}")


class WikidataSource:
    name = "wikidata"

    def __init__(self, client: ApiClient) -> None:
        self.client = client

    def match_domains(self, domains: Sequence[str]) -> dict[str, list[Candidate]]:
        """Find items whose official website (P856) is the domain's home page."""
        result: dict[str, list[Candidate]] = {}
        for i in range(0, len(domains), MATCH_BATCH):
            batch = list(domains[i : i + MATCH_BATCH])
            response = self.client.get(
                SPARQL_ENDPOINT,
                params={"query": build_match_query(batch), "format": "json"},
            )
            try:
                data = response.json()
            except ValueError as exc:
                raise ApiError("SPARQL endpoint returned non-JSON") from exc
            result.update(parse_match_results(data, batch))
        return result

    def news_media(self, qids: Iterable[str]) -> set[str]:
        """Those of `qids` that Wikidata classifies as news media or mass media (instance
        of a subclass of either)."""
        wanted = sorted({q for q in qids if QID_RE.match(q)}, key=lambda q: int(q[1:]))
        if not wanted:
            return set()
        query = (
            "SELECT DISTINCT ?item WHERE {"
            f" VALUES ?item {{ {' '.join('wd:' + q for q in wanted)} }}"
            f" VALUES ?type {{ {' '.join('wd:' + t for t in NEWS_TYPES)} }}"
            " ?item wdt:P31/wdt:P279* ?type ."
            " }"
        )
        response = self.client.get(SPARQL_ENDPOINT, params={"query": query, "format": "json"})
        try:
            bindings = response.json().get("results", {}).get("bindings", [])
        except (ValueError, AttributeError) as exc:
            raise ApiError("SPARQL endpoint returned non-JSON") from exc
        found = {b.get("item", {}).get("value", "").rsplit("/", 1)[-1] for b in bindings}
        return found & set(wanted)

    def search(self, name: str) -> list[str]:
        """Items whose label or alias matches `name` (English, then French), best first."""
        out: list[str] = []
        for lang in LANGUAGES:
            response = self.client.get(
                API_ENDPOINT,
                params={
                    "action": "wbsearchentities",
                    "search": name,
                    "language": lang,
                    "type": "item",
                    "limit": str(SEARCH_LIMIT),
                    "format": "json",
                    "maxlag": "5",
                },
                check=_check_api,
            )
            for hit in response.json().get("search", []):
                qid = hit.get("id") if isinstance(hit, dict) else None
                if isinstance(qid, str) and QID_RE.match(qid) and qid not in out:
                    out.append(qid)
        return out

    def get_entities(
        self, qids: Iterable[str], now: datetime | None = None
    ) -> dict[str, EntityData]:
        """Fetch and parse entities. Redirected IDs are returned under the requested ID."""
        now = now or datetime.now(UTC)
        wanted = sorted({q for q in qids if QID_RE.match(q)}, key=lambda q: int(q[1:]))
        out: dict[str, EntityData] = {}
        for i in range(0, len(wanted), BATCH):
            batch = wanted[i : i + BATCH]
            body = self._wbgetentities(batch, "labels|descriptions|claims")
            for requested, entity in self._by_request(batch, body).items():
                parsed = parse_entity(entity, now)
                if parsed:
                    out[requested] = parsed
        return out

    def get_labels(self, qids: Iterable[str]) -> dict[str, str]:
        wanted = sorted({q for q in qids if QID_RE.match(q)}, key=lambda q: int(q[1:]))
        out: dict[str, str] = {}
        for i in range(0, len(wanted), BATCH):
            batch = wanted[i : i + BATCH]
            body = self._wbgetentities(batch, "labels")
            for requested, entity in self._by_request(batch, body).items():
                if "missing" not in entity:
                    out[requested] = _label(entity)
        return out

    def _wbgetentities(self, ids: Sequence[str], props: str) -> dict:
        response = self.client.get(
            API_ENDPOINT,
            params={
                "action": "wbgetentities",
                "ids": "|".join(ids),
                "props": props,
                "languages": "|".join(LANGUAGES),
                "languagefallback": "1",
                "format": "json",
                "maxlag": "5",
            },
            check=_check_api,
        )
        body = response.json()
        return body if isinstance(body, dict) else {}

    @staticmethod
    def _by_request(batch: Sequence[str], body: dict) -> dict[str, dict]:
        entities = body.get("entities") or {}
        out = {}
        for requested in batch:
            entity = entities.get(requested)
            if entity is None:  # redirect: find the entity whose redirect source matches
                entity = next(
                    (
                        e
                        for e in entities.values()
                        if isinstance(e, dict) and e.get("redirects", {}).get("from") == requested
                    ),
                    None,
                )
            if isinstance(entity, dict):
                out[requested] = entity
        return out


def commons_thumb_url(filename: str, width: int = 64) -> str:
    return f"{COMMONS_FILE_PATH}{quote(filename.replace(' ', '_'))}?width={width}"


def commons_file_page(filename: str) -> str:
    return f"{COMMONS_FILE_PAGE}{quote(filename.replace(' ', '_'))}"
