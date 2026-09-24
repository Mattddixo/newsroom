from __future__ import annotations

import pytest

from newsroom.urls import canonical_key, host_matches, is_http_url


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("https://www.cbc.ca/news/x", "http://cbc.ca/news/x"),
        ("https://cbc.ca/news/x/", "https://cbc.ca/news/x"),
        ("https://cbc.ca/news/x#comments", "https://cbc.ca/news/x"),
        ("https://CBC.ca/news/x?utm_source=t&utm_medium=s", "https://cbc.ca/news/x"),
        ("https://cbc.ca/x?b=2&a=1", "https://cbc.ca/x?a=1&b=2"),
        ("https://cbc.ca/x?fbclid=abc&id=5", "https://cbc.ca/x?id=5"),
        ("https://cbc.ca:443/x", "https://cbc.ca/x"),
    ],
)
def test_canonical_key_equivalent(a: str, b: str) -> None:
    assert canonical_key(a) == canonical_key(b)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("https://cbc.ca/news/x", "https://cbc.ca/news/y"),
        ("https://cbc.ca/x?id=1", "https://cbc.ca/x?id=2"),
        ("https://cbc.ca/X", "https://cbc.ca/x"),  # paths are case-sensitive
        ("https://a.cbc.ca/x", "https://cbc.ca/x"),
    ],
)
def test_canonical_key_distinct(a: str, b: str) -> None:
    assert canonical_key(a) != canonical_key(b)


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("https://cbc.ca/x", True),
        ("http://cbc.ca", True),
        ("javascript:alert(1)", False),
        ("data:text/html,hi", False),
        ("ftp://cbc.ca/x", False),
        ("https:///nohost", False),
        ("https://cbc.ca/a b", False),
        ("", False),
        ("https://cbc.ca/" + "a" * 3000, False),
    ],
)
def test_is_http_url(url: str, ok: bool) -> None:
    assert is_http_url(url) is ok


def test_host_matches() -> None:
    assert host_matches("www.cbc.ca", "cbc.ca")
    assert host_matches("cbc.ca", "cbc.ca")
    assert not host_matches("fakecbc.ca", "cbc.ca")
    assert not host_matches("cbc.ca.evil.com", "cbc.ca")
