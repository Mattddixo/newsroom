"""Security headers and method restriction, as plain ASGI middleware."""

from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send

CSP = "; ".join(
    [
        "default-src 'none'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self'",
        "font-src 'self'",
        "connect-src 'self'",
        "form-action 'self'",
        "base-uri 'none'",
        "frame-ancestors 'none'",
    ]
)

PERMISSIONS_POLICY = ", ".join(
    f"{feature}=()"
    for feature in (
        "accelerometer",
        "camera",
        "geolocation",
        "gyroscope",
        "magnetometer",
        "microphone",
        "payment",
        "usb",
        "interest-cohort",
        "browsing-topics",
    )
)

READ_METHODS = {"GET", "HEAD"}


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp, *, hsts: bool = False) -> None:
        self.app = app
        self.headers: list[tuple[bytes, bytes]] = [
            (b"content-security-policy", CSP.encode()),
            (b"x-content-type-options", b"nosniff"),
            (b"referrer-policy", b"no-referrer"),
            (b"x-frame-options", b"DENY"),
            (b"permissions-policy", PERMISSIONS_POLICY.encode()),
            (b"cross-origin-opener-policy", b"same-origin"),
            (b"cross-origin-resource-policy", b"same-origin"),
        ]
        if hsts:
            self.headers.append((b"strict-transport-security", b"max-age=31536000"))

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                existing = {name.lower() for name, _ in message.get("headers", [])}
                extra = [(k, v) for k, v in self.headers if k not in existing]
                message["headers"] = [*message.get("headers", []), *extra]
            await send(message)

        await self.app(scope, receive, send_with_headers)


class ReadOnlyMethodsMiddleware:
    """The site is read-only: anything other than GET/HEAD gets 405 before routing.

    HEAD is served by the GET route with the body dropped, so uptime checkers work.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] == "HEAD":

            async def send_headers_only(message: Message) -> None:
                if message["type"] == "http.response.body":
                    message = {**message, "body": b""}
                await send(message)

            await self.app({**scope, "method": "GET"}, receive, send_headers_only)
            return
        if scope["type"] == "http" and scope["method"] not in READ_METHODS:
            await send(
                {
                    "type": "http.response.start",
                    "status": 405,
                    "headers": [(b"allow", b"GET, HEAD"), (b"content-type", b"text/plain")],
                }
            )
            await send({"type": "http.response.body", "body": b"Method Not Allowed"})
            return
        await self.app(scope, receive, send)
