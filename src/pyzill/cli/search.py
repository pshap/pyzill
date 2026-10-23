"""Search commands: Typer wiring only.

The workflow itself lives in :mod:`pyzill.workflows`; this module parses the
flags, calls the workflow, maps its errors to exit codes, and renders the
result.
"""

from collections.abc import Mapping
from dataclasses import fields
from typing import Any, NoReturn

import typer

from pyzill._kinds import Kind
from pyzill.cli.console import (
    context,
    debug,
    error,
    network_errors,
    progress,
    report_notices,
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
from pyzill.cli.safety import MAX_REQUESTS_CAP
from pyzill.geocode import GeocodeError, GeocodeUnavailableError, geocode
from pyzill.workflows import (
    BoundingBoxError,
    RangeError,
    SearchFilters,
    SearchRequest,
    Viewport,
    check_range,
    fetch_listings,
    resolve_viewport,
)
from pyzill.workflows import extract_listings as extract_listings

search_app = typer.Typer(name="search", help="Search Zillow listings")

# The names the search functions have, for the verbose diagnostics.
_SEARCH_NAMES = {"sale": "for_sale", "rent": "for_rent", "sold": "sold"}


def search_filter_options(args: Mapping[str, Any]) -> SearchFilters:
    """Build shared filter options used by search variants and database sync.

    The filters are the fields of :class:`~pyzill.workflows.SearchFilters`,
    so the names are read from there instead of being listed a second time:
    a filter added to the model reaches the commands with it.
    """
    return SearchFilters(**{name: args[name] for name in _FILTER_OPTION_NAMES})


_FILTER_OPTION_NAMES = tuple(field.name for field in fields(SearchFilters))


def search_options_from_args(args: Mapping[str, Any]) -> SearchRequest:
    """Build a search request from a command's own arguments.

    ``args`` is the calling command's ``locals()``. The filter values are the
    fields of :class:`~pyzill.workflows.SearchFilters`, so a command declares
    them once and a new filter only has to be added to the command signatures.
    ``is_room``/``is_entire_place`` are rental-only switches that the sale
    and sold commands do not have.

    The request-budget floor is enforced here rather than in the workflow:
    the CLI owns how an agent may pace itself, while the library stays
    permissive for callers that build their own client.
    """
    _enforce_budget_floor(args)
    filters = search_filter_options(args)
    return SearchRequest(
        query=args["query"],
        filters=filters,
        is_room=args.get("is_room", False),
        is_entire_place=args.get("is_entire_place", False),
        transport=context().http_kwargs,
    )


def _enforce_budget_floor(args: Mapping[str, Any]) -> None:
    """Refuse a dangerous ``--max-requests`` unless ``--force`` was passed.

    The floor lives in the CLI layer, not the library: a script that builds
    its own client is free to pace itself however it likes. An agent running
    the CLI gets the conservative default unless it says ``--force`` on the
    same invocation. ``--rate-limit`` is floored by the root callback, so it
    is checked there and is not re-checked here.
    """
    if context().force:
        return
    max_requests = args.get("max_requests")
    if max_requests is not None and max_requests > MAX_REQUESTS_CAP:
        error(
            f"--max-requests must be at most {MAX_REQUESTS_CAP} (use --force to override)",
            code=2,
        )


def _viewport_error(exc: BoundingBoxError) -> NoReturn:
    """Report incomplete or invalid viewport bounds as a usage error."""
    if exc.missing:
        flags = ", ".join(f"--{name}" for name in exc.missing)
        error(f"A bounding box needs all four of --north/--south/--east/--west (missing: {flags})", code=2)
    error(str(exc), code=2)


def _range_error(exc: RangeError) -> NoReturn:
    """Report an inverted filter range as a usage error."""
    error(f"--min-{exc.name} ({exc.low}) is greater than --max-{exc.name} ({exc.high})", code=2)


def resolve_search_viewport(request: SearchRequest) -> Viewport:
    """Resolve the viewport of a search, reporting failures as CLI errors."""
    bounds = (request.filters.north, request.filters.south, request.filters.east, request.filters.west)
    if all(bound is None for bound in bounds):
        # No explicit viewport: the query text is resolved to one, so the
        # search never silently falls back to a continental-US search.
        debug(f"Geocoding {request.query!r}")
    try:
        viewport = resolve_viewport(request, geocode=geocode)
    except BoundingBoxError as exc:
        _viewport_error(exc)
    except GeocodeUnavailableError as exc:
        # The geocoder could not be reached: trying again might work, so this
        # is the transient failure the exit-code contract gives code 6.
        error(str(exc), code=6, error_code="network", retryable=True)
    except GeocodeError as exc:
        error(str(exc), code=1)
    if viewport.display_name:
        debug(f"Geocoded {request.query!r} -> {viewport.display_name}")
    return viewport


def prepare_search(request: SearchRequest) -> Viewport:
    """Validate a search request and resolve its viewport.

    The checks the CLI owns (mutually exclusive flags, inverted filter
    ranges) are reported as usage errors before any request is made.
    """
    if request.is_room and request.is_entire_place:
        error("--room and --entire-place can't be used together", code=2)
    filters = request.filters
    try:
        check_range("beds", filters.min_beds, filters.max_beds)
        check_range("bathrooms", filters.min_bathrooms, filters.max_bathrooms)
        check_range("price", filters.min_price, filters.max_price)
    except RangeError as exc:
        _range_error(exc)
    return resolve_search_viewport(request)


def fetch_and_normalize(
    kind: Kind,
    request: SearchRequest,
    *,
    raw: bool,
) -> Any:
    """Fetch from Zillow and optionally normalize listings.

    Returns the raw response if raw=True, else the normalized listings.
    """
    viewport = prepare_search(request)
    debug(f"{_SEARCH_NAMES.get(kind, kind)}: query={request.query!r} bbox={viewport.bbox}")

    with network_errors("Search failed"):
        result = fetch_listings(
            kind,
            request,
            bbox=viewport.bbox,
            on_progress=lambda used, total: progress(f"tile {used}:", f"{total} listings so far"),
        )
    report_notices(result.notices)
    if raw:
        return result.raw
    return result.listings


def run_search(
    kind: Kind,
    *,
    request: SearchRequest,
    raw: bool,
    output_format: OutputFormat,
    output: str | None,
) -> None:
    """Everything the sale/rent/sold commands have in common."""
    if raw and output_format is not OutputFormat.json:
        error("--raw only supports JSON format", code=2)

    data = fetch_and_normalize(kind, request, raw=raw)
    write_output(data, output_format, output)


def _search_command(kind: Kind, args: Mapping[str, Any]) -> None:
    """Run one of the sale/rent/sold commands.

    ``args`` is the calling command's ``locals()``, so the shared options are
    collected without repeating them at every call site.
    """
    request = search_options_from_args(args)
    run_search(
        kind,
        request=request,
        raw=args["raw"],
        output_format=args["output_format"],
        output=args["output"],
    )


@search_app.command("sale")
def search_sale(
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
    _search_command("sale", locals())


@search_app.command("rent")
def search_rent(
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
    _search_command("rent", locals())


@search_app.command("sold")
def search_sold(
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
    _search_command("sold", locals())
