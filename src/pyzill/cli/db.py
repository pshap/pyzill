"""Database commands: Typer wiring only.

The sync and import pipelines live in :mod:`pyzill.workflows`; this module
parses the flags, calls the workflow, maps its errors to exit codes, and
renders the result.
"""

import datetime as dt
import sqlite3
import sys
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Annotated

import typer

from pyzill._kinds import ListingKind, as_kind
from pyzill.cli.console import (
    context,
    emit,
    error,
    progress,
    read_json,
    report_notices,
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
    WhereJSONOpt,
    WhereOpt,
    Zoom,
)
from pyzill.cli.output import OutputFormat, dumps
from pyzill.cli.query import cli_query_database
from pyzill.cli.search import prepare_search, search_options_from_args
from pyzill.config import read_env
from pyzill.db import (
    SyncStats,
    connect,
    connect_readonly,
    db_stats,
    default_db_path,
    get_changes,
    get_price_history,
    utc_now,
)
from pyzill.workflows import fetch_listings, import_rows, prepare_import, sync_to_db

db_app = typer.Typer(name="db", help="Local SQLite database for tracking listings")


class _WarningState:
    legacy_db_shown: bool = False


_warning_state = _WarningState()


def resolve_db_path(explicit_path: str | None, environment_path: str | None, default_path: Path) -> Path:
    if explicit_path:
        return Path(explicit_path)
    if environment_path:
        return Path(environment_path)
    return default_path


def normalize_utc_timestamp(value: str, *, label: str) -> str:
    """Parse an ISO-8601 timestamp and return the canonical ``...Z`` form.

    Normalizing keeps string comparisons against stored timestamps (which are
    fixed-width UTC) correct even when the caller passes an offset or
    milliseconds. Invalid input is a usage error (exit 2).
    """
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        error(f"Invalid {label} timestamp: {value}. Use ISO format like 2026-10-17T12:00:00Z", code=2)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _resolve_db_path(db_path: str | None) -> Path:
    """Resolve database path from --db, PYZILL_DB env, or default."""
    environment_path = read_env("PYZILL_DB")
    if db_path is None and environment_path is None:
        path = default_db_path()
        legacy_path = Path.cwd() / "pyzill.db"
        if legacy_path.exists() and legacy_path.resolve() != path.resolve() and not _warning_state.legacy_db_shown:
            stderr_line(
                "yellow",
                "Warning:",
                f"Found {legacy_path}; default database is now {path}. Use --db to select it.",
            )
            _warning_state.legacy_db_shown = True
        return path
    return resolve_db_path(db_path, environment_path, default_db_path())


def _connect_db(db_path: str | None, *, must_exist: bool = False, readonly: bool = False) -> sqlite3.Connection:
    """Resolve the db path and open it, creating the schema if needed."""
    path = _resolve_db_path(db_path)
    if must_exist and not path.exists():
        error(f"Database not found: {path}. Run 'pyzill db sync' or 'pyzill db import' first.", code=1)
    try:
        return connect_readonly(path) if readonly else connect(path)
    except sqlite3.Error as e:
        error(f"Failed to open database: {e}", code=1)


@contextmanager
def _db_session(
    db_path: str | None, *, must_exist: bool = False, readonly: bool = False
) -> Iterator[sqlite3.Connection]:
    """Open and close a database connection, translating SQLite failures to CLI errors."""
    try:
        with closing(_connect_db(db_path, must_exist=must_exist, readonly=readonly)) as conn:
            yield conn
    except sqlite3.Error as e:
        error(f"Database operation failed: {e}", code=1)


def _report_sync_stats(stats: SyncStats, verb: str, output_format: OutputFormat | None) -> None:
    total = stats.added + stats.updated
    if output_format is OutputFormat.json:
        emit(
            dumps(
                {
                    "action": verb.lower(),
                    "total": total,
                    "added": stats.added,
                    "updated": stats.updated,
                    "price_changes": stats.price_changes,
                    "skipped": stats.skipped,
                }
            ),
            None,
        )
        return
    if output_format is not None:
        error("db sync/import summaries only support --format json", code=2)
    stderr_line(
        "green",
        f"{verb} {total} listings:",
        f"{stats.added} new, {stats.updated} updated ({stats.price_changes} price changes), {stats.skipped} skipped",
    )


