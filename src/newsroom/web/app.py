"""FastAPI application: server-rendered, read-only."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from newsroom import __version__
from newsroom.log import setup_logging
from newsroom.settings import Settings, get_settings
from newsroom.web.security import ReadOnlyMethodsMiddleware, SecurityHeadersMiddleware

HERE = Path(__file__).resolve().parent
log = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    setup_logging(settings.log_level)

    app = FastAPI(
        title="newsroom",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    templates = Jinja2Templates(directory=HERE / "templates")
    limiter = Limiter(key_func=get_remote_address, default_limits=[settings.rate_limit])
    app.state.limiter = limiter
    app.state.settings = settings
    app.state.templates = templates

    # Starlette runs the last-added middleware first.
    app.add_middleware(SlowAPIMiddleware)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)
    app.add_middleware(ReadOnlyMethodsMiddleware)
    app.add_middleware(SecurityHeadersMiddleware, hsts=settings.enable_hsts)

    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    @app.get("/healthz", include_in_schema=False)
    @limiter.exempt
    async def healthz() -> PlainTextResponse:
        return PlainTextResponse("ok")

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> Response:
        return templates.TemplateResponse(request, "index.html", {})

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
