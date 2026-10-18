from collections.abc import Callable, Iterator, Mapping
from itertools import islice
from typing import Any, TypedDict, Unpack, cast

from pyzill import http
from pyzill.bbox import CONTINENTAL_US, BBox
from pyzill.exceptions import ZillowBlockedError

_SEARCH_URL = f"{http.ZILLOW_HOME}/async-create-search-page-state"


class SearchParams(TypedDict, total=False):
    search_value: str
    pagination: int
    min_beds: int | None
    max_beds: int | None
    min_bathrooms: int | None
    max_bathrooms: int | None
    min_price: int | None
    max_price: int | None
    bbox: BBox | None
    zoom_value: int | None
    filter_state: dict[str, Any] | None
    proxy_url: str | None
    impersonate: str
    timeout: float
    max_retries: int


_SEARCH_PARAM_NAMES = frozenset(SearchParams.__annotations__)


def _validate_search_kwargs(kwargs: Mapping[str, Any]) -> None:
    unknown = kwargs.keys() - _SEARCH_PARAM_NAMES
    if unknown:
        valid = ", ".join(sorted(_SEARCH_PARAM_NAMES))
        invalid = ", ".join(sorted(unknown))
        raise TypeError(f"Unexpected search keyword(s): {invalid}. Valid keywords: {valid}")


