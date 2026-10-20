"""Search-request building, tiling, and lazy iteration over raw results.

``pyzill.client`` imports this module, so the public ``for_sale``/``for_rent``/
``sold`` entry points import ``get_default_client`` lazily inside each function
body. A module-level import would create an import cycle (client -> search ->
client); keeping it local lets both import orders work.
"""

import json
import warnings
from collections import deque
from collections.abc import Callable, Iterator, Mapping
from typing import Any, TypedDict, Unpack, cast

from pyzill import http
from pyzill.bbox import CONTINENTAL_US, BBox
from pyzill.exceptions import ZillowParseError
from pyzill.normalize import is_building_result

_SEARCH_URL = f"{http.ZILLOW_HOME}/async-create-search-page-state"
_RESULT_CAP = 500
_DEFAULT_MAX_REQUESTS = 50


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
    allow_nationwide: bool
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
    """Get listings for sale through the lazily initialized default client."""
    from pyzill.client import get_default_client

    return get_default_client().search_sale(**kwargs)


def _for_sale_impl(*, options: http.RequestOptions | None = None, **kwargs: Unpack[SearchParams]) -> dict[str, Any]:
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
    _reject_filter_state(kwargs)
    search_kwargs: SearchParams = {
        **kwargs,
        "filter_state": _flags(sortSelection="globalrelevanceex", isAllHomes=True),
    }
    return _search_impl(**search_kwargs, options=options)


def for_rent(
    *,
    is_room: bool = False,
    is_entire_place: bool = False,
    **kwargs: Unpack[SearchParams],
) -> dict[str, Any]:
    """Get rental listings through the lazily initialized default client."""
    from pyzill.client import get_default_client

    return get_default_client().search_rent(is_room=is_room, is_entire_place=is_entire_place, **kwargs)


