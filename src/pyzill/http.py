"""Shared HTTP plumbing for Zillow requests.

Centralizes header construction, proxy resolution, throttling, retries, and
status handling so that search and detail requests stay consistent.
"""

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Literal

from curl_cffi import requests
from curl_cffi.requests import ProxySpec, Response
from curl_cffi.requests.exceptions import RequestException

from pyzill.exceptions import ZillowBlockedError, ZillowNotFoundError, ZillowServerError
from pyzill.ratelimit import _limiter
from pyzill.utils import resolve_proxy

ZILLOW_HOME = "https://www.zillow.com"
DEFAULT_IMPERSONATE = "chrome136"
DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_RETRIES = 1


@dataclass(frozen=True, slots=True)
class RequestOptions:
    """Common options for a Zillow request."""

    proxy_url: str | None = None
    impersonate: str = DEFAULT_IMPERSONATE
    timeout: float = DEFAULT_TIMEOUT
    max_retries: int = DEFAULT_MAX_RETRIES

    def __post_init__(self) -> None:
        if self.timeout <= 0:
            raise ValueError("timeout must be greater than 0")
        if self.max_retries < 0:
            raise ValueError("max_retries must be at least 0")


_HTTP_OK_MIN = 200
_HTTP_OK_MAX = 300  # exclusive
_HTTP_SERVER_ERROR_MIN = 500
_HTTP_NOT_FOUND = 404
_HTTP_TOO_MANY_REQUESTS = 429

_RETRY_DELAY = 1.0


def _retry_delay(attempt: int, retry_after: str | None = None) -> float:
    if retry_after:
        try:
            return max(0.0, float(retry_after))
        except ValueError:
            try:
                retry_time = parsedate_to_datetime(retry_after)
                if retry_time.tzinfo is None:
                    retry_time = retry_time.replace(tzinfo=UTC)
                delta = retry_time - datetime.now(UTC)
                retry_delay_seconds = delta.days * 86_400 + delta.seconds + delta.microseconds / 1_000_000
                return max(0.0, retry_delay_seconds)
            except (TypeError, ValueError, OverflowError):
                pass
    delay: float = _RETRY_DELAY * (2**attempt)
    monotonic_ns = int(time.monotonic_ns())
    jitter_fraction: float = (monotonic_ns % 1_000_000_000) / 1_000_000_000
    jitter: float = jitter_fraction * min(1.0, delay * 0.25)
    return float(delay + jitter)


_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"  # noqa: E501
_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{version}.0.0.0 Safari/537.36"  # noqa: E501


def build_headers(impersonate: str = DEFAULT_IMPERSONATE) -> dict[str, str]:
    """Return browser headers matching a curl_cffi impersonate profile.

    curl_cffi already sets the TLS/JA3 fingerprint and most headers; these fill
    in the HTTP headers Zillow's bot detection inspects.

    Args:
        impersonate: curl_cffi impersonation profile name.

    Returns:
        dict[str, str]: base headers (callers add request-specific ones).
    """
    default_version = DEFAULT_IMPERSONATE.removeprefix("chrome")
    version = "".join(char for char in impersonate.removeprefix("chrome") if char.isdigit()) or default_version
    return {
        "Accept": _ACCEPT,
        "Accept-Language": "en",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Sec-Ch-Ua": f'"Chromium";v="{version}", "Google Chrome";v="{version}", "Not.A/Brand";v="99"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
        "User-Agent": _USER_AGENT.format(version=version),
    }


def build_proxies(proxy_url: str | None) -> ProxySpec | None:
    """Resolve ``proxy_url`` (with ``ZILLOW_PROXY`` fallback) into a proxies dict."""
    proxy_url = resolve_proxy(proxy_url)
    return {"http": proxy_url, "https": proxy_url} if proxy_url else None


