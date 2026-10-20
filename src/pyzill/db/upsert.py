"""Writing normalized listings into the database.

Holds the UPSERT statement, the batched pre-reads that decide what changed,
and the write path that stores listings, price history, sightings, and status
transitions inside one transaction.
"""

import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pyzill._kinds import KINDS, Kind
from pyzill.db.runs import begin_sync_run, finish_sync_run, utc_now
from pyzill.db.schema import _LISTING_TABLE_COLUMNS
from pyzill.normalize import parse_price

__all__ = ["SyncStats", "upsert_listings"]


@dataclass
class SyncStats:
    """Statistics from a sync/import operation.

    ``price_changes`` is a per-batch heuristic, not a durable metric: each
    priced sighting of an existing same-kind listing is compared against the
    recorded state, so it counts a price that differs from the most recent
    prior recorded price. New zpids never count; the first-ever price on an
    existing listing counts. It is useful for "what changed in this sync"
    reporting, not for long-term tracking — use ``price_history`` for that.
    """

    added: int
    updated: int
    price_changes: int
    skipped: int


_DATA_COLUMNS = (
    "price_label",
    "beds",
    "baths",
    "area",
    "address",
    "street",
    "city",
    "state",
    "zipcode",
    "latitude",
    "longitude",
    "home_type",
    "status",
    "status_type",
    "detail_url",
    "image_url",
    "zestimate",
    "rent_zestimate",
    "days_on_zillow",
    "price_change",
    "price_change_date",
    "price_reduction",
    "lot_area_value",
    "lot_area_unit",
    "year_built",
    "tax_assessed_value",
    "listing_sub_type",
    "unit",
    "broker_name",
)


_CHUNK = 500


def _placeholders(count: int) -> str:
    return ", ".join("?" * count)


def _listing_upsert_sql() -> str:
    """Build the listings UPSERT used by upsert_listings.

    A sighting updates the row only when it is at least as recent as the
    stored one; older backfills extend first_seen_utc only. Descriptive
    fields use COALESCE so missing values never erase known ones.

    The conflict target is ``(zpid, kind)``, so an insert can never change a
    row's kind; there is no kind-transition branch.
    """
    columns = ", ".join(_LISTING_TABLE_COLUMNS)
    placeholders = _placeholders(len(_LISTING_TABLE_COLUMNS))
    current = "excluded.last_seen_utc >= listings.last_seen_utc"
    assignments = [f"price = CASE WHEN {current} THEN COALESCE(excluded.price, listings.price) ELSE listings.price END"]
    assignments.extend(
        f"{column} = CASE WHEN {current} THEN COALESCE(excluded.{column}, listings.{column}) ELSE listings.{column} END"
        for column in _DATA_COLUMNS
    )
    assignments.append("first_seen_utc = MIN(listings.first_seen_utc, excluded.first_seen_utc)")
    assignments.append("last_seen_utc = MAX(listings.last_seen_utc, excluded.last_seen_utc)")
    assignment_sql = ",\n        ".join(assignments)
    # Column names and placeholders come from fixed module constants, not user input.
    return (
        f"INSERT INTO listings ({columns}) VALUES ({placeholders}) ON CONFLICT(zpid, kind) DO UPDATE SET"  # noqa: S608
        f"\n        {assignment_sql}"
    )


_LISTING_UPSERT_SQL = _listing_upsert_sql()


def _prefetch(
    conn: sqlite3.Connection, zpids: list[int], kind: str, seen_utc: str
) -> tuple[dict[tuple[int, str], Any], dict[tuple[int, str], int | None]]:
    """Read pre-batch listing state and prior prices for the batch zpids.

    Returns zpid -> (price, kind) for listings that already exist, and
    zpid -> previous price for this kind (an entry with the batch timestamp
    wins over the latest strictly-earlier one; missing when none exists).
    """
    existing: dict[tuple[int, str], Any] = {}
    prior_prices: dict[tuple[int, str], int | None] = {}
    for start in range(0, len(zpids), _CHUNK):
        chunk = zpids[start : start + _CHUNK]
        marks = _placeholders(len(chunk))
        for zpid, price in conn.execute(
            f"SELECT zpid, price FROM listings WHERE zpid IN ({marks}) AND kind = ?",  # noqa: S608
            (*chunk, kind),
        ):
            existing[(zpid, kind)] = price
        exact: dict[int, int] = {}
        latest_before: dict[int, tuple[str, int]] = {}
        for zpid, price, row_seen in conn.execute(
            f"SELECT zpid, price, seen_utc FROM price_history WHERE zpid IN ({marks}) AND kind = ? AND seen_utc <= ?",  # noqa: S608
            (*chunk, kind, seen_utc),
        ):
            if row_seen == seen_utc:
                exact[zpid] = price
            elif zpid not in latest_before or row_seen > latest_before[zpid][0]:
                latest_before[zpid] = (row_seen, price)
        for zpid in chunk:
            key = (zpid, kind)
            if zpid in exact:
                prior_prices[key] = exact[zpid]
            elif zpid in latest_before:
                prior_prices[key] = latest_before[zpid][1]
    return existing, prior_prices


def _latest_statuses(conn: sqlite3.Connection, zpids: list[int], kind: str, seen_utc: str) -> dict[int, str]:
    """Return zpid -> latest status recorded at or before ``seen_utc``.

    Replaces a per-row SELECT so a batch issues one query per chunk instead of
    one per listing.
    """
    latest: dict[int, tuple[str, str]] = {}
    for start in range(0, len(zpids), _CHUNK):
        chunk = zpids[start : start + _CHUNK]
        marks = _placeholders(len(chunk))
        query = (
            "SELECT zpid, status, first_seen_utc FROM status_history "  # noqa: S608 - values are bound
            f"WHERE zpid IN ({marks}) AND kind = ? AND first_seen_utc <= ?"
        )
        for zpid, status, row_seen in conn.execute(query, (*chunk, kind, seen_utc)):
            current = latest.get(zpid)
            if current is None or row_seen > current[0]:
                latest[zpid] = (row_seen, status)
    return {zpid: status for zpid, (_, status) in latest.items()}


