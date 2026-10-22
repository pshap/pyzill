"""The public convenience API: module-level calls over the default client.

Everything in this module is a module-level entry point that runs against the
lazily created default client: the search variants, the ``iter_*`` helpers,
and the detail fetchers. It sits above :mod:`pyzill.client`, so no module
below it ever has to reach up for the client.

The request building lives in :mod:`pyzill.search_request` and
:mod:`pyzill.detail_request`; the client owns the session, the rate limit,
and the proxy rotation.
"""

import warnings
from collections.abc import Callable, Iterator, Mapping
from typing import Any, Unpack

from pyzill.client import get_default_client
from pyzill.config import DEFAULT_IMPERSONATE, DEFAULT_MAX_RETRIES, DEFAULT_TIMEOUT
from pyzill.http import RequestOptions, build_request_options
from pyzill.normalize import is_building_result
from pyzill.search_request import SearchParams

__all__ = [
    "for_rent",
    "for_sale",
    "get_from_apartment_id",
    "get_from_apartment_url",
    "get_from_department_id",
    "get_from_department_url",
    "get_from_home_id",
    "get_from_home_url",
    "iter_rent",
    "iter_sale",
    "iter_search",
    "iter_sold",
    "search",
    "sold",
]


def for_sale(**kwargs: Unpack[SearchParams]) -> dict[str, Any]:
    """Get listings that are for sale.

    Accepts the same keyword arguments as :func:`search`, except
    ``filter_state`` (which it builds itself; pass ``filter_state`` to
    :func:`search` for custom filters).

    Returns:
        dict[str, Any]: keys ``mapResults`` (all listings, capped at 500 per
        query — narrow the bbox/zoom to get more) and ``listResults``.

    Raises:
        ValueError: If ``filter_state`` is passed, or if ``bbox`` is omitted
            without ``allow_nationwide=True``.
        ZillowBlockedError: If Zillow responds with a non-2xx status.
    """
    return get_default_client().search_sale(**kwargs)


def for_rent(
    *,
    is_room: bool = False,
    is_entire_place: bool = False,
    **kwargs: Unpack[SearchParams],
) -> dict[str, Any]:
    """Get listings that are for rent.

    Accepts the same keyword arguments as :func:`for_sale`, plus ``is_room``
    (bool, default False) and ``is_entire_place`` (bool, default False).
    Does not accept ``filter_state`` (see :func:`for_sale`).

    Raises:
        ValueError: If ``filter_state`` is passed, or if ``bbox`` is omitted
            without ``allow_nationwide=True``.
        ZillowBlockedError: If Zillow responds with a non-2xx status.
    """
    return get_default_client().search_rent(is_room=is_room, is_entire_place=is_entire_place, **kwargs)


def sold(**kwargs: Unpack[SearchParams]) -> dict[str, Any]:
    """Get listings that were sold.

    Accepts the same keyword arguments as :func:`for_sale`, except
    ``filter_state`` (which it builds itself; pass ``filter_state`` to
    :func:`search` for custom filters).

    Raises:
        ValueError: If ``filter_state`` is passed, or if ``bbox`` is omitted
            without ``allow_nationwide=True``.
        ZillowBlockedError: If Zillow responds with a non-2xx status.
    """
    return get_default_client().search_sold(**kwargs)


def search(**kwargs: Unpack[SearchParams]) -> dict[str, Any]:
    """Run a raw Zillow search with a custom filter_state.

    Most callers should use :func:`for_sale`, :func:`for_rent`, or
    :func:`sold` instead.

    Args:
        search_value: Free-text location (e.g. "Schenectady NY").
        pagination: Page number for the sidebar list.
        min_beds / max_beds: Bedroom filter bounds.
        min_bathrooms / max_bathrooms: Bathroom filter bounds.
        min_price / max_price: Price filter bounds.
        bbox: :class:`~pyzill.bbox.BBox` viewport for the search. Required
            unless ``allow_nationwide=True`` is passed.
        allow_nationwide: Opt in to the continental-US default viewport when
            ``bbox`` is omitted (default False). A missing bbox otherwise
            raises ``ValueError``.
        zoom_value: Map zoom level.
        filter_state: Raw Zillow filterState dict.
        proxy_url: Proxy URL (residential proxies strongly recommended).
        impersonate: curl_cffi TLS impersonation profile.
        timeout: Request timeout in seconds (default 30).
        max_retries: Retries on transient 5xx, 429, and network errors (default 1).

    Returns:
        dict[str, Any]: listing of properties in JSON format

    Raises:
        ValueError: If ``bbox`` is omitted and ``allow_nationwide`` is not True.
        ZillowBlockedError: If Zillow responds with a non-2xx status.
    """
    return get_default_client().search(**kwargs)


def _iter_search(
    search_fn: Callable[..., dict[str, Any]],
    *,
    max_items: int | None,
    kwargs: dict[str, Any],
) -> Iterator[Mapping[str, Any]]:
    # Zillow sends "mapResults": null when it has no rows for the viewport,
    # so the key being present is not the key being a list.
    map_results = search_fn(**kwargs).get("mapResults") or []
    yielded = 0
    for listing in map_results:
        if max_items is not None and yielded >= max_items:
            break
        if isinstance(listing, Mapping) and not is_building_result(listing):
            yielded += 1
            yield listing


