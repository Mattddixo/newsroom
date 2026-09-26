"""A mock Wikidata HTTP backend serving the recorded-format fixtures."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx

from newsroom.net.http import ApiClient
from newsroom.sources.wikidata import WikidataSource

FIXTURES = Path(__file__).parent / "fixtures" / "wikidata"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeWikidata:
    def __init__(self) -> None:
        data = load("entities.json")
        self.redirected = data.pop("redirected_Q1006")
        self.entities: dict = data["entities"]
        self.labels = load("labels.json")["entities"]
        self.sparql = load("sparql_match.json")
        self.news: set[str] = set()  # items Wikidata classifies as news media
        self.search_results: dict[str, list[str]] = {}  # name -> QIDs
        self.requests: list[dict[str, list[str]]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        params = parse_qs(urlsplit(str(request.url)).query)
        self.requests.append(params)
        if request.url.host == "query.wikidata.org":
            query = params["query"][0]
            if "wdt:P279*" in query:  # news-media classification
                hits = [q for q in self.news if f"wd:{q} " in query + " "]
                rows = [{"item": {"value": f"http://www.wikidata.org/entity/{q}"}} for q in hits]
                return httpx.Response(200, json={"results": {"bindings": rows}})
            return httpx.Response(200, json=self.sparql)
        if params.get("action") == ["wbsearchentities"]:
            hits = self.search_results.get(params["search"][0], [])
            return httpx.Response(200, json={"search": [{"id": q} for q in hits]})
        ids = params["ids"][0].split("|")
        out = {}
        for q in ids:
            if q == "Q1006":
                out["Q1003"] = self.redirected
            elif q in self.entities:
                out[q] = self.entities[q]
            elif q in self.labels:
                out[q] = self.labels[q]
            else:
                out[q] = {"id": q, "missing": ""}
        return httpx.Response(200, json={"entities": out, "success": 1})

    def source(self) -> WikidataSource:
        client = ApiClient(
            "test-agent (test@example.org)",
            transport=httpx.MockTransport(self.handler),
            sleep=lambda s: None,
        )
        return WikidataSource(client)
