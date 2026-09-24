"""HTTP client for fixed, known data-source APIs (GDELT, Wikidata, ...).

Adds a descriptive User-Agent, a minimum interval between requests (per client),
and exponential backoff on 429, 5xx and network errors, honouring Retry-After.
Arbitrary URLs from external data must go through net.safe_fetch instead.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping

import httpx

log = logging.getLogger(__name__)

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


class ApiError(Exception):
    """A request failed after retries, or the API returned something unusable."""


class RateLimited(ApiError):
    """Raised by callers when a 200 response is actually a rate-limit message."""


class ApiClient:
    def __init__(
        self,
        user_agent: str,
        *,
        timeout: float = 30.0,
        min_interval: float = 0.0,
        max_retries: int = 3,
        backoff_base: float = 10.0,
        backoff_max: float = 300.0,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = httpx.Client(
            timeout=timeout,
            headers={"User-Agent": user_agent},
            transport=transport,
            follow_redirects=True,
        )
        self.min_interval = min_interval
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self._sleep = sleep
        self._clock = clock
        self._last: float | None = None

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> ApiClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _pace(self) -> None:
        if self._last is not None and self.min_interval > 0:
            wait = self.min_interval - (self._clock() - self._last)
            if wait > 0:
                self._sleep(wait)
        self._last = self._clock()

    def _backoff(self, attempt: int, retry_after: str | None) -> float:
        if retry_after and retry_after.isdigit():
            return min(float(retry_after), self.backoff_max)
        return min(self.backoff_base * (2**attempt), self.backoff_max)

    def get(
        self,
        url: str,
        params: Mapping[str, str | int] | None = None,
        *,
        check: Callable[[httpx.Response], None] | None = None,
    ) -> httpx.Response:
        """GET with pacing and retries. `check` may raise RateLimited to trigger a retry."""
        last_error = "unknown error"
        for attempt in range(self.max_retries + 1):
            self._pace()
            retry_after = None
            try:
                response = self._client.get(url, params=params)
                if response.status_code in RETRY_STATUSES:
                    retry_after = response.headers.get("retry-after")
                    last_error = f"HTTP {response.status_code}"
                elif response.status_code != 200:
                    raise ApiError(f"HTTP {response.status_code} from {response.url.host}")
                else:
                    if check:
                        check(response)
                    return response
            except RateLimited as exc:
                last_error = f"rate limited: {exc}"
            except httpx.TransportError as exc:
                last_error = type(exc).__name__
            if attempt < self.max_retries:
                delay = self._backoff(attempt, retry_after)
                log.warning(
                    "request failed, backing off",
                    extra={"error": last_error, "attempt": attempt + 1, "delay_s": delay},
                )
                self._sleep(delay)
        raise ApiError(f"giving up after {self.max_retries + 1} attempts: {last_error}")