def iter_search(*, max_items: int | None = None, **kwargs: Any) -> Iterator[Mapping[str, Any]]:
    """Yield for-sale listings from a single search response.

    Use :func:`iter_sale`, :func:`iter_rent`, or :func:`iter_sold` when the
    listing type should be explicit. Apply :func:`normalize_listings` to the
    iterator when normalized output is needed.

    Note:
        Despite the name, this is **not** a paginating iterator: it issues one
        request and iterates that response's ``mapResults``. Nothing happens
        until the first listing is asked for -- the request is made then, and
        a missing ``bbox`` is reported then too. Zillow caps ``mapResults``
        at **500 listings per query**; the ``pagination`` argument does NOT
        raise this cap — it only affects the sidebar ``listResults``. For
        depth beyond one response use :func:`tiled_search` (bounded by
        ``max_requests``) or split the bbox with :func:`split_bbox` and query
        each quadrant.

    Args:
        max_items: Stop after yielding this many listings (None = all).
        **kwargs: Passed directly to the search function, which requires a
            ``bbox`` or ``allow_nationwide=True``.

    Yields:
        Mapping[str, Any]: one raw listing from mapResults.

    Example:
        >>> bbox = pyzill.geocode.geocode("Schenectady NY").bbox
        >>> for listing in pyzill.iter_search(search_value="Schenectady NY", bbox=bbox):
        ...     print(listing["address"])
    """
    yield from _iter_search(for_sale, max_items=max_items, kwargs=kwargs)


def iter_sale(*, max_items: int | None = None, **kwargs: Any) -> Iterator[Mapping[str, Any]]:
    """Yield for-sale listings from a single search response (see :func:`iter_search`)."""
    yield from _iter_search(for_sale, max_items=max_items, kwargs=kwargs)


def iter_rent(*, max_items: int | None = None, **kwargs: Any) -> Iterator[Mapping[str, Any]]:
    """Yield for-rent listings from a single search response (see :func:`iter_search`)."""
    yield from _iter_search(for_rent, max_items=max_items, kwargs=kwargs)


def iter_sold(*, max_items: int | None = None, **kwargs: Any) -> Iterator[Mapping[str, Any]]:
    """Yield sold listings from a single search response (see :func:`iter_search`)."""
    yield from _iter_search(sold, max_items=max_items, kwargs=kwargs)


def _detail_options(
    options: RequestOptions | None,
    proxy_url: str | None,
    impersonate: str,
    timeout: float,
    max_retries: int,
) -> RequestOptions:
    """Return the transport options for one detail fetch."""
    if options is not None:
        return options
    return build_request_options(
        proxy_url=proxy_url,
        impersonate=impersonate,
        timeout=timeout,
        max_retries=max_retries,
    )


def get_from_home_id(  # noqa: PLR0913 - the detail fetchers take the transport options
    property_id: int,
    proxy_url: str | None = None,
    impersonate: str = DEFAULT_IMPERSONATE,
    timeout: float = DEFAULT_TIMEOUT,
    max_retries: int = DEFAULT_MAX_RETRIES,
    *,
    options: RequestOptions | None = None,
) -> dict[str, Any]:
    """Fetch a home's detail record by zpid."""
    return get_default_client().home(
        property_id,
        options=_detail_options(options, proxy_url, impersonate, timeout, max_retries),
    )


def get_from_home_url(  # noqa: PLR0913 - the detail fetchers take the transport options
    home_url: str,
    proxy_url: str | None = None,
    impersonate: str = DEFAULT_IMPERSONATE,
    timeout: float = DEFAULT_TIMEOUT,
    max_retries: int = DEFAULT_MAX_RETRIES,
    *,
    options: RequestOptions | None = None,
) -> dict[str, Any]:
    """Fetch a home's detail record by Zillow URL."""
    return get_default_client().home(
        home_url,
        options=_detail_options(options, proxy_url, impersonate, timeout, max_retries),
    )


def get_from_apartment_id(  # noqa: PLR0913 - the detail fetchers take the transport options
    apartment_id: str,
    proxy_url: str | None = None,
    impersonate: str = DEFAULT_IMPERSONATE,
    timeout: float = DEFAULT_TIMEOUT,
    max_retries: int = DEFAULT_MAX_RETRIES,
    *,
    options: RequestOptions | None = None,
) -> dict[str, Any]:
    """Fetch an apartment building's detail record by id."""
    return get_default_client().apartment(
        apartment_id,
        options=_detail_options(options, proxy_url, impersonate, timeout, max_retries),
    )


def get_from_apartment_url(  # noqa: PLR0913 - the detail fetchers take the transport options
    apartment_url: str,
    proxy_url: str | None = None,
    impersonate: str = DEFAULT_IMPERSONATE,
    timeout: float = DEFAULT_TIMEOUT,
    max_retries: int = DEFAULT_MAX_RETRIES,
    *,
    options: RequestOptions | None = None,
) -> dict[str, Any]:
    """Fetch an apartment building's detail record by Zillow URL."""
    return get_default_client().apartment(
        apartment_url,
        options=_detail_options(options, proxy_url, impersonate, timeout, max_retries),
    )


def _deprecated_alias(alias_name: str, target: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
    def deprecated(*args: Any, **kwargs: Any) -> dict[str, Any]:
        warnings.warn(f"{alias_name} is deprecated; use {target.__name__}", DeprecationWarning, stacklevel=2)
        return target(*args, **kwargs)

    deprecated.__name__ = alias_name
    deprecated.__doc__ = f"Deprecated alias for :func:`{target.__name__}`."
    return deprecated


get_from_department_id = _deprecated_alias("get_from_department_id", get_from_apartment_id)
get_from_department_url = _deprecated_alias("get_from_department_url", get_from_apartment_url)
