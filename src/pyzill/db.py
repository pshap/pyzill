"""SQLite database for tracking Zillow listings with price history.

Uses stdlib sqlite3 for zero new dependencies. Database lives at ./pyzill.db
by default (configurable via --db or PYZILL_DB).
"""

import json
import os
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pyzill.normalize import parse_price

__all__ = [
    "DBStats",
    "SyncStats",
    "begin_sync_run",
    "comparable_run_had_results",
    "connect",
    "connect_readonly",
    "db_stats",
    "default_db_path",
    "finish_sync_run",
    "get_changes",
    "get_price_history",
    "load_listings",
    "upsert_listings",
    "utc_now",
]


def default_db_path() -> Path:
    """Return the XDG data directory's pyzill database path."""
    data_home = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return data_home / "pyzill" / "pyzill.db"


def utc_now() -> str:
    """Return current UTC time as ISO string (fixed-width %Y-%m-%dT%H:%M:%SZ)."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def begin_sync_run(  # noqa: PLR0913
    conn: sqlite3.Connection,
    *,
    kind: str,
    query: str = "",
    bbox: Mapping[str, Any] | None = None,
    filters: Mapping[str, Any] | None = None,
    started_utc: str | None = None,
) -> int:
    """Create a run record and return its database ID."""
    cursor = conn.execute(
        "INSERT INTO sync_runs (started_utc, kind, query, bbox_json, filters_json) VALUES (?, ?, ?, ?, ?)",
        (
            started_utc or utc_now(),
            kind,
            query,
            json.dumps(bbox, sort_keys=True) if bbox is not None else None,
            json.dumps(filters, sort_keys=True) if filters is not None else None,
        ),
    )
    if cursor.lastrowid is None:
        raise sqlite3.DatabaseError("Failed to retrieve sync_run id")
    return int(cursor.lastrowid)


def finish_sync_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    result_count: int,
    truncated: bool = False,
    finished_utc: str | None = None,
) -> None:
    """Complete a run after its results have been stored."""
    conn.execute(
        "UPDATE sync_runs SET finished_utc = ?, result_count = ?, truncated = ? WHERE id = ?",
        (finished_utc or utc_now(), result_count, int(truncated), run_id),
    )


def comparable_run_had_results(
    conn: sqlite3.Connection,
    *,
    kind: str,
    query: str = "",
    bbox: Mapping[str, Any] | None = None,
    filters: Mapping[str, Any] | None = None,
) -> bool:
    """Return True if a prior complete run with the same key returned any rows.

    Used to flag an empty result for a search that previously matched as
    suspect (a soft block or transient glitch), so it is not mistaken for a
    complete run in which every listing genuinely disappeared.
    """
    row = conn.execute(
        "SELECT 1 FROM sync_runs WHERE kind = ? AND query = ? AND bbox_json IS ? AND filters_json IS ? "
        "AND finished_utc IS NOT NULL AND truncated = 0 AND result_count > 0 LIMIT 1",
        (
            kind,
            query,
            json.dumps(bbox, sort_keys=True) if bbox is not None else None,
            json.dumps(filters, sort_keys=True) if filters is not None else None,
        ),
    ).fetchone()
    return row is not None


_SCHEMA_VERSION = 3

# Canonical listings columns, in schema order. Shared by the schema DDL, the
# v1->v2 migration, and upsert_listings so the three can never drift apart.
_LISTING_TABLE_COLUMNS = (
    "zpid",
    "kind",
    "price",
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
    "first_seen_utc",
    "last_seen_utc",
)

_CREATE_LISTINGS_SQL = """CREATE TABLE IF NOT EXISTS {name} (
    zpid INTEGER NOT NULL,
    kind TEXT NOT NULL DEFAULT 'sale',
    price INTEGER,
    price_label TEXT,
    beds REAL,
    baths REAL,
    area INTEGER,
    address TEXT,
    street TEXT,
    city TEXT,
    state TEXT,
    zipcode TEXT,
    latitude REAL,
    longitude REAL,
    home_type TEXT,
    status TEXT,
    status_type TEXT,
    detail_url TEXT,
    image_url TEXT,
    zestimate INTEGER,
    rent_zestimate INTEGER,
    days_on_zillow INTEGER,
    price_change INTEGER,
    price_change_date TEXT,
    price_reduction TEXT,
    lot_area_value REAL,
    lot_area_unit TEXT,
    year_built INTEGER,
    tax_assessed_value INTEGER,
    listing_sub_type TEXT,
    unit TEXT,
    broker_name TEXT,
    first_seen_utc TEXT NOT NULL,
    last_seen_utc TEXT NOT NULL,
    PRIMARY KEY (zpid, kind)
);"""


_CREATE_PRICE_HISTORY_SQL = """CREATE TABLE IF NOT EXISTS {name} (
    zpid INTEGER NOT NULL,
  kind TEXT NOT NULL DEFAULT 'sale',
  price INTEGER NOT NULL,
  seen_utc TEXT NOT NULL,
    PRIMARY KEY (zpid, kind, seen_utc),
    FOREIGN KEY (zpid, kind) REFERENCES listings(zpid, kind) ON DELETE CASCADE
);"""

_CREATE_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS ix_listings_kind_zipcode ON listings(kind, zipcode)",
    "CREATE INDEX IF NOT EXISTS ix_listings_kind_city ON listings(kind, city)",
    "CREATE INDEX IF NOT EXISTS ix_listings_kind_price ON listings(kind, price)",
    "CREATE INDEX IF NOT EXISTS ix_listings_last_seen ON listings(last_seen_utc)",
)

_CREATE_TRACKING_SQL = (
    """CREATE TABLE IF NOT EXISTS sync_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_utc TEXT NOT NULL,
                finished_utc TEXT,
                kind TEXT NOT NULL,
                query TEXT NOT NULL,
                bbox_json TEXT,
                filters_json TEXT,
                result_count INTEGER,
                truncated INTEGER NOT NULL DEFAULT 0
        )""",
    """CREATE TABLE IF NOT EXISTS sightings (
                run_id INTEGER NOT NULL REFERENCES sync_runs(id) ON DELETE CASCADE,
                zpid INTEGER NOT NULL,
                kind TEXT NOT NULL,
                price INTEGER,
                status TEXT,
                status_type TEXT,
                seen_utc TEXT NOT NULL,
                raw_json TEXT,
                PRIMARY KEY (run_id, zpid, kind),
                FOREIGN KEY (zpid, kind) REFERENCES listings(zpid, kind) ON DELETE CASCADE
        )""",
    """CREATE TABLE IF NOT EXISTS status_history (
                zpid INTEGER NOT NULL,
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                first_seen_utc TEXT NOT NULL,
                PRIMARY KEY (zpid, kind, status, first_seen_utc),
                FOREIGN KEY (zpid, kind) REFERENCES listings(zpid, kind) ON DELETE CASCADE
        )""",
    "CREATE INDEX IF NOT EXISTS ix_sightings_zpid_kind_seen ON sightings(zpid, kind, seen_utc)",
    "CREATE INDEX IF NOT EXISTS ix_sync_runs_kind_started ON sync_runs(kind, started_utc)",
)


def _migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
    """Rebuild listings so ``kind`` sits second, matching a fresh v2 schema.

    The naive ``ALTER TABLE listings ADD COLUMN kind`` would append it after
    ``last_seen_utc``, leaving migrated and fresh databases with different
    column orders. Rebuilding the parent listings table with foreign_keys=ON
    would cascade-delete price_history rows, and ``ALTER TABLE ... RENAME``
    would repoint child foreign keys at the renamed-away table, so the price
    history is staged in a plain (FK-free) table across the rebuild.
    """
    old_columns = {row[1] for row in conn.execute("PRAGMA table_info(listings)")}
    carried = [col for col in _LISTING_TABLE_COLUMNS if col != "kind" and col in old_columns]

    conn.execute(
        """CREATE TABLE price_history_staging (
            zpid INTEGER NOT NULL,
            price INTEGER NOT NULL,
            seen_utc TEXT NOT NULL
        )"""
    )
    conn.execute(
        "INSERT INTO price_history_staging (zpid, price, seen_utc) SELECT zpid, price, seen_utc FROM price_history"
    )
    conn.execute("DROP TABLE price_history")

    conn.execute(_CREATE_LISTINGS_SQL.format(name="listings_v2"))
    columns_sql = ", ".join(carried)
    # columns_sql is built from the fixed _LISTING_TABLE_COLUMNS constant, not user input.
    conn.execute(f"INSERT INTO listings_v2 ({columns_sql}) SELECT {columns_sql} FROM listings")  # noqa: S608
    conn.execute("ALTER TABLE listings RENAME TO listings_v1")
    conn.execute("ALTER TABLE listings_v2 RENAME TO listings")
    conn.execute("DROP TABLE listings_v1")

    conn.execute(_CREATE_PRICE_HISTORY_SQL.format(name="price_history"))
    conn.execute(
        "INSERT INTO price_history (zpid, kind, price, seen_utc) "
        "SELECT zpid, 'sale', price, seen_utc FROM price_history_staging"
    )
    conn.execute("DROP TABLE price_history_staging")
    for statement in _CREATE_INDEX_SQL:
        conn.execute(statement)


def _migrate_v2_to_v3(conn: sqlite3.Connection) -> None:
    """Rebuild v2 listing tables around composite (zpid, kind) keys."""
    old_columns = {row[1] for row in conn.execute("PRAGMA table_info(listings)")}
    carried = [col for col in _LISTING_TABLE_COLUMNS if col in old_columns]

    conn.execute("CREATE TABLE listings_v2_staging AS SELECT * FROM listings")
    conn.execute("CREATE TABLE price_history_v2_staging AS SELECT * FROM price_history")
    conn.execute("DROP TABLE price_history")
    conn.execute("DROP TABLE listings")

    conn.execute(_CREATE_LISTINGS_SQL.format(name="listings"))
    columns_sql = ", ".join(carried)
    conn.execute(
        f"INSERT INTO listings ({columns_sql}) SELECT {columns_sql} FROM listings_v2_staging"  # noqa: S608
    )
    conn.execute(_CREATE_PRICE_HISTORY_SQL.format(name="price_history"))
    conn.execute(
        "INSERT INTO price_history (zpid, kind, price, seen_utc) "
        "SELECT zpid, kind, price, seen_utc FROM price_history_v2_staging"
    )
    conn.execute("DROP TABLE price_history_v2_staging")
    conn.execute("DROP TABLE listings_v2_staging")
    for statement in _CREATE_INDEX_SQL + _CREATE_TRACKING_SQL:
        conn.execute(statement)


_MIGRATIONS: dict[int, Any] = {1: _migrate_v1_to_v2, 2: _migrate_v2_to_v3}


@dataclass
class SyncStats:
    """Statistics from a sync/import operation.

    ``price_changes`` is a per-batch heuristic, not a durable metric: each
    priced sighting of an existing same-kind listing is compared against the
    recorded state, so it counts a price that differs from the most recent
    prior recorded price. New zpids and kind changes never count; the
    first-ever price on an existing listing counts. It is useful for
    "what changed in this sync" reporting, not for long-term tracking —
    use ``price_history`` for that.
    """

    added: int
    updated: int
    price_changes: int
    skipped: int


def connect(path: Path | str) -> sqlite3.Connection:
    """Connect to the database, creating schema if needed.

    Args:
        path: Path to the SQLite database file.

    Returns:
        Connection with foreign_keys enabled.
    """
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")

    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version > _SCHEMA_VERSION:
        conn.close()
        raise sqlite3.DatabaseError(
            f"Database schema version {version} is newer than supported version {_SCHEMA_VERSION}"
        )

    if version < _SCHEMA_VERSION:
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("BEGIN IMMEDIATE")
            if version == 0:
                conn.execute(_CREATE_LISTINGS_SQL.format(name="listings"))
                conn.execute(_CREATE_PRICE_HISTORY_SQL.format(name="price_history"))
                for statement in _CREATE_INDEX_SQL + _CREATE_TRACKING_SQL:
                    conn.execute(statement)
                version = _SCHEMA_VERSION
            while version < _SCHEMA_VERSION:
                migration = _MIGRATIONS.get(version)
                if migration is None:
                    raise sqlite3.DatabaseError(f"No migration available from schema version {version}")
                migration(conn)
                version += 1
            existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(listings)")}
            for col in _LISTING_TABLE_COLUMNS:
                if col not in existing_cols:
                    col_type = (
                        "REAL"
                        if col == "lot_area_value"
                        else (
                            "INTEGER"
                            if col in ("days_on_zillow", "price_change", "year_built", "tax_assessed_value")
                            else "TEXT"
                        )
                    )
                    conn.execute(f"ALTER TABLE listings ADD COLUMN {col} {col_type}")
            conn.execute(f"PRAGMA user_version = {version}")
            conn.commit()

        except Exception:
            conn.rollback()
            conn.close()
            raise

    return conn


def connect_readonly(path: Path | str) -> sqlite3.Connection:
    """Open an existing database without schema writes or migration attempts."""
    uri = f"{Path(path).resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA query_only = ON")
    return conn


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
    fields use COALESCE so missing values never erase known ones; a kind
    change takes the new price outright.
    """
    columns = ", ".join(_LISTING_TABLE_COLUMNS)
    placeholders = _placeholders(len(_LISTING_TABLE_COLUMNS))
    current = "excluded.last_seen_utc >= listings.last_seen_utc"
    assignments = [
        f"kind = CASE WHEN {current} THEN excluded.kind ELSE listings.kind END",
        (
            f"price = CASE WHEN {current} THEN "
            "CASE WHEN listings.kind != excluded.kind THEN excluded.price "
            "ELSE COALESCE(excluded.price, listings.price) END "
            "ELSE listings.price END"
        ),
    ]
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