def for_sale(**kwargs: Unpack[SearchParams]) -> dict[str, Any]:
    """Get listings that are for sale.

    Accepts the same keyword arguments as :func:`search`, except
    ``filter_state`` (which it builds itself; pass ``filter_state`` to
    :func:`search` for custom filters).

    Returns:
        dict[str, Any]: keys ``mapResults`` (all listings, capped at 500 per
        query — narrow the bbox/zoom to get more) and ``listResults``.

    Raises:
        ValueError: If ``filter_state`` is passed.
        ZillowBlockedError: If Zillow responds with a non-2xx status.
    """
    _reject_filter_state(kwargs)
    search_kwargs: SearchParams = {
        **kwargs,
        "filter_state": _flags(sortSelection="globalrelevanceex", isAllHomes=True),
    }
    return search(**search_kwargs)


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
        ValueError: If ``filter_state`` is passed.
        ZillowBlockedError: If Zillow responds with a non-2xx status.
    """
    _reject_filter_state(kwargs)
    filter_state = {
        **_RENT_SOLD_BASE,
        **_flags(sortSelection="priorityscore", isForRent=True),
    }
    if is_room:
        filter_state["isRoomForRent"] = {"value": True}
    if not is_entire_place:
        filter_state["isEntirePlaceForRent"] = {"value": False}
    search_kwargs = cast(SearchParams, {**kwargs, "filter_state": filter_state})
    return search(**search_kwargs)


def sold(**kwargs: Unpack[SearchParams]) -> dict[str, Any]:
    """Get listings that were sold.

    Accepts the same keyword arguments as :func:`for_sale`, except
    ``filter_state`` (which it builds itself; pass ``filter_state`` to
    :func:`search` for custom filters).

    Raises:
        ValueError: If ``filter_state`` is passed.
        ZillowBlockedError: If Zillow responds with a non-2xx status.
    """
    _reject_filter_state(kwargs)
    filter_state = {
        **_RENT_SOLD_BASE,
        **_flags(sortSelection="globalrelevanceex", isForRent=False, isRecentlySold=True),
    }
    search_kwargs = cast(SearchParams, {**kwargs, "filter_state": filter_state})
    return search(**search_kwargs)


class _QueryStateParams(TypedDict):
    search_value: str
    pagination: int
    bbox: BBox
    zoom_value: int | None
    filter_state: dict[str, Any] | None
    filters: dict[str, dict[str, int] | None]


def _flags(**values: Any) -> dict[str, dict[str, Any]]:
    return {key: {"value": value} for key, value in values.items()}


def _reject_filter_state(kwargs: Mapping[str, Any]) -> None:
    """Reject caller-supplied ``filter_state`` on the built-in search variants.

    ``for_sale``/``for_rent``/``sold`` build their own filter state; a caller
    passing ``filter_state`` would be silently overwritten, so we reject it
    explicitly.  Use :func:`search` for custom filters.
    """
    if "filter_state" in kwargs:
        raise ValueError("for_sale/for_rent/sold build their own filter_state; pass custom filters to search() instead")


_RENT_SOLD_BASE = _flags(
    isNewConstruction=False,
    isForSaleForeclosure=False,
    isForSaleByOwner=False,
    isForSaleByAgent=False,
    isComingSoon=False,
    isAuction=False,
    isAllHomes=True,
)


def _range_filter(min_value: int | None, max_value: int | None) -> dict[str, int] | None:
    """Build a Zillow ``{min, max}`` range filter, or None if both are unset."""
    if min_value is None and max_value is None:
        return None
    result: dict[str, int] = {}
    if min_value is not None:
        result["min"] = min_value
    if max_value is not None:
        result["max"] = max_value
    return result


def _resolve_bbox(bbox: BBox | None) -> BBox:
    """Use the continental-US viewport when no bbox is supplied."""
    return CONTINENTAL_US if bbox is None else bbox


def _build_search_query_state(**kwargs: Unpack[_QueryStateParams]) -> dict[str, Any]:
    """Assemble the ``searchQueryState`` payload from its parts."""
    search_value = kwargs["search_value"]
    pagination = kwargs["pagination"]
    bbox = kwargs["bbox"]
    zoom_value = kwargs["zoom_value"]
    filter_state = kwargs["filter_state"]
    filters = kwargs["filters"]
    filter_state = {} if filter_state is None else filter_state.copy()
    query_state: dict[str, Any] = {
        "isMapVisible": True,
        "isListVisible": True,
        "mapBounds": bbox.to_map_bounds(),
        "filterState": filter_state,
        "pagination": {"currentPage": pagination},
    }
    if zoom_value is not None:
        query_state["mapZoom"] = zoom_value
    if search_value:
        query_state["usersSearchTerm"] = search_value
    for key, value in filters.items():
        if value is not None:
            filter_state[key] = value
    return query_state


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
        bbox: Optional :class:`~pyzill.bbox.BBox` viewport. Defaults to the
            continental United States.
        zoom_value: Map zoom level.
        filter_state: Raw Zillow filterState dict.
        proxy_url: Proxy URL (residential proxies strongly recommended).
        impersonate: curl_cffi TLS impersonation profile.
        timeout: Request timeout in seconds (default 30).
        max_retries: Retries on 5xx / network errors (default 1).

    Returns:
        dict[str, Any]: listing of properties in JSON format

    Raises:
        ZillowBlockedError: If Zillow responds with a non-2xx status.
    """
    _validate_search_kwargs(kwargs)
    search_value = kwargs.get("search_value", "")
    pagination = kwargs.get("pagination", 1)
    min_beds = kwargs.get("min_beds")
    max_beds = kwargs.get("max_beds")
    min_bathrooms = kwargs.get("min_bathrooms")
    max_bathrooms = kwargs.get("max_bathrooms")
    min_price = kwargs.get("min_price")
    max_price = kwargs.get("max_price")
    bbox = kwargs.get("bbox")
    zoom_value = kwargs.get("zoom_value")
    filter_state = kwargs.get("filter_state")
    proxy_url = kwargs.get("proxy_url")
    impersonate = kwargs.get("impersonate", http.DEFAULT_IMPERSONATE)
    timeout = kwargs.get("timeout", http.DEFAULT_TIMEOUT)
    max_retries = kwargs.get("max_retries", http.DEFAULT_MAX_RETRIES)
    query_state = _build_search_query_state(
        search_value=search_value,
        pagination=pagination,
        bbox=_resolve_bbox(bbox),
        zoom_value=zoom_value,
        filter_state=filter_state,
        filters={
            "beds": _range_filter(min_beds, max_beds),
            "baths": _range_filter(min_bathrooms, max_bathrooms),
            "price": _range_filter(min_price, max_price),
        },
    )
    input_data: dict[str, Any] = {
        "searchQueryState": query_state,
        "wants": {
            "cat1": ["listResults", "mapResults"],
            "cat2": ["total"],
        },
        "requestId": 10,
        "isDebugRequest": False,
    }
    response = http.put_json(
        _SEARCH_URL,
        input_data,
        options=http.RequestOptions(
            proxy_url=proxy_url,
            impersonate=impersonate,
            timeout=timeout,
            max_retries=max_retries,
        ),
    )
    try:
        data = response.json()  # type: ignore[no-untyped-call]
    except ValueError as exc:
        raise ZillowBlockedError(
            status_code=response.status_code,
            url=_SEARCH_URL,
            body_snippet=response.text or "",
        ) from exc
    if not isinstance(data, dict):
        raise ZillowBlockedError(
            status_code=response.status_code,
            url=_SEARCH_URL,
            body_snippet=response.text or "",
        )
    cat1 = data.get("cat1")
    search_results = cat1.get("searchResults") if isinstance(cat1, dict) else None
    if not isinstance(search_results, dict):
        raise ZillowBlockedError(
            status_code=response.status_code,
            url=_SEARCH_URL,
            body_snippet=response.text or "",
        )
    return search_results


