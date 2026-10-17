"""CLI for pyzill Zillow scraper.

Uses Typer for command-line interface and Rich for terminal output.
"""

import csv
import io
import json
import os
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from functools import cached_property
from pathlib import Path
from typing import Annotated, Any, NoReturn, cast

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table
from rich.text import Text

from pyzill import (
    BBox,
    for_rent,
    for_sale,
    get_from_apartment_id,
    get_from_apartment_url,
    get_from_home_id,
    get_from_home_url,
    normalize_listings,
    resolve_proxy,
    set_min_interval,
    sold,
)
from pyzill.exceptions import ZillowBlockedError

DEFAULT_IMPERSONATE = "chrome136"

app = typer.Typer(
    name="pyzill",
    pretty_exceptions_show_locals=False,
    add_completion=False,
    no_args_is_help=True,
)

search_app = typer.Typer(name="search", help="Search Zillow listings")
detail_app = typer.Typer(name="detail", help="Fetch property details")
app.add_typer(search_app)
app.add_typer(detail_app)


class OutputFormat(str, Enum):  # noqa: UP042
    """Output format options."""

    json = "json"
    jsonl = "jsonl"
    csv = "csv"
    table = "table"


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
    timeout: float = 30.0
    max_retries: int = 1

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


_ctx: Context = Context()

# user:password@ inside a URL (proxy credentials, etc.)
_USERINFO_RE = re.compile(r"(?<=://)[^/@\s]+@")


def _redact(message: str) -> str:
    """Strip credentials from any URLs embedded in a message."""
    return _USERINFO_RE.sub("***@", message)


def error(message: str, code: int = 1) -> NoReturn:
    """Print error to stderr and exit."""
    _ctx.stderr_console.print(f"[red]Error:[/red] {escape(_redact(message))}")
    raise typer.Exit(code)


def warn(message: str) -> None:
    """Print warning to stderr."""
    if not _ctx.quiet:
        _ctx.stderr_console.print(f"[yellow]Warning:[/yellow] {escape(message)}")


def debug(message: str) -> None:
    """Print debug message to stderr."""
    if _ctx.verbose:
        _ctx.stderr_console.print(f"[dim]Debug:[/dim] {escape(message)}")


def version_callback(value: bool) -> None:
    """Show version and exit."""
    if value:
        from importlib.metadata import version

        try:
            ver = version("pyzill")
        except Exception:
            ver = "unknown"
        typer.echo(f"pyzill {ver}")
        raise typer.Exit


