"""Shared HTTP plumbing for Zillow requests.

Centralizes header construction, proxy resolution, throttling, retries, and
status handling so that search and detail requests stay consistent.
"""

import time
from typing import Any, cast

from curl_cffi import requests
from curl_cffi.requests import Response

from pyzill.exceptions import ZillowBlockedError
from pyzill.ratelimit import wait_for_slot
from pyzill.utils import resolve_proxy

ZILLOW_HOME = "https://www.zillow.com"

# Chrome major version for each supported curl_cffi impersonate profile.
_CHROME_VERSIONS = {
    "chrome124": "124",
    "chrome131": "131",
    "chrome136": "136",
}
_DEFAULT_CHROME_VERSION = "136"

_HTTP_OK_MIN = 200
_HTTP_OK_MAX = 300  # exclusive
_HTTP_SERVER_ERROR_MIN = 500

_RETRY_DELAY = 1.0

_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"  # noqa: E501
_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{version}.0.0.0 Safari/537.36"  # noqa: E501


def build_headers(impersonate: str = "chrome136") -> dict[str, str]:
    """Return browser headers matching a curl_cffi impersonate profile.

    curl_cffi already sets the TLS/JA3 fingerprint and most headers; these fill
    in the HTTP headers Zillow's bot detection inspects.

    Args:
        impersonate: curl_cffi impersonation profile name.

    Returns:
        dict[str, str]: base headers (callers add request-specific ones).
    """
    version = _CHROME_VERSIONS.get(impersonate, _DEFAULT_CHROME_VERSION)
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


def build_proxies(proxy_url: str | None) -> dict[str, str] | None:
    """Resolve ``proxy_url`` (with ``ZILLOW_PROXY`` fallback) into a proxies dict."""
    proxy_url = resolve_proxy(proxy_url)
    return {"http": proxy_url, "https": proxy_url} if proxy_url else None


def request(  # noqa: PLR0913 - central request primitive
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    proxy_url: str | None = None,
    impersonate: str = "chrome136",
    timeout: float = 30.0,
    max_retries: int = 0,
    json: Any = None,
) -> Response:
    """Issue a throttled Zillow request, retrying transient (5xx) failures.

    2xx responses are returned.  All other statuses raise
    :class:`ZillowBlockedError`; 5xx responses are retried up to
    ``max_retries`` times first.

    Args:
        method: HTTP verb, e.g. ``"GET"`` or ``"PUT"``.
        url: Absolute request URL.
        headers: Headers to send.
        proxy_url: Proxy URL (falls back to ``ZILLOW_PROXY``).
        impersonate: curl_cffi TLS impersonation profile.
        timeout: Request timeout in seconds.
        max_retries: Retries on 5xx / network errors (default 0).
        json: JSON body to send, or None.

    Returns:
        Response: the curl_cffi response object.

    Raises:
        ZillowBlockedError: If the response status is not 2xx.
    """
    proxies = build_proxies(proxy_url)
    send = getattr(requests, method.lower())
    body: dict[str, Any] = {} if json is None else {"json": json}
    last_exception: Exception = RuntimeError("pyzill.http.request sent no requests")

    for attempt in range(max_retries + 1):
        wait_for_slot()
        try:
            response = cast(
                "Response",
                send(
                    url=url,
                    headers=headers,
                    proxies=proxies,
                    impersonate=impersonate,
                    timeout=timeout,
                    **body,
                ),
            )
        except Exception as exc:
            last_exception = exc
            if attempt < max_retries:
                time.sleep(_RETRY_DELAY)
                continue
            raise

        if _HTTP_OK_MIN <= response.status_code < _HTTP_OK_MAX:
            return response

        last_exception = ZillowBlockedError(
            status_code=response.status_code,
            url=url,
            body_snippet=response.text or "",
        )
        if response.status_code >= _HTTP_SERVER_ERROR_MIN and attempt < max_retries:
            time.sleep(_RETRY_DELAY)
            continue
        raise last_exception

    raise last_exception
