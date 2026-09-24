"""The single entry point for fetching arbitrary remote files (logos, images).

Guards, in order, on every hop including redirects:
  1. scheme is http/https, port is 80/443, no userinfo in the URL
  2. host is on the caller's allowlist (exact host or a subdomain of it)
  3. every resolved IP is globally routable (blocks private, loopback,
     link-local, CGNAT/Tailscale 100.64/10, multicast, reserved, ...)
  4. the connection goes to the IP that was checked, not a fresh DNS lookup,
     so DNS rebinding cannot swap in an internal address
  5. timeout, response size cap (streamed), and content-type allowlist

API clients (GDELT, Wikidata, ...) talk to fixed, known endpoints and use
newsroom.net.http instead; this module is for URLs that come from external data.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx

Resolver = Callable[[str, int], list[str]]

DEFAULT_MAX_BYTES = 2 * 1024 * 1024
DEFAULT_TIMEOUT = 10.0
MAX_REDIRECTS = 3
# SVG is deliberately excluded: it can carry script. Wikimedia serves PNG renders of SVGs.
IMAGE_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})


class FetchBlocked(Exception):
    """The request was refused by a guard. The message says which one.
    `status` is the HTTP status when the server answered with a non-200."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class FetchResult:
    url: str
    content_type: str
    body: bytes


def system_resolver(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return sorted({str(info[4][0]) for info in infos})


def host_allowed(host: str, allowlist: Iterable[str]) -> bool:
    host = host.lower().rstrip(".")
    for entry in allowlist:
        entry = entry.lower().rstrip(".")
        if host == entry or host.endswith("." + entry):
            return True
    return False


def ip_is_public(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def _check_url(url: str, allowlist: Iterable[str]) -> tuple[str, str, int]:
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"}:
        raise FetchBlocked(f"scheme not allowed: {scheme or '(none)'}")
    if parts.username or parts.password:
        raise FetchBlocked("credentials in URL not allowed")
    host = parts.hostname
    if not host:
        raise FetchBlocked("URL has no host")
    try:
        port = parts.port or (443 if scheme == "https" else 80)
    except ValueError as exc:
        raise FetchBlocked("invalid port") from exc
    if port not in {80, 443}:
        raise FetchBlocked(f"port not allowed: {port}")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise FetchBlocked("IP-literal hosts not allowed")
    if not host_allowed(host, allowlist):
        raise FetchBlocked(f"host not on allowlist: {host}")
    return scheme, host, port


def _pinned_request(
    client: httpx.Client, url: str, host: str, ip: str, max_bytes: int, truncate: bool
) -> tuple[httpx.Response, bytes]:
    parts = urlsplit(url)
    ip_host = f"[{ip}]" if ":" in ip else ip
    netloc = ip_host if parts.port is None else f"{ip_host}:{parts.port}"
    pinned = parts._replace(netloc=netloc).geturl()
    request = client.build_request(
        "GET",
        pinned,
        headers={"Host": parts.netloc.rsplit("@", 1)[-1]},
        extensions={"sni_hostname": host},
    )
    response = client.send(request, stream=True)
    try:
        declared = response.headers.get("content-length")
        if not truncate and declared and declared.isdigit() and int(declared) > max_bytes:
            raise FetchBlocked(f"response too large: {declared} bytes")
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_bytes():
            if total + len(chunk) > max_bytes:
                if truncate:  # keep the first max_bytes and stop downloading
                    chunks.append(chunk[: max_bytes - total])
                    break
                raise FetchBlocked(f"response exceeds {max_bytes} bytes")
            total += len(chunk)
            chunks.append(chunk)
        return response, b"".join(chunks)
    finally:
        response.close()


def safe_fetch(
    url: str,
    *,
    allowlist: Iterable[str],
    allowed_types: Iterable[str] = IMAGE_TYPES,
    max_bytes: int = DEFAULT_MAX_BYTES,
    timeout: float = DEFAULT_TIMEOUT,
    user_agent: str = "newsroom/0.1",
    resolver: Resolver = system_resolver,
    transport: httpx.BaseTransport | None = None,
    truncate: bool = False,
) -> FetchResult:
    """Fetch under all the guards above. With `truncate`, a body longer than `max_bytes`
    is cut off there instead of refused (for reading just the start of an HTML page)."""
    allowlist = tuple(allowlist)
    allowed = frozenset(t.lower() for t in allowed_types)
    with httpx.Client(
        timeout=timeout,
        follow_redirects=False,
        trust_env=False,  # never route through env-configured proxies
        transport=transport,
        headers={"User-Agent": user_agent, "Accept": ", ".join(sorted(allowed))},
    ) as client:
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            _, host, port = _check_url(current, allowlist)
            try:
                addresses = resolver(host, port)
            except OSError as exc:
                raise FetchBlocked(f"DNS lookup failed for {host}") from exc
            if not addresses:
                raise FetchBlocked(f"no addresses for {host}")
            for address in addresses:
                if not ip_is_public(address):
                    raise FetchBlocked(f"{host} resolves to non-public address {address}")
            try:
                response, body = _pinned_request(
                    client, current, host, addresses[0], max_bytes, truncate
                )
            except httpx.HTTPError as exc:
                raise FetchBlocked(f"request failed: {type(exc).__name__}") from exc
            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    raise FetchBlocked("redirect without location")
                current = urljoin(current, location)
                continue
            if response.status_code != 200:
                raise FetchBlocked(
                    f"unexpected status {response.status_code}", status=response.status_code
                )
            content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
            if content_type not in allowed:
                raise FetchBlocked(f"content-type not allowed: {content_type or '(none)'}")
            return FetchResult(url=current, content_type=content_type, body=body)
    raise FetchBlocked("too many redirects")
