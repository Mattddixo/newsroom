"""FastAPI application: server-rendered, read-only."""

from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import StrictUndefined
from limits import parse as parse_limit
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from newsroom import __version__, funding_view
from newsroom.db import connect_readonly
from newsroom.log import setup_logging
from newsroom.ownership_view import Graph
from newsroom.settings import Settings, get_settings
from newsroom.sources.wikidata import QID_RE
from newsroom.web import queries
from newsroom.web.security import (
    ReadOnlyMethodsMiddleware,
    SecurityHeadersMiddleware,
    client_ip_resolver,
)

HERE = Path(__file__).resolve().parent
LOGO_NAME = re.compile(r"^Q[1-9]\d{0,11}\.(png|jpg|gif|webp)$")
LOGO_TYPES = {"png": "image/png", "jpg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}
log = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    setup_logging(settings.log_level)
    tz = ZoneInfo(settings.timezone)

    app = FastAPI(
        title="newsroom",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    templates = Jinja2Templates(directory=HERE / "templates")
    # A missing variable must fail loudly, never render as an empty value: an absent
    # funding list silently becoming "Not publicly disclosed" would be a false statement.
    templates.env.undefined = StrictUndefined
    templates.env.filters["qs"] = lambda params: urlencode(params)
    templates.env.globals["day_label"] = lambda d: _day_label(d, datetime.now(tz).date())
    templates.env.filters["pct"] = lambda v: f"{v * 100:.4g}%"
    templates.env.filters["article_date"] = lambda d: _article_date(d, datetime.now(tz))
    templates.env.filters["money"] = _money
    templates.env.globals["page_url"] = lambda f, n: _feed_url(f.params(page=n))
    templates.env.globals["feed_url"] = _feed_url

    # New articles wait (at most PUBDATE_HOLD_MINUTES) until their publication date has
    # been checked, normally within a minute of arriving. No date checking, no wait.
    dates_checked = settings.pubdate_fetch and bool(settings.contact_email)
    visibility = queries.Visibility(
        timedelta(minutes=settings.pubdate_hold_minutes if dates_checked else 0)
    )
    templates.env.globals["mixes"] = queries.MIXES
    templates.env.filters["source_name"] = _source_name
    client_ip = client_ip_resolver(settings.trusted_proxies)
    limiter = Limiter(key_func=client_ip, default_limits=[settings.rate_limit])
    search_limit = parse_limit(settings.search_rate_limit)
    app.state.limiter = limiter
    app.state.settings = settings
    app.state.templates = templates

    # Starlette runs the last-added middleware first.
    app.add_middleware(SlowAPIMiddleware)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)
    app.add_middleware(ReadOnlyMethodsMiddleware)
    app.add_middleware(SecurityHeadersMiddleware, hsts=settings.enable_hsts)

    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    @contextmanager
    def database() -> Iterator[sqlite3.Connection | None]:
        """Read-only connection, or None before the worker has created the database."""
        if not settings.db_path.exists():
            yield None
            return
        conn = connect_readonly(settings.db_path)
        try:
            yield conn
        finally:
            conn.close()

    @app.get("/healthz", include_in_schema=False)
    @limiter.exempt
    async def healthz() -> PlainTextResponse:
        return PlainTextResponse("ok")

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> Response:
        filters = queries.FeedFilters.parse(dict(request.query_params))
        # htmx requests (a filter or sort changed) are answered directly and told which
        # clean URL to put in the address bar; plain requests are redirected to it.
        is_htmx = request.headers.get("hx-request") == "true"
        if dict(request.query_params) != filters.params() and not is_htmx:
            return RedirectResponse(_feed_url(filters.params()), 303)
        if filters.q and not limiter.limiter.hit(search_limit, "search", client_ip(request)):
            return _error(request, 429, "Too many searches. Please wait a minute and try again.")
        context: dict[str, object] = {
            "filters": filters,
            "page": None,
            "options": None,
            "graph": None,
            "owner_options": [],
            "last_ingest": None,
            "stale": False,
            "since": "",
            "interval": settings.ingest_interval_minutes,
            "cap": settings.feed_outlet_cap,
        }
        with database() as conn:
            if conn is not None:
                try:
                    drawn = datetime.now(UTC).replace(microsecond=0)
                    context["page"] = queries.feed(
                        conn, filters, tz, settings.feed_outlet_cap, visibility, drawn
                    )
                    context["since"] = drawn.strftime(SINCE_FORMAT)
                    context["options"] = queries.filter_options(conn)
                    graph = Graph.load(conn)
                    context["graph"] = graph
                    context["owner_options"] = queries.owner_options(graph, queries.outlets(conn))
                    last = queries.last_ingest(conn)
                    context["last_ingest"] = last.astimezone(tz) if last else None
                    stale_after = timedelta(minutes=3 * settings.ingest_interval_minutes)
                    context["stale"] = bool(last and datetime.now(tz) - last > stale_after)
                except sqlite3.OperationalError:
                    # Schema not migrated yet (worker still starting).
                    context["page"] = None
        page = context["page"]
        if isinstance(page, queries.FeedPage) and page.page != filters.page and not is_htmx:
            # Past the last page (e.g. filters narrowed): go to the last real page.
            return RedirectResponse(_feed_url(filters.params(page=page.page)), 303)
        response = templates.TemplateResponse(request, "index.html", context)
        if is_htmx:
            current = page.page if isinstance(page, queries.FeedPage) else 1
            response.headers["HX-Push-Url"] = _feed_url(filters.params(page=current))
        return response

    @app.get("/robots.txt", include_in_schema=False)
    def robots() -> PlainTextResponse:
        return PlainTextResponse("User-agent: *\nDisallow: /fragments/\n")

    @app.get("/about", response_class=HTMLResponse)
    def about(request: Request) -> Response:
        return templates.TemplateResponse(
            request,
            "about.html",
            {"interval": settings.ingest_interval_minutes, "cap": settings.feed_outlet_cap},
        )

    @app.get("/fragments/new", response_class=HTMLResponse)
    def new_articles(request: Request) -> Response:
        """Polled by the feed: how many articles arrived since the page was loaded."""
        params = dict(request.query_params)
        since = params.pop("since", "")
        filters = queries.FeedFilters.parse(params)
        count = 0
        drawn = _parse_since(since)
        if drawn and not filters.q:
            with database() as conn:
                if conn is not None:
                    count = queries.count_new(
                        conn, filters, tz, drawn, settings.feed_outlet_cap, visibility
                    )
        return templates.TemplateResponse(
            request, "_new_articles.html", {"count": count, "filters": filters}
        )

    @app.get("/outlets", response_class=HTMLResponse)
    def outlets_page(request: Request) -> Response:
        with database() as conn:
            sort = _table_sort(request, OUTLET_SORTS, "name")
            if conn is None:
                return templates.TemplateResponse(
                    request, "outlets.html", {"rows": [], "sort": sort}
                )
            graph = Graph.load(conn)
            counts = queries.article_counts(conn, datetime.now(tz))
            rows = [
                (o, graph.summary(o["entity_id"]), counts.get(o["id"], (0, 0)))
                for o in queries.outlets(conn)
            ]
        rows.sort(key=OUTLET_SORTS[sort])
        return templates.TemplateResponse(request, "outlets.html", {"rows": rows, "sort": sort})

    @app.get("/outlet/{domain}", response_class=HTMLResponse)
    def outlet_page(request: Request, domain: str) -> Response:
        with database() as conn:
            outlet = queries.outlet(conn, domain) if conn else None
            if conn is None or outlet is None:
                raise StarletteHTTPException(404)
            graph = Graph.load(conn)
            node = graph.nodes.get(outlet["entity_id"]) if outlet["entity_id"] else None
            context = {
                "outlet": outlet,
                "node": node,
                "chain": graph.chain(node.id) if node else [],
                "graph": graph,
                "funding_rows": funding_view.records(conn, graph.lineage(outlet["entity_id"])),
                "articles": [
                    (
                        r,
                        queries.parse_ts(r["shown_at"]).astimezone(tz),
                        "published" if r["outlet_published_at"] else "seen",
                    )
                    for r in queries.recent_articles(conn, outlet["id"])
                ],
            }
        return templates.TemplateResponse(request, "outlet.html", context)

    @app.get("/fragments/ownership/{domain}", response_class=HTMLResponse)
    def ownership_fragment(request: Request, domain: str) -> Response:
        with database() as conn:
            outlet = queries.outlet(conn, domain) if conn else None
            if conn is None or outlet is None:
                raise StarletteHTTPException(404)
            graph = Graph.load(conn)
            node = graph.nodes.get(outlet["entity_id"]) if outlet["entity_id"] else None
            context = {
                "outlet": outlet,
                "node": node,
                "chain": graph.chain(node.id) if node else [],
                "graph": graph,
                "funding_rows": funding_view.records(conn, graph.lineage(outlet["entity_id"])),
            }
        return templates.TemplateResponse(request, "_ownership_panel.html", context)

    @app.get("/owners", response_class=HTMLResponse)
    def owners_page(request: Request) -> Response:
        with database() as conn:
            rows: list[queries.OwnerRow] = []
            if conn is not None:
                graph = Graph.load(conn)
                counts = queries.article_counts(conn, datetime.now(tz))
                rows = queries.owners(graph, queries.outlets(conn), counts)
        sort = _table_sort(request, OWNER_SORTS, "outlets")
        rows.sort(key=OWNER_SORTS[sort])
        return templates.TemplateResponse(request, "owners.html", {"rows": rows, "sort": sort})

    @app.get("/owner/{qid}", response_class=HTMLResponse)
    def owner_page(request: Request, qid: str) -> Response:
        if not QID_RE.match(qid):
            raise StarletteHTTPException(404)
        with database() as conn:
            if conn is None:
                raise StarletteHTTPException(404)
            graph = Graph.load(conn)
            node = graph.by_qid(qid)
            if node is None:
                raise StarletteHTTPException(404)
            counts = queries.article_counts(conn, datetime.now(tz))
            owned = queries.owned_outlets(graph, node.id, queries.outlets(conn), counts)
            context = {
                "node": node,
                "chain": graph.chain(node.id),
                "owned": owned,
                "graph": graph,
                "funding_rows": funding_view.records(conn, graph.lineage(node.id)),
            }
        return templates.TemplateResponse(request, "owner.html", context)

    @app.get("/logos/{name}")
    def logo(name: str) -> Response:
        m = LOGO_NAME.match(name)
        path = settings.logo_dir / name
        if not m or not path.is_file():
            raise StarletteHTTPException(404)
        return FileResponse(
            path,
            media_type=LOGO_TYPES[m.group(1)],
            headers={"Cache-Control": "public, max-age=86400"},
        )

    @app.exception_handler(RateLimitExceeded)
    async def rate_limited(request: Request, exc: RateLimitExceeded) -> Response:
        return _error(request, 429, "Too many requests. Please wait a minute and try again.")

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> Response:
        message = "Page not found." if exc.status_code == 404 else "That request didn't work."
        return _error(request, exc.status_code, message)

    @app.exception_handler(Exception)
    async def server_error(request: Request, exc: Exception) -> Response:
        log.exception("unhandled error", extra={"path": request.url.path})
        return _error(request, 500, "Something went wrong. Please try again later.")

    def _error(request: Request, status: int, message: str) -> Response:
        return templates.TemplateResponse(
            request, "error.html", {"status": status, "message": message}, status_code=status
        )

    return app


# Column sorts for the Outlets and Owners tables: text ascending, numbers descending.
OUTLET_SORTS = {
    "name": lambda r: r[0]["display_name"].casefold(),
    "country": lambda r: (r[0]["country"], r[0]["display_name"].casefold()),
    "articles": lambda r: (-(r[2][1] or 0), r[0]["display_name"].casefold()),
}
OWNER_SORTS = {
    "outlets": lambda r: (-r.outlets, -r.recent, r.name.casefold()),
    "name": lambda r: r.name.casefold(),
    "articles": lambda r: (-r.recent, -r.outlets, r.name.casefold()),
}


def _table_sort(request: Request, allowed: dict, default: str) -> str:
    value = request.query_params.get("sort", default)
    return value if value in allowed else default


def _article_date(when: datetime, now: datetime) -> str:
    """'Sep 23, 8:38 p.m. EDT': 12-hour clock and the time zone, the way outlets print
    times, so a card can be compared with the article page. The year is added for
    articles from another year."""
    year = f" {when.year}" if when.year != now.year else ""
    hour = when.hour % 12 or 12
    half = "a.m." if when.hour < 12 else "p.m."
    return f"{when:%b} {when.day}{year}, {hour}:{when:%M} {half} {when:%Z}".rstrip()


SINCE_FORMAT = "%Y%m%d%H%M%S"  # when the feed page was drawn (UTC), for the new-articles poll


def _parse_since(value: str) -> datetime | None:
    if not (value.isdigit() and len(value) == 14):
        return None
    try:
        return datetime.strptime(value, SINCE_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return None


def _feed_url(params: dict[str, str]) -> str:
    return f"/?{urlencode(params)}" if params else "/"


def _day_label(day: date, today: date) -> str:
    if day == today:
        return "Today"
    if day == today - timedelta(days=1):
        return "Yesterday"
    return f"{day:%A}, {day.day} {day:%B %Y}"


SOURCE_NAMES = {
    "sec_edgar": "SEC EDGAR",
    "propublica": "ProPublica Nonprofit Explorer",
    "cra": "Canada Revenue Agency",
}


def _money(amount: float, currency: str | None) -> str:
    symbol = {"USD": "US$", "CAD": "C$"}.get(currency or "", "")
    text = f"{amount:,.0f}"
    return f"{symbol}{text}" if symbol else f"{text} {currency or ''}".strip()


def _source_name(row: sqlite3.Row) -> str:
    if row["source"] in SOURCE_NAMES:
        return SOURCE_NAMES[row["source"]]
    host = urlsplit(row["source_url"]).hostname or row["source_url"]
    return host.removeprefix("www.")
