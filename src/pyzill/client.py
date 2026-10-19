"""Stateful client for Zillow search and detail requests."""

from __future__ import annotations

import re
import threading
from collections.abc import Callable, Sequence
from dataclasses import replace
from functools import cache
from typing import Any, Literal, TypeVar
from urllib.parse import quote

from curl_cffi import requests

from pyzill import details, search
from pyzill.exceptions import ZillowBlockedError
from pyzill.http import DEFAULT_IMPERSONATE, DEFAULT_MAX_RETRIES, DEFAULT_TIMEOUT, ZILLOW_HOME, RequestOptions
from pyzill.parse import parse_body_departments, parse_body_home
from pyzill.ratelimit import RateLimiter, _limiter, get_min_interval
from pyzill.utils import resolve_proxy

_HTTP_FORBIDDEN = 403
_MIN_PROXY_ROTATION_COUNT = 2
_Result = TypeVar("_Result")


class ZillowClient:
    """A reusable Zillow client with an isolated session and rate limiter."""

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
        self._proxy_urls = [resolve_proxy(item) for item in proxies]
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
        self.limiter = _limiter_instance or RateLimiter(
            min_interval=get_min_interval() if rate_limit is None else rate_limit
        )
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

    def _options_for(self, kwargs: dict[str, Any]) -> RequestOptions:
        overrides: dict[str, Any] = {}
        for name in ("proxy_url", "impersonate", "timeout", "max_retries", "max_retry_after"):
            if name in kwargs:
                overrides[name] = kwargs[name]
        options = replace(self.options, **overrides)
        return replace(options, session=self._session_for(options.proxy_url))

    def _invoke(self, call: Callable[[RequestOptions], _Result], options: RequestOptions) -> _Result:
        try:
            return call(options)
        except ZillowBlockedError as exc:
            if exc.status_code != _HTTP_FORBIDDEN or len(self._proxy_urls) < _MIN_PROXY_ROTATION_COUNT:
                raise
            with self._rotation_lock:
                self._proxy_index = (self._proxy_index + 1) % len(self._proxy_urls)
                proxy_url = self._proxy_urls[self._proxy_index]
                session = self._session_for(proxy_url)
                self.session = session
                # Rotate only the transport, not any per-call overrides
                # (timeout, impersonate, ...) that belonged to this one request.
                self.options = replace(self.options, proxy_url=proxy_url, session=session)
            retry_options = replace(options, proxy_url=proxy_url, session=session)
            return call(retry_options)

    def search_sale(self, **kwargs: Any) -> dict[str, Any]:
        options = self._options_for(kwargs)
        return self._invoke(lambda request_options: search._for_sale_impl(**kwargs, options=request_options), options)

    def search_rent(self, **kwargs: Any) -> dict[str, Any]:
        options = self._options_for(kwargs)
        return self._invoke(lambda request_options: search._for_rent_impl(**kwargs, options=request_options), options)

    def search_sold(self, **kwargs: Any) -> dict[str, Any]:
        options = self._options_for(kwargs)
        return self._invoke(lambda request_options: search._sold_impl(**kwargs, options=request_options), options)

    def search(self, **kwargs: Any) -> dict[str, Any]:
        options = self._options_for(kwargs)
        return self._invoke(lambda request_options: search._search_impl(**kwargs, options=request_options), options)

    def tiled_search(
        self,
        kind: Literal["sale", "rent", "sold"],
        **kwargs: Any,
    ) -> dict[str, Any]:
        search_fn = {"sale": self.search_sale, "rent": self.search_rent, "sold": self.search_sold}[kind]
        return search.tiled_search(search_fn, **kwargs)

    def home(self, zpid_or_url: int | str, *, options: RequestOptions | None = None) -> dict[str, Any]:
        base = options or self.options
        request_options = replace(base, session=self._session_for(base.proxy_url), limiter=self.limiter)
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

        return self._invoke(call, request_options)

    def apartment(self, apartment_id_or_url: str, *, options: RequestOptions | None = None) -> dict[str, Any]:
        base = options or self.options
        request_options = replace(base, session=self._session_for(base.proxy_url), limiter=self.limiter)
        if apartment_id_or_url.startswith("https://"):
            url = apartment_id_or_url
        else:
            url = f"{ZILLOW_HOME}/apartments/{quote(apartment_id_or_url.strip(), safe='')}/"

        def call(selected: RequestOptions) -> dict[str, Any]:
            return details._fetch(url, parse_body_departments, selected)

        return self._invoke(call, request_options)


@cache
def get_default_client() -> ZillowClient:
    """Return the lazily initialized module-level client."""
    return ZillowClient(_limiter_instance=_limiter)
