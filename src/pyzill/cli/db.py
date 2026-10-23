"""Database commands: Typer wiring only.

The sync and import pipelines live in :mod:`pyzill.workflows`; this module
parses the flags, calls the workflow, maps its errors to exit codes, and
renders the result.
"""

import sqlite3
import sys
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Any

import typer

from pyzill._kinds import ListingKind, as_kind
from pyzill.api import get_from_home_id
from pyzill.cli.console import (
    context,
    emit,
    error,
    network_errors,
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
from pyzill.cli.output import DB_LISTING_TABLE_COLUMNS, OutputFormat, dumps
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
    list_runs,
    normalize_utc_timestamp,
    prune_database,
    resolve_db_path,
    upsert_property_details,
    utc_now,
)
from pyzill.workflows import SyncResult, fetch_listings, import_rows, prepare_import, sync_to_db

db_app = typer.Typer(name="db", help="Local SQLite database for tracking listings")


class _WarningState:
    legacy_db_shown: bool = False


_warning_state = _WarningState()


def _timestamp(value: str, *, label: str) -> str:
    """Normalize a timestamp, reporting an invalid one as a usage error."""
    try:
        return normalize_utc_timestamp(value, label=label)
    except ValueError as exc:
        error(str(exc), code=2)


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


def _require_json_summary(output_format: OutputFormat | None) -> None:
    """Reject a format the sync/import summary cannot render.

    The check runs before the work, not after it: an empty result used to
    skip it, so the same invalid flag exited 0 on an empty file and 2 on a
    file with rows.
    """
    if output_format is not None and output_format is not OutputFormat.json:
        error("db sync/import summaries only support --format json", code=2)


def _report_sync_stats(result: SyncResult, verb: str, output_format: OutputFormat | None) -> None:
    """Print the summary of a sync or an import.

    The JSON summary carries the run's id and whether the run was truncated,
    so a script can tell a complete run from a partial one without reading
    the human-readable warnings on stderr.
    """
    stats = result.stats
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
                    "run_id": result.run_id,
                    "truncated": result.truncated,
                }
            ),
            None,
        )
        return
    stderr_line(
        "green",
        f"{verb} {total} listings:",
        f"{stats.added} new, {stats.updated} updated ({stats.price_changes} price changes), {stats.skipped} skipped",
    )