def upsert_listings(  # noqa: PLR0912, PLR0913, PLR0915
    conn: sqlite3.Connection,
    rows: Sequence[Mapping[str, Any]],
    *,
    seen_utc: str | None = None,
    kind: Literal["sale", "rent", "sold"] = "sale",
    raw_rows: Sequence[Mapping[str, Any]] | None = None,
    run_id: int | None = None,
    truncated: bool = False,
) -> SyncStats:
    """Upsert listing rows into the database and finalize their sync run.

    Skips rows without a zpid (counted in stats). For each zpid:
        - ``listings`` stores the latest sighting for each zpid and its kind
          (the primary key is ``(zpid, kind)``, so a sighting never changes a
          row's kind).
        - ``price_history`` stores priced sightings partitioned by kind.
        - Missing fields never erase known values; a missing price keeps the last
            price for the same kind.

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
    if kind not in ("sale", "rent", "sold"):
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
        if isinstance(status, str) and status:
            previous_status = conn.execute(
                "SELECT status FROM status_history WHERE zpid = ? AND kind = ? "
                "AND first_seen_utc <= ? ORDER BY first_seen_utc DESC LIMIT 1",
                (zpid, kind, seen_utc),
            ).fetchone()
            if previous_status is None or previous_status[0] != status:
                status_entries.append((zpid, kind, status, seen_utc))

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


def load_listings(
    conn: sqlite3.Connection,
    *,
    filters: Mapping[str, Any] | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Load listings with optional equality filters and a row bound.

    Returns:
        List of listing dicts (with first_seen_utc and last_seen_utc).
    """
    if limit is not None and limit < 1:
        raise ValueError("limit must be at least 1")
    fields = set(_LISTING_TABLE_COLUMNS)
    conditions: list[str] = []
    parameters: list[Any] = []
    for field, value in (filters or {}).items():
        if field not in fields:
            raise ValueError(f"Unknown listing filter: {field}")
        conditions.append(f'"{field}" IS ?' if value is None else f'"{field}" = ?')
        parameters.append(value)
    sql = "SELECT * FROM listings"
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += " ORDER BY last_seen_utc DESC, zpid DESC, kind ASC"
    if limit is not None:
        sql += " LIMIT ?"
        parameters.append(limit)
    cursor = conn.execute(sql, parameters)
    columns = [desc[0] for desc in cursor.description]
    return [dict(zip(columns, row, strict=False)) for row in cursor.fetchall()]