def upsert_listings(  # noqa: PLR0912, PLR0913, PLR0915
    conn: sqlite3.Connection,
    rows: Sequence[Mapping[str, Any]],
    *,
    seen_utc: str | None = None,
    kind: Kind = "sale",
    raw_rows: Sequence[Mapping[str, Any]] | None = None,
    run_id: int | None = None,
    truncated: bool = False,
) -> SyncStats:
    """Upsert listing rows into the database and finalize their sync run.

    Skips rows without a zpid (counted in stats). For each zpid:
        - ``listings`` stores the latest sighting for each ``(zpid, kind)``
          pair, so a sighting never changes a row's kind.
        - ``price_history`` stores priced sightings partitioned by kind.
        - Missing fields never erase known values; a missing price keeps the
          last price for the same ``(zpid, kind)``.

    Existing-row state, prior prices, and prior statuses are read in batched
    queries before any writes; the write itself is one UPSERT per row. The run
    is finalized and committed in that same transaction.

    Args:
        conn: Database connection.
        rows: List of normalized listing dicts (must have 'zpid' field).
        seen_utc: Timestamp for this batch (default: utc_now()).
        run_id: Existing sync_run to attach sightings to (created if omitted).
        truncated: Whether the source result was partial (capped search or
            exhausted request budget); stored on the run so change detection
            can skip comparing it.

    Returns:
        SyncStats with counts of added/updated/price_changes/skipped.
        ``price_changes`` is a per-batch heuristic — see :class:`SyncStats`.
    """
    if seen_utc is None:
        seen_utc = utc_now()
    if kind not in KINDS:
        raise ValueError("kind must be one of: sale, rent, sold")

    stats = SyncStats(added=0, updated=0, price_changes=0, skipped=0)

    # parse_price tolerates raw "$459,000" strings from unnormalized rows
    # and maps 0/"$0" (Zillow's unknown sentinel) to None.
    prepared: list[tuple[int, Mapping[str, Any], int | None, Mapping[str, Any]]] = []

    for index, row in enumerate(rows):
        zpid = row.get("zpid")
        if zpid is None:
            stats.skipped += 1
            continue
        try:
            zpid = int(zpid)
        except (ValueError, TypeError):
            stats.skipped += 1
            continue
        raw_row = raw_rows[index] if raw_rows is not None and index < len(raw_rows) else row
        prepared.append((zpid, row, parse_price(row.get("price")), raw_row))

    zpids = list(dict.fromkeys(zpid for zpid, _, _, _ in prepared))
    existing, prior_prices = _prefetch(conn, zpids, kind, seen_utc)
    prior_statuses = _latest_statuses(conn, zpids, kind, seen_utc)
    if run_id is None:
        run_id = begin_sync_run(conn, kind=kind, started_utc=seen_utc)

    listing_params: list[tuple[Any, ...]] = []
    history_params: list[tuple[int, str, int, str]] = []
    sighting_params: list[tuple[Any, ...]] = []
    status_entries: list[tuple[int, str, str, str]] = []
    added_in_batch: set[tuple[int, str]] = set()
    for zpid, row, price_int, raw_row in prepared:
        key = (zpid, kind)
        if key in existing:
            stats.updated += 1
            old_price = existing[key]
            prior_price = prior_prices.get(key)
            if price_int is not None and (
                (prior_price is not None and prior_price != price_int) or (prior_price is None and old_price is None)
            ):
                stats.price_changes += 1
        elif key in added_in_batch:
            stats.updated += 1
        else:
            stats.added += 1
            added_in_batch.add(key)

        listing_params.append(
            (zpid, kind, price_int, *(row.get(column) for column in _DATA_COLUMNS), seen_utc, seen_utc)
        )
        if price_int is not None and prior_prices.get(key) != price_int:
            history_params.append((zpid, kind, price_int, seen_utc))
        status = row.get("status")
        status_type = row.get("status_type")
        sighting_params.append(
            (
                run_id,
                zpid,
                kind,
                price_int,
                status,
                status_type,
                seen_utc,
                json.dumps(raw_row, ensure_ascii=False, allow_nan=False),
            )
        )
        if isinstance(status, str) and status and prior_statuses.get(zpid) != status:
            status_entries.append((zpid, kind, status, seen_utc))
            # Later rows for the same zpid in this batch must compare against
            # the status we just staged, not the stale pre-batch one.
            prior_statuses[zpid] = status

    conn.executemany(_LISTING_UPSERT_SQL, listing_params)
    if history_params:
        conn.executemany(
            "INSERT INTO price_history (zpid, kind, price, seen_utc) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(zpid, kind, seen_utc) DO UPDATE SET price = excluded.price",
            history_params,
        )
    if sighting_params:
        conn.executemany(
            "INSERT OR REPLACE INTO sightings "
            "(run_id, zpid, kind, price, status, status_type, seen_utc, raw_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            sighting_params,
        )
    if status_entries:
        conn.executemany(
            "INSERT INTO status_history (zpid, kind, status, first_seen_utc) VALUES (?, ?, ?, ?) "
            "ON CONFLICT DO NOTHING",
            status_entries,
        )
    finish_sync_run(
        conn,
        run_id,
        result_count=len(sighting_params),
        truncated=truncated,
        finished_utc=seen_utc,
    )
    conn.commit()
    return stats
