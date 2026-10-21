"""Application workflows: the operations the CLI and library users share.

This layer sits above the client and the database. It holds the search
request objects, the viewport resolution, the listing extraction, and the
sync/import pipelines.

Nothing here prints and nothing here exits the process: the workflows
return data plus structured notices, and the caller (the CLI, or a library
user) decides what to report and how to react.
"""

import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any, cast

from pyzill._kinds import Kind
from pyzill.bbox import BBox
from pyzill.client import ZillowClient, get_default_client
from pyzill.db import SyncStats, begin_sync_run, comparable_run_had_results, upsert_listings
from pyzill.db.runs import utc_now
from pyzill.geocode import GeoResult
from pyzill.normalize import Listing, is_building_result, normalize_listing
from pyzill.tiling import RESULT_CAP, tiled_search

__all__ = [
    "BoundingBoxError",
    "Notice",
    "RangeError",
    "SearchFilters",
    "SearchRequest",
    "SearchResult",
    "SyncResult",
    "Viewport",
    "build_viewport",
    "check_range",
    "extract_listings",
    "fetch_listings",
    "import_rows",
    "prepare_import",
    "query_zipcode",
    "resolve_viewport",
    "sync_to_db",
    "usable_row_count",
]

_ZIPCODE_LENGTH = 5

# The geocoder used to resolve a place name into a viewport. It is injected
# so the caller decides which geocoder (and which proxy) is used.
Geocoder = Callable[..., GeoResult]


class BoundingBoxError(ValueError):
    """The viewport bounds are incomplete or invalid.

    ``missing`` names the bounds that were not given, so the caller can
    render a message in its own vocabulary.
    """

    def __init__(self, message: str, missing: Sequence[str] = ()) -> None:
        self.missing = tuple(missing)
        super().__init__(message)


class RangeError(ValueError):
    """A minimum filter is larger than its matching maximum."""

    def __init__(self, name: str, low: int, high: int) -> None:
        self.name = name
        self.low = low
        self.high = high
        super().__init__(f"{name} minimum {low} is greater than maximum {high}")


@dataclass(frozen=True, slots=True)
class Notice:
    """A non-fatal condition the caller may report to the user.

    The workflow layer reports conditions as structured notices instead of
    messages, so the wording belongs to the caller (the CLI) and not to the
    library.
    """

    code: str
    values: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SearchFilters:
    """The filters of one search (what to look for, not how to send it)."""

    north: float | None = None
    south: float | None = None
    east: float | None = None
    west: float | None = None
    zoom: int | None = None
    min_beds: int | None = None
    max_beds: int | None = None
    min_bathrooms: int | None = None
    max_bathrooms: int | None = None
    min_price: int | None = None
    max_price: int | None = None
    tile: bool = False
    max_depth: int = 4
    max_requests: int = 50


@dataclass(frozen=True, slots=True)
class SearchRequest:
    """One search: the query, its filters, and the transport to use."""

    query: str
    filters: SearchFilters = field(default_factory=SearchFilters)
    is_room: bool = False
    is_entire_place: bool = False
    transport: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Viewport:
    """The resolved viewport of a search."""

    bbox: BBox
    display_name: str | None = None
    notices: list[Notice] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class SearchResult:
    """The outcome of one search."""

    raw: dict[str, Any]
    listings: list[Listing]
    raw_listings: list[Mapping[str, Any]]
    truncated: bool
    bbox: BBox
    zipcode: str | None = None
    notices: list[Notice] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class SyncResult:
    """The outcome of a sync or an import."""

    stats: SyncStats
    rows: Sequence[Mapping[str, Any]] = field(default_factory=list)
    truncated: bool = False
    run_id: int = 0
    notices: list[Notice] = field(default_factory=list)


def query_zipcode(query: str) -> str | None:
    """Return the query's zipcode, if it is one."""
    zipcode = query.strip()
    return zipcode if len(zipcode) == _ZIPCODE_LENGTH and zipcode.isascii() and zipcode.isdecimal() else None


