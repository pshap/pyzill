"""Stateful client for Zillow search and detail requests."""

from __future__ import annotations

import re
import threading
from collections.abc import Callable, Sequence
from dataclasses import replace
from functools import cache
from typing import Any, TypeVar
from urllib.parse import quote

from curl_cffi import requests

from pyzill import details, search
from pyzill._kinds import Kind
from pyzill.exceptions import ZillowBlockedError
from pyzill.http import DEFAULT_IMPERSONATE, DEFAULT_MAX_RETRIES, DEFAULT_TIMEOUT, ZILLOW_HOME, RequestOptions
from pyzill.parse import parse_body_departments, parse_body_home
from pyzill.ratelimit import RateLimiter, _limiter, _SharedBudgetLimiter
from pyzill.utils import resolve_proxy

_HTTP_FORBIDDEN = 403
_MIN_PROXY_ROTATION_COUNT = 2
_Result = TypeVar("_Result")


def _resolve_limiter(rate_limit: float | None, limiter_instance: RateLimiter | None) -> RateLimiter:
    """Pick the rate limiter for a client.

    Without an explicit ``rate_limit`` the client shares the process-wide
    limiter, so several clients in one process stay inside one request budget
    for Zillow. An explicit interval is layered on top of that budget (the
    client may be stricter, never looser), except for an explicit ``0``,
    which opts that client out of throttling entirely.
    """
    if limiter_instance is not None:
        return limiter_instance
    if rate_limit is None:
        return _limiter
    if rate_limit <= 0:
        return RateLimiter(min_interval=0.0)
    return _SharedBudgetLimiter(_limiter, min_interval=rate_limit)


