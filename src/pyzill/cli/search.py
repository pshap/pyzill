"""Search commands plus the shared search options/validation they and db sync use."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast

import typer

from pyzill import for_rent, for_sale, normalize_listings, sold
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
from pyzill.search import tiled_search

_RESULT_CAP = 500

search_app = typer.Typer(name="search", help="Search Zillow listings")


@dataclass(frozen=True, slots=True)
class SearchOptions:
    query: str
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
    is_room: bool = False
    is_entire_place: bool = False


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


def extract_listings(data: Any, *, wrap_single: bool = False) -> list[Any]:
    """Pull the listing list out of a Zillow response (or a bare list).

    Digs through nested response containers (``cat1`` / ``cat2`` /
    ``searchResults``) so a full saved search body works, not just the
    unwrapped ``mapResults`` shape. With wrap_single=True, a dict that has no
    results key is treated as one listing; otherwise it yields an empty list.
    """
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("mapResults", "listResults"):
            if key in data:
                return data[key] or []
        for key in ("cat1", "cat2", "searchResults"):
            if key in data:
                # It's a response envelope: honor its (possibly empty) contents
                # rather than treating the envelope itself as a listing.
                return extract_listings(data[key])
        return [data] if wrap_single else []
    error("Input must be a JSON object or array", code=2)
    return []  # unreachable; error() exits


def search_options(  # noqa: PLR0913, PLR0917
    query: str,
    north: float | None,
    south: float | None,
    east: float | None,
    west: float | None,
    zoom: int | None,
    min_beds: int | None,
    max_beds: int | None,
    min_bathrooms: int | None,
    max_bathrooms: int | None,
    min_price: int | None,
    max_price: int | None,
    tile: bool,
    max_depth: int,
    max_requests: int,
    is_room: bool = False,
    is_entire_place: bool = False,
) -> SearchOptions:
    """Build SearchOptions from CLI params (positional to prevent field-swap bugs)."""
    return SearchOptions(
        query=query,
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
        is_room=is_room,
        is_entire_place=is_entire_place,
    )


def fetch_and_normalize(
    search_fn: Callable[..., dict[str, Any]],
    options: SearchOptions,
    *,
    raw: bool,
) -> Any:
    """Fetch from Zillow and optionally normalize listings.

    Returns raw response if raw=True, else normalized list of listings.
    Reused by run_search and db sync.
    """
    if options.is_room and options.is_entire_place:
        error("--room and --entire-place can't be used together", code=2)

    _cli_check_range("beds", options.min_beds, options.max_beds)
    _cli_check_range("bathrooms", options.min_bathrooms, options.max_bathrooms)
    _cli_check_range("price", options.min_price, options.max_price)

    bbox = _cli_build_bbox(options.north, options.south, options.east, options.west)
    http_kwargs = context().http_kwargs
    if bbox is None:
        # No explicit viewport: resolve the query text to one via Nominatim so
        # we never silently fall back to a continental-US search.
        debug(f"Geocoding {options.query!r}")
        try:
            geo = geocode(options.query)
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
    search_kwargs = {
        "search_value": options.query,
        "zoom_value": options.zoom,
        "min_beds": options.min_beds,
        "max_beds": options.max_beds,
        "min_bathrooms": options.min_bathrooms,
        "max_bathrooms": options.max_bathrooms,
        "min_price": options.min_price,
        "max_price": options.max_price,
        **http_kwargs,
    }
    if search_fn is for_rent:
        search_kwargs.update(is_room=options.is_room, is_entire_place=options.is_entire_place)

    with network_errors("Search failed"):
        if options.tile:
            result = tiled_search(
                search_fn,
                bbox=bbox,
                max_depth=options.max_depth,
                max_requests=options.max_requests,
                on_cap=lambda: warn(
                    f"A tile still returned {_RESULT_CAP} listings at --max-depth {options.max_depth}; "
                    "results may be truncated"
                ),
                on_progress=lambda used, total: progress(f"tile {used}:", f"{total} listings so far"),
                **search_kwargs,
            )
        else:
            result = search_fn(bbox=bbox, **search_kwargs)
            if len(result.get("mapResults", []) or []) >= _RESULT_CAP:
                warn(
                    f"Zillow returned {_RESULT_CAP} listings, its per-search cap; "
                    "use --tile to subdivide the bounding box"
                )

    return result if raw else normalize_listings(extract_listings(result))


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


@search_app.command("sale")
def search_sale(  # noqa: PLR0913, PLR0917
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
    options = search_options(
        query,
        north,
        south,
        east,
        west,
        zoom,
        min_beds,
        max_beds,
        min_bathrooms,
        max_bathrooms,
        min_price,
        max_price,
        tile,
        max_depth,
        max_requests,
    )
    run_search(for_sale, options=options, raw=raw, output_format=output_format, output=output)


@search_app.command("rent")
def search_rent(  # noqa: PLR0913, PLR0917
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
    options = search_options(
        query,
        north,
        south,
        east,
        west,
        zoom,
        min_beds,
        max_beds,
        min_bathrooms,
        max_bathrooms,
        min_price,
        max_price,
        tile,
        max_depth,
        max_requests,
        is_room,
        is_entire_place,
    )
    run_search(for_rent, options=options, raw=raw, output_format=output_format, output=output)


@search_app.command("sold")
def search_sold(  # noqa: PLR0913, PLR0917
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
    options = search_options(
        query,
        north,
        south,
        east,
        west,
        zoom,
        min_beds,
        max_beds,
        min_bathrooms,
        max_bathrooms,
        min_price,
        max_price,
        tile,
        max_depth,
        max_requests,
    )
    run_search(sold, options=options, raw=raw, output_format=output_format, output=output)
