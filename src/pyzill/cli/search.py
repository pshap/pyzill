"""Search commands plus the shared search options/validation they and db sync use."""

import inspect
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, cast

import typer

from pyzill import (
    for_rent,
    for_sale,
    is_building_result,
    normalize_listing,
    normalize_listings,
    sold,
)
from pyzill.bbox import BBox
from pyzill.cli.console import (
    context,
    debug,
    error,
    network_errors,
    progress,
    warn,
    write_output,
)
from pyzill.cli.options import (
    East,
    FormatOpt,
    IsEntirePlace,
    IsRoom,
    MaxBaths,
    MaxBeds,
    MaxDepth,
    MaxPrice,
    MaxRequests,
    MinBaths,
    MinBeds,
    MinPrice,
    North,
    OutputOpt,
    Query,
    Raw,
    South,
    Tile,
    West,
    Zoom,
)
from pyzill.cli.output import OutputFormat
from pyzill.geocode import geocode
from pyzill.tiling import RESULT_CAP, tiled_search

search_app = typer.Typer(name="search", help="Search Zillow listings")
_ZIPCODE_LENGTH = 5


@dataclass(frozen=True, slots=True)
class SearchFilters:
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
class SearchOptions:
    query: str
    filters: SearchFilters
    is_room: bool = False
    is_entire_place: bool = False


def search_filter_options(  # noqa: PLR0913
    north: North = None,
    south: South = None,
    east: East = None,
    west: West = None,
    zoom: Zoom = None,
    min_beds: MinBeds = None,
    max_beds: MaxBeds = None,
    min_bathrooms: MinBaths = None,
    max_bathrooms: MaxBaths = None,
    min_price: MinPrice = None,
    max_price: MaxPrice = None,
    tile: Tile = False,
    max_depth: MaxDepth = 4,
    max_requests: MaxRequests = 50,
) -> SearchFilters:
    """Build shared filter options used by search variants and database sync."""
    return SearchFilters(
        north=north,
        south=south,
        east=east,
        west=west,
        zoom=zoom,
        min_beds=min_beds,
        max_beds=max_beds,
        min_bathrooms=min_bathrooms,
        max_bathrooms=max_bathrooms,
        min_price=min_price,
        max_price=max_price,
        tile=tile,
        max_depth=max_depth,
        max_requests=max_requests,
    )


def build_bbox(north: float | None, south: float | None, east: float | None, west: float | None) -> BBox | None:
    values = {"north": north, "south": south, "east": east, "west": west}
    missing = [name for name, value in values.items() if value is None]
    if len(missing) == len(values):
        return None
    if missing:
        flags = ", ".join(f"--{name}" for name in missing)
        raise ValueError(f"A bounding box needs all four of --north/--south/--east/--west (missing: {flags})")
    try:
        return BBox(
            north=cast(float, north),
            south=cast(float, south),
            east=cast(float, east),
            west=cast(float, west),
        )
    except ValueError as exc:
        raise ValueError(f"Invalid bounding box: {exc}") from exc


def check_range(name: str, low: int | None, high: int | None) -> None:
    if low is not None and high is not None and low > high:
        raise ValueError(f"--min-{name} ({low}) is greater than --max-{name} ({high})")


def _cli_build_bbox(
    north: float | None,
    south: float | None,
    east: float | None,
    west: float | None,
) -> BBox | None:
    """Build a BBox from the four CLI options (all four or none)."""
    try:
        return build_bbox(north, south, east, west)
    except ValueError as exc:
        error(str(exc), code=2)


def _cli_check_range(name: str, low: int | None, high: int | None) -> None:
    """Fail early if a min option is larger than its max option."""
    try:
        check_range(name, low, high)
    except ValueError as exc:
        error(str(exc), code=2)


