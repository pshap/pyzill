"""CLI for pyzill Zillow scraper.

Uses Typer for command-line interface and Rich for terminal output.
"""

import datetime as dt
import json
import os
import re
import sqlite3
import sys
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import cached_property
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Annotated, Any, NoReturn, cast

import typer
from rich.console import Console
from rich.markup import escape

from pyzill import (
    BBox,
    connect,
    db_stats,
    default_db_path,
    for_rent,
    for_sale,
    get_from_apartment_id,
    get_from_apartment_url,
    get_from_home_id,
    get_from_home_url,
    get_price_history,
    normalize_listing,
    normalize_listings,
    resolve_proxy,
    set_min_interval,
    sold,
    upsert_listings,
    utc_now,
)
from pyzill.cli.db import ListingKind, resolve_db_path
from pyzill.cli.detail import home_detail_target
from pyzill.cli.output import OutputFormat, dumps
from pyzill.cli.output import write_output as _render_output
from pyzill.cli.query import QueryValidationError
from pyzill.cli.query import query_database as _query_database_engine
from pyzill.cli.query import query_rows as _query_rows_engine
from pyzill.cli.search import SearchOptions
from pyzill.cli.search import build_bbox as _build_bbox
from pyzill.cli.search import check_range as _check_range
from pyzill.config import DEFAULT_IMPERSONATE, DEFAULT_MAX_RETRIES, DEFAULT_TIMEOUT, read_env
from pyzill.exceptions import (
    ZillowBlockedError,
    ZillowNotFoundError,
    ZillowParseError,
    ZillowServerError,
)
from pyzill.geocode import geocode
from pyzill.normalize import Listing
from pyzill.search import tiled_search

_RESULT_CAP = 500

app = typer.Typer(
    name="pyzill",
    pretty_exceptions_show_locals=False,
    add_completion=False,
    no_args_is_help=True,
)

search_app = typer.Typer(name="search", help="Search Zillow listings")
detail_app = typer.Typer(name="detail", help="Fetch property details")
db_app = typer.Typer(name="db", help="Local SQLite database for tracking listings")
app.add_typer(search_app)
app.add_typer(detail_app)
app.add_typer(db_app)


# --------------------------------------------------------------------------- #
# Shared context and console helpers
# --------------------------------------------------------------------------- #


@dataclass
class Context:
    """Shared context for CLI commands."""

    quiet: bool = False
    verbose: bool = False
    no_color: bool = False
    proxy: str | None = None
    impersonate: str = DEFAULT_IMPERSONATE
    timeout: float = DEFAULT_TIMEOUT
    max_retries: int = DEFAULT_MAX_RETRIES

    @cached_property
    def stderr_console(self) -> Console:
        """Console for diagnostics and progress."""
        return Console(stderr=True, no_color=self.no_color, force_terminal=False)

    @cached_property
    def stdout_console(self) -> Console:
        """Console for human-readable output (not machine formats)."""
        return Console(stderr=False, no_color=self.no_color, force_terminal=False)

    @property
    def http_kwargs(self) -> dict[str, Any]:
        """Keyword arguments every pyzill network call accepts."""
        try:
            proxy_url = resolve_proxy(self.proxy)
        except ValueError as e:
            # Backstop; the root callback validates the proxy before any
            # command runs. Keeps a bad ZILLOW_PROXY from becoming a traceback.
            error(f"Invalid proxy: {e}", code=2)
        return {
            "proxy_url": proxy_url,
            "impersonate": self.impersonate,
            "timeout": self.timeout,
            "max_retries": self.max_retries,
        }


_active_context: ContextVar[Context | None] = ContextVar("pyzill_cli_context", default=None)


def _context() -> Context:
    return _active_context.get() or Context()


# user:password@ inside a URL (proxy credentials, etc.)
_USERINFO_RE = re.compile(r"(?<=://)[^/@\s]+@")


def _redact(message: str) -> str:
    """Strip credentials from any URLs embedded in a message."""
    return _USERINFO_RE.sub("***@", message)


def _stderr_line(style: str, label: str, message: str) -> None:
    """Print one diagnostic line to stderr, redacting credentials."""
    _context().stderr_console.print(f"[{style}]{label}[/] {escape(_redact(message))}")


