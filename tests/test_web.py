from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from newsroom.settings import Settings
from newsroom.web.app import create_app


def test_healthz(client: TestClient) -> None:
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.text == "ok"


def test_index_renders(client: TestClient) -> None:
    resp = client.get("/")
    assert resp.status_code == 200
    assert "<h1>Feed</h1>" in resp.text


def test_security_headers(client: TestClient) -> None:
    resp = client.get("/")
    csp = resp.headers["content-security-policy"]
    assert "default-src 'none'" in csp
    assert "script-src 'self'" in csp
    assert "unsafe-inline" not in csp
    assert "frame-ancestors 'none'" in csp
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["referrer-policy"] == "no-referrer"
    assert "camera=()" in resp.headers["permissions-policy"]
    assert "strict-transport-security" not in resp.headers


def test_hsts_only_when_enabled(tmp_path: Path) -> None:
    client = TestClient(create_app(Settings(data_dir=tmp_path, enable_hsts=True)))
    assert client.get("/").headers["strict-transport-security"].startswith("max-age=")


def test_headers_on_static_and_errors(client: TestClient) -> None:
    for path in ("/static/css/site.css", "/does-not-exist"):
        assert "content-security-policy" in client.get(path).headers


def test_not_found_page(client: TestClient) -> None:
    resp = client.get("/nope")
    assert resp.status_code == 404
    assert "Page not found." in resp.text


def test_write_methods_rejected(client: TestClient) -> None:
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        resp = client.request(method, "/")
        assert resp.status_code == 405
        assert resp.headers["allow"] == "GET, HEAD"


def test_head_supported(client: TestClient) -> None:
    for path in ("/", "/healthz"):
        resp = client.head(path)
        assert resp.status_code == 200
        assert resp.content == b""
        assert "content-security-policy" in resp.headers


def test_no_api_docs(client: TestClient) -> None:
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


def test_rate_limit(tmp_path: Path) -> None:
    client = TestClient(create_app(Settings(data_dir=tmp_path, rate_limit="3/minute")))
    codes = [client.get("/").status_code for _ in range(5)]
    assert codes[:3] == [200, 200, 200]
    assert codes[3] == 429
    # Health checks are never rate limited.
    assert all(client.get("/healthz").status_code == 200 for _ in range(10))


def test_untrusted_host_rejected(tmp_path: Path) -> None:
    client = TestClient(
        create_app(Settings(data_dir=tmp_path, allowed_hosts=["news.example"])),
        base_url="http://evil.example",
    )
    assert client.get("/").status_code == 400


def test_templates_autoescape(client: TestClient) -> None:
    templates = client.app.state.templates  # type: ignore[attr-defined]
    rendered = templates.get_template("error.html").render(
        status=400, message="<script>alert(1)</script>", request=None
    )
    assert "<script>alert(1)</script>" not in rendered
    assert "&lt;script&gt;" in rendered
