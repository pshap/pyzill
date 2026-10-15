import time
from typing import Any

from curl_cffi import requests

from pyzill.exceptions import ZillowBlockedError
from pyzill.normalize import normalize_listing
from pyzill.ratelimit import wait_for_slot
from pyzill.utils import resolve_proxy

# Wide bounding box covering the continental US, used as default when no
# coordinates are supplied.  Note: this does NOT narrow to search_value --
# Zillow searches the viewport, so omitting coordinates yields national results.
_DEFAULT_BBOX = {
    "ne_lat": 49.38,
    "ne_long": -66.94,
    "sw_lat": 24.54,
    "sw_long": -124.79,
}

_SEARCH_URL = "https://www.zillow.com/async-create-search-page-state"

_HTTP_OK_MIN = 200
_HTTP_OK_MAX = 300  # exclusive
_HTTP_CLIENT_ERROR_MIN = 400
_HTTP_SERVER_ERROR_MIN = 500


# Headers matching the default impersonate profile (chrome136).
# curl_cffi already sets TLS/JA3 and most headers; these fill in the rest.
def _headers(impersonate: str) -> dict[str, str]:
    # Map impersonate profile name to a Chrome major version for the
    # fallback User-Agent / Sec-Ch-Ua headers.
    version_map = {
        "chrome124": "124",
        "chrome131": "131",
        "chrome136": "136",
    }
    major = version_map.get(impersonate, "136")
    return {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",  # noqa: E501
        "Accept-Language": "en",
        "Content-Type": "application/json",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "origin": "https://www.zillow.com",
        "Referer": "https://www.zillow.com/",
        "Sec-Ch-Ua": f'"Chromium";v="{major}", "Google Chrome";v="{major}", "Not.A/Brand";v="99"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-site",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
        "User-Agent": f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36",  # noqa: E501
    }


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
    """Fill in any missing bbox corner from the continental-US default."""
    return {
        "ne_lat": _DEFAULT_BBOX["ne_lat"] if ne_lat is None else ne_lat,
        "ne_long": _DEFAULT_BBOX["ne_long"] if ne_long is None else ne_long,
        "sw_lat": _DEFAULT_BBOX["sw_lat"] if sw_lat is None else sw_lat,
        "sw_long": _DEFAULT_BBOX["sw_long"] if sw_long is None else sw_long,
    }


def _build_search_query_state(  # noqa: PLR0913, PLR0917 - assembles many query parts
    search_value: str,
    pagination: int,
    bbox: dict[str, float],
    zoom_value: int | None,
    filter_state: dict[str, Any] | None,
    filters: dict[str, dict[str, int] | None],
) -> dict[str, Any]:
    """Assemble the ``searchQueryState`` payload from its parts."""
    if filter_state is None:
        filter_state = {}
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


def _do_search_request(
    input_data: dict[str, Any],
    proxy_url: str | None,
    impersonate: str,
    timeout: float,
    max_retries: int,
) -> dict[str, Any]:
    """Issue the PUT request with retry logic and status checking."""
    proxy_url = resolve_proxy(proxy_url)
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
    headers = _headers(impersonate)
    last_exception: Exception | None = None

    for attempt in range(max_retries + 1):
        wait_for_slot()
        try:
            response = requests.put(
                url=_SEARCH_URL,
                json=input_data,
                headers=headers,
                proxies=proxies,
                impersonate=impersonate,
                timeout=timeout,
            )
        except Exception as exc:
            last_exception = exc
            if attempt < max_retries:
                time.sleep(1)
                continue
            raise

        if _HTTP_OK_MIN <= response.status_code < _HTTP_OK_MAX:
            return response.json()

        # Non-2xx: raise immediately on 4xx (client errors like 403 won't
        # succeed on retry); retry on 5xx.
        if _HTTP_CLIENT_ERROR_MIN <= response.status_code < _HTTP_SERVER_ERROR_MIN:
            raise ZillowBlockedError(
                status_code=response.status_code,
                url=_SEARCH_URL,
                body_snippet=response.text or "",
            )
        last_exception = ZillowBlockedError(
            status_code=response.status_code,
            url=_SEARCH_URL,
            body_snippet=response.text or "",
        )
        if attempt < max_retries:
            time.sleep(1)

    raise last_exception  # pragma: no cover