def error(message: str, code: int = 1) -> NoReturn:
    """Print error to stderr and exit."""
    _stderr_line("red", "Error:", message)
    raise typer.Exit(code)


def warn(message: str) -> None:
    """Print warning to stderr."""
    if not _context().quiet:
        _stderr_line("yellow", "Warning:", message)


def debug(message: str) -> None:
    """Print debug message to stderr."""
    if _context().verbose:
        _stderr_line("dim", "Debug:", message)


def version_callback(value: bool) -> None:
    """Show version and exit."""
    if value:
        try:
            ver = version("pyzill")
        except PackageNotFoundError:
            ver = "unknown"
        typer.echo(f"pyzill {ver}")
        raise typer.Exit


@contextmanager
def _network_errors(failure: str) -> Iterator[None]:
    """Turn library failures into clean exit-1 errors."""
    try:
        yield
    except ZillowServerError as e:
        error(f"Zillow server error after retries: {e}", code=1)
    except ZillowNotFoundError as e:
        error(f"Zillow listing was not found: {e}", code=1)
    except ZillowParseError as e:
        error(f"Could not parse Zillow's response; its page schema may have changed: {e}", code=1)
    except ZillowBlockedError as e:
        error(f"Zillow blocked the request: {e}", code=1)
    except Exception as e:
        error(f"{failure}: {e}", code=1)


@app.callback()
def main(  # noqa: PLR0913, PLR0917
    ctx: typer.Context,
    quiet: Annotated[
        bool,
        typer.Option("--quiet", "-q", help="Suppress progress and nonessential diagnostics"),
    ] = False,
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v", help="Additional stderr diagnostics"),
    ] = False,
    no_color: Annotated[
        bool,
        typer.Option("--no-color", help="Disable colors and styling"),
    ] = False,
    proxy: Annotated[
        str | None,
        typer.Option(
            "--proxy",
            help="Proxy URL (overrides PYZILL_PROXY and ZILLOW_PROXY)",
            envvar="PYZILL_PROXY",
        ),
    ] = None,
    impersonate: Annotated[
        str,
        typer.Option(
            "--impersonate",
            help=f"TLS impersonation profile (default: {DEFAULT_IMPERSONATE})",
            envvar="PYZILL_IMPERSONATE",
        ),
    ] = DEFAULT_IMPERSONATE,
    timeout: Annotated[
        float,
        typer.Option("--timeout", min=0, help="Request timeout in seconds (must be greater than 0; default: 30)"),
    ] = DEFAULT_TIMEOUT,
    max_retries: Annotated[
        int,
        typer.Option("--max-retries", min=0, help="Retries on transient 5xx, 429, and network errors (default: 1)"),
    ] = DEFAULT_MAX_RETRIES,
    rate_limit: Annotated[
        float | None,
        typer.Option(
            "--rate-limit",
            help="Minimum seconds between requests (default: PYZILL_RATE_LIMIT or 1.5; 0 disables)",
            envvar="PYZILL_RATE_LIMIT",
        ),
    ] = None,
    version: Annotated[  # noqa: ARG001
        bool,
        typer.Option(
            "--version",
            callback=version_callback,
            is_eager=True,
            help="Show version and exit",
        ),
    ] = False,
) -> None:
    """pyzill — Zillow scraper CLI.

    Search for-sale/for-rent/sold listings and fetch property details.
    """
    if timeout <= 0:
        error("--timeout must be greater than 0", code=2)

    # Typer already applies the PYZILL_PROXY / PYZILL_IMPERSONATE env vars (see
    # `envvar=` above), so there is no need to read them again by hand.
    context = Context(
        quiet=quiet,
        verbose=verbose,
        no_color=no_color or any(name in os.environ for name in ("NO_COLOR", "PYZILL_NO_COLOR")),
        proxy=proxy,
        impersonate=impersonate or DEFAULT_IMPERSONATE,
        timeout=timeout,
        max_retries=max_retries,
    )
    ctx.obj = context
    _active_context.set(ctx.obj)

    # Validate the proxy (explicit, PYZILL_PROXY, or ZILLOW_PROXY) up front so
    # a bad URL is a clean usage error, not a traceback mid-command. The
    # ValueError message quotes the full URL, so route it through error()'s
    # credential redaction.
    try:
        resolve_proxy(proxy)
    except ValueError as e:
        error(f"Invalid proxy: {e}", code=2)

    if rate_limit is not None:
        set_min_interval(rate_limit)


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #


def _emit(content: str, output: str | None) -> None:
    """Write text to a file, or to stdout when no file is given."""
    if content and not content.endswith("\n"):
        content += "\n"

    if output:
        try:
            with Path(output).open("w", encoding="utf-8", newline="") as fh:
                fh.write(content)
        except OSError as e:
            error(f"Failed to write to {output}: {e}", code=1)
        return

    try:
        typer.echo(content, nl=False)
        sys.stdout.flush()
    except BrokenPipeError:
        # e.g. `pyzill ... | head`. Point stdout at devnull so Python doesn't
        # complain again while shutting down.
        devnull = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(devnull, sys.stdout.fileno())
        finally:
            os.close(devnull)
        raise typer.Exit(0) from None


def write_output(
    data: Any,
    output_format: OutputFormat,
    output: str | None = None,
    columns: list[str] | None = None,
    use_db_columns: bool = False,
) -> None:
    """Write output using the standalone renderer and CLI callbacks."""
    _render_output(
        data,
        output_format,
        output,
        columns,
        use_db_columns,
        emit=_emit,
        error=error,
        warn=warn,
        console=_context().stdout_console,
    )


# --------------------------------------------------------------------------- #
# Shared option definitions (declared once, reused by every command)
# --------------------------------------------------------------------------- #

Query = Annotated[str, typer.Argument(help="Search query: place name (geocoded), ZIP, or address")]
North = Annotated[float | None, typer.Option("--north", help="Bounding box north latitude")]
South = Annotated[float | None, typer.Option("--south", help="Bounding box south latitude")]
East = Annotated[float | None, typer.Option("--east", help="Bounding box east longitude")]
West = Annotated[float | None, typer.Option("--west", help="Bounding box west longitude")]
Zoom = Annotated[int | None, typer.Option("--zoom", help="Map zoom level")]
MinBeds = Annotated[int | None, typer.Option("--min-beds", help="Minimum bedrooms")]
MaxBeds = Annotated[int | None, typer.Option("--max-beds", help="Maximum bedrooms")]
MinBaths = Annotated[int | None, typer.Option("--min-bathrooms", help="Minimum bathrooms")]
MaxBaths = Annotated[int | None, typer.Option("--max-bathrooms", help="Maximum bathrooms")]
MinPrice = Annotated[int | None, typer.Option("--min-price", help="Minimum price")]
MaxPrice = Annotated[int | None, typer.Option("--max-price", help="Maximum price")]
Tile = Annotated[bool, typer.Option("--tile", help="Recursively split capped map results")]
MaxDepth = Annotated[int, typer.Option("--max-depth", min=0, max=8, help="Maximum recursive tile depth (default: 4)")]
Raw = Annotated[bool, typer.Option("--raw", help="Return raw search response (not normalized)")]
FormatOpt = Annotated[
    OutputFormat,
    typer.Option("--format", "-f", help="Output format (default: json)"),
]
OutputOpt = Annotated[
    str | None,
    typer.Option("--output", "-o", help="Output file (default: stdout)"),
]


# --------------------------------------------------------------------------- #
# Search
# --------------------------------------------------------------------------- #


def build_bbox(
    north: float | None,
    south: float | None,
    east: float | None,
    west: float | None,
) -> BBox | None:
    """Build a BBox from the four CLI options (all four or none)."""
    try:
        return _build_bbox(north, south, east, west)
    except ValueError as exc:
        error(str(exc), code=2)