def usable_row_count(rows: list[Any], *, zipcode: str | None = None) -> int:
    """Count rows that normalize to a real listing with an integer zpid.

    ``isBuilding`` cards are the common case, but Zillow sometimes stamps a
    building card with a non-numeric ``zpid`` (a ``buildingId`` like
    ``"47.61503--122.34274"``), so the check is on the normalized zpid
    rather than the flag alone.
    """
    count = 0
    for row in rows:
        if not isinstance(row, dict) or is_building_result(row):
            continue
        normalized = normalize_listing(row)
        if (
            normalized is not None
            and isinstance(normalized["zpid"], int)
            and (zipcode is None or normalized["zipcode"] == zipcode)
        ):
            count += 1
    return count


def extract_listings(data: Any, *, wrap_single: bool = False, zipcode: str | None = None) -> list[Any]:
    """Pull the listing list out of a Zillow response (or a bare list).

    Digs through nested response containers (``cat1`` / ``cat2`` /
    ``searchResults``) so a full saved search body works, not just the
    unwrapped ``mapResults`` shape. With wrap_single=True, a dict that has no
    results key is treated as one listing; otherwise it yields an empty list.

    When both ``mapResults`` and ``listResults`` are present, the list with
    real listings wins. Zillow's ``mapResults`` often contain only
    building-level cards (``isBuilding``, no zpid), while ``listResults``
    hold the individual units, so returning the first key blindly would drop
    every usable row in those areas.

    Raises:
        ValueError: If the payload is neither an object nor an array.
    """
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        candidates = [data[key] or [] for key in ("mapResults", "listResults") if key in data]
        if candidates:
            return max(candidates, key=lambda rows: usable_row_count(rows, zipcode=zipcode))
        for key in ("cat1", "cat2", "searchResults"):
            if key in data:
                # It's a response envelope: honor its (possibly empty) contents
                # rather than treating the envelope itself as a listing.
                return extract_listings(data[key], zipcode=zipcode)
        return [data] if wrap_single else []
    raise ValueError("Input must be a JSON object or array")


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
    values = {
        "north": filters.north,
        "south": filters.south,
        "east": filters.east,
        "west": filters.west,
    }
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


def _search_function(client: ZillowClient, kind: Kind) -> Callable[..., dict[str, Any]]:
    """Return the client's search call for one listing kind."""
    if kind == "sale":
        return client.search_sale
    if kind == "rent":
        return client.search_rent
    if kind == "sold":
        return client.search_sold
    raise ValueError(f"Unknown listing kind: {kind!r}")


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

    search_kwargs: dict[str, Any] = {
        "search_value": request.query,
        "zoom_value": request.filters.zoom,
        "min_beds": request.filters.min_beds,
        "max_beds": request.filters.max_beds,
        "min_bathrooms": request.filters.min_bathrooms,
        "max_bathrooms": request.filters.max_bathrooms,
        "min_price": request.filters.min_price,
        "max_price": request.filters.max_price,
        **dict(request.transport),
    }
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
    else:
        result = search_fn(bbox=bbox, **search_kwargs)
        if len(result.get("mapResults", []) or []) >= RESULT_CAP:
            notices.append(Notice("result_cap", {"cap": RESULT_CAP}))

    truncated = bool(
        capped_at_depth
        or result.get("budget_exhausted")
        or len(result.get("mapResults", []) or []) >= RESULT_CAP
        or len(result.get("listResults", []) or []) >= RESULT_CAP
    )

    zipcode = query_zipcode(request.query)
    listings: list[Listing] = []
    raw_listings: list[Mapping[str, Any]] = []
    for row in extract_listings(result, zipcode=zipcode):
        if not isinstance(row, dict) or is_building_result(row):
            continue
        normalized = normalize_listing(row)
        if normalized is None:
            continue
        if zipcode is not None and normalized["zipcode"] != zipcode:
            continue
        listings.append(normalized)
        raw_listings.append(row)

    return SearchResult(
        raw=result,
        listings=listings,
        raw_listings=raw_listings,
        truncated=truncated,
        bbox=bbox,
        zipcode=zipcode,
        notices=notices,
    )


