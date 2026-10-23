"""Tiling, deduplication, and breadth-first recursion over capped searches."""

import json
import warnings
from collections import deque
from collections.abc import Callable, Mapping
from typing import Any

from pyzill.bbox import CONTINENTAL_US, BBox
from pyzill.exceptions import ZillowError
from pyzill.search_request import RentParams

__all__ = ["RESULT_CAP", "TiledSearchError", "TiledSearchParams", "split_bbox", "tiled_search"]

# Zillow's per-search result cap. A search that returns this many rows was
# truncated by Zillow, which is why tiling subdivides the viewport.
RESULT_CAP = 500
_DEFAULT_MAX_REQUESTS = 50


class TiledSearchParams(RentParams, total=False):
    """Every keyword a tiled search accepts: the search parameters plus its bounds."""

    max_depth: int
    max_requests: int
    on_cap: Callable[[], None] | None
    on_progress: Callable[[int, int], None] | None
    on_budget: Callable[[], None] | None


def _listing_identity(listing: Mapping[str, Any]) -> str:
    identity = listing.get("zpid") or listing.get("detailUrl")
    if identity is None:
        identity = json.dumps(listing, sort_keys=True, default=str)
    return str(identity)


def _budget_exhausted(max_requests: int, on_budget: Callable[[], None] | None) -> None:
    if on_budget is not None:
        on_budget()
        return
    warnings.warn(
        f"request budget of {max_requests} exhausted; returning partial results "
        "(raise max_requests or lower max_depth for more)",
        UserWarning,
        stacklevel=3,
    )


def _merge_response(
    combined: dict[str, dict[str, Any]],
    response: Mapping[str, Any],
) -> tuple[list[Any], list[Any]]:
    """Add one tile's listings to the union and return its two result lists."""
    map_results = response.get("mapResults", []) or []
    list_results = response.get("listResults", []) or []
    for result_key in combined:
        for listing in response.get(result_key, []) or []:
            if isinstance(listing, Mapping):
                combined[result_key].setdefault(_listing_identity(listing), dict(listing))
    return map_results, list_results


def _payload(combined: Mapping[str, Mapping[str, dict[str, Any]]], *, budget_exhausted: bool) -> dict[str, Any]:
    """Assemble the response-shaped result of the tiles completed so far."""
    return {
        "mapResults": list(combined["mapResults"].values()),
        "listResults": list(combined["listResults"].values()),
        "budget_exhausted": budget_exhausted,
    }


class TiledSearchError(ZillowError):
    """A tile failed after other tiles had already returned listings.

    The completed tiles are not thrown away with the failing one: they are on
    ``partial`` in the same shape :func:`tiled_search` returns. The error's
    code and status mirror the failure that stopped the run, so callers that
    map a Zillow failure still see, for example, a blocked request as blocked.
    """

    def __init__(self, cause: Exception, partial: dict[str, Any], *, requests_used: int, tile: BBox) -> None:
        self.cause = cause
        self.partial = partial
        self.requests_used = requests_used
        self.tile = tile
        self.code = getattr(cause, "code", "other")
        self.retryable = getattr(cause, "retryable", False)
        status_code = getattr(cause, "status_code", None)
        if status_code is not None:
            self.status_code = status_code
        super().__init__(
            f"Tiled search failed after {requests_used} completed request(s): {cause}. "
            "The listings from the completed tiles are on .partial."
        )


def _tile_is_capped(map_results: list[Any], list_results: list[Any]) -> bool:
    """Whether either result list reached Zillow's cap for this tile."""
    return len(map_results) >= RESULT_CAP or len(list_results) >= RESULT_CAP


def _request_tile(
    search_fn: Callable[..., dict[str, Any]],
    tile: BBox,
    kwargs: Mapping[str, Any],
    combined: dict[str, dict[str, Any]],
    requests_used: int,
) -> dict[str, Any]:
    """Run one tile, keeping the completed tiles' listings if it fails."""
    try:
        return search_fn(**kwargs, bbox=tile)
    except Exception as exc:
        # The tiles that already answered are real listings; losing them
        # because a later tile failed would silently shrink the snapshot.
        raise TiledSearchError(
            exc,
            _payload(combined, budget_exhausted=False),
            requests_used=requests_used,
            tile=tile,
        ) from exc


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
    if max_depth < 0:
        raise ValueError("max_depth must be non-negative")
    if max_requests < 1:
        raise ValueError("max_requests must be at least 1")

    combined: dict[str, dict[str, Any]] = {"mapResults": {}, "listResults": {}}
    requests_used = 0
    budget_exhausted = False

    queue: deque[tuple[BBox, int]] = deque([(bbox, 0)])
    while queue:
        if requests_used >= max_requests:
            budget_exhausted = True
            _budget_exhausted(max_requests, on_budget)
            break
        tile, depth = queue.popleft()
        response = _request_tile(search_fn, tile, kwargs, combined, requests_used)
        requests_used += 1
        map_results, list_results = _merge_response(combined, response)
        if on_progress is not None:
            on_progress(requests_used, len(combined["mapResults"]))

        # A tile is cut off when *either* result list reached the cap: Zillow
        # caps each list separately, and a tile whose map results are empty
        # but whose sidebar list is full is truncated just as much.
        if not _tile_is_capped(map_results, list_results):
            continue
        if depth < max_depth:
            for child in tile.split():
                queue.append((child, depth + 1))
        elif on_cap is not None:
            on_cap()

    return _payload(combined, budget_exhausted=budget_exhausted)


def split_bbox(bbox: BBox = CONTINENTAL_US) -> tuple[BBox, BBox, BBox, BBox]:
    return bbox.split()
