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
from pyzill.exceptions import (
    ZillowBlockedError,
    ZillowError,
    ZillowNetworkError,
    ZillowNotFoundError,
    ZillowServerError,
)
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
# "perimeterx" is intentionally excluded: normal Zillow pages embed a
# `scripts.perimeterx` tag for bot mitigation, so the bare word appears on
# legitimate detail HTML. The markers below are challenge-specific.
_BLOCK_MARKERS = ("px-captcha", "captcha-delivery")


def json_body(response: Response) -> Any:
    """Parse a response body as JSON, the single place ``type: ignore`` lives."""
    return response.json()  # type: ignore[no-untyped-call]


def raise_for_status(response: Response) -> None:
    """Raise an HTTPError for unsuccessful responses (used by geocode)."""
    response.raise_for_status()  # type: ignore[no-untyped-call]


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
    return delay + random.uniform(0.0, min(1.0, delay * 0.25))  # noqa: S311 (retry jitter, not crypto)


_HTML_ACCEPT = (
    "text/html,application/xhtml+xml,application/xml;q=0.9,"
    "image/avif,image/webp,image/apng,*/*;q=0.8,"
    "application/signed-exchange;v=b3;q=0.7"
)
_JSON_ACCEPT = "application/json, text/plain, */*"
_ACCEPT_LANGUAGE = "en-US,en;q=0.9"

_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/{version}.0.0.0 Safari/537.36"
)
_ANDROID_UA = (
    "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/{version}.0.0.0 Mobile Safari/537.36"
)
_EDGE_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/{version}.0.0.0 Safari/537.36 Edg/{version}.0.0.0"
)
_FIREFOX_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:{version}.0) Gecko/20100101 Firefox/{version}.0"
)
_SAFARI_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/{version} Safari/605.1.15"
)
_SAFARI_IOS_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS {os_version} like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/{version} Mobile/15E148 Safari/604.1"
)

_DEFAULT_CHROME_VERSION = DEFAULT_IMPERSONATE.removeprefix("chrome") or "136"
_DEFAULT_SAFARI_VERSION = "18.0"
_DEFAULT_FIREFOX_VERSION = "133"

# Safari packs "major.patch" into its profile name: "153" is 15.3, "170" is
# 17.0.  Two digits is a bare major, three is major+patch.
_SAFARI_MAJOR_ONLY_DIGITS = 2
_SAFARI_MAJOR_PATCH_DIGITS = 3


@dataclass(frozen=True, slots=True)
class _BrowserProfile:
    """The browser identity an impersonation profile should present.

    curl_cffi sets the TLS/JA3 fingerprint; these are the HTTP-side facts that
    must agree with it. Presenting a Chrome User-Agent over a Safari
    fingerprint is a classic bot signal, so every field here is derived from
    the requested profile.
    """

    user_agent: str
    sec_ch_ua: str | None
    sec_ch_ua_platform: str | None
    mobile: bool
    send_fetch_metadata: bool
    send_fetch_user: bool


def _digits(text: str) -> str:
    return "".join(char for char in text if char.isdigit())


def _sec_ch_ua(version: str, extra_brand: str | None = None) -> str:
    """Build the Chromium client-hint brand list for a browser version."""
    brands = f'"Chromium";v="{version}", "Google Chrome";v="{version}"'
    if extra_brand:
        brands += f", {extra_brand};v=\"{version}\""
    return f'{brands}, "Not.A/Brand";v="99"'


def _safari_version(profile: str) -> tuple[str, bool]:
    """Return the (version, is_ios) pair for a Safari impersonation profile.

    Safari version numbers are packed into the profile name: ``safari184`` is
    18.4, ``safari15_3`` is 15.3, and ``safari172_ios`` is iOS 17.2.
    """
    body = profile.removeprefix("safari").lstrip("_")
    is_ios = body.endswith("_ios")
    if is_ios:
        body = body[: -len("_ios")]
    if "_" in body:
        major, _, minor = body.partition("_")
        return (f"{major}.{minor}" if minor else major), is_ios
    digits = _digits(body)
    if not digits:
        return _DEFAULT_SAFARI_VERSION, is_ios
    if len(digits) <= _SAFARI_MAJOR_ONLY_DIGITS:
        return f"{digits}.0", is_ios
    if len(digits) == _SAFARI_MAJOR_PATCH_DIGITS:
        return f"{digits[:2]}.{digits[2]}", is_ios
    return f"{digits[:2]}." + ".".join(digits[2:]), is_ios


def _resolve_profile(impersonate: str) -> _BrowserProfile:
    """Map a curl_cffi impersonation profile onto a matching browser identity."""
    name = impersonate.lower()
    if name.startswith("edge"):
        version = _digits(name) or _DEFAULT_CHROME_VERSION
        return _BrowserProfile(
            user_agent=_EDGE_UA.format(version=version),
            sec_ch_ua=_sec_ch_ua(version, '"Microsoft Edge"'),
            sec_ch_ua_platform='"Windows"',
            mobile=False,
            send_fetch_metadata=True,
            send_fetch_user=True,
        )
    if name.startswith("chrome"):
        version = _digits(name) or _DEFAULT_CHROME_VERSION
        android = "android" in name
        return _BrowserProfile(
            user_agent=(_ANDROID_UA if android else _CHROME_UA).format(version=version),
            sec_ch_ua=_sec_ch_ua(version),
            sec_ch_ua_platform='"Android"' if android else '"Windows"',
            mobile=android,
            send_fetch_metadata=True,
            send_fetch_user=True,
        )
    if name.startswith(("firefox", "tor")):
        version = _digits(name) or _DEFAULT_FIREFOX_VERSION
        return _BrowserProfile(
            user_agent=_FIREFOX_UA.format(version=version),
            sec_ch_ua=None,
            sec_ch_ua_platform=None,
            mobile=False,
            # Firefox sends neither Sec-Ch-Ua client hints nor Sec-Fetch
            # metadata; presenting them is a fingerprint mismatch.
            send_fetch_metadata=False,
            send_fetch_user=False,
        )
    if name.startswith("safari"):
        version, is_ios = _safari_version(name)
        user_agent = (
            _SAFARI_IOS_UA.format(version=version, os_version=version.replace(".", "_"))
            if is_ios
            else _SAFARI_UA.format(version=version)
        )
        return _BrowserProfile(
            user_agent=user_agent,
            sec_ch_ua=None,
            sec_ch_ua_platform=None,
            mobile=is_ios,
            send_fetch_metadata=True,
            send_fetch_user=False,
        )
    return _resolve_profile(DEFAULT_IMPERSONATE)