@db_app.command("sync")
def db_sync(  # noqa: PLR0913
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
    confirm_empty: Annotated[
        bool, typer.Option("--confirm-empty", help="Treat an empty response as a complete search")
    ] = False,
    db_path: DbPathOpt = None,
    output_format: Annotated[OutputFormat | None, typer.Option("--format", "-f")] = None,
) -> None:
    """Fetch listings from Zillow and upsert to local database."""
    if (is_room or is_entire_place) and kind is not ListingKind.rent:
        error("--room and --entire-place only work with 'rent' kind", code=2)
    if not query.strip():
        # A search run is only comparable with another run of the same query
        # when it records one; an empty query would make the run look like a
        # file import to change detection.
        error("Query must not be empty", code=2)

    request = search_options_from_args(locals())
    viewport = prepare_search(request)
    result = fetch_listings(
        as_kind(kind),
        request,
        bbox=viewport.bbox,
        on_progress=lambda used, total: progress(f"tile {used}:", f"{total} listings so far"),
    )
    with _db_session(db_path) as conn:
        sync_result = sync_to_db(
            conn,
            as_kind(kind),
            request,
            result=result,
            confirm_empty=confirm_empty,
        )
    report_notices(sync_result.notices)

    if not sync_result.rows and output_format is not OutputFormat.json:
        warn("No listings found")
        return
    _report_sync_stats(sync_result.stats, "Synced", output_format)


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
    output_format: Annotated[OutputFormat | None, typer.Option("--format", "-f")] = None,
) -> None:
    """Import listings from a saved JSON file (raw response or normalized array)."""
    data = read_json(input_or_dash)

    # Detect input shape: raw rows (camelCase / response keys) get normalized,
    # rows that already use normalized field names are kept as-is so a re-import
    # of `db query` output doesn't null out street/city/etc.
    rows, raw_rows = prepare_import(data)

    if not rows:
        if output_format is OutputFormat.json:
            _report_sync_stats(SyncStats(added=0, updated=0, price_changes=0, skipped=0), "Imported", output_format)
        else:
            warn("No listings to import")
        return

    # Parse timestamp; store it in the canonical fixed-width UTC format.
    fetched_at = normalize_utc_timestamp(fetched_at, label="--fetched-at") if fetched_at else utc_now()

    with _db_session(db_path) as conn:
        result = import_rows(conn, rows, raw_rows, kind=as_kind(kind), seen_utc=fetched_at)
    _report_sync_stats(result.stats, "Imported", output_format)


@db_app.command("query")
def db_query(  # noqa: PLR0913
    where: WhereOpt = None,
    where_json: WhereJSONOpt = None,
    sort: SortOpt = None,
    cols: ColsOpt = None,
    limit: LimitOpt = None,
    db_path: DbPathOpt = None,
    output_format: FormatOpt = OutputFormat.json,
    output: OutputOpt = None,
) -> None:
    """Query listings from the local database."""
    with _db_session(db_path, must_exist=True, readonly=True) as conn:
        rows, selected = cli_query_database(
            conn,
            where=where,
            where_json=where_json,
            sort=sort,
            cols=cols,
            limit=limit,
        )

    if not rows and output_format is not OutputFormat.json:
        warn("No listings matched the query")
        return

    write_output(rows, output_format, output, columns=selected, use_db_columns=True)


@db_app.command("history")
def db_history(
    zpid: Annotated[int, typer.Argument(help="Zillow property ID")],
    db_path: DbPathOpt = None,
    output_format: FormatOpt = OutputFormat.json,
) -> None:
    """Show price history for a single listing."""
    with _db_session(db_path, must_exist=True, readonly=True) as conn:
        history = get_price_history(conn, zpid)

    if not history and output_format is not OutputFormat.json:
        warn(f"No price history found for zpid {zpid}")
        return

    write_output(history, output_format)


@db_app.command("changes")
def db_changes(
    since: Annotated[str, typer.Argument(help="UTC timestamp lower bound, e.g. 2026-10-01T00:00:00Z")],
    db_path: DbPathOpt = None,
    output_format: FormatOpt = OutputFormat.json,
    output: OutputOpt = None,
) -> None:
    """Report listing changes since a timestamp."""
    since = normalize_utc_timestamp(since, label="SINCE argument")
    with _db_session(db_path, must_exist=True, readonly=True) as conn:
        events = get_changes(conn, since)
    write_output(events, output_format, output)


@db_app.command("stats")
def db_stats_cmd(
    db_path: DbPathOpt = None,
    output_format: Annotated[OutputFormat | None, typer.Option("--format", "-f")] = None,
) -> None:
    """Show database statistics."""
    with _db_session(db_path, must_exist=True, readonly=True) as conn:
        stats = db_stats(conn)

    if output_format is OutputFormat.json or (output_format is None and not sys.stdout.isatty()):
        emit(
            dumps(
                {
                    "total_listings": stats.total_listings,
                    "oldest_seen": stats.oldest_seen,
                    "newest_seen": stats.newest_seen,
                    "db_path": stats.db_path,
                    "db_size_bytes": stats.db_size_bytes,
                }
            ),
            None,
        )
        return
    if output_format is not None:
        error("db stats only supports --format json", code=2)

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
