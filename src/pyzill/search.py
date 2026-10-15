from collections.abc import Callable, Iterator, Mapping
from itertools import islice
from typing import Any, TypedDict, Unpack

from pyzill import http
from pyzill.exceptions import ZillowBlockedError
from pyzill.normalize import normalize_listing

# Wide bounding box covering the continental US, used as default when no
# coordinates are supplied.  Note: this does NOT narrow to search_value --
# Zillow searches the viewport, so omitting coordinates yields national results.
_DEFAULT_BBOX = {
    "ne_lat": 49.38,
    "ne_long": -66.94,
    "sw_lat": 24.54,
    "sw_long": -124.79,
}

_SEARCH_URL = f"{http.ZILLOW_HOME}/async-create-search-page-state"


class _SearchParams(TypedDict, total=False):
    search_value: str
    pagination: int
    min_beds: int | None
    max_beds: int | None
    min_bathrooms: int | None
    max_bathrooms: int | None
    min_price: int | None
    max_price: int | None
    ne_lat: float | None
    ne_long: float | None
    sw_lat: float | None
    sw_long: float | None
    zoom_value: int | None
    filter_state: dict[str, Any] | None
    proxy_url: str | None
    impersonate: str
    timeout: float
    max_retries: int


class _QueryStateParams(TypedDict):
    search_value: str
    pagination: int
    bbox: dict[str, float]
    zoom_value: int | None
    filter_state: dict[str, Any] | None
    filters: dict[str, dict[str, int] | None]


def _flags(**values: Any) -> dict[str, dict[str, Any]]:
    return {key: {"value": value} for key, value in values.items()}


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


def _resolve_bbox(
    ne_lat: float | None,
    ne_long: float | None,
    sw_lat: float | None,
    sw_long: float | None,
) -> dict[str, float]:
    """Resolve a complete bbox, or use the continental-US default."""
    coordinates = (ne_lat, ne_long, sw_lat, sw_long)
    if all(value is None for value in coordinates):
        return _DEFAULT_BBOX.copy()
    if any(value is None for value in coordinates):
        raise ValueError("all four bbox coordinates must be provided together")

    resolved = {
        "ne_lat": _required_coordinate(ne_lat),
        "ne_long": _required_coordinate(ne_long),
        "sw_lat": _required_coordinate(sw_lat),
        "sw_long": _required_coordinate(sw_long),
    }
    if resolved["ne_lat"] <= resolved["sw_lat"] or resolved["ne_long"] <= resolved["sw_long"]:
        raise ValueError("north-east bbox coordinates must be greater than south-west coordinates")
    return resolved


def _required_coordinate(value: float | None) -> float:
    if value is None:
        raise ValueError("all four bbox coordinates must be provided together")
    return value


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
        "mapBounds": {
            "north": bbox["ne_lat"],
            "east": bbox["ne_long"],
            "south": bbox["sw_lat"],
            "west": bbox["sw_long"],
        },
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


def for_sale(**kwargs: Any) -> dict[str, Any]:
    """Get listings that are for sale.

    Accepts the same keyword arguments as :func:`search` (``search_value``,
    ``pagination``, ``min_beds`` / ``max_beds``, ``min_bathrooms`` /
    ``max_bathrooms``, ``min_price`` / ``max_price``, ``ne_lat`` / ``ne_long``
    / ``sw_lat`` / ``sw_long``, ``zoom_value``, ``proxy_url``,
    ``impersonate``, ``timeout``, ``max_retries``).

    Returns:
        dict[str, Any]: keys ``mapResults`` (all listings, capped at 500 per
        query — narrow the bbox/zoom to get more) and ``listResults``.

    Raises:
        ZillowBlockedError: If Zillow responds with a non-2xx status.
    """
    return search(
        filter_state=_flags(sortSelection="globalrelevanceex", isAllHomes=True),
        **kwargs,
    )


def for_rent(**kwargs: Any) -> dict[str, Any]:
    """Get listings that are for rent.

    Accepts the same keyword arguments as :func:`for_sale`, plus ``is_room``
    (bool, default False) and ``is_entire_place`` (bool, default False).

    Raises:
        ZillowBlockedError: If Zillow responds with a non-2xx status.
    """
    is_room = kwargs.pop("is_room", False)
    is_entire_place = kwargs.pop("is_entire_place", False)
    filter_state = {
        **_RENT_SOLD_BASE,
        **_flags(sortSelection="priorityscore", isForRent=True),
    }
    if is_room:
        filter_state["isRoomForRent"] = {"value": True}
    if not is_entire_place:
        filter_state["isEntirePlaceForRent"] = {"value": False}
    return search(filter_state=filter_state, **kwargs)


def sold(**kwargs: Any) -> dict[str, Any]:
    """Get listings that were sold.

    Accepts the same keyword arguments as :func:`for_sale`.

    Raises:
        ZillowBlockedError: If Zillow responds with a non-2xx status.
    """
    filter_state = {
        **_RENT_SOLD_BASE,
        **_flags(sortSelection="globalrelevanceex", isForRent=False, isRecentlySold=True),
    }
    return search(filter_state=filter_state, **kwargs)