def get_page(
    url: str,
    *,
    options: RequestOptions | None = None,
) -> Response:
    """Fetch a Zillow page using browser navigation headers."""
    options = options or RequestOptions()
    return request(
        "GET",
        url,
        headers={**build_headers(options.impersonate), "Sec-Fetch-Site": "none"},
        proxy_url=options.proxy_url,
        impersonate=options.impersonate,
        timeout=options.timeout,
        max_retries=options.max_retries,
    )


def put_json(
    url: str,
    payload: dict[str, Any],
    *,
    options: RequestOptions | None = None,
) -> Response:
    """Send a Zillow JSON request using XHR headers."""
    options = options or RequestOptions()
    return request(
        "PUT",
        url,
        headers={
            **build_headers(options.impersonate),
            "Content-Type": "application/json",
            "origin": ZILLOW_HOME,
            "Referer": f"{ZILLOW_HOME}/",
            "Sec-Fetch-Site": "same-site",
        },
        proxy_url=options.proxy_url,
        impersonate=options.impersonate,
        timeout=options.timeout,
        max_retries=options.max_retries,
        json=payload,
    )


def request(  # noqa: PLR0913 - central request primitive
    method: Literal["GET", "POST", "PUT", "DELETE", "OPTIONS", "HEAD", "TRACE", "PATCH", "QUERY"],
    url: str,
    *,
    headers: dict[str, str],
    proxy_url: str | None = None,
    impersonate: str = DEFAULT_IMPERSONATE,
    timeout: float = DEFAULT_TIMEOUT,
    max_retries: int = DEFAULT_MAX_RETRIES,
    json: Any = None,
) -> Response:
    """Issue a throttled Zillow request, retrying transient 5xx and 429 failures.

    2xx responses are returned.  HTTP 404 and 5xx have dedicated error
    subclasses; other unsuccessful statuses raise :class:`ZillowBlockedError`.
    Transient 5xx and 429 responses are retried up to ``max_retries`` times.

    Args:
        method: HTTP verb, e.g. ``"GET"`` or ``"PUT"``.
        url: Absolute request URL.
        headers: Headers to send.
        proxy_url: Proxy URL (falls back to ``ZILLOW_PROXY``).
        impersonate: curl_cffi TLS impersonation profile.
        timeout: Request timeout in seconds.
        max_retries: Retries on 5xx, 429, and request errors (default 1).
        json: JSON body to send, or None.

    Returns:
        Response: the curl_cffi response object.

    Raises:
        ZillowError: If the response status is not 2xx.
    """
    RequestOptions(proxy_url, impersonate, timeout, max_retries)
    proxies = build_proxies(proxy_url)
    last_exception: Exception = RuntimeError("pyzill.http.request sent no requests")

    for attempt in range(max_retries + 1):
        _limiter.wait()
        try:
            response = requests.request(
                method=method,
                url=url,
                headers=headers,
                proxies=proxies,
                impersonate=impersonate,
                timeout=timeout,
                json=json,
            )
        except RequestException:
            if attempt < max_retries:
                time.sleep(_retry_delay(attempt))
                continue
            raise

        if _HTTP_OK_MIN <= response.status_code < _HTTP_OK_MAX:
            return response

        body_snippet = response.text or ""
        if response.status_code == _HTTP_NOT_FOUND:
            raise ZillowNotFoundError(response.status_code, url, body_snippet)
        if response.status_code >= _HTTP_SERVER_ERROR_MIN:
            last_exception = ZillowServerError(response.status_code, url, body_snippet)
        else:
            last_exception = ZillowBlockedError(response.status_code, url, body_snippet)
        should_retry = response.status_code >= _HTTP_SERVER_ERROR_MIN or response.status_code == _HTTP_TOO_MANY_REQUESTS
        if should_retry and attempt < max_retries:
            retry_after = (
                response.headers.get("Retry-After") if response.status_code == _HTTP_TOO_MANY_REQUESTS else None
            )
            time.sleep(_retry_delay(attempt, retry_after))
            continue
        raise last_exception

    raise last_exception
