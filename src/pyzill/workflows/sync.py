"""The sync and import pipelines: what a search stores, and how.

A sync searches Zillow and upserts the result into the local database.  An
import stores listings the caller already has.  Both finish a sync run, so
the database knows which run saw which listings.
"""

import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict
from typing import Any

from pyzill._kinds import Kind
from pyzill.bbox import BBox
from pyzill.client import ZillowClient
from pyzill.db import SyncStats, begin_sync_run, comparable_run_had_results, upsert_listings
from pyzill.db.runs import utc_now
from pyzill.normalize import Listing, is_building_result, normalize_listing
from pyzill.workflows.extract import extract_listings
from pyzill.workflows.model import (
    BUDGET_FILTERS,
    VIEWPORT_FILTERS,
    Notice,
    SearchRequest,
    SearchResult,
    SyncResult,
)
from pyzill.workflows.search import fetch_listings
from pyzill.workflows.viewport import Geocoder

__all__ = ["import_rows", "prepare_import", "sync_to_db"]


def _looks_normalized(row: Any) -> bool:
    """True when a row uses normalized field names (so normalizing would null them)."""
    normalized_keys = frozenset(Listing.__annotations__) | {"first_seen_utc", "last_seen_utc"}
    return isinstance(row, dict) and set(row) <= normalized_keys


def _comparable_filters(request: SearchRequest) -> dict[str, Any]:
    """Return the filter values that make two sync runs comparable.

    Two runs are comparable when they searched the same thing: the same
    query, the same viewport (recorded beside the filters as the run's
    bbox), and the same search filters.  The viewport bounds and the
    tiling knobs are not part of that -- the bounds are already the
    run's viewport, and the tiling knobs describe how the run was carried
    out rather than what it searched.  A request budget, in particular,
    only truncates a run, and a truncated run is recorded as such and
    never compared.
    """
    values = asdict(request.filters)
    for name in (*VIEWPORT_FILTERS, *BUDGET_FILTERS):
        values.pop(name, None)
    values["is_room"] = request.is_room
    values["is_entire_place"] = request.is_entire_place
    return values


def prepare_import(data: Any) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    """Split an imported payload into storable rows and their raw rows.

    Rows that already use normalized field names are kept as-is so a
    re-import of ``db query`` output does not null out street/city/etc.;
    raw rows are normalized. Building cards and rows without a zpid are
    dropped.

    An imported row only has to carry a zpid, not an integer one: the
    write path coerces the zpid and counts the rows it cannot store.  A
    search is stricter, because a card without a usable zpid is not a
    listing there either.
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
    """Upsert prepared import rows and report the statistics.

    The import is recorded as a run of its own, so the result can name it.
    The run carries no query text: an import says nothing about which
    listings exist, so its absences never count as delistings.
    """
    if not rows:
        return SyncResult(stats=SyncStats())
    seen_utc = seen_utc or utc_now()
    run_id = begin_sync_run(conn, kind=kind, started_utc=seen_utc)
    stats = upsert_listings(
        conn,
        rows,
        seen_utc=seen_utc,
        kind=kind,
        raw_rows=raw_rows,
        run_id=run_id,
    )
    return SyncResult(stats=stats, rows=list(rows), run_id=run_id)


def sync_to_db(  # noqa: PLR0913 - the sync pipeline's inputs
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
    started_utc = utc_now()
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
    seen_utc = utc_now()
    filter_values = _comparable_filters(request)
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
        seen_utc=seen_utc,
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
