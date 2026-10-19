"""Database commands plus shared path resolution and session helpers."""

import datetime as dt
import sqlite3
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import closing, contextmanager
from dataclasses import asdict
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
    WhereJSONOpt,
    WhereOpt,
    Zoom,
)
from pyzill.cli.output import OutputFormat, dumps
from pyzill.cli.query import cli_query_database
from pyzill.cli.search import extract_listings, fetch_and_normalize, search_filter_options, search_options
from pyzill.config import read_env
from pyzill.db import SyncStats, begin_sync_run, comparable_run_had_results, connect_readonly, get_changes
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


def _upsert_and_report(  # noqa: PLR0913
    rows: Sequence[Mapping[str, Any]],
    db_path: str | None,
    verb: str,
    *,
    seen_utc: str | None = None,
    kind: ListingKind = ListingKind.sale,
    output_format: OutputFormat | None = None,
    raw_rows: Sequence[Mapping[str, Any]] | None = None,
) -> None:
    """Upsert rows in one transaction and print the sync summary to stderr."""
    with _db_session(db_path) as conn:
        stats = upsert_listings(conn, rows, seen_utc=seen_utc, kind=kind.value, raw_rows=raw_rows)
    _report_sync_stats(stats, verb, output_format)


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


def _looks_normalized(row: Any) -> bool:
    """True when a row uses normalized field names (so normalizing would null them)."""
    return isinstance(row, dict) and set(row) <= _NORMALIZED_KEYS


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

    filters = search_filter_options(
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
    options = search_options(query, filters, is_room, is_entire_place)
    metadata: dict[str, Any] = {}
    response = fetch_and_normalize(
        _SEARCH_FNS[kind],
        options,
        raw=True,
        metadata=metadata,
    )
    raw_rows = [row for row in extract_listings(response) if isinstance(row, dict) and not is_building_result(row)]
    normalized_rows = [normalize_listing(row) for row in raw_rows]
    pairs = [(normalized, raw) for normalized, raw in zip(normalized_rows, raw_rows, strict=True) if normalized]
    rows = [normalized for normalized, _ in pairs]
    raw_rows = [raw for _, raw in pairs]
    started_utc = utc_now()
    filter_values = asdict(filters)
    filter_values.update(is_room=is_room, is_entire_place=is_entire_place)
    bbox = metadata.get("bbox")
    truncated = bool(response.get("truncated", False))
    with _db_session(db_path) as conn:
        # A zero-row answer for a search that previously matched is more likely
        # a soft block or transient glitch than a search whose listings all
        # vanished; flag it truncated so change detection skips the run.
        if (
            not rows
            and not confirm_empty
            and comparable_run_had_results(conn, kind=kind.value, query=query, bbox=bbox, filters=filter_values)
        ):
            truncated = True
            warn(
                "Zillow returned no listings for a search that previously matched; "
                "recording this run as truncated (use --confirm-empty to accept a verified empty result)"
            )
        run_id = begin_sync_run(
            conn,
            kind=kind.value,
            query=query,
            bbox=bbox,
            filters=filter_values,
            started_utc=started_utc,
        )
        # upsert_listings finalizes the run inside its own transaction, so the
        # truncated flag is persisted rather than rolled back with the session.
        stats = upsert_listings(
            conn,
            rows,
            raw_rows=raw_rows,
            seen_utc=started_utc,
            kind=kind.value,
            run_id=run_id,
            truncated=truncated,
        )

    if not rows and output_format is not OutputFormat.json:
        warn("No listings found")
        return
    _report_sync_stats(stats, "Synced", output_format)


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

    if not imported:
        if output_format is OutputFormat.json:
            _report_sync_stats(SyncStats(added=0, updated=0, price_changes=0, skipped=0), "Imported", output_format)
        else:
            warn("No listings to import")
        return

    # Parse timestamp; store it in the canonical fixed-width UTC format.
    fetched_at = normalize_utc_timestamp(fetched_at, label="--fetched-at") if fetched_at else utc_now()

    _upsert_and_report(
        imported,
        db_path,
        "Imported",
        seen_utc=fetched_at,
        kind=kind,
        output_format=output_format,
        raw_rows=raw_imported,
    )


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

    if not rows:
        warn("No listings in database")
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