def _iter_search(
    search_fn: Callable[..., dict[str, Any]],
    *,
    max_items: int | None,
    kwargs: dict[str, Any],
) -> Iterator[Mapping[str, Any]]:
    map_results = search_fn(**kwargs).get("mapResults", [])
    for listing in islice(map_results, max_items):
        if isinstance(listing, Mapping):
            yield listing


def iter_search(*, max_items: int | None = None, **kwargs: Any) -> Iterator[Mapping[str, Any]]:
    """Yield individual for-sale listings from ``mapResults``.

    Use :func:`iter_sale`, :func:`iter_rent`, or :func:`iter_sold` when the
    listing type should be explicit. Apply :func:`normalize_listings` to the
    iterator when normalized output is needed.

    Note:
        Zillow caps ``mapResults`` at **500 listings per query**.  The
        ``pagination`` argument does NOT increase this cap — it only
        affects the sidebar ``listResults``.  If you get exactly 500
        results and need more, split the bounding box into smaller
        quadrants and run one query per quadrant (the ``split_bbox`` helper
        below can generate those quadrants for you).

    Args:
        max_items: Stop after yielding this many listings (None = all).
        **kwargs: Passed directly to the search function.

    Yields:
        Mapping[str, Any]: one raw listing from mapResults.

    Example:
        >>> for listing in pyzill.iter_search(search_value="Schenectady NY"):
        ...     print(listing["address"])
    """
    yield from _iter_search(for_sale, max_items=max_items, kwargs=kwargs)


def iter_sale(*, max_items: int | None = None, **kwargs: Any) -> Iterator[Mapping[str, Any]]:
    """Yield individual for-sale listings from ``mapResults``."""
    yield from _iter_search(for_sale, max_items=max_items, kwargs=kwargs)


def iter_rent(*, max_items: int | None = None, **kwargs: Any) -> Iterator[Mapping[str, Any]]:
    """Yield individual for-rent listings from ``mapResults``."""
    yield from _iter_search(for_rent, max_items=max_items, kwargs=kwargs)


def iter_sold(*, max_items: int | None = None, **kwargs: Any) -> Iterator[Mapping[str, Any]]:
    """Yield individual sold listings from ``mapResults``."""
    yield from _iter_search(sold, max_items=max_items, kwargs=kwargs)


def split_bbox(bbox: BBox = CONTINENTAL_US) -> tuple[BBox, BBox, BBox, BBox]:
    """Split a bounding box into four ``BBox`` quadrants."""
    return bbox.split()
