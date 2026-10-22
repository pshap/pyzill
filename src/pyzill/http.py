"""Shared HTTP plumbing for Zillow requests.

Centralizes proxy resolution, throttling, retries, and status handling so
that search and detail requests stay consistent. The browser identity
(headers and client hints) lives in :mod:`pyzill.http_headers`.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from functools import cache
from typing import Any, Literal, cast

from curl_cffi.requests import ProxySpec, Response, Session
from curl_cffi.requests.exceptions import RequestException

from pyzill.config import DEFAULT_IMPERSONATE, DEFAULT_MAX_RETRIES, DEFAULT_TIMEOUT, ZILLOW_HOME
from pyzill.exceptions import (
    ZillowBlockedError,
    ZillowError,
    ZillowNetworkError,
    ZillowNotFoundError,
    ZillowServerError,
)
from pyzill.http_headers import JSON_ACCEPT, build_headers
from pyzill.ratelimit import RateLimiter, default_limiter
from pyzill.responses import json_body, raise_for_status
from pyzill.utils import jitter, resolve_proxy

__all__ = [
    "DEFAULT_IMPERSONATE",
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_TIMEOUT",
    "ZILLOW_HOME",
    "RequestOptions",
    "build_headers",
    "build_proxies",
    "build_request_options",
    "default_session",
    "get_page",
    "json_body",
    "put_json",
    "raise_for_status",
    "request",
]

# ``ZILLOW_HOME`` is re-exported here (it is listed in ``__all__``) because
# this module has always been the place callers imported it from; the value
# itself now lives in ``pyzill.config``.


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
# "perimeterx" is intentionally excluded: normal Zillow pages embed a
# `scripts.perimeterx` tag for bot mitigation, so the bare word appears on
# legitimate detail HTML. The markers below are challenge-specific.
_BLOCK_MARKERS = ("px-captcha", "captcha-delivery")


def build_request_options(  # noqa: PLR0913 - one builder for every Zillow request
    *,
    proxy_url: str | None = None,
    impersonate: str = DEFAULT_IMPERSONATE,
    timeout: float = DEFAULT_TIMEOUT,
    max_retries: int = DEFAULT_MAX_RETRIES,
    max_retry_after: float = 60.0,
    session: Any | None = None,
    limiter: RateLimiter | None = None,
) -> RequestOptions:
    """Build the transport options for a Zillow request.

    This is the one place loose transport arguments become a
    :class:`RequestOptions`, so the defaults live in exactly one spot.
    """
    return RequestOptions(
        proxy_url=proxy_url,
        impersonate=impersonate,
        timeout=timeout,
        max_retries=max_retries,
        max_retry_after=max_retry_after,
        session=session,
        limiter=limiter,
    )


@cache
def default_session() -> Any:
    """Return the fallback session used when a request carries no session.

    Callers that own a session (every :class:`~pyzill.client.ZillowClient`)
    pass one in; this is the session for direct, one-off requests.
    """
    return Session()


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
        json_body(response)
    except ValueError:
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
    return delay + jitter(min(1.0, delay * 0.25))


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
    options = options or build_request_options()
    return _send(
        "GET",
        url,
        headers=build_headers(options.impersonate),
        json=None,
        options=options,
    )


def put_json(
    url: str,
    payload: dict[str, Any],
    *,
    options: RequestOptions | None = None,
) -> Response:
    """Send a Zillow JSON request using XHR headers.

    The search endpoint is called by the page's own script, so it must look
    like a same-origin ``fetch``/XHR: ``Sec-Fetch-Dest: empty``,
    ``Sec-Fetch-Mode: cors``, a JSON ``Accept``, and no
    ``Upgrade-Insecure-Requests``.  Sending the navigation headers used by
    :func:`get_page` would be a self-inflicted fingerprint mismatch.
    """
    options = options or build_request_options()
    headers = {
        **build_headers(
            options.impersonate,
            accept=JSON_ACCEPT,
            navigation=False,
            fetch_dest="empty",
            fetch_mode="cors",
            fetch_site="same-site",
            user_initiated=False,
        ),
        "Content-Type": "application/json",
        "Origin": ZILLOW_HOME,
        "Referer": f"{ZILLOW_HOME}/",
    }
    return _send("PUT", url, headers=headers, json=payload, options=options)


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
    options = build_request_options(
        proxy_url=proxy_url,
        impersonate=impersonate,
        timeout=timeout,
        max_retries=max_retries,
        max_retry_after=max_retry_after,
        session=session,
        limiter=limiter,
    )
    return _send(method, url, headers=headers, json=json, options=options)


def _send(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    json: Any,
    options: RequestOptions,
) -> Response:
    """Send one request with the given transport options, retrying failures.

    This is the retry loop; :func:`request` is the public wrapper that builds
    the options for it.
    """
    proxies = build_proxies(options.proxy_url)
    session = options.session if options.session is not None else default_session()
    limiter = options.limiter if options.limiter is not None else default_limiter()
    # The retry loop always returns or raises; this typed fallback only
    # covers the "loop body never ran" case, and keeps the raised type a
    # ZillowError rather than a bare RuntimeError.
    last_exception: ZillowError = ZillowNetworkError(url)
    retry_delay = 0.0

    for attempt in range(options.max_retries + 1):
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