@app.callback()
def main(  # noqa: PLR0913, PLR0917
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
        typer.Option("--timeout", help="Request timeout in seconds (default: 30)"),
    ] = 30.0,
    max_retries: Annotated[
        int,
        typer.Option("--max-retries", help="Retries on 5xx errors (default: 1)"),
    ] = 1,
    rate_limit: Annotated[
        float | None,
        typer.Option(
            "--rate-limit",
            help="Minimum seconds between requests (default: ZILLOW_MIN_INTERVAL or 1.5; 0 disables)",
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
    global _ctx  # noqa: PLW0603

    # Typer already applies the PYZILL_PROXY / PYZILL_IMPERSONATE env vars (see
    # `envvar=` above), so there is no need to read them again by hand.
    _ctx = Context(
        quiet=quiet,
        verbose=verbose,
        no_color=no_color or "NO_COLOR" in os.environ or "PYZILL_NO_COLOR" in os.environ,
        proxy=proxy,
        impersonate=impersonate or DEFAULT_IMPERSONATE,
        timeout=timeout,
        max_retries=max_retries,
    )

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


def _cell(value: Any) -> str:
    """Turn any value into a display string (None -> '', nested -> JSON)."""
    if value is None:
        return ""
    if isinstance(value, dict | list):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _require_rows(data: Any, output_format: OutputFormat) -> list[dict[str, Any]]:
    if not isinstance(data, list):
        error(f"{output_format.value} output requires a list", code=2)
    return data


def _columns(rows: list[dict[str, Any]]) -> list[str]:
    """Union of keys across all rows, in first-seen order."""
    columns: dict[str, None] = {}
    for row in rows:
        columns.update(dict.fromkeys(row))
    return list(columns)


def _render_csv(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return ""
    columns = _columns(rows)
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({col: _cell(row.get(col)) for col in columns})
    return buffer.getvalue()


def _build_table(rows: list[dict[str, Any]]) -> Table:
    columns = _columns(rows)
    table = Table(title="Listings")
    for col in columns:
        table.add_column(col)
    for row in rows:
        # Text() stops Rich from treating "[brackets]" in listing data as markup.
        table.add_row(*(Text(_cell(row.get(col))) for col in columns))
    return table


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
) -> None:
    """Write data to stdout or file in specified format."""
    if output == "-":
        output = None  # stdout

    if output_format is OutputFormat.json:
        _emit(json.dumps(data, ensure_ascii=False), output)
        return

    if output_format is OutputFormat.jsonl:
        items = data if isinstance(data, list) else [data]
        _emit("\n".join(json.dumps(item, ensure_ascii=False) for item in items), output)
        return

    # csv and table both need a list of rows. An empty result is not an error.
    rows = _require_rows(data, output_format)
    if not rows:
        warn("No listings found")

    if output_format is OutputFormat.csv:
        _emit(_render_csv(rows), output)
    elif rows:
        table = _build_table(rows)
        if output:
            # Render to text so --output works for tables too.
            console = Console(file=io.StringIO(), no_color=True, force_terminal=False, width=200)
            console.print(table)
            _emit(cast(io.StringIO, console.file).getvalue(), output)
        else:
            _ctx.stdout_console.print(table)


# --------------------------------------------------------------------------- #
# Shared option definitions (declared once, reused by every command)
# --------------------------------------------------------------------------- #

Query = Annotated[str, typer.Argument(help="Search query (e.g., 'Schenectady NY' or ZIP)")]
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
    values = {"north": north, "south": south, "east": east, "west": west}
    missing = [name for name, value in values.items() if value is None]
    if len(missing) == len(values):
        return None
    if missing:
        flags = ", ".join(f"--{name}" for name in missing)
        error(f"A bounding box needs all four of --north/--south/--east/--west (missing: {flags})", code=2)
    try:
        return BBox(**cast(dict[str, float], values))
    except ValueError as e:
        error(f"Invalid bounding box: {e}", code=2)


def check_range(name: str, low: int | None, high: int | None) -> None:
    """Fail early if a min option is larger than its max option."""
    if low is not None and high is not None and low > high:
        error(f"--min-{name} ({low}) is greater than --max-{name} ({high})", code=2)


def extract_listings(data: Any, *, wrap_single: bool = False) -> list[Any]:
    """Pull the listing list out of a Zillow response (or a bare list).

    With wrap_single=True, a dict that has no results key is treated as one
    listing; otherwise it yields an empty list.
    """
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("mapResults", "listResults"):
            if key in data:
                return data[key] or []
        return [data] if wrap_single else []
    error("Input must be a JSON object or array", code=2)


def run_search(  # noqa: PLR0913
    search_fn: Callable[..., dict[str, Any]],
    *,
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
    raw: bool,
    output_format: OutputFormat,
    output: str | None,
    **extra: Any,
) -> None:
    """Everything the sale/rent/sold commands have in common."""
    if raw and output_format is not OutputFormat.json:
        error("--raw only supports JSON format", code=2)
    if extra.get("is_room") and extra.get("is_entire_place"):
        error("--room and --entire-place can't be used together", code=2)

    check_range("beds", min_beds, max_beds)
    check_range("bathrooms", min_bathrooms, max_bathrooms)
    check_range("price", min_price, max_price)

    bbox = build_bbox(north, south, east, west)
    http_kwargs = _ctx.http_kwargs
    debug(f"{search_fn.__name__}: query={query!r} bbox={bbox}")

    try:
        result = search_fn(
            search_value=query,
            bbox=bbox,
            zoom_value=zoom,
            min_beds=min_beds,
            max_beds=max_beds,
            min_bathrooms=min_bathrooms,
            max_bathrooms=max_bathrooms,
            min_price=min_price,
            max_price=max_price,
            **extra,
            **http_kwargs,
        )
    except ZillowBlockedError as e:
        error(f"Zillow blocked the request: {e}", code=1)
    except Exception as e:
        error(f"Search failed: {e}", code=1)

    data = result if raw else normalize_listings(extract_listings(result))
    write_output(data, output_format, output)


# NOTE for the three commands below: `locals()` is called as the very first
# statement, so it contains exactly the command's parameters. Keep it first.


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
    raw: Raw = False,
    output_format: FormatOpt = OutputFormat.json,
    output: OutputOpt = None,
) -> None:
    """Search for-sale listings."""
    run_search(for_sale, **locals())


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
    run_search(for_rent, **locals())


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
    raw: Raw = False,
    output_format: FormatOpt = OutputFormat.json,
    output: OutputOpt = None,
) -> None:
    """Search sold listings."""
    run_search(sold, **locals())


# --------------------------------------------------------------------------- #
# Details
# --------------------------------------------------------------------------- #


def run_detail(
    fetch: Callable[..., Any],
    value: str | int,
    output_format: OutputFormat,
    output: str | None,
) -> None:
    """Everything the home/apartment detail commands have in common."""
    if output_format is not OutputFormat.json:
        error("Detail commands only support JSON format", code=2)

    http_kwargs = _ctx.http_kwargs
    debug(f"{fetch.__name__}: {value!r}")

    try:
        result = fetch(value, **http_kwargs)
    except ZillowBlockedError as e:
        error(f"Zillow blocked the request: {e}", code=1)
    except Exception as e:
        error(f"Detail fetch failed: {e}", code=1)

    write_output(result, output_format, output)


@detail_app.command("home")
def detail_home(
    url_or_id: Annotated[str, typer.Argument(help="Zillow home URL or zpid")],
    output_format: FormatOpt = OutputFormat.json,
    output: OutputOpt = None,
) -> None:
    """Fetch home details by URL or zpid."""
    value = url_or_id.strip()
    if value.isdigit():
        run_detail(get_from_home_id, int(value), output_format, output)
    else:
        run_detail(get_from_home_url, value, output_format, output)


@detail_app.command("apartment")
def detail_apartment(
    url_or_id: Annotated[str, typer.Argument(help="Zillow apartment URL or apartment ID")],
    output_format: FormatOpt = OutputFormat.json,
    output: OutputOpt = None,
) -> None:
    """Fetch apartment details by URL or apartment ID."""
    value = url_or_id.strip()
    fetch = get_from_apartment_url if value.startswith("http") else get_from_apartment_id
    run_detail(fetch, value, output_format, output)


# --------------------------------------------------------------------------- #
# Normalize
# --------------------------------------------------------------------------- #


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
    if input_or_dash == "-":
        try:
            text = typer.get_text_stream("stdin").read()
        except (OSError, UnicodeDecodeError) as e:
            error(f"Failed to read stdin: {e}", code=1)
    else:
        input_path = Path(input_or_dash)
        if not input_path.exists():
            error(f"Input file not found: {input_or_dash}", code=2)
        try:
            text = input_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            error(f"Failed to read {input_or_dash}: {e}", code=1)

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