def check_range(name: str, low: int | None, high: int | None) -> None:
    """Fail early if a min option is larger than its max option."""
    try:
        _check_range(name, low, high)
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

    check_range("beds", options.min_beds, options.max_beds)
    check_range("bathrooms", options.min_bathrooms, options.max_bathrooms)
    check_range("price", options.min_price, options.max_price)

    bbox = build_bbox(options.north, options.south, options.east, options.west)
    http_kwargs = _context().http_kwargs
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

    with _network_errors("Search failed"):
        if options.tile:
            result = tiled_search(
                search_fn,
                bbox=bbox,
                max_depth=options.max_depth,
                on_cap=lambda: warn(
                    f"A tile still returned {_RESULT_CAP} listings at --max-depth {options.max_depth}; "
                    "results may be truncated"
                ),
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
    raw: Raw = False,
    output_format: FormatOpt = OutputFormat.json,
    output: OutputOpt = None,
) -> None:
    """Search for-sale listings."""
    options = SearchOptions(
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
    is_room: Annotated[bool, typer.Option("--room", help="Filter for rooms for rent")] = False,
    is_entire_place: Annotated[
        bool,
        typer.Option("--entire-place", help="Filter for entire places for rent"),
    ] = False,
    raw: Raw = False,
    output_format: FormatOpt = OutputFormat.json,
    output: OutputOpt = None,
) -> None:
    """Search for-rent listings."""
    options = SearchOptions(
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
    raw: Raw = False,
    output_format: FormatOpt = OutputFormat.json,
    output: OutputOpt = None,
) -> None:
    """Search sold listings."""
    options = SearchOptions(
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
    )
    run_search(sold, options=options, raw=raw, output_format=output_format, output=output)


# --------------------------------------------------------------------------- #
# Details
# --------------------------------------------------------------------------- #

_KEYS = Annotated[bool, typer.Option("--keys", help="List available fields instead of dumping the record")]
_PATH = Annotated[
    str | None,
    typer.Option("--path", help="Extract one field by dotted path, e.g. 'address.city' or 'priceHistory[0].price'"),
]


def _path_segments(path: str) -> list[str | int]:
    """Split a dotted path into keys and list indices ('a.b[0].c' -> a, b, 0, c)."""
    segments: list[str | int] = []
    for part in path.split("."):
        name, _, rest = part.partition("[")
        if name:
            segments.append(name)
        while rest:
            rest = rest.removeprefix("[")
            index, close, rest = rest.partition("]")
            if not close or not index.isdigit():
                error(f"Bad --path {path!r}: expected [n] list indices", code=2)
            segments.append(int(index))
    if not segments:
        error("--path must not be empty", code=2)
    return segments


def _extract_path(data: Any, path: str) -> Any:
    """Walk a dotted path through nested dicts/lists; error when it goes missing."""
    current = data
    walked: list[str] = []
    for segment in _path_segments(path):
        label = f"[{segment}]" if isinstance(segment, int) else segment
        if isinstance(segment, int):
            ok = isinstance(current, list) and segment < len(current)
        else:
            ok = isinstance(current, dict) and segment in current
        if not ok:
            here = ".".join(walked) or "(root)"
            error(f"--path {path!r} not found: no {label!r} at {here}", code=1)
        current = current[segment]
        walked.append(str(label))
    return current


def _emit_keys(data: Any, output: str | None = None) -> None:
    """Print 'key: type' lines for a detail record (or the dict at --path)."""
    if not isinstance(data, dict):
        error(f"--keys needs an object (got {type(data).__name__}); try --path to reach one", code=2)
    width = max((len(key) for key in data), default=0)
    lines = [f"{key.ljust(width)}  {type(value).__name__}" for key, value in data.items()]
    _emit("\n".join(lines), output)


def run_detail(  # noqa: PLR0913, PLR0917
    fetch: Callable[..., Any],
    value: str | int,
    output_format: OutputFormat,
    output: str | None,
    keys: bool = False,
    path: str | None = None,
) -> None:
    """Everything the home/apartment detail commands have in common."""
    if output_format is not OutputFormat.json:
        error("Detail commands only support JSON format", code=2)
    if keys and path:
        error("--keys and --path can't be used together", code=2)

    http_kwargs = _context().http_kwargs
    debug(f"{fetch.__name__}: {value!r}")

    with _network_errors("Detail fetch failed"):
        result = fetch(value, **http_kwargs)

    if path:
        write_output(_extract_path(result, path), output_format, output)
    elif keys:
        _emit_keys(result, output)
    else:
        write_output(result, output_format, output)