def for_sale(  # noqa: PLR0913, PLR0917 - mirrors Zillow's filter surface
    search_value: str = "",
    pagination: int = 1,
    min_beds: int | None = None,
    max_beds: int | None = None,
    min_bathrooms: int | None = None,
    max_bathrooms: int | None = None,
    min_price: int | None = None,
    max_price: int | None = None,
    ne_lat: float | None = None,
    ne_long: float | None = None,
    sw_lat: float | None = None,
    sw_long: float | None = None,
    zoom_value: int | None = None,
    proxy_url: str | None = None,
    impersonate: str = "chrome136",
    timeout: float = 30.0,
    max_retries: int = 1,
) -> dict[str, Any]:
    """Get listings that are for sale.

    Returns a dict with keys ``mapResults`` and ``listResults``.
    Use ``mapResults`` — it contains all listings across paginations
    (capped at 500 by Zillow; narrow the bbox/zoom to get more).

    Args:
        search_value: Free-text location (e.g. "Schenectady NY").
        pagination: Page number for the sidebar list (rarely needed).
        min_beds / max_beds: Bedroom filter bounds.
        min_bathrooms / max_bathrooms: Bathroom filter bounds.
        min_price / max_price: Price filter bounds.
        ne_lat / ne_long / sw_lat / sw_long: Bounding-box corners that define
            the search viewport. If omitted, a continental-US bbox is used and
            results will span the whole country (``search_value`` only labels
            the search; it does not narrow the viewport).
        zoom_value: Map zoom level.
        proxy_url: Proxy URL (residential proxies strongly recommended).
        impersonate: curl_cffi TLS impersonation profile (default "chrome136").
        timeout: Request timeout in seconds (default 30).
        max_retries: Retries on 5xx / network errors (default 1).

    Returns:
        dict[str, Any]: listing of properties in JSON format

    Raises:
        ZillowBlockedError: If Zillow responds with a non-2xx status.
    """
    filter_state = {
        "sortSelection": {"value": "globalrelevanceex"},
        "isAllHomes": {"value": True},
    }
    return search(
        search_value=search_value,
        pagination=pagination,
        min_beds=min_beds,
        max_beds=max_beds,
        min_bathrooms=min_bathrooms,
        max_bathrooms=max_bathrooms,
        min_price=min_price,
        max_price=max_price,
        ne_lat=ne_lat,
        ne_long=ne_long,
        sw_lat=sw_lat,
        sw_long=sw_long,
        zoom_value=zoom_value,
        filter_state=filter_state,
        proxy_url=proxy_url,
        impersonate=impersonate,
        timeout=timeout,
        max_retries=max_retries,
    )


def for_rent(  # noqa: PLR0913, PLR0917 - mirrors Zillow's filter surface
    search_value: str = "",
    pagination: int = 1,
    is_entire_place: bool = False,
    is_room: bool = False,
    min_beds: int | None = None,
    max_beds: int | None = None,
    min_bathrooms: int | None = None,
    max_bathrooms: int | None = None,
    min_price: int | None = None,
    max_price: int | None = None,
    ne_lat: float | None = None,
    ne_long: float | None = None,
    sw_lat: float | None = None,
    sw_long: float | None = None,
    zoom_value: int | None = None,
    proxy_url: str | None = None,
    impersonate: str = "chrome136",
    timeout: float = 30.0,
    max_retries: int = 1,
) -> dict[str, Any]:
    """Get listings that are for rent.

    See :func:`for_sale` for argument descriptions.

    Raises:
        ZillowBlockedError: If Zillow responds with a non-2xx status.
    """
    filter_state = {
        "sortSelection": {"value": "priorityscore"},
        "isNewConstruction": {"value": False},
        "isForSaleForeclosure": {"value": False},
        "isForSaleByOwner": {"value": False},
        "isForSaleByAgent": {"value": False},
        "isForRent": {"value": True},
        "isComingSoon": {"value": False},
        "isAuction": {"value": False},
        "isAllHomes": {"value": True},
    }
    if is_room:
        filter_state["isRoomForRent"] = {"value": True}
    if not is_entire_place:
        filter_state["isEntirePlaceForRent"] = {"value": False}
    return search(
        search_value=search_value,
        pagination=pagination,
        min_beds=min_beds,
        max_beds=max_beds,
        min_bathrooms=min_bathrooms,
        max_bathrooms=max_bathrooms,
        min_price=min_price,
        max_price=max_price,
        ne_lat=ne_lat,
        ne_long=ne_long,
        sw_lat=sw_lat,
        sw_long=sw_long,
        zoom_value=zoom_value,
        filter_state=filter_state,
        proxy_url=proxy_url,
        impersonate=impersonate,
        timeout=timeout,
        max_retries=max_retries,
    )


