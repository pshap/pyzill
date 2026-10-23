"""Running one search and reporting what came back.

This is the workflow the CLI's search commands and the database sync both
build on: it resolves the viewport, calls Zillow (optionally tiled), and
returns the real listings of the response.
"""

from collections.abc import Callable, Mapping
from dataclasses import asdict
from typing import Any

from pyzill._kinds import Kind
from pyzill.bbox import BBox
from pyzill.client import ZillowClient, get_default_client
from pyzill.tiling import RESULT_CAP, tiled_search
from pyzill.workflows.extract import query_zipcode, select_listings
from pyzill.workflows.model import (
    FILTER_PARAMS,
    Notice,
    SearchRequest,
    SearchResult,
)
from pyzill.workflows.viewport import Geocoder, resolve_viewport

__all__ = ["fetch_listings"]


def _search_function(client: ZillowClient, kind: Kind) -> Callable[..., dict[str, Any]]:
    """Return the client's search call for one listing kind."""
    if kind == "sale":
        return client.search_sale
    if kind == "rent":
        return client.search_rent
    if kind == "sold":
        return client.search_sold
    raise ValueError(f"Unknown listing kind: {kind!r}")


def _search_kwargs(request: SearchRequest) -> dict[str, Any]:
    """Build the keyword arguments of one search call.

    The arguments are derived from the request's filters through
    :data:`~pyzill.workflows.model.FILTER_PARAMS`, so a new filter reaches
    the search request by adding it to that table alone.
    """
    filters = asdict(request.filters)
    kwargs: dict[str, Any] = {"search_value": request.query}
    for filter_name, param_name in FILTER_PARAMS.items():
        kwargs[param_name] = filters[filter_name]
    kwargs.update(request.transport)
    return kwargs


def _hit_cap(result: Mapping[str, Any]) -> bool:
    """True when a single search's response was cut off by Zillow's result cap.

    Zillow caps one search at :data:`~pyzill.tiling.RESULT_CAP` rows, so a
    response that reaches the cap was truncated by Zillow. This applies to a
    single search only: a tiled run's response is the union of its tiles and
    is expected to exceed the cap.
    """
    return any(len(result.get(key, []) or []) >= RESULT_CAP for key in ("mapResults", "listResults"))


def fetch_listings(  # noqa: PLR0913 - one search entry point with its inputs
    kind: Kind,
    request: SearchRequest,
    *,
    client: ZillowClient | None = None,
    bbox: BBox | None = None,
    geocode: Geocoder | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> SearchResult:
    """Run one search and return its raw response and normalized listings.

    Args:
        kind: The listing kind to search for.
        request: The query, its filters, and the transport to use.
        client: The client to search through (default: the shared client).
        bbox: An already resolved viewport; resolved from the request (and
            the geocoder) when omitted.
        geocode: The geocoder used when the request carries no bounds.
        on_progress: Called with ``(requests_used, listings_so_far)`` while
            a tiled search runs.

    Returns:
        SearchResult: the raw response, the normalized listings, whether the
        result was truncated, the viewport, and any notices.
    """
    client = client or get_default_client()
    notices: list[Notice] = []
    if bbox is None:
        viewport = resolve_viewport(request, geocode=geocode)
        bbox = viewport.bbox
        notices.extend(viewport.notices)

    search_kwargs = _search_kwargs(request)
    if kind == "rent":
        search_kwargs["is_room"] = request.is_room
        search_kwargs["is_entire_place"] = request.is_entire_place

    search_fn = _search_function(client, kind)
    capped_at_depth = False

    def on_cap() -> None:
        nonlocal capped_at_depth
        capped_at_depth = True
        notices.append(
            Notice("tile_capped", {"cap": RESULT_CAP, "max_depth": request.filters.max_depth}),
        )

    def on_budget() -> None:
        notices.append(Notice("budget_exhausted", {"max_requests": request.filters.max_requests}))

    if request.filters.tile:
        result = tiled_search(
            search_fn,
            bbox=bbox,
            max_depth=request.filters.max_depth,
            max_requests=request.filters.max_requests,
            on_cap=on_cap,
            on_progress=on_progress,
            on_budget=on_budget,
            **search_kwargs,
        )
        # A tiled result is the union of every tile, so Zillow's per-search
        # cap cannot be applied to it: exceeding the cap is exactly what
        # tiling is for, and a union larger than the cap is a complete run.
        # The tiling loop reports the two ways a tiled run can be incomplete:
        # a tile that hit the cap and could not be subdivided any further,
        # and a run that ran out of its request budget.
        truncated = bool(capped_at_depth or result.get("budget_exhausted"))
    else:
        result = search_fn(bbox=bbox, **search_kwargs)
        truncated = _hit_cap(result)
        if len(result.get("mapResults", []) or []) >= RESULT_CAP:
            notices.append(Notice("result_cap", {"cap": RESULT_CAP}))

    zipcode = query_zipcode(request.query)
    _, usable = select_listings(result, wrap_single=False, zipcode=zipcode)
    listings = [normalized for _, normalized in usable]
    raw_listings = [row for row, _ in usable]

    return SearchResult(
        raw=result,
        listings=listings,
        raw_listings=raw_listings,
        truncated=truncated,
        bbox=bbox,
        zipcode=zipcode,
        notices=notices,
    )