@detail_app.command("home")
def detail_home(
    url_or_id: Annotated[str, typer.Argument(help="Zillow home URL or zpid")],
    output_format: FormatOpt = OutputFormat.json,
    output: OutputOpt = None,
    keys: _KEYS = False,
    path: _PATH = None,
) -> None:
    """Fetch home details by URL or zpid."""
    try:
        target_kind, target = home_detail_target(url_or_id)
    except ValueError as exc:
        error(str(exc), code=2)
    fetch = get_from_home_id if target_kind == "id" else get_from_home_url
    run_detail(fetch, target, output_format, output, keys=keys, path=path)


@detail_app.command("apartment")
def detail_apartment(
    url_or_id: Annotated[str, typer.Argument(help="Zillow apartment URL or apartment ID")],
    output_format: FormatOpt = OutputFormat.json,
    output: OutputOpt = None,
    keys: _KEYS = False,
    path: _PATH = None,
) -> None:
    """Fetch apartment details by URL or apartment ID."""
    value = url_or_id.strip()
    fetch = get_from_apartment_url if value.startswith("http") else get_from_apartment_id
    run_detail(fetch, value, output_format, output, keys=keys, path=path)


# --------------------------------------------------------------------------- #
# Query (offline re-slice of saved results; no network)
# --------------------------------------------------------------------------- #


def _query_rows(*args: Any, **kwargs: Any) -> tuple[list[dict[str, Any]], list[str] | None]:
    try:
        return _query_rows_engine(*args, **kwargs)
    except QueryValidationError as exc:
        error(str(exc), code=2)


def _query_database(*args: Any, **kwargs: Any) -> tuple[list[dict[str, Any]], list[str] | None]:
    try:
        return _query_database_engine(*args, **kwargs)
    except QueryValidationError as exc:
        error(str(exc), code=2)


@app.command("query")
def query(  # noqa: PLR0913, PLR0917
    input_or_dash: Annotated[str, typer.Argument(help="Saved search JSON file, or '-' for stdin")],
    where: Annotated[
        list[str] | None,
        typer.Option("--where", help="Filter: FIELD OP VALUE (repeatable; OP: >= <= > < == != ~)"),
    ] = None,
    sort: Annotated[str | None, typer.Option("--sort", help="Sort by FIELD or FIELD:asc / FIELD:desc")] = None,
    cols: Annotated[str | None, typer.Option("--cols", help="Comma-separated columns to output")] = None,
    limit: Annotated[int | None, typer.Option("--limit", help="Maximum rows to output")] = None,
    output_format: FormatOpt = OutputFormat.json,
    output: OutputOpt = None,
) -> None:
    """Re-filter and sort saved search results without hitting Zillow."""
    if limit is not None and limit < 1:
        error("--limit must be at least 1", code=2)

    text = _read_input(input_or_dash)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        error(f"Invalid JSON input: {e}", code=1)
    if not isinstance(data, list):
        error("query input must be a JSON array of listings (run 'pyzill normalize' first if it isn't)", code=2)
    rows = [row for row in data if isinstance(row, dict)]
    if data and not rows:
        error("query input contains no listing objects", code=2)

    rows, selected = _query_rows(rows, where=where, sort=sort, cols=cols, limit=limit)
    write_output(rows, output_format, output, columns=selected)


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #

_DbPath = Annotated[
    str | None,
    typer.Option("--db", help="Database path (default: ./pyzill.db or PYZILL_DB env)"),
]


def _resolve_db_path(db_path: str | None) -> Path:
    """Resolve database path from --db, PYZILL_DB env, or default."""
    return resolve_db_path(db_path, read_env("PYZILL_DB"), default_db_path())


def _connect_db(db_path: str | None, *, must_exist: bool = False) -> sqlite3.Connection:
    """Resolve the db path and open it, creating the schema if needed."""
    path = _resolve_db_path(db_path)
    if must_exist and not path.exists():
        error(f"Database not found: {path}. Run 'pyzill db sync' or 'pyzill db import' first.", code=1)
    try:
        return connect(path)
    except sqlite3.Error as e:
        error(f"Failed to open database: {e}", code=1)