def sold(  # noqa: PLR0913, PLR0917 - mirrors Zillow's filter surface
    search_value: str = "",
    pagination: int = 1,
    min_beds: int | None = None,
    max_beds: int | None = None,
    min_bathrooms: int | None = None,
    max_bathrooms: int | None = None,
    min_price: int | None = None,
    max_price: int | None = None,
    ne_lat: float | None = None,
    ne_long: float | None = None,
    sw_lat: float | None = None,
    sw_long: float | None = None,
    zoom_value: int | None = None,
    proxy_url: str | None = None,
    impersonate: str = "chrome136",
    timeout: float = 30.0,
    max_retries: int = 1,
) -> dict[str, Any]:
    """Get listings that were sold.

    See :func:`for_sale` for argument descriptions.

    Raises:
        ZillowBlockedError: If Zillow responds with a non-2xx status.
    """
    filter_state = {
        "sortSelection": {"value": "globalrelevanceex"},
        "isNewConstruction": {"value": False},
        "isForSaleForeclosure": {"value": False},
        "isForSaleByOwner": {"value": False},
        "isForSaleByAgent": {"value": False},
        "isForRent": {"value": False},
        "isComingSoon": {"value": False},
        "isAuction": {"value": False},
        "isAllHomes": {"value": True},
        "isRecentlySold": {"value": True},
    }
    return search(
        search_value=search_value,
        pagination=pagination,
        min_beds=min_beds,
        max_beds=max_beds,
        min_bathrooms=min_bathrooms,
        max_bathrooms=max_bathrooms,
        min_price=min_price,
        max_price=max_price,
        ne_lat=ne_lat,
        ne_long=ne_long,
        sw_lat=sw_lat,
        sw_long=sw_long,
        zoom_value=zoom_value,
        filter_state=filter_state,
        proxy_url=proxy_url,
        impersonate=impersonate,
        timeout=timeout,
        max_retries=max_retries,
    )


def search(  # noqa: PLR0913, PLR0917 - mirrors Zillow's filter surface
    search_value: str = "",
    pagination: int = 1,
    min_beds: int | None = None,
    max_beds: int | None = None,
    min_bathrooms: int | None = None,
    max_bathrooms: int | None = None,
    min_price: int | None = None,
    max_price: int | None = None,
    ne_lat: float | None = None,
    ne_long: float | None = None,
    sw_lat: float | None = None,
    sw_long: float | None = None,
    zoom_value: int | None = None,
    filter_state: dict[str, Any] | None = None,
    proxy_url: str | None = None,
    impersonate: str = "chrome136",
    timeout: float = 30.0,
    max_retries: int = 1,
) -> dict[str, Any]:
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
        impersonate: curl_cffi TLS impersonation profile (default "chrome136").
        timeout: Request timeout in seconds (default 30).
        max_retries: Retries on 5xx / network errors (default 1).

    Returns:
        dict[str, Any]: listing of properties in JSON format

    Raises:
        ZillowBlockedError: If Zillow responds with a non-2xx status.
    """
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
    data = _do_search_request(
        input_data=input_data,
        proxy_url=proxy_url,
        impersonate=impersonate,
        timeout=timeout,
        max_retries=max_retries,
    )
    return data.get("cat1", {}).get("searchResults", {})


def iter_search(
    search_fn=None,
    max_items: int | None = None,
    normalize: bool = False,
    **kwargs: Any,
):
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
        dict[str, Any]: one listing from mapResults.

    Example:
        >>> for listing in pyzill.iter_search(
        ...     pyzill.for_sale, search_value="Schenectady NY"
        ... ):
        ...     print(listing["address"])
    """
    if search_fn is None:
        search_fn = for_sale
    result = search_fn(**kwargs)
    map_results = result.get("mapResults", [])
    for i, listing in enumerate(map_results):
        if max_items is not None and i >= max_items:
            return
        yield normalize_listing(listing) if normalize else listing


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