@db_app.command("sync")
def db_sync(
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
    _require_json_summary(output_format)

    request = search_options_from_args(locals())
    viewport = prepare_search(request)
    with network_errors("Search failed"):
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
    _report_sync_stats(sync_result, "Synced", output_format)


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
    _require_json_summary(output_format)
    data = read_json(input_or_dash)

    # Detect input shape: raw rows (camelCase / response keys) get normalized,
    # rows that already use normalized field names are kept as-is so a re-import
    # of `db query` output doesn't null out street/city/etc.
    try:
        rows, raw_rows = prepare_import(data)
    except ValueError as exc:
        error(str(exc), code=2)

    if not rows:
        if output_format is OutputFormat.json:
            _report_sync_stats(
                SyncResult(stats=SyncStats(added=0, updated=0, price_changes=0, skipped=0)),
                "Imported",
                output_format,
            )
        else:
            warn("No listings to import")
        return

    # Parse timestamp; store it in the canonical fixed-width UTC format.
    fetched_at = _timestamp(fetched_at, label="--fetched-at") if fetched_at else utc_now()

    with _db_session(db_path) as conn:
        result = import_rows(conn, rows, raw_rows, kind=as_kind(kind), seen_utc=fetched_at)
    _report_sync_stats(result, "Imported", output_format)


@db_app.command("query")
def db_query(
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

    write_output(rows, output_format, output, columns=selected, default_columns=DB_LISTING_TABLE_COLUMNS)


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

    write_output(history, output_format, default_columns=None, title="Price history")


@db_app.command("changes")
def db_changes(
    since: Annotated[str, typer.Argument(help="UTC timestamp lower bound, e.g. 2026-10-01T00:00:00Z")],
    db_path: DbPathOpt = None,
    output_format: FormatOpt = OutputFormat.json,
    output: OutputOpt = None,
) -> None:
    """Report listing changes since a timestamp."""
    since = _timestamp(since, label="SINCE argument")
    with _db_session(db_path, must_exist=True, readonly=True) as conn:
        events = get_changes(conn, since)
    write_output(events, output_format, output, default_columns=None, title="Changes", empty_message="No changes found")


@db_app.command("runs")
def db_runs(
    kind: Annotated[
        ListingKind | None,
        typer.Option("--kind", help="Only runs of this kind: sale, rent, or sold"),
    ] = None,
    limit: LimitOpt = None,
    db_path: DbPathOpt = None,
    output_format: FormatOpt = OutputFormat.json,
    output: OutputOpt = None,
) -> None:
    """List the recorded sync runs, newest first.

    A run with no query text is an import or an anonymous upsert rather than
    a search snapshot, so the query column says which kind of run it was.
    """
    with _db_session(db_path, must_exist=True, readonly=True) as conn:
        runs = list_runs(conn, kind=kind, limit=limit)
    write_output(
        [asdict(run) for run in runs],
        output_format,
        output,
        default_columns=None,
        title="Sync runs",
        empty_message="No sync runs found",
    )


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


@db_app.command("prune")
def db_prune(
    older_than: Annotated[
        int | None,
        typer.Option("--older-than", help="Delete runs that started more than this many days ago"),
    ] = None,
    keep_runs: Annotated[
        int | None,
        typer.Option("--keep-runs", help="Keep only the newest N runs"),
    ] = None,
    vacuum: Annotated[
        bool,
        typer.Option("--vacuum", help="Rebuild the database file to reclaim the freed space"),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Report what would be deleted without changing the database"),
    ] = False,
    db_path: DbPathOpt = None,
    output_format: Annotated[OutputFormat | None, typer.Option("--format", "-f")] = None,
) -> None:
    """Delete old sync runs and their raw sightings.

    The listings and their price history are kept; what goes away is the
    per-search record (including the raw payloads) that grows with every run.
    Use --older-than DAYS, --keep-runs N, or both. --vacuum reclaims the file
    space afterwards. --dry-run reports what would be deleted without
    changing the database, so a destructive prune can be reviewed first.
    """
    if older_than is None and keep_runs is None and not vacuum and not dry_run:
        error("Nothing to prune: pass --older-than, --keep-runs, --vacuum, or --dry-run", code=2)
    if (older_than is not None and older_than < 0) or (keep_runs is not None and keep_runs < 0):
        error("--older-than and --keep-runs must be non-negative", code=2)
    with _db_session(db_path, must_exist=True) as conn:
        stats = prune_database(conn, older_than_days=older_than, keep_runs=keep_runs, vacuum=vacuum, dry_run=dry_run)

    if output_format is OutputFormat.json or (output_format is None and not sys.stdout.isatty()):
        emit(dumps(asdict(stats)), None)
        return
    if output_format is not None:
        error("db prune only supports --format json", code=2)
    stderr_line(
        "green",
        "Pruned" if not dry_run else "Would prune",
        f"{stats.runs_deleted} runs, {stats.sightings_deleted} sightings"
        + (", database vacuumed" if stats.vacuumed else ""),
    )


def _enrich_listing(conn: sqlite3.Connection, zpid: int, kind: str, http_kwargs: dict[str, Any]) -> None:
    """Fetch one property detail record and upsert it; aborts the command on failure."""
    with network_errors("Detail fetch failed"):
        detail = get_from_home_id(int(zpid), **http_kwargs)
    upsert_property_details(conn, detail, kind=str(kind))


@db_app.command("enrich")
def db_enrich(
    where: WhereOpt = None,
    where_json: WhereJSONOpt = None,
    sort: SortOpt = None,
    limit: LimitOpt = None,
    max_requests: Annotated[int | None, typer.Option("--max-requests", help="Maximum detail requests to make")] = None,
    db_path: DbPathOpt = None,
    output_format: Annotated[OutputFormat | None, typer.Option("--format", "-f")] = None,
) -> None:
    """Fetch property details for shortlisted listings and store them."""
    with _db_session(db_path, must_exist=True, readonly=True) as conn:
        rows, _ = cli_query_database(conn, where=where, where_json=where_json, sort=sort, limit=limit)

    if not rows:
        if output_format is OutputFormat.json:
            emit(dumps({"enriched": 0}), None)
        else:
            warn("No listings to enrich")
        return

    budget = max_requests if max_requests is not None else 50
    if budget < 0:
        error("--max-requests must be non-negative", code=2)

    enriched = 0
    http_kwargs = context().http_kwargs
    with _db_session(db_path) as conn:
        for i, row in enumerate(rows):
            if i >= budget:
                break
            zpid = row.get("zpid")
            if zpid is None:
                continue
            kind = row.get("kind") or "sale"
            _enrich_listing(conn, int(zpid), kind, http_kwargs)
            enriched += 1
            progress(f"enriched {enriched}:", f"{zpid}")

    if output_format is OutputFormat.json:
        emit(dumps({"enriched": enriched}), None)
        return
    stderr_line("green", "Enriched", f"{enriched} listings")