@contextmanager
def _db_session(db_path: str | None, *, must_exist: bool = False) -> Iterator[sqlite3.Connection]:
    """Open and close a database connection, translating SQLite failures to CLI errors."""
    try:
        with closing(_connect_db(db_path, must_exist=must_exist)) as conn:
            yield conn
    except sqlite3.Error as e:
        error(f"Database operation failed: {e}", code=1)


def _upsert_and_report(
    rows: list[dict[str, Any]],
    db_path: str | None,
    verb: str,
    *,
    seen_utc: str | None = None,
    kind: ListingKind = ListingKind.sale,
) -> None:
    """Upsert rows in one transaction and print the sync summary to stderr."""
    with _db_session(db_path) as conn:
        stats = upsert_listings(conn, rows, seen_utc=seen_utc, kind=kind.value)
    total = stats.added + stats.updated
    _stderr_line(
        "green",
        f"{verb} {total} listings:",
        f"{stats.added} new, {stats.updated} updated ({stats.price_changes} price changes), {stats.skipped} skipped",
    )


# Keys a row may have and still count as already-normalized for `db import`:
# the normalized listing fields, plus the database's own timestamp columns.
_NORMALIZED_KEYS = frozenset(Listing.__annotations__) | {"first_seen_utc", "last_seen_utc"}


def _looks_normalized(row: Any) -> bool:
    """True when a row uses normalized field names (so normalizing would null them)."""
    return isinstance(row, dict) and set(row) <= _NORMALIZED_KEYS


@db_app.command("sync")
def db_sync(  # noqa: PLR0913, PLR0917
    kind: Annotated[ListingKind, typer.Argument(help="Search kind: sale, rent, or sold")],
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
    is_room: Annotated[bool, typer.Option("--room", help="Filter for rooms for rent")] = False,
    is_entire_place: Annotated[
        bool,
        typer.Option("--entire-place", help="Filter for entire places for rent"),
    ] = False,
    db_path: _DbPath = None,
) -> None:
    """Fetch listings from Zillow and upsert to local database."""
    if (is_room or is_entire_place) and kind is not ListingKind.rent:
        error("--room and --entire-place only work with 'rent' kind", code=2)

    # Map kind to search function
    search_fns: dict[ListingKind, Callable[..., dict[str, Any]]] = {
        ListingKind.sale: for_sale,
        ListingKind.rent: for_rent,
        ListingKind.sold: sold,
    }
    search_fn = search_fns[kind]

    # Fetch and normalize
    rows = fetch_and_normalize(
        search_fn,
        SearchOptions(
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
            is_room,
            is_entire_place,
        ),
        raw=False,
    )

    if not rows:
        warn("No listings found")
        return

    _upsert_and_report(rows, db_path, "Synced", kind=kind)


@db_app.command("import")
def db_import(
    input_or_dash: Annotated[str, typer.Argument(help="JSON file path or '-' for stdin")],
    fetched_at: Annotated[
        str | None,
        typer.Option("--fetched-at", help="Timestamp for this batch (default: now)"),
    ] = None,
    kind: Annotated[
        ListingKind,
        typer.Option("--kind", help="Listing kind for this import: sale, rent, or sold"),
    ] = ListingKind.sale,
    db_path: _DbPath = None,
) -> None:
    """Import listings from a saved JSON file (raw response or normalized array)."""
    text = _read_input(input_or_dash)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        error(f"Invalid JSON input: {e}", code=1)

    # Detect input shape: raw rows (camelCase / response keys) get normalized,
    # rows that already use normalized field names are kept as-is so a re-import
    # of `db query` output doesn't null out street/city/etc.
    rows = extract_listings(data, wrap_single=True)
    normalized = [row if _looks_normalized(row) else normalize_listing(row) for row in rows]
    normalized = [row for row in normalized if row is not None]

    if not normalized:
        warn("No listings to import")
        return

    # Parse timestamp; store it in the canonical fixed-width UTC format.
    if fetched_at:
        try:
            parsed = dt.datetime.fromisoformat(fetched_at)
        except ValueError:
            error(f"Invalid --fetched-at timestamp: {fetched_at}. Use ISO format like 2026-10-17T12:00:00Z", code=2)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.UTC)
        fetched_at = parsed.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    else:
        fetched_at = utc_now()

    _upsert_and_report(cast(list[dict[str, Any]], normalized), db_path, "Imported", seen_utc=fetched_at, kind=kind)