def _usable_row_count(rows: list[Any], *, zipcode: str | None = None) -> int:
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
    """
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        candidates = [data[key] or [] for key in ("mapResults", "listResults") if key in data]
        if candidates:
            return max(candidates, key=lambda rows: _usable_row_count(rows, zipcode=zipcode))
        for key in ("cat1", "cat2", "searchResults"):
            if key in data:
                # It's a response envelope: honor its (possibly empty) contents
                # rather than treating the envelope itself as a listing.
                return extract_listings(data[key], zipcode=zipcode)
        return [data] if wrap_single else []
    error("Input must be a JSON object or array", code=2)
    return []  # unreachable; error() exits


def query_zipcode(query: str) -> str | None:
    zipcode = query.strip()
    return zipcode if len(zipcode) == _ZIPCODE_LENGTH and zipcode.isascii() and zipcode.isdecimal() else None


def search_options(
    query: str,
    filters: SearchFilters,
    is_room: bool = False,
    is_entire_place: bool = False,
) -> SearchOptions:
    """Combine a query, shared search filters, and rental-only switches."""
    return SearchOptions(
        query=query,
        filters=filters,
        is_room=is_room,
        is_entire_place=is_entire_place,
    )


_FILTER_OPTION_NAMES = tuple(inspect.signature(search_filter_options).parameters)


def search_options_from_args(args: Mapping[str, Any]) -> SearchOptions:
    """Build search options from a command's own arguments.

    ``args`` is the calling command's ``locals()``. The filter values are the
    parameters of :func:`search_filter_options`, so a command declares them
    once and a new filter only has to be added to the command signatures.
    ``is_room``/``is_entire_place`` are rental-only switches that the sale
    and sold commands do not have.
    """
    filters = search_filter_options(**{name: args[name] for name in _FILTER_OPTION_NAMES})
    return search_options(
        args["query"],
        filters,
        args.get("is_room", False),
        args.get("is_entire_place", False),
    )


def fetch_and_normalize(
    search_fn: Callable[..., dict[str, Any]],
    options: SearchOptions,
    *,
    raw: bool,
    metadata: dict[str, Any] | None = None,
) -> Any:
    """Fetch from Zillow and optionally normalize listings.

    Returns raw response if raw=True, else normalized list of listings.
    Reused by run_search and db sync.
    """
    if options.is_room and options.is_entire_place:
        error("--room and --entire-place can't be used together", code=2)

    filters = options.filters
    _cli_check_range("beds", filters.min_beds, filters.max_beds)
    _cli_check_range("bathrooms", filters.min_bathrooms, filters.max_bathrooms)
    _cli_check_range("price", filters.min_price, filters.max_price)

    bbox = _cli_build_bbox(filters.north, filters.south, filters.east, filters.west)
    http_kwargs = context().http_kwargs
    if bbox is None:
        # No explicit viewport: resolve the query text to one via Nominatim so
        # we never silently fall back to a continental-US search.
        debug(f"Geocoding {options.query!r}")
        try:
            geo = geocode(options.query, proxy_url=http_kwargs.get("proxy_url"))
        except ValueError as e:
            error(str(e), code=1)
        debug(f"Geocoded {options.query!r} -> {geo.display_name}")
        bbox = geo.bbox
        if geo.synthetic_bbox:
            warn(
                "Nominatim returned a point-like viewport; pyzill expanded it synthetically, "
                "which may not cover the full area"
            )
    debug(f"{search_fn.__name__}: query={options.query!r} bbox={bbox}")
    if metadata is not None:
        metadata["bbox"] = bbox.to_map_bounds()
    search_kwargs = {
        "search_value": options.query,
        "zoom_value": filters.zoom,
        "min_beds": filters.min_beds,
        "max_beds": filters.max_beds,
        "min_bathrooms": filters.min_bathrooms,
        "max_bathrooms": filters.max_bathrooms,
        "min_price": filters.min_price,
        "max_price": filters.max_price,
        **http_kwargs,
    }
    if search_fn is for_rent:
        search_kwargs.update(is_room=options.is_room, is_entire_place=options.is_entire_place)

    capped_at_depth = False

    def on_cap() -> None:
        nonlocal capped_at_depth
        capped_at_depth = True
        warn(
            f"A tile still returned {RESULT_CAP} listings at --max-depth {filters.max_depth}; results may be truncated"
        )

    with network_errors("Search failed"):
        if filters.tile:
            result = tiled_search(
                search_fn,
                bbox=bbox,
                max_depth=filters.max_depth,
                max_requests=filters.max_requests,
                on_cap=on_cap,
                on_progress=lambda used, total: progress(f"tile {used}:", f"{total} listings so far"),
                on_budget=lambda: warn(
                    f"request budget of {filters.max_requests} exhausted; returning partial results "
                    "(raise --max-requests or lower --max-depth for more)"
                ),
                **search_kwargs,
            )
        else:
            result = search_fn(bbox=bbox, **search_kwargs)
            if len(result.get("mapResults", []) or []) >= RESULT_CAP:
                warn(
                    f"Zillow returned {RESULT_CAP} listings, its per-search cap; "
                    "use --tile to subdivide the bounding box"
                )

    result["truncated"] = bool(
        capped_at_depth
        or result.get("budget_exhausted")
        or len(result.get("mapResults", []) or []) >= RESULT_CAP
        or len(result.get("listResults", []) or []) >= RESULT_CAP
    )
    if raw:
        return result
    zipcode = query_zipcode(options.query)
    listings = normalize_listings(extract_listings(result, zipcode=zipcode))
    return [listing for listing in listings if listing["zipcode"] == zipcode] if zipcode else listings


def run_search(
    search_fn: Callable[..., dict[str, Any]],
    *,
    options: SearchOptions,
    raw: bool,
    output_format: OutputFormat,
    output: str | None,
) -> None:
    """Everything the sale/rent/sold commands have in common."""
    if raw and output_format is not OutputFormat.json:
        error("--raw only supports JSON format", code=2)

    data = fetch_and_normalize(
        search_fn,
        options,
        raw=raw,
    )
    write_output(data, output_format, output)


def _search_command(search_fn: Callable[..., dict[str, Any]], args: Mapping[str, Any]) -> None:
    """Run one of the sale/rent/sold commands.

    ``args`` is the calling command's ``locals()``, so the shared options are
    collected without repeating them at every call site.
    """
    options = search_options_from_args(args)
    run_search(
        search_fn,
        options=options,
        raw=args["raw"],
        output_format=args["output_format"],
        output=args["output"],
    )


@search_app.command("sale")
def search_sale(  # noqa: PLR0913
    query: Query,
    north: North = None,
    south: South = None,
    east: East = None,
    west: West = None,
    zoom: Zoom = None,
    min_beds: MinBeds = None,
    max_beds: MaxBeds = None,
    min_bathrooms: MinBaths = None,
    max_bathrooms: MaxBaths = None,
    min_price: MinPrice = None,
    max_price: MaxPrice = None,
    tile: Tile = False,
    max_depth: MaxDepth = 4,
    max_requests: MaxRequests = 50,
    raw: Raw = False,
    output_format: FormatOpt = OutputFormat.json,
    output: OutputOpt = None,
) -> None:
    """Search for-sale listings."""
    _search_command(for_sale, locals())


@search_app.command("rent")
def search_rent(  # noqa: PLR0913
    query: Query,
    north: North = None,
    south: South = None,
    east: East = None,
    west: West = None,
    zoom: Zoom = None,
    min_beds: MinBeds = None,
    max_beds: MaxBeds = None,
    min_bathrooms: MinBaths = None,
    max_bathrooms: MaxBaths = None,
    min_price: MinPrice = None,
    max_price: MaxPrice = None,
    tile: Tile = False,
    max_depth: MaxDepth = 4,
    max_requests: MaxRequests = 50,
    is_room: IsRoom = False,
    is_entire_place: IsEntirePlace = False,
    raw: Raw = False,
    output_format: FormatOpt = OutputFormat.json,
    output: OutputOpt = None,
) -> None:
    """Search for-rent listings."""
    _search_command(for_rent, locals())


@search_app.command("sold")
def search_sold(  # noqa: PLR0913
    query: Query,
    north: North = None,
    south: South = None,
    east: East = None,
    west: West = None,
    zoom: Zoom = None,
    min_beds: MinBeds = None,
    max_beds: MaxBeds = None,
    min_bathrooms: MinBaths = None,
    max_bathrooms: MaxBaths = None,
    min_price: MinPrice = None,
    max_price: MaxPrice = None,
    tile: Tile = False,
    max_depth: MaxDepth = 4,
    max_requests: MaxRequests = 50,
    raw: Raw = False,
    output_format: FormatOpt = OutputFormat.json,
    output: OutputOpt = None,
) -> None:
    """Search sold listings."""
    _search_command(sold, locals())
