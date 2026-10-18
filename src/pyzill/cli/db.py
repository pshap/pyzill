"""Database commands plus shared path resolution and session helpers."""

import datetime as dt
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

import typer

from pyzill import (
    connect,
    db_stats,
    default_db_path,
    for_rent,
    for_sale,
    get_price_history,
    is_building_result,
    normalize_listing,
    sold,
    upsert_listings,
    utc_now,
)
from pyzill.cli.console import (
    context,
    emit,
    error,
    read_json,
    stderr_line,
    warn,
    write_output,
)
from pyzill.cli.options import (
    ColsOpt,
    DbPathOpt,
    East,
    FormatOpt,
    IsEntirePlace,
    IsRoom,
    LimitOpt,
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
    SortOpt,
    South,
    Tile,
    West,
    WhereOpt,
    Zoom,
)
from pyzill.cli.output import OutputFormat, dumps
from pyzill.cli.query import cli_query_database
from pyzill.cli.search import extract_listings, fetch_and_normalize, search_options
from pyzill.config import read_env
from pyzill.normalize import Listing


class ListingKind(StrEnum):
    sale = "sale"
    rent = "rent"
    sold = "sold"


db_app = typer.Typer(name="db", help="Local SQLite database for tracking listings")

_SEARCH_FNS: dict[ListingKind, Callable[..., dict[str, Any]]] = {
    ListingKind.sale: for_sale,
    ListingKind.rent: for_rent,
    ListingKind.sold: sold,
}

# Keys a row may have and still count as already-normalized for `db import`:
# the normalized listing fields, plus the database's own timestamp columns.
_NORMALIZED_KEYS = frozenset(Listing.__annotations__) | {"first_seen_utc", "last_seen_utc"}


def resolve_db_path(explicit_path: str | None, environment_path: str | None, default_path: Path) -> Path:
    if explicit_path:
        return Path(explicit_path)
    if environment_path:
        return Path(environment_path)
    return default_path


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
    stderr_line(
        "green",
        f"{verb} {total} listings:",
        f"{stats.added} new, {stats.updated} updated ({stats.price_changes} price changes), {stats.skipped} skipped",
    )


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
    max_requests: MaxRequests = 50,
    is_room: IsRoom = False,
    is_entire_place: IsEntirePlace = False,
    db_path: DbPathOpt = None,
) -> None:
    """Fetch listings from Zillow and upsert to local database."""
    if (is_room or is_entire_place) and kind is not ListingKind.rent:
        error("--room and --entire-place only work with 'rent' kind", code=2)

    rows = fetch_and_normalize(
        _SEARCH_FNS[kind],
        search_options(
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
    db_path: DbPathOpt = None,
) -> None:
    """Import listings from a saved JSON file (raw response or normalized array)."""
    data = read_json(input_or_dash)

    # Detect input shape: raw rows (camelCase / response keys) get normalized,
    # rows that already use normalized field names are kept as-is so a re-import
    # of `db query` output doesn't null out street/city/etc.
    rows = extract_listings(data, wrap_single=True)
    imported = [row if _looks_normalized(row) else normalize_listing(row) for row in rows]
    imported = [row for row in imported if row is not None and not is_building_result(row)]

    if not imported:
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

    _upsert_and_report(imported, db_path, "Imported", seen_utc=fetched_at, kind=kind)


@db_app.command("query")
def db_query(  # noqa: PLR0913, PLR0917
    where: WhereOpt = None,
    sort: SortOpt = None,
    cols: ColsOpt = None,
    limit: LimitOpt = None,
    db_path: DbPathOpt = None,
    output_format: FormatOpt = OutputFormat.json,
    output: OutputOpt = None,
) -> None:
    """Query listings from the local database."""
    with _db_session(db_path, must_exist=True) as conn:
        rows, selected = cli_query_database(conn, where=where, sort=sort, cols=cols, limit=limit)

    if not rows:
        warn("No listings in database")
        return

    write_output(rows, output_format, output, columns=selected, use_db_columns=True)


@db_app.command("history")
def db_history(
    zpid: Annotated[int, typer.Argument(help="Zillow property ID")],
    db_path: DbPathOpt = None,
) -> None:
    """Show price history for a single listing."""
    with _db_session(db_path, must_exist=True) as conn:
        history = get_price_history(conn, zpid)

    if not history:
        warn(f"No price history found for zpid {zpid}")
        return

    # Output as JSON array
    emit(dumps(history), None)


@db_app.command("stats")
def db_stats_cmd(db_path: DbPathOpt = None) -> None:
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

    context().stdout_console.print("\n".join(lines))
