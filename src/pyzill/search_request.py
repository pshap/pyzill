"""Building Zillow search requests and checking their responses.

This is the pure request/response layer: it knows how to turn search
parameters into a payload, send it, and validate the answer. It never picks
a transport (the caller supplies :class:`~pyzill.http.RequestOptions`) and
it never prints or exits.
"""

from collections.abc import Mapping
from typing import Any, TypedDict, cast

from pyzill import http
from pyzill._kinds import Kind
from pyzill.bbox import CONTINENTAL_US, BBox
from pyzill.exceptions import ZillowParseError

__all__ = [
    "SEARCH_URL",
    "SearchParams",
    "SearchQuery",
    "TransportParams",
    "build_custom_params",
    "build_kind_params",
    "filter_state_for_kind",
    "resolve_bbox",
    "search_request",
    "split_params",
]

SEARCH_URL = f"{http.ZILLOW_HOME}/async-create-search-page-state"


class SearchQuery(TypedDict, total=False):
    """The query fields of a Zillow search (what to search for)."""

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


class TransportParams(TypedDict, total=False):
    """The transport fields of a search call (how to send it)."""

    proxy_url: str | None
    impersonate: str
    timeout: float
    max_retries: int


class SearchParams(SearchQuery, TransportParams, total=False):
    """Every keyword a search call accepts (query fields + transport fields)."""


_QUERY_FIELDS = frozenset(SearchQuery.__annotations__)
_TRANSPORT_FIELDS = frozenset(TransportParams.__annotations__)
_SEARCH_PARAM_NAMES = frozenset(SearchParams.__annotations__)


def _validate_search_kwargs(kwargs: Mapping[str, Any]) -> None:
    unknown = kwargs.keys() - _SEARCH_PARAM_NAMES
    if unknown:
        valid = ", ".join(sorted(_SEARCH_PARAM_NAMES))
        invalid = ", ".join(sorted(unknown))
        raise TypeError(f"Unexpected search keyword(s): {invalid}. Valid keywords: {valid}")


def split_params(kwargs: Mapping[str, Any]) -> tuple[SearchQuery, dict[str, Any]]:
    """Split loose keyword arguments into query fields and transport fields.

    The transport fields (``proxy_url``, ``impersonate``, ``timeout``,
    ``max_retries``) describe how to send the request; everything else
    describes what to search for.

    Raises:
        TypeError: If an unknown keyword is passed.
    """
    _validate_search_kwargs(kwargs)
    query = {name: value for name, value in kwargs.items() if name in _QUERY_FIELDS}
    transport = {name: value for name, value in kwargs.items() if name in _TRANSPORT_FIELDS}
    return cast(SearchQuery, query), transport


def reject_filter_state(kwargs: Mapping[str, Any]) -> None:
    """Reject a caller-supplied ``filter_state`` on the built-in variants.

    ``for_sale``/``for_rent``/``sold`` build their own filter state; a caller
    passing ``filter_state`` would be silently overwritten, so we reject it
    explicitly.  Use a custom search for custom filters.
    """
    if "filter_state" in kwargs:
        raise ValueError("for_sale/for_rent/sold build their own filter_state; pass custom filters to search() instead")


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


def filter_state_for_kind(
    kind: Kind,
    *,
    is_room: bool = False,
    is_entire_place: bool = False,
) -> dict[str, Any]:
    """Return the ``filter_state`` Zillow expects for one listing kind."""
    if kind == "sale":
        return _flags(sortSelection="globalrelevanceex", isAllHomes=True)
    if kind == "rent":
        filter_state: dict[str, Any] = {
            **_RENT_SOLD_BASE,
            **_flags(sortSelection="priorityscore", isForRent=True),
        }
        if is_room:
            filter_state["isRoomForRent"] = {"value": True}
        if not is_entire_place:
            filter_state["isEntirePlaceForRent"] = {"value": False}
        return filter_state
    if kind == "sold":
        return {
            **_RENT_SOLD_BASE,
            **_flags(sortSelection="globalrelevanceex", isForRent=False, isRecentlySold=True),
        }
    raise ValueError(f"Unknown listing kind: {kind!r}")


def build_kind_params(
    kind: Kind,
    kwargs: Mapping[str, Any],
    *,
    is_room: bool = False,
    is_entire_place: bool = False,
) -> SearchQuery:
    """Build the query parameters for one of the built-in search variants.

    The variant owns its ``filter_state``, so a caller-supplied one is
    rejected rather than silently overwritten.

    Raises:
        ValueError: If ``filter_state`` was passed.
        TypeError: If an unknown keyword was passed.
    """
    reject_filter_state(kwargs)
    query, _ = split_params(kwargs)
    params = dict(query)
    params["filter_state"] = filter_state_for_kind(
        kind,
        is_room=is_room,
        is_entire_place=is_entire_place,
    )
    return cast(SearchQuery, params)


def build_custom_params(kwargs: Mapping[str, Any]) -> SearchQuery:
    """Build the query parameters for a custom search (caller's filters).

    Raises:
        TypeError: If an unknown keyword was passed.
    """
    query, _ = split_params(kwargs)
    return query


def resolve_bbox(bbox: BBox | None, allow_nationwide: bool = False) -> BBox:
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


def _build_search_query_state(params: SearchQuery, bbox: BBox) -> dict[str, Any]:
    """Assemble the ``searchQueryState`` payload from its parts."""
    filter_state = params.get("filter_state")
    filter_state = {} if filter_state is None else filter_state.copy()
    # The range filters are merged into the filter state *before* it is put
    # into the query state, so the payload never depends on the two dicts
    # happening to be the same object.
    filters = {
        "beds": _range_filter(params.get("min_beds"), params.get("max_beds")),
        "baths": _range_filter(params.get("min_bathrooms"), params.get("max_bathrooms")),
        "price": _range_filter(params.get("min_price"), params.get("max_price")),
    }
    for key, value in filters.items():
        if value is not None:
            filter_state[key] = value
    query_state: dict[str, Any] = {
        "isMapVisible": True,
        "isListVisible": True,
        "mapBounds": bbox.to_map_bounds(),
        "filterState": filter_state,
        "pagination": {"currentPage": params.get("pagination", 1)},
    }
    zoom_value = params.get("zoom_value")
    if zoom_value is not None:
        query_state["mapZoom"] = zoom_value
    search_value = params.get("search_value", "")
    if search_value:
        query_state["usersSearchTerm"] = search_value
    return query_state


def search_request(params: SearchQuery, options: http.RequestOptions) -> dict[str, Any]:
    """Run one Zillow search and return its ``searchResults``.

    Args:
        params: The query fields of the search.
        options: The transport options (proxy, timeout, retries, session).

    Returns:
        dict[str, Any]: keys ``mapResults`` (all listings, capped at 500 per
        query) and ``listResults``.

    Raises:
        ValueError: If ``bbox`` is omitted and ``allow_nationwide`` is not True.
        ZillowParseError: If the response is not the expected JSON shape.
        ZillowBlockedError: If Zillow responds with a non-2xx status.
    """
    bbox = resolve_bbox(params.get("bbox"), params.get("allow_nationwide", False))
    query_state = _build_search_query_state(params, bbox)
    input_data: dict[str, Any] = {
        "searchQueryState": query_state,
        "wants": {
            "cat1": ["listResults", "mapResults"],
            "cat2": ["total"],
        },
        "requestId": 10,
        "isDebugRequest": False,
    }
    response = http.put_json(SEARCH_URL, input_data, options=options)
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