def sync_to_db(  # noqa: PLR0913
    conn: sqlite3.Connection,
    kind: Kind,
    request: SearchRequest,
    *,
    result: SearchResult | None = None,
    client: ZillowClient | None = None,
    bbox: BBox | None = None,
    geocode: Geocoder | None = None,
    confirm_empty: bool = False,
    on_progress: Callable[[int, int], None] | None = None,
) -> SyncResult:
    """Search Zillow and upsert the listings into the database.

    An empty result for a search that previously matched is recorded as
    truncated (unless ``confirm_empty`` is set), because a zero-row answer
    is more likely a soft block or a transient glitch than a search whose
    listings all vanished.

    Args:
        conn: The database connection to write to.
        kind: The listing kind that was searched for.
        request: The query, its filters, and the transport to use.
        result: An already fetched result; fetched here when omitted.
        client: The client to search through (default: the shared client).
        bbox: An already resolved viewport.
        geocode: The geocoder used when the request carries no bounds.
        confirm_empty: Treat an empty result as a complete search.
        on_progress: Progress callback for a tiled search.
    """
    if result is None:
        result = fetch_listings(
            kind,
            request,
            client=client,
            bbox=bbox,
            geocode=geocode,
            on_progress=on_progress,
        )
    notices = list(result.notices)
    truncated = result.truncated
    started_utc = utc_now()
    filter_values = asdict(request.filters)
    filter_values.update(is_room=request.is_room, is_entire_place=request.is_entire_place)
    bbox_bounds = result.bbox.to_map_bounds()
    if (
        not result.listings
        and not confirm_empty
        and comparable_run_had_results(
            conn,
            kind=kind,
            query=request.query,
            bbox=bbox_bounds,
            filters=filter_values,
        )
    ):
        truncated = True
        notices.append(Notice("suspect_empty"))
    run_id = begin_sync_run(
        conn,
        kind=kind,
        query=request.query,
        bbox=bbox_bounds,
        filters=filter_values,
        started_utc=started_utc,
    )
    stats = upsert_listings(
        conn,
        result.listings,
        raw_rows=result.raw_listings,
        seen_utc=started_utc,
        kind=kind,
        run_id=run_id,
        truncated=truncated,
    )
    return SyncResult(
        stats=stats,
        rows=result.listings,
        truncated=truncated,
        run_id=run_id,
        notices=notices,
    )


def _looks_normalized(row: Any) -> bool:
    """True when a row uses normalized field names (so normalizing would null them)."""
    normalized_keys = frozenset(Listing.__annotations__) | {"first_seen_utc", "last_seen_utc"}
    return isinstance(row, dict) and set(row) <= normalized_keys


def prepare_import(data: Any) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    """Split an imported payload into storable rows and their raw rows.

    Rows that already use normalized field names are kept as-is so a
    re-import of ``db query`` output does not null out street/city/etc.;
    raw rows are normalized. Building cards and rows without a zpid are
    dropped.
    """
    rows = extract_listings(data, wrap_single=True)
    imported: list[Mapping[str, Any]] = []
    raw_imported: list[Mapping[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict) or is_building_result(row):
            continue
        normalized = row if _looks_normalized(row) else normalize_listing(row)
        if normalized is not None and normalized.get("zpid") is not None:
            imported.append(normalized)
            raw_imported.append(row)
    return imported, raw_imported


def import_rows(
    conn: sqlite3.Connection,
    rows: Sequence[Mapping[str, Any]],
    raw_rows: Sequence[Mapping[str, Any]],
    *,
    kind: Kind,
    seen_utc: str | None = None,
) -> SyncResult:
    """Upsert prepared import rows and report the statistics."""
    if not rows:
        return SyncResult(stats=SyncStats(added=0, updated=0, price_changes=0, skipped=0))
    stats = upsert_listings(
        conn,
        rows,
        seen_utc=seen_utc,
        kind=kind,
        raw_rows=raw_rows,
    )
    return SyncResult(stats=stats, rows=list(rows))