@db_app.command("query")
def db_query(  # noqa: PLR0913, PLR0917
    where: Annotated[
        list[str] | None,
        typer.Option("--where", help="Filter: FIELD OP VALUE (repeatable; OP: >= <= > < == != ~)"),
    ] = None,
    sort: Annotated[str | None, typer.Option("--sort", help="Sort by FIELD or FIELD:asc / FIELD:desc")] = None,
    cols: Annotated[str | None, typer.Option("--cols", help="Comma-separated columns to output")] = None,
    limit: Annotated[int | None, typer.Option("--limit", help="Maximum rows to output")] = None,
    db_path: _DbPath = None,
    output_format: FormatOpt = OutputFormat.json,
    output: OutputOpt = None,
) -> None:
    """Query listings from the local database."""
    if limit is not None and limit < 1:
        error("--limit must be at least 1", code=2)

    with _db_session(db_path, must_exist=True) as conn:
        rows, selected = _query_database(conn, where=where, sort=sort, cols=cols, limit=limit)

    if not rows:
        warn("No listings in database")
        return

    write_output(rows, output_format, output, columns=selected, use_db_columns=True)


@db_app.command("history")
def db_history(
    zpid: Annotated[int, typer.Argument(help="Zillow property ID")],
    db_path: _DbPath = None,
) -> None:
    """Show price history for a single listing."""
    with _db_session(db_path, must_exist=True) as conn:
        history = get_price_history(conn, zpid)

    if not history:
        warn(f"No price history found for zpid {zpid}")
        return

    # Output as JSON array
    _emit(dumps(history), None)


@db_app.command("stats")
def db_stats_cmd(db_path: _DbPath = None) -> None:
    """Show database statistics."""
    with _db_session(db_path, must_exist=True) as conn:
        stats = db_stats(conn)

    # Format size as human-readable
    size_mb = stats.db_size_bytes / (1024 * 1024)
    size_str = f"{size_mb:.2f} MB" if size_mb >= 1 else f"{stats.db_size_bytes / 1024:.2f} KB"

    lines = [
        f"Database: {stats.db_path}",
        f"Size: {size_str}",
        f"Total listings: {stats.total_listings}",
    ]
    if stats.oldest_seen:
        lines.append(f"Oldest first_seen: {stats.oldest_seen}")
    if stats.newest_seen:
        lines.append(f"Newest last_seen: {stats.newest_seen}")

    _context().stdout_console.print("\n".join(lines))


# --------------------------------------------------------------------------- #
# Normalize
# --------------------------------------------------------------------------- #


def _read_input(source: str) -> str:
    """Read text from a file path, or from stdin when source is '-'."""
    if source == "-":
        try:
            return typer.get_text_stream("stdin").read()
        except (OSError, UnicodeDecodeError) as e:
            error(f"Failed to read stdin: {e}", code=1)

    path = Path(source)
    if not path.exists():
        error(f"Input file not found: {source}", code=2)
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        error(f"Failed to read {source}: {e}", code=1)


@app.command("normalize")
def normalize(
    input_or_dash: Annotated[
        str,
        typer.Argument(help="JSON file path or '-' for stdin"),
    ],
    output_format: FormatOpt = OutputFormat.json,
    output: OutputOpt = None,
) -> None:
    """Normalize raw listing data from file or stdin."""
    text = _read_input(input_or_dash)

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        error(f"Invalid JSON input: {e}", code=1)

    listings = extract_listings(data, wrap_single=True)
    write_output(normalize_listings(listings), output_format, output)


def main_entry() -> None:
    """Entry point for the CLI."""
    app()


if __name__ == "__main__":
    main_entry()
