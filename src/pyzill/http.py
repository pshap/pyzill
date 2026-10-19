"""Shared HTTP plumbing for Zillow requests.

Centralizes header construction, proxy resolution, throttling, retries, and
status handling so that search and detail requests stay consistent.
"""

import random
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Literal, cast

from curl_cffi.requests import ProxySpec, Response
from curl_cffi.requests.exceptions import RequestException

from pyzill.config import DEFAULT_IMPERSONATE, DEFAULT_MAX_RETRIES, DEFAULT_TIMEOUT
from pyzill.exceptions import ZillowBlockedError, ZillowNetworkError, ZillowNotFoundError, ZillowServerError
from pyzill.ratelimit import RateLimiter
from pyzill.utils import resolve_proxy

__all__ = [
    "DEFAULT_IMPERSONATE",
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_TIMEOUT",
    "ZILLOW_HOME",
    "RequestOptions",
    "build_headers",
    "build_proxies",
    "get_page",
    "put_json",
    "request",
]

ZILLOW_HOME = "https://www.zillow.com"


@dataclass(frozen=True, slots=True)
class RequestOptions:
    """Common options for a Zillow request."""

    proxy_url: str | None = None
    impersonate: str = DEFAULT_IMPERSONATE
    timeout: float = DEFAULT_TIMEOUT
    max_retries: int = DEFAULT_MAX_RETRIES
    session: Any | None = field(default=None, repr=False, compare=False)
    limiter: RateLimiter | None = field(default=None, repr=False, compare=False)
    max_retry_after: float = 60.0

    def __post_init__(self) -> None:
        if self.timeout <= 0:
            raise ValueError("timeout must be greater than 0")
        if self.max_retries < 0:
            raise ValueError("max_retries must be at least 0")
        if self.max_retry_after < 0:
            raise ValueError("max_retry_after must be at least 0")


_HTTP_OK_MIN = 200
_HTTP_OK_MAX = 300  # exclusive
_HTTP_SERVER_ERROR_MIN = 500
_HTTP_NOT_FOUND = 404
_HTTP_TOO_MANY_REQUESTS = 429

_RETRY_DELAY = 1.0

# PerimeterX challenge markers checked on a 2xx body that is not JSON.
_BLOCK_MARKERS = ("px-captcha", "perimeterx", "captcha-delivery", "_pxhd")


def _looks_blocked(response: Response) -> bool:
    """Detect a bot challenge served with a 2xx status.

    A real JSON API response is never a challenge page, so the body is only
    inspected when the content type is not JSON and the body does not parse as
    JSON. This keeps the scan off the hot path for multi-megabyte search
    payloads while still catching HTML challenge pages.
    """
    content_type = ""
    try:
        content_type = (response.headers.get("content-type") or "").lower()
    except AttributeError:
        content_type = ""
    if "json" in content_type:
        return False
    try:
        response.json()  # type: ignore[no-untyped-call]
    except Exception:
        body = (response.text or "").lower()
        return any(marker in body for marker in _BLOCK_MARKERS)
    return False


def _retry_delay(attempt: int, retry_after: str | None = None, max_retry_after: float = 60.0) -> float:
    if retry_after:
        try:
            return min(max_retry_after, max(0.0, float(retry_after)))
        except ValueError:
            try:
                retry_time = parsedate_to_datetime(retry_after)
                if retry_time.tzinfo is None:
                    retry_time = retry_time.replace(tzinfo=UTC)
                delta = retry_time - datetime.now(UTC)
                retry_delay_seconds = delta.days * 86_400 + delta.seconds + delta.microseconds / 1_000_000
                return min(max_retry_after, max(0.0, retry_delay_seconds))
            except (TypeError, ValueError, OverflowError):
                pass
    delay: float = _RETRY_DELAY * (2**attempt)
    return delay + random.uniform(0.0, min(1.0, delay * 0.25))  # noqa: S311 (retry jitter, not crypto)


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
        session=options.session,
        limiter=options.limiter,
        max_retry_after=options.max_retry_after,
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
        session=options.session,
        limiter=options.limiter,
        max_retry_after=options.max_retry_after,
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
    session: Any | None = None,
    limiter: RateLimiter | None = None,
    max_retry_after: float = 60.0,
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
    options = RequestOptions(
        proxy_url,
        impersonate,
        timeout,
        max_retries,
        max_retry_after=max_retry_after,
    )
    proxies = build_proxies(options.proxy_url)
    last_exception: Exception = RuntimeError("pyzill.http.request sent no requests")
    retry_delay = 0.0

    for attempt in range(options.max_retries + 1):
        if session is None or limiter is None:
            from pyzill.client import get_default_client

            default_client = get_default_client()
            session = session or default_client.session
            limiter = limiter or default_client.limiter
        limiter.wait(retry_delay)
        try:
            response = cast(
                Response,
                session.request(
                    method=method,
                    url=url,
                    headers=headers,
                    proxies=proxies,
                    impersonate=options.impersonate,
                    timeout=options.timeout,
                    json=json,
                ),
            )
        except RequestException as exc:
            if attempt < options.max_retries:
                retry_delay = _retry_delay(attempt, max_retry_after=options.max_retry_after)
                continue
            raise ZillowNetworkError(url) from exc

        if _HTTP_OK_MIN <= response.status_code < _HTTP_OK_MAX:
            if _looks_blocked(response):
                raise ZillowBlockedError(response.status_code, url, (response.text or "")[:200])
            return response

        body_snippet = response.text or ""
        if response.status_code == _HTTP_NOT_FOUND:
            raise ZillowNotFoundError(response.status_code, url, body_snippet)
        if response.status_code >= _HTTP_SERVER_ERROR_MIN:
            last_exception = ZillowServerError(response.status_code, url, body_snippet)
        else:
            last_exception = ZillowBlockedError(response.status_code, url, body_snippet)
        should_retry = response.status_code >= _HTTP_SERVER_ERROR_MIN or response.status_code == _HTTP_TOO_MANY_REQUESTS
        if should_retry and attempt < options.max_retries:
            retry_after = response.headers.get("Retry-After")
            retry_delay = _retry_delay(attempt, retry_after, options.max_retry_after)
            continue
        raise last_exception

    raise last_exception