class ZillowClient:
    """A reusable Zillow client with an isolated session and rate limiter.

    The client owns a session (cookie jar) per proxy URL, because a
    PerimeterX cookie issued on one IP must not be replayed through another.

    Rate limiting follows the process-wide budget unless the client is given
    an explicit ``rate_limit``: a client then paces itself at least as slowly
    as the shared budget, so several clients in one process still cannot
    multiply the request rate against Zillow. ``rate_limit=0`` is an explicit
    opt-out that disables throttling for that client only.
    """

    def __init__(  # noqa: PLR0913
        self,
        proxy: str | Sequence[str] | None = None,
        *,
        impersonate: str = DEFAULT_IMPERSONATE,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        max_retry_after: float = 60.0,
        rate_limit: float | None = None,
        _limiter_instance: RateLimiter | None = None,
    ) -> None:
        proxies = [proxy] if isinstance(proxy, str) else list(proxy or [])
        # Deduplicate: two entries for the same proxy would rotate onto
        # themselves and burn the one retry without changing the transport.
        self._proxy_urls = list(dict.fromkeys(resolve_proxy(item) for item in proxies))
        if not self._proxy_urls:
            default_proxy = resolve_proxy(None)
            if default_proxy:
                self._proxy_urls = [default_proxy]
        self._proxy_index = 0
        self._rotation_lock = threading.Lock()
        # One session (cookie jar) per proxy URL: PerimeterX cookies issued on
        # one IP must not be replayed through another.
        self._sessions: dict[str | None, Any] = {}
        self.session: Any = self._session_for(self._proxy_urls[0] if self._proxy_urls else None)
        self.limiter = _resolve_limiter(rate_limit, _limiter_instance)
        self.options = RequestOptions(
            proxy_url=self._proxy_urls[0] if self._proxy_urls else None,
            impersonate=impersonate,
            timeout=timeout,
            max_retries=max_retries,
            max_retry_after=max_retry_after,
            session=self.session,
            limiter=self.limiter,
        )

    def _session_for(self, proxy_url: str | None) -> Any:
        """Return the session dedicated to ``proxy_url``, creating it if needed."""
        session = self._sessions.get(proxy_url)
        if session is None:
            session = requests.Session()
            self._sessions[proxy_url] = session
        return session

    def close(self) -> None:
        """Close every per-proxy HTTP session."""
        for session in self._sessions.values():
            session.close()

    def __enter__(self) -> ZillowClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _options_for(self, kwargs: dict[str, Any]) -> tuple[RequestOptions, bool]:
        """Build the per-call options and report whether the proxy is pinned.

        A caller-supplied ``proxy_url`` pins the transport for that call: the
        client will not rotate onto another proxy when it gets a 403, because
        the caller asked for that specific one. ``proxy_url=None`` counts as
        "not specified" and leaves the client's own proxy pool in place.
        """
        overrides: dict[str, Any] = {}
        for name in ("impersonate", "timeout", "max_retries", "max_retry_after"):
            if name in kwargs:
                overrides[name] = kwargs[name]
        pinned_proxy = kwargs.get("proxy_url") is not None
        if pinned_proxy:
            overrides["proxy_url"] = kwargs["proxy_url"]
        options = replace(self.options, **overrides)
        return replace(options, session=self._session_for(options.proxy_url)), pinned_proxy

    def _invoke(
        self,
        call: Callable[[RequestOptions], _Result],
        options: RequestOptions,
        *,
        pinned_proxy: bool = False,
    ) -> _Result:
        try:
            return call(options)
        except ZillowBlockedError as exc:
            if (
                exc.status_code != _HTTP_FORBIDDEN
                or pinned_proxy
                or len(self._proxy_urls) < _MIN_PROXY_ROTATION_COUNT
            ):
                raise
            with self._rotation_lock:
                self._proxy_index = (self._proxy_index + 1) % len(self._proxy_urls)
                proxy_url = self._proxy_urls[self._proxy_index]
                session = self._session_for(proxy_url)
                self.session = session
                # Stay on the transport that worked: re-using a proxy that
                # just returned 403 would double the request count against an
                # IP Zillow has flagged. Per-call overrides (timeout,
                # impersonate, ...) that belonged to this one request are not
                # touched.
                self.options = replace(self.options, proxy_url=proxy_url, session=session)
            retry_options = replace(options, proxy_url=proxy_url, session=session)
            return call(retry_options)

    def search_sale(self, **kwargs: Any) -> dict[str, Any]:
        options, pinned_proxy = self._options_for(kwargs)
        return self._invoke(
            lambda request_options: search._for_sale_impl(**kwargs, options=request_options),
            options,
            pinned_proxy=pinned_proxy,
        )

    def search_rent(self, **kwargs: Any) -> dict[str, Any]:
        options, pinned_proxy = self._options_for(kwargs)
        return self._invoke(
            lambda request_options: search._for_rent_impl(**kwargs, options=request_options),
            options,
            pinned_proxy=pinned_proxy,
        )

    def search_sold(self, **kwargs: Any) -> dict[str, Any]:
        options, pinned_proxy = self._options_for(kwargs)
        return self._invoke(
            lambda request_options: search._sold_impl(**kwargs, options=request_options),
            options,
            pinned_proxy=pinned_proxy,
        )

    def search(self, **kwargs: Any) -> dict[str, Any]:
        options, pinned_proxy = self._options_for(kwargs)
        return self._invoke(
            lambda request_options: search._search_impl(**kwargs, options=request_options),
            options,
            pinned_proxy=pinned_proxy,
        )

    def tiled_search(
        self,
        kind: Kind,
        **kwargs: Any,
    ) -> dict[str, Any]:
        search_fn = {"sale": self.search_sale, "rent": self.search_rent, "sold": self.search_sold}[kind]
        return search.tiled_search(search_fn, **kwargs)

    def home(self, zpid_or_url: int | str, *, options: RequestOptions | None = None) -> dict[str, Any]:
        base = options or self.options
        request_options = replace(base, session=self._session_for(base.proxy_url), limiter=self.limiter)
        # A caller-provided proxy pins this request; the client's own pool is
        # only rotated through when the caller did not choose a transport.
        pinned_proxy = options is not None and base.proxy_url is not None
        if isinstance(zpid_or_url, str) and zpid_or_url.startswith("https://"):
            match = re.search(r"(\d+)_zpid", zpid_or_url)
            requested_zpid = match.group(1) if match else None
            url = zpid_or_url
        else:
            requested_zpid = int(zpid_or_url)
            url = f"{ZILLOW_HOME}/homedetails/any-title/{requested_zpid}_zpid/"

        def parser(body: bytes) -> dict[str, Any]:
            return parse_body_home(body, requested_zpid)

        def call(selected: RequestOptions) -> dict[str, Any]:
            return details._fetch(url, parser, selected)

        return self._invoke(call, request_options, pinned_proxy=pinned_proxy)

    def apartment(self, apartment_id_or_url: str, *, options: RequestOptions | None = None) -> dict[str, Any]:
        base = options or self.options
        request_options = replace(base, session=self._session_for(base.proxy_url), limiter=self.limiter)
        pinned_proxy = options is not None and base.proxy_url is not None
        if apartment_id_or_url.startswith("https://"):
            url = apartment_id_or_url
        else:
            url = f"{ZILLOW_HOME}/apartments/{quote(apartment_id_or_url.strip(), safe='')}/"

        def call(selected: RequestOptions) -> dict[str, Any]:
            return details._fetch(url, parse_body_departments, selected)

        return self._invoke(call, request_options, pinned_proxy=pinned_proxy)


@cache
def get_default_client() -> ZillowClient:
    """Return the lazily initialized module-level client."""
    return ZillowClient(_limiter_instance=_limiter)
