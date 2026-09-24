"""FastAPI application: server-rendered, read-only."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from limits import parse as parse_limit
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from newsroom import __version__
from newsroom.db import connect_readonly
from newsroom.log import setup_logging
from newsroom.settings import Settings, get_settings
from newsroom.web import queries
from newsroom.web.security import ReadOnlyMethodsMiddleware, SecurityHeadersMiddleware

HERE = Path(__file__).resolve().parent
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
    templates.env.filters["qs"] = lambda params: urlencode(params)
    templates.env.globals["day_label"] = lambda d: _day_label(d, datetime.now(tz).date())
    limiter = Limiter(key_func=get_remote_address, default_limits=[settings.rate_limit])
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
        canonical = filters.params(**({"before": filters.cursor} if filters.before else {}))
        if dict(request.query_params) != canonical:
            # Drop empty or invalid params (e.g. from the filter form) for clean, shareable URLs.
            return RedirectResponse(f"/?{urlencode(canonical)}" if canonical else "/", 303)
        if filters.q and not limiter.limiter.hit(
            search_limit, "search", get_remote_address(request)
        ):
            return _error(request, 429, "Too many searches. Please wait a minute and try again.")
        context: dict[str, object] = {"filters": filters, "page": None, "options": None}
        with database() as conn:
            if conn is not None:
                try:
                    context["page"] = queries.feed(conn, filters, tz)
                    context["options"] = queries.filter_options(conn)
                    last = queries.last_ingest(conn)
                    context["last_ingest"] = last.astimezone(tz) if last else None
                except sqlite3.OperationalError:
                    # Schema not migrated yet (worker still starting).
                    context["page"] = None
        return templates.TemplateResponse(request, "index.html", context)

    @app.exception_handler(RateLimitExceeded)
    async def rate_limited(request: Request, exc: RateLimitExceeded) -> Response:
        return _error(request, 429, "Too many requests. Please wait a minute and try again.")

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> Response:
        message = "Page not found." if exc.status_code == 404 else "Request could not be served."
        return _error(request, exc.status_code, message)

    @app.exception_handler(Exception)
    async def server_error(request: Request, exc: Exception) -> Response:
        log.exception("unhandled error", extra={"path": request.url.path})
        return _error(request, 500, "Something went wrong on our side.")

    def _error(request: Request, status: int, message: str) -> Response:
        return templates.TemplateResponse(
            request, "error.html", {"status": status, "message": message}, status_code=status
        )

    return app


def _day_label(day: date, today: date) -> str:
    if day == today:
        return "Today"
    if day == today - timedelta(days=1):
        return "Yesterday"
    return f"{day:%A}, {day.day} {day:%B %Y}"