def _base_headers(profile: _BrowserProfile, accept: str, *, navigation: bool) -> dict[str, str]:
    """Build the headers every Zillow request carries."""
    headers = {
        "Accept": accept,
        "Accept-Language": _ACCEPT_LANGUAGE,
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "User-Agent": profile.user_agent,
    }
    if profile.sec_ch_ua is not None:
        headers["Sec-Ch-Ua"] = profile.sec_ch_ua
        headers["Sec-Ch-Ua-Mobile"] = "?1" if profile.mobile else "?0"
        if profile.sec_ch_ua_platform is not None:
            headers["Sec-Ch-Ua-Platform"] = profile.sec_ch_ua_platform
    if navigation:
        # Only document navigations carry this; an XHR does not.
        headers["Upgrade-Insecure-Requests"] = "1"
    return headers


def _fetch_headers(
    profile: _BrowserProfile,
    *,
    dest: str,
    mode: str,
    site: str,
    user_initiated: bool = False,
) -> dict[str, str]:
    """Build the Sec-Fetch-* metadata for a request, if the browser sends it."""
    if not profile.send_fetch_metadata:
        return {}
    headers = {
        "Sec-Fetch-Dest": dest,
        "Sec-Fetch-Mode": mode,
        "Sec-Fetch-Site": site,
    }
    if user_initiated and profile.send_fetch_user:
        headers["Sec-Fetch-User"] = "?1"
    return headers


def build_headers(  # noqa: PLR0913 - one builder for every Zillow request shape
    impersonate: str = DEFAULT_IMPERSONATE,
    *,
    accept: str = _HTML_ACCEPT,
    navigation: bool = True,
    fetch_dest: str = "document",
    fetch_mode: str = "navigate",
    fetch_site: str = "none",
    user_initiated: bool = True,
) -> dict[str, str]:
    """Return request headers matching a curl_cffi impersonate profile.

    curl_cffi already sets the TLS/JA3 fingerprint and most headers; these fill
    in the HTTP headers Zillow's bot detection inspects.  The User-Agent,
    client hints, and Sec-Fetch metadata are all derived from the requested
    profile, so the HTTP identity never contradicts the TLS fingerprint.

    Args:
        impersonate: curl_cffi impersonation profile name.
        accept: Value for the ``Accept`` header.
        navigation: Whether this is a document navigation (which sends
            ``Upgrade-Insecure-Requests``) rather than a scripted request.
        fetch_dest: ``Sec-Fetch-Dest`` value.
        fetch_mode: ``Sec-Fetch-Mode`` value.
        fetch_site: ``Sec-Fetch-Site`` value.
        user_initiated: Whether the request came from a user gesture, which
            adds ``Sec-Fetch-User`` on Chromium browsers.

    Returns:
        dict[str, str]: base headers (callers add request-specific ones).
    """
    profile = _resolve_profile(impersonate)
    headers = _base_headers(profile, accept, navigation=navigation)
    headers.update(
        _fetch_headers(
            profile,
            dest=fetch_dest,
            mode=fetch_mode,
            site=fetch_site,
            user_initiated=user_initiated,
        )
    )
    return headers


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
        headers=build_headers(options.impersonate),
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
    """Send a Zillow JSON request using XHR headers.

    The search endpoint is called by the page's own script, so it must look
    like a same-origin ``fetch``/XHR: ``Sec-Fetch-Dest: empty``,
    ``Sec-Fetch-Mode: cors``, a JSON ``Accept``, and no
    ``Upgrade-Insecure-Requests``.  Sending the navigation headers used by
    :func:`get_page` would be a self-inflicted fingerprint mismatch.
    """
    options = options or RequestOptions()
    return request(
        "PUT",
        url,
        headers={
            **build_headers(
                options.impersonate,
                accept=_JSON_ACCEPT,
                navigation=False,
                fetch_dest="empty",
                fetch_mode="cors",
                fetch_site="same-site",
                user_initiated=False,
            ),
            "Content-Type": "application/json",
            "Origin": ZILLOW_HOME,
            "Referer": f"{ZILLOW_HOME}/",
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
    # The retry loop always returns or raises; this typed fallback only
    # covers the "loop body never ran" case, and keeps the raised type a
    # ZillowError rather than a bare RuntimeError.
    last_exception: ZillowError = ZillowNetworkError(url)
    retry_delay = 0.0

    if session is None or limiter is None:
        # Imported here, not at module scope: pyzill.client imports this
        # module, so a top-level import would be a cycle.
        from pyzill.client import get_default_client

        default_client = get_default_client()
        session = session if session is not None else default_client.session
        limiter = limiter if limiter is not None else default_client.limiter

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