def get_price_history(conn: sqlite3.Connection, zpid: int) -> list[dict[str, Any]]:
    """Get price history for a specific listing.

    Args:
        conn: Database connection.
        zpid: Zillow property ID.

    Returns:
        List of {price, seen_utc} dicts, sorted by seen_utc ascending.
    """
    cursor = conn.execute(
        "SELECT price, seen_utc, kind FROM price_history WHERE zpid = ? ORDER BY seen_utc ASC, kind ASC",
        (zpid,),
    )
    columns = [desc[0] for desc in cursor.description]
    return [dict(zip(columns, row, strict=False)) for row in cursor.fetchall()]


def get_changes(conn: sqlite3.Connection, since: str) -> list[dict[str, Any]]:
    """Summarize new listings, price/status transitions, and complete-run absences."""
    events: list[dict[str, Any]] = []
    listings = {(row["zpid"], row["kind"]): row for row in load_listings(conn)}

    def observed_days(key: tuple[int, str]) -> int:
        first_seen = listings[key]["first_seen_utc"]
        first = datetime.fromisoformat(first_seen)
        return max(0, (datetime.now(UTC) - first).days)

    for key, listing in listings.items():
        if listing["first_seen_utc"] >= since:
            events.append(
                {
                    "event": "new",
                    "zpid": key[0],
                    "kind": key[1],
                    "price": listing["price"],
                    "days_observed": observed_days(key),
                }
            )

    history = conn.execute(
        "SELECT zpid, kind, price, seen_utc FROM price_history ORDER BY zpid, kind, seen_utc"
    ).fetchall()
    latest: dict[tuple[int, str], int] = {}
    for zpid, kind, price, seen_utc in history:
        key = (zpid, kind)
        previous = latest.get(key)
        if seen_utc >= since and previous is not None and previous != price:
            events.append(
                {
                    "event": "price_drop" if price < previous else "price_increase",
                    "zpid": zpid,
                    "kind": kind,
                    "old_price": previous,
                    "price": price,
                    "seen_utc": seen_utc,
                    "days_observed": observed_days(key) if key in listings else None,
                }
            )
        latest[key] = price

    for zpid, kind, status, first_seen_utc in conn.execute(
        "SELECT zpid, kind, status, first_seen_utc FROM status_history "
        "WHERE first_seen_utc >= ? ORDER BY first_seen_utc",
        (since,),
    ):
        events.append(
            {
                "event": "status_change",
                "zpid": zpid,
                "kind": kind,
                "status": status,
                "seen_utc": first_seen_utc,
                "days_observed": observed_days((zpid, kind)) if (zpid, kind) in listings else None,
            }
        )

    run_rows = conn.execute(
        "SELECT id, started_utc, finished_utc, kind, query, bbox_json, filters_json FROM sync_runs "
        "WHERE finished_utc IS NOT NULL AND truncated = 0 ORDER BY started_utc, id"
    ).fetchall()
    comparable: dict[tuple[str, str, str | None, str | None], list[tuple[int, str]]] = {}
    for run_id, started_utc, _, kind, query, bbox_json, filters_json in run_rows:
        # A run is only comparable to another with the same kind, query,
        # viewport, *and* filters: an unfiltered run after a --max-price run
        # would otherwise report every excluded listing as delisted.
        comparable.setdefault((kind, query, bbox_json, filters_json), []).append((run_id, started_utc))
    for run_list in comparable.values():
        prior_sets: list[set[tuple[int, str]]] = []
        for run_id, started_utc in run_list:
            present = {
                (zpid, kind)
                for zpid, kind in conn.execute("SELECT zpid, kind FROM sightings WHERE run_id = ?", (run_id,))
            }
            if prior_sets and started_utc >= since:
                prev_set = prior_sets[-1]
                events.extend(
                    {
                        "event": "delisted",
                        "zpid": key[0],
                        "kind": key[1],
                        "seen_utc": started_utc,
                        "days_observed": observed_days(key) if key in listings else None,
                    }
                    for key in sorted(prev_set - present)
                )
                all_prior = set().union(*prior_sets)
                events.extend(
                    {
                        "event": "relisted",
                        "zpid": key[0],
                        "kind": key[1],
                        "seen_utc": started_utc,
                        "days_observed": observed_days(key) if key in listings else None,
                    }
                    for key in sorted((present - prev_set) & all_prior)
                )
            prior_sets.append(present)

    return sorted(events, key=lambda event: (event.get("seen_utc", since), event["zpid"], event["event"]))


@dataclass
class DBStats:
    """Database statistics."""

    total_listings: int
    oldest_seen: str | None
    newest_seen: str | None
    db_path: str
    db_size_bytes: int


def db_stats(conn: sqlite3.Connection) -> DBStats:
    """Get database statistics.

    Args:
        conn: Database connection.

    Returns:
        DBStats with counts, timestamps, and file info.
    """
    cursor = conn.execute("SELECT COUNT(*) FROM listings")
    total_listings = cursor.fetchone()[0]

    cursor = conn.execute("SELECT MIN(first_seen_utc), MAX(last_seen_utc) FROM listings")
    oldest_seen, newest_seen = cursor.fetchone()

    db_path = conn.execute("PRAGMA database_list").fetchone()[2]
    db_size_bytes = Path(db_path).stat().st_size if db_path and Path(db_path).exists() else 0

    return DBStats(
        total_listings=total_listings,
        oldest_seen=oldest_seen,
        newest_seen=newest_seen,
        db_path=db_path,
        db_size_bytes=db_size_bytes,
    )
