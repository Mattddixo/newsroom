from __future__ import annotations

import httpx
import pytest

from newsroom.net.safe_fetch import FetchBlocked, ip_is_public, safe_fetch

ALLOW = ["upload.wikimedia.org"]
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


def public_resolver(host: str, port: int) -> list[str]:
    return ["198.35.26.112"]


def make_transport(handler) -> httpx.MockTransport:  # type: ignore[no-untyped-def]
    return httpx.MockTransport(handler)


def ok_png(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, headers={"content-type": "image/png"}, content=PNG)


def fetch(url: str, **kw):  # type: ignore[no-untyped-def]
    kw.setdefault("allowlist", ALLOW)
    kw.setdefault("resolver", public_resolver)
    kw.setdefault("transport", make_transport(ok_png))
    return safe_fetch(url, **kw)


def test_happy_path_pins_ip_and_keeps_host() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return ok_png(request)

    result = fetch("https://upload.wikimedia.org/a.png", transport=make_transport(handler))
    assert result.body == PNG
    assert result.content_type == "image/png"
    assert seen[0].url.host == "198.35.26.112"
    assert seen[0].headers["host"] == "upload.wikimedia.org"
    assert seen[0].extensions["sni_hostname"] == "upload.wikimedia.org"


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "gopher://upload.wikimedia.org/",
        "ftp://upload.wikimedia.org/a.png",
        "javascript:alert(1)",
        "https://user:pw@upload.wikimedia.org/a.png",
        "https://upload.wikimedia.org:8443/a.png",
        "https://127.0.0.1/a.png",
        "https://[::1]/a.png",
        "https://example.com/a.png",
        "https://upload.wikimedia.org.evil.com/a.png",
        "https://evilupload.wikimedia.org.com/a.png",
    ],
)
def test_rejects_bad_urls(url: str) -> None:
    with pytest.raises(FetchBlocked):
        fetch(url)


def test_allows_subdomain_of_allowlisted_host() -> None:
    assert fetch("https://x.upload.wikimedia.org/a.png").body == PNG


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.5",
        "172.16.3.4",
        "192.168.1.10",
        "169.254.169.254",  # cloud metadata
        "100.101.102.103",  # Tailscale / CGNAT
        "0.0.0.0",
        "224.0.0.1",
        "::1",
        "fe80::1",
        "fd00::1",
        "::ffff:10.0.0.1",
    ],
)
def test_rejects_private_resolution(address: str) -> None:
    assert not ip_is_public(address)
    with pytest.raises(FetchBlocked, match="non-public"):
        fetch("https://upload.wikimedia.org/a.png", resolver=lambda h, p: [address])


def test_rejects_if_any_address_private() -> None:
    with pytest.raises(FetchBlocked):
        fetch(
            "https://upload.wikimedia.org/a.png",
            resolver=lambda h, p: ["198.35.26.112", "10.0.0.1"],
        )


def test_redirect_to_disallowed_host_blocked() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://evil.example/x.png"})

    with pytest.raises(FetchBlocked, match="allowlist"):
        fetch("https://upload.wikimedia.org/a.png", transport=make_transport(handler))


def test_redirect_revalidates_dns() -> None:
    calls = {"n": 0}

    def resolver(host: str, port: int) -> list[str]:
        calls["n"] += 1
        return ["198.35.26.112"] if calls["n"] == 1 else ["10.0.0.1"]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "/b.png"})

    with pytest.raises(FetchBlocked, match="non-public"):
        fetch(
            "https://upload.wikimedia.org/a.png",
            resolver=resolver,
            transport=make_transport(handler),
        )


def test_redirect_followed_within_allowlist() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/a.png":
            return httpx.Response(301, headers={"location": "/b.png"})
        return ok_png(request)

    result = fetch("https://upload.wikimedia.org/a.png", transport=make_transport(handler))
    assert result.url == "https://upload.wikimedia.org/b.png"


def test_too_many_redirects() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "/loop.png"})

    with pytest.raises(FetchBlocked, match="too many redirects"):
        fetch("https://upload.wikimedia.org/a.png", transport=make_transport(handler))


def test_size_limit_declared() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "image/png", "content-length": "99999999"},
            content=iter([b"x"]),
        )

    with pytest.raises(FetchBlocked, match="too large"):
        fetch("https://upload.wikimedia.org/a.png", transport=make_transport(handler))


def test_size_limit_streamed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # Streamed with no Content-Length: the cap must be enforced while reading.
        chunks = iter([b"x" * 500] * 10)
        return httpx.Response(200, headers={"content-type": "image/png"}, content=chunks)

    with pytest.raises(FetchBlocked, match="exceeds"):
        fetch(
            "https://upload.wikimedia.org/a.png", max_bytes=1000, transport=make_transport(handler)
        )


@pytest.mark.parametrize("ctype", ["text/html", "image/svg+xml", "application/octet-stream", ""])
def test_content_type_checked(ctype: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": ctype}, content=b"<svg/>")

    with pytest.raises(FetchBlocked, match="content-type"):
        fetch("https://upload.wikimedia.org/a.png", transport=make_transport(handler))


def test_non_200_rejected() -> None:
    with pytest.raises(FetchBlocked, match="status 404"):
        fetch(
            "https://upload.wikimedia.org/a.png",
            transport=make_transport(lambda r: httpx.Response(404)),
        )


def test_timeout_becomes_blocked() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(FetchBlocked, match="ReadTimeout"):
        fetch("https://upload.wikimedia.org/a.png", transport=make_transport(handler))


def test_truncate_keeps_the_start_instead_of_refusing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/html"}, content=iter([b"a" * 600, b"b" * 600])
        )

    result = fetch(
        "https://upload.wikimedia.org/page",
        allowed_types={"text/html"},
        max_bytes=1000,
        truncate=True,
        transport=make_transport(handler),
    )
    assert result.body == b"a" * 600 + b"b" * 400


def test_status_is_reported() -> None:
    with pytest.raises(FetchBlocked) as exc:
        fetch(
            "https://upload.wikimedia.org/a.png",
            transport=make_transport(lambda r: httpx.Response(429)),
        )
    assert exc.value.status == 429
