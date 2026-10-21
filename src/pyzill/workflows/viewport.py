"""Resolving the viewport of a search.

A search needs a viewport before it can run.  Explicit bounds win; without
them the query text is geocoded, so a search never silently falls back to a
continental-US viewport.
"""

from collections.abc import Callable
from typing import cast

from pyzill.bbox import BBox
from pyzill.geocode import GeoResult
from pyzill.workflows.model import (
    VIEWPORT_FILTERS,
    BoundingBoxError,
    Notice,
    RangeError,
    SearchFilters,
    SearchRequest,
    Viewport,
)

__all__ = ["Geocoder", "build_viewport", "check_range", "resolve_viewport"]


# The geocoder used to resolve a place name into a viewport. It is injected
# so the caller decides which geocoder (and which proxy) is used.
Geocoder = Callable[..., GeoResult]


def check_range(name: str, low: int | None, high: int | None) -> None:
    """Reject a minimum filter that is larger than its matching maximum.

    Raises:
        RangeError: If ``low`` is greater than ``high``.
    """
    if low is not None and high is not None and low > high:
        raise RangeError(name, low, high)


def build_viewport(filters: SearchFilters) -> BBox | None:
    """Return the viewport named by the four bounds, or None if none were given.

    Raises:
        BoundingBoxError: If only some of the bounds were given, or if the
            bounds do not describe a valid viewport.
    """
    values = {name: getattr(filters, name) for name in VIEWPORT_FILTERS}
    missing = [name for name, value in values.items() if value is None]
    if len(missing) == len(values):
        return None
    if missing:
        raise BoundingBoxError("incomplete viewport bounds", missing)
    try:
        return BBox(
            north=cast(float, values["north"]),
            south=cast(float, values["south"]),
            east=cast(float, values["east"]),
            west=cast(float, values["west"]),
        )
    except ValueError as exc:
        raise BoundingBoxError(f"Invalid bounding box: {exc}") from exc


def resolve_viewport(request: SearchRequest, *, geocode: Geocoder | None = None) -> Viewport:
    """Resolve the viewport of a search, geocoding the query when needed.

    Explicit bounds win. Without them the query text is geocoded, so a
    search never silently falls back to a continental-US viewport.

    Raises:
        BoundingBoxError: If the bounds are incomplete or invalid.
        ValueError: If the query must be geocoded but no geocoder was given,
            or if the geocoder failed.
    """
    bbox = build_viewport(request.filters)
    if bbox is not None:
        return Viewport(bbox=bbox)
    if geocode is None:
        raise ValueError("A viewport is required: pass explicit bounds or a geocodable query")
    geo = geocode(request.query, proxy_url=request.transport.get("proxy_url"))
    notices: list[Notice] = []
    if geo.synthetic_bbox:
        notices.append(Notice("synthetic_viewport"))
    return Viewport(bbox=geo.bbox, display_name=geo.display_name, notices=notices)