def search(**kwargs: Unpack[_SearchParams]) -> dict[str, Any]:
    """Run a raw Zillow search with a custom filter_state.

    Most callers should use :func:`for_sale`, :func:`for_rent`, or
    :func:`sold` instead.

    Args:
        search_value: Free-text location (e.g. "Schenectady NY").
        pagination: Page number for the sidebar list.
        min_beds / max_beds: Bedroom filter bounds.
        min_bathrooms / max_bathrooms: Bathroom filter bounds.
        min_price / max_price: Price filter bounds.
        ne_lat / ne_long / sw_lat / sw_long: Bounding-box corners.
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
    search_value = kwargs.get("search_value", "")
    pagination = kwargs.get("pagination", 1)
    min_beds = kwargs.get("min_beds")
    max_beds = kwargs.get("max_beds")
    min_bathrooms = kwargs.get("min_bathrooms")
    max_bathrooms = kwargs.get("max_bathrooms")
    min_price = kwargs.get("min_price")
    max_price = kwargs.get("max_price")
    ne_lat = kwargs.get("ne_lat")
    ne_long = kwargs.get("ne_long")
    sw_lat = kwargs.get("sw_lat")
    sw_long = kwargs.get("sw_long")
    zoom_value = kwargs.get("zoom_value")
    filter_state = kwargs.get("filter_state")
    proxy_url = kwargs.get("proxy_url")
    impersonate = kwargs.get("impersonate", http.DEFAULT_IMPERSONATE)
    timeout = kwargs.get("timeout", http.DEFAULT_TIMEOUT)
    max_retries = kwargs.get("max_retries", http.DEFAULT_MAX_RETRIES)
    query_state = _build_search_query_state(
        search_value=search_value,
        pagination=pagination,
        bbox=_resolve_bbox(ne_lat, ne_long, sw_lat, sw_long),
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


def iter_search(
    search_fn: Callable[..., dict[str, Any]] = for_sale,
    max_items: int | None = None,
    normalize: bool = False,
    **kwargs: Any,
) -> Iterator[Mapping[str, Any]]:
    """Yield individual mapResults listings from a search function.

    Wraps :func:`for_sale`, :func:`for_rent`, or :func:`sold` and yields
    one listing dict at a time from ``mapResults``.

    Note:
        Zillow caps ``mapResults`` at **500 listings per query**.  The
        ``pagination`` argument does NOT increase this cap — it only
        affects the sidebar ``listResults``.  If you get exactly 500
        results and need more, split the bounding box into smaller
        quadrants and run one query per quadrant (the ``split_bbox`` helper
        below can generate those quadrants for you).

    Args:
        search_fn: One of :func:`for_sale`, :func:`for_rent`, or
            :func:`sold`.  Defaults to :func:`for_sale`.
        max_items: Stop after yielding this many listings (None = all).
        normalize: When True, yield :func:`~pyzill.normalize.normalize_listing`
            output (flat, numeric-or-None fields) instead of raw Zillow dicts.
        **kwargs: Passed directly to the search function.

    Yields:
        Mapping[str, Any]: one listing from mapResults — a raw Zillow mapping,
        or a :class:`~pyzill.normalize.Listing` when ``normalize=True``.

    Example:
        >>> for listing in pyzill.iter_search(
        ...     pyzill.for_sale, search_value="Schenectady NY"
        ... ):
        ...     print(listing["address"])
    """
    map_results = search_fn(**kwargs).get("mapResults", [])
    for listing in islice(map_results, max_items):
        if normalize:
            normalized = normalize_listing(listing)
            if normalized is not None:
                yield normalized
        elif isinstance(listing, Mapping):
            yield listing


def split_bbox(
    ne_lat: float,
    ne_long: float,
    sw_lat: float,
    sw_long: float,
) -> list[dict[str, float]]:
    """Split a bounding box into 4 quadrants.

    Useful for working around Zillow's 500-result cap on mapResults:
    run one search per quadrant and combine the results.

    Args:
        ne_lat: North-east latitude.
        ne_long: North-east longitude.
        sw_lat: South-west latitude.
        sw_long: South-west longitude.

    Returns:
        list[dict[str, float]]: four dicts with ne_lat/ne_long/sw_lat/sw_long keys.
    """
    mid_lat = (ne_lat + sw_lat) / 2
    mid_long = (ne_long + sw_long) / 2
    return [
        {"ne_lat": ne_lat, "ne_long": mid_long, "sw_lat": mid_lat, "sw_long": sw_long},
        {"ne_lat": ne_lat, "ne_long": ne_long, "sw_lat": mid_lat, "sw_long": mid_long},
        {"ne_lat": mid_lat, "ne_long": mid_long, "sw_lat": sw_lat, "sw_long": sw_long},
        {"ne_lat": mid_lat, "ne_long": ne_long, "sw_lat": sw_lat, "sw_long": mid_long},
    ]
