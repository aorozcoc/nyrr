"""Polite, bounded HTTP client for NYRR public pages (spec §11).

- connect timeout 10 s, read timeout 20 s
- at most 3 attempts per request; backoff 2 s, 4 s (8 s reserved if attempts are raised)
- honours Retry-After (capped); 401/403 stop collection immediately
- strictly sequential, >= `request_delay` seconds between request starts
- hard per-run request cap; refuses to run unless collection is authorized
- no proxies, no IP rotation, no CAPTCHA handling, no browser automation
"""

from __future__ import annotations

import email.utils
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from urllib.parse import urljoin, urlparse

import httpx

log = logging.getLogger(__name__)

BACKOFF_SECONDS = (2, 4, 8)
MAX_ATTEMPTS = 3
MAX_RETRY_AFTER_SECONDS = 60


class SourceError(Exception):
    """Retrieval failed; the observation must not be treated as availability evidence."""


class AccessDenied(SourceError):
    """HTTP 401/403 — stop collecting and alert operations."""


class RateLimited(SourceError):
    """HTTP 429 that could not be satisfied within the bounded retry budget."""


class RequestBudgetExceeded(SourceError):
    pass


class CollectionNotAuthorized(Exception):
    """NYRR_COLLECTION_AUTHORIZED is not true (Gate 1 not passed)."""


class NyrrClient:
    def __init__(
        self,
        *,
        base_url: str,
        user_agent: str,
        authorized: bool,
        request_delay: float = 2.0,
        max_requests: int = 30,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        now_fn: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if not authorized:
            raise CollectionNotAuthorized(
                "NYRR collection is not authorized (set NYRR_COLLECTION_AUTHORIZED=true only after Gate 1)"
            )
        self.base_url = base_url.rstrip("/")
        self._host = urlparse(self.base_url).hostname
        self.request_delay = request_delay
        self.max_requests = max_requests
        self.requests_made = 0
        self._sleep = sleep
        self._monotonic = monotonic
        self._now = now_fn
        self._last_request_at: float | None = None
        self._client = httpx.Client(
            timeout=httpx.Timeout(20.0, connect=10.0),
            headers={
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.8",
            },
            follow_redirects=True,
            max_redirects=3,
            transport=transport,
            trust_env=False,  # never route through environment proxies
        )

    def __enter__(self) -> NyrrClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------------ public
    def get_html(self, path_or_url: str) -> tuple[str, str, datetime]:
        """Return (html, final_url, fetched_at_utc). Raises SourceError on any failure."""
        url = urljoin(self.base_url + "/", path_or_url)
        if urlparse(url).hostname != self._host:
            raise SourceError(f"refusing to fetch off-site URL host={urlparse(url).hostname}")
        last_error: Exception | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            self._pace()
            try:
                response = self._client.get(url)
            except httpx.TimeoutException as exc:
                last_error = SourceError(f"timeout fetching {urlparse(url).path}: {type(exc).__name__}")
            except httpx.HTTPError as exc:
                last_error = SourceError(f"network error fetching {urlparse(url).path}: {type(exc).__name__}")
            else:
                status = response.status_code
                if urlparse(str(response.url)).hostname != self._host:
                    raise SourceError("redirected off-site; refusing to parse")
                if status == 200:
                    ctype = response.headers.get("content-type", "")
                    if "html" not in ctype:
                        raise SourceError(f"unexpected content-type {ctype!r}")
                    return response.text, str(response.url), self._now()
                if status in (401, 403):
                    raise AccessDenied(f"HTTP {status} from NYRR for {urlparse(url).path}")
                if status == 429:
                    wait = _retry_after_seconds(response.headers.get("retry-after"))
                    if wait is None or wait > MAX_RETRY_AFTER_SECONDS or attempt == MAX_ATTEMPTS:
                        raise RateLimited(f"HTTP 429 (retry-after={wait}) for {urlparse(url).path}")
                    log.warning("nyrr_rate_limited", extra={"wait_s": wait, "attempt": attempt})
                    self._sleep(wait)
                    continue
                if 500 <= status <= 599:
                    last_error = SourceError(f"HTTP {status} for {urlparse(url).path}")
                    wait = _retry_after_seconds(response.headers.get("retry-after"))
                    if wait is not None and wait > MAX_RETRY_AFTER_SECONDS:
                        # the server asked us to stay away longer than a run can wait: stop this run
                        raise RateLimited(f"HTTP {status} with Retry-After {wait:.0f}s for {urlparse(url).path}")
                    if wait is not None and attempt < MAX_ATTEMPTS:
                        self._sleep(wait)
                        continue
                else:
                    raise SourceError(f"HTTP {status} for {urlparse(url).path}")
            if attempt < MAX_ATTEMPTS:
                backoff = BACKOFF_SECONDS[attempt - 1]
                log.warning("nyrr_retry", extra={"attempt": attempt, "backoff_s": backoff, "error": str(last_error)})
                self._sleep(backoff)
        assert last_error is not None
        raise last_error

    # ------------------------------------------------------------------ internals
    def _pace(self) -> None:
        if self.requests_made >= self.max_requests:
            raise RequestBudgetExceeded(f"per-run request cap reached ({self.max_requests})")
        if self._last_request_at is not None:
            elapsed = self._monotonic() - self._last_request_at
            if elapsed < self.request_delay:
                self._sleep(self.request_delay - elapsed)
        self._last_request_at = self._monotonic()
        self.requests_made += 1


def _retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())
