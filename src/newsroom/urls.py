"""URL validation and canonicalization for untrusted URLs from external data."""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

MAX_URL_LENGTH = 2048

# Query parameters that only track the click and never change the page.
# Kept deliberately short: stripping a meaningful parameter would merge distinct articles.
_TRACKING_PARAMS = frozenset(
    {
        "fbclid",
        "gclid",
        "dclid",
        "msclkid",
        "mc_cid",
        "mc_eid",
        "ocid",
        "cmpid",
        "taid",
        "smid",
        "sr_share",
        "__twitter_impression",
        "ref_src",
        "ref_url",
    }
)


def is_http_url(url: str) -> bool:
    if not url or len(url) > MAX_URL_LENGTH or any(c.isspace() for c in url):
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return parts.scheme in {"http", "https"} and bool(parts.hostname)


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().rstrip(".")


def host_matches(host: str, domain: str) -> bool:
    host, domain = host.lower().rstrip("."), domain.lower().rstrip(".")
    return host == domain or host.endswith("." + domain)


def canonical_key(url: str) -> str:
    """Dedup key: scheme-less, lowercased host without www, no fragment,
    no tracking parameters, sorted query, no trailing slash.

    Only used for comparison; the original URL is what gets linked.
    """
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    port = parts.port
    if port and port not in {80, 443}:
        host = f"{host}:{port}"
    path = parts.path or "/"
    if len(path) > 1:
        path = path.rstrip("/")
    query = sorted(
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in _TRACKING_PARAMS
    )
    return urlunsplit(("", host, path, urlencode(query), "")).lstrip("/")