def _for_rent_impl(
    *,
    is_room: bool = False,
    is_entire_place: bool = False,
    options: http.RequestOptions | None = None,
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
    return _search_impl(**search_kwargs, options=options)


def sold(**kwargs: Unpack[SearchParams]) -> dict[str, Any]:
    """Get sold listings through the lazily initialized default client."""
    from pyzill.client import get_default_client

    return get_default_client().search_sold(**kwargs)


def _sold_impl(*, options: http.RequestOptions | None = None, **kwargs: Unpack[SearchParams]) -> dict[str, Any]:
    """Get listings that were sold.

    Accepts the same keyword arguments as :func:`for_sale`, except
    ``filter_state`` (which it builds itself; pass ``filter_state`` to
    :func:`search` for custom filters).

    Raises:
        ValueError: If ``filter_state`` is passed, or if ``bbox`` is omitted
            without ``allow_nationwide=True``.
        ZillowBlockedError: If Zillow responds with a non-2xx status.
    """
    _reject_filter_state(kwargs)
    filter_state = {
        **_RENT_SOLD_BASE,
        **_flags(sortSelection="globalrelevanceex", isForRent=False, isRecentlySold=True),
    }
    search_kwargs = cast(SearchParams, {**kwargs, "filter_state": filter_state})
    return _search_impl(**search_kwargs, options=options)


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


def _resolve_bbox(bbox: BBox | None, allow_nationwide: bool = False) -> BBox:
    """Return the search viewport, refusing to default to a nationwide one.

    A missing bbox used to silently fall back to the continental US, so a
    bare ``for_sale(search_value="12305")`` searched the whole country.
    Callers must now either pass ``bbox=`` or opt in with
    ``allow_nationwide=True``.
    """
    if bbox is not None:
        return bbox
    if allow_nationwide:
        return CONTINENTAL_US
    raise ValueError(
        "bbox is required: pass bbox=... to bound the search, "
        "or allow_nationwide=True to explicitly search the continental United States"
    )


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
    """Run a raw search through the lazily initialized default client."""
    from pyzill.client import get_default_client

    return get_default_client().search(**kwargs)


def _search_impl(*, options: http.RequestOptions | None = None, **kwargs: Unpack[SearchParams]) -> dict[str, Any]:
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
    _validate_search_kwargs(kwargs)
    search_value = kwargs.get("search_value", "")
    pagination = kwargs.get("pagination", 1)
    min_beds = kwargs.get("min_beds")
    max_beds = kwargs.get("max_beds")
    min_bathrooms = kwargs.get("min_bathrooms")
    max_bathrooms = kwargs.get("max_bathrooms")
    min_price = kwargs.get("min_price")
    max_price = kwargs.get("max_price")
    bbox = _resolve_bbox(kwargs.get("bbox"), kwargs.get("allow_nationwide", False))
    zoom_value = kwargs.get("zoom_value")
    filter_state = kwargs.get("filter_state")
    proxy_url = kwargs.get("proxy_url")
    impersonate = kwargs.get("impersonate", http.DEFAULT_IMPERSONATE)
    timeout = kwargs.get("timeout", http.DEFAULT_TIMEOUT)
    max_retries = kwargs.get("max_retries", http.DEFAULT_MAX_RETRIES)
    query_state = _build_search_query_state(
        search_value=search_value,
        pagination=pagination,
        bbox=bbox,
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
    request_options = options or http.RequestOptions(
        proxy_url=proxy_url,
        impersonate=impersonate,
        timeout=timeout,
        max_retries=max_retries,
    )
    response = http.put_json(
        _SEARCH_URL,
        input_data,
        options=request_options,
    )
    try:
        data = http.json_body(response)
    except ValueError as exc:
        raise ZillowParseError("Zillow search response was not valid JSON") from exc
    if not isinstance(data, dict):
        raise ZillowParseError("Zillow search response must be a JSON object")
    cat1 = data.get("cat1")
    search_results = cat1.get("searchResults") if isinstance(cat1, dict) else None
    if not isinstance(search_results, dict):
        raise ZillowParseError("Zillow search response is missing cat1.searchResults")
    return search_results


def _iter_search(
    search_fn: Callable[..., dict[str, Any]],
    *,
    max_items: int | None,
    kwargs: dict[str, Any],
) -> Iterator[Mapping[str, Any]]:
    map_results = search_fn(**kwargs).get("mapResults", [])
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
        request and iterates that response's ``mapResults`` (lazily with
        respect to yielding, but the request happens up front). Zillow caps
        ``mapResults`` at **500 listings per query**; the ``pagination``
        argument does NOT raise this cap — it only affects the sidebar
        ``listResults``. For depth beyond one response use :func:`tiled_search`
        (bounded by ``max_requests``) or split the bbox with :func:`split_bbox`
        and query each quadrant.

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


def _listing_identity(listing: Mapping[str, Any]) -> str:
    """A stable dedup key for a raw listing.

    Prefers the canonical identifiers (``zpid`` then ``detailUrl``); falls
    back to a JSON serialization of the whole row. ``repr(sorted(...))``
    is avoided because mixed-type values (e.g. a building card whose
    ``zpid`` is a lat-lng string) make ``sorted`` raise ``TypeError``.
    """
    identity = listing.get("zpid") or listing.get("detailUrl")
    if identity is None:
        identity = json.dumps(listing, sort_keys=True, default=str)
    return str(identity)


def _budget_exhausted(max_requests: int, on_budget: Callable[[], None] | None) -> None:
    """Stop tiling when the request budget runs out: callback or warning.

    The warning's ``stacklevel`` points at the caller of ``tiled_search``
    (caller -> tiled_search -> _budget_exhausted -> warnings.warn), so the
    reported line is the one that invoked the tiling, not this helper.
    """
    if on_budget is not None:
        on_budget()
        return
    warnings.warn(
        f"request budget of {max_requests} exhausted; returning partial results "
        "(raise max_requests or lower max_depth for more)",
        UserWarning,
        stacklevel=3,
    )


def tiled_search(  # noqa: PLR0913 - a single bounded tiling loop with callbacks
    search_fn: Callable[..., dict[str, Any]],
    *,
    bbox: BBox,
    max_depth: int = 4,
    max_requests: int = _DEFAULT_MAX_REQUESTS,
    on_cap: Callable[[], None] | None = None,
    on_progress: Callable[[int, int], None] | None = None,
    on_budget: Callable[[], None] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Recursively split capped map searches and deduplicate their results.

    Every tile is a normal search call, so the shared request limiter applies.
    ``on_cap`` is called when a tile still contains 500 results at max depth.
    ``on_progress`` (if given) is called after each tile with
    ``(requests_used, unique_map_results_so_far)``.

    ``max_requests`` (default 50) bounds the total number of search requests.
    Without a budget, ``--max-depth``-style deep tiling can issue tens of
    thousands of requests. When the budget runs out mid-tree, recursion
    stops and the partial results are returned with ``budget_exhausted``
    set to True; ``on_budget`` (if given) is called once when that happens,
    otherwise a ``UserWarning`` is emitted.

    Returns:
        dict[str, Any]: ``mapResults`` and ``listResults`` lists, plus a
        ``budget_exhausted`` bool flagging truncated (partial) results.
    """
    if max_depth < 0:
        raise ValueError("max_depth must be non-negative")
    if max_requests < 1:
        raise ValueError("max_requests must be at least 1")

    combined: dict[str, dict[str, Any]] = {"mapResults": {}, "listResults": {}}
    requests_used = 0
    budget_exhausted = False

    # Breadth-first so a partial run under the request budget covers the whole
    # area uniformly instead of exhausting the budget inside the first subtree.
    queue: deque[tuple[BBox, int]] = deque([(bbox, 0)])
    while queue:
        if requests_used >= max_requests:
            budget_exhausted = True
            _budget_exhausted(max_requests, on_budget)
            break
        tile, depth = queue.popleft()
        response = search_fn(**kwargs, bbox=tile)
        requests_used += 1
        map_results = response.get("mapResults", []) or []
        for result_key in combined:
            for listing in response.get(result_key, []) or []:
                if not isinstance(listing, Mapping):
                    continue
                combined[result_key].setdefault(_listing_identity(listing), dict(listing))
        if on_progress is not None:
            on_progress(requests_used, len(combined["mapResults"]))

        if len(map_results) >= _RESULT_CAP:
            if depth < max_depth:
                for child in tile.split():
                    queue.append((child, depth + 1))
            elif on_cap is not None:
                on_cap()

    return {
        "mapResults": list(combined["mapResults"].values()),
        "listResults": list(combined["listResults"].values()),
        "budget_exhausted": budget_exhausted,
    }


def split_bbox(bbox: BBox = CONTINENTAL_US) -> tuple[BBox, BBox, BBox, BBox]:
    """Split a bounding box into four ``BBox`` quadrants."""
    return bbox.split()
