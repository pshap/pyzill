"""SQLite database for tracking Zillow listings with price history.

Uses stdlib sqlite3 for zero new dependencies. Database lives at ./pyzill.db
by default (configurable via --db or PYZILL_DB).
"""

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pyzill.normalize import parse_price

__all__ = [
    "DBStats",
    "SyncStats",
    "connect",
    "db_stats",
    "default_db_path",
    "get_price_history",
    "load_listings",
    "upsert_listings",
    "utc_now",
]


def default_db_path() -> Path:
    """Return the default database path (./pyzill.db)."""
    return Path.cwd() / "pyzill.db"


def utc_now() -> str:
    """Return current UTC time as ISO string (fixed-width %Y-%m-%dT%H:%M:%SZ)."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


_SCHEMA_VERSION = 2

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
    "first_seen_utc",
    "last_seen_utc",
)

_CREATE_LISTINGS_SQL = """CREATE TABLE IF NOT EXISTS {name} (
  zpid INTEGER PRIMARY KEY,
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
  first_seen_utc TEXT NOT NULL,
  last_seen_utc TEXT NOT NULL
);"""

_CREATE_PRICE_HISTORY_SQL = """CREATE TABLE IF NOT EXISTS {name} (
  zpid INTEGER NOT NULL REFERENCES listings(zpid) ON DELETE CASCADE,
  kind TEXT NOT NULL DEFAULT 'sale',
  price INTEGER NOT NULL,
  seen_utc TEXT NOT NULL,
  PRIMARY KEY (zpid, kind, seen_utc)
);"""

_CREATE_INDEX_SQL = "CREATE INDEX IF NOT EXISTS ix_listings_last_seen ON listings(last_seen_utc)"


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
    conn.execute(_CREATE_INDEX_SQL)


_MIGRATIONS: dict[int, Any] = {1: _migrate_v1_to_v2}


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
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA journal_mode = WAL")

    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version > _SCHEMA_VERSION:
        conn.close()
        raise sqlite3.DatabaseError(
            f"Database schema version {version} is newer than supported version {_SCHEMA_VERSION}"
        )

    try:
        conn.execute("BEGIN IMMEDIATE")
        if version == 0:
            conn.execute(_CREATE_LISTINGS_SQL.format(name="listings"))
            conn.execute(_CREATE_PRICE_HISTORY_SQL.format(name="price_history"))
            conn.execute(_CREATE_INDEX_SQL)
            version = _SCHEMA_VERSION
        while version < _SCHEMA_VERSION:
            migration = _MIGRATIONS.get(version)
            if migration is None:
                raise sqlite3.DatabaseError(f"No migration available from schema version {version}")
            migration(conn)
            version += 1
        conn.execute(f"PRAGMA user_version = {version}")
        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        raise

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
        f"INSERT INTO listings ({columns}) VALUES ({placeholders}) ON CONFLICT(zpid) DO UPDATE SET"  # noqa: S608
        f"\n        {assignment_sql}"
    )


_LISTING_UPSERT_SQL = _listing_upsert_sql()


def _prefetch(
    conn: sqlite3.Connection, zpids: list[int], kind: str, seen_utc: str
) -> tuple[dict[int, tuple[Any, str]], dict[int, int | None]]:
    """Read pre-batch listing state and prior prices for the batch zpids.

    Returns zpid -> (price, kind) for listings that already exist, and
    zpid -> previous price for this kind (an entry with the batch timestamp
    wins over the latest strictly-earlier one; missing when none exists).
    """
    existing: dict[int, tuple[Any, str]] = {}
    prior_prices: dict[int, int | None] = {}
    for start in range(0, len(zpids), _CHUNK):
        chunk = zpids[start : start + _CHUNK]
        marks = _placeholders(len(chunk))
        for zpid, price, stored_kind in conn.execute(
            f"SELECT zpid, price, kind FROM listings WHERE zpid IN ({marks})",  # noqa: S608
            chunk,
        ):
            existing[zpid] = (price, stored_kind)
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
            if zpid in exact:
                prior_prices[zpid] = exact[zpid]
            elif zpid in latest_before:
                prior_prices[zpid] = latest_before[zpid][1]
    return existing, prior_prices


def upsert_listings(
    conn: sqlite3.Connection,
    rows: list[dict[str, Any]],
    *,
    seen_utc: str | None = None,
    kind: Literal["sale", "rent", "sold"] = "sale",
) -> SyncStats:
    """Upsert listing rows into the database.

    Skips rows without a zpid (counted in stats). For each zpid:
        - ``listings`` stores the latest sighting for each zpid and its kind.
        - ``price_history`` stores priced sightings partitioned by kind.
        - Missing fields never erase known values; a missing price keeps the last
            price for the same kind.

    Existing-row state and prior prices are read in two batched queries
    before any writes; the write itself is one UPSERT per row.

    Args:
        conn: Database connection.
        rows: List of normalized listing dicts (must have 'zpid' field).
        seen_utc: Timestamp for this batch (default: utc_now()).

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
    prepared: list[tuple[int, dict[str, Any], int | None]] = []
    for row in rows:
        zpid = row.get("zpid")
        if zpid is None:
            stats.skipped += 1
            continue
        try:
            zpid = int(zpid)
        except (ValueError, TypeError):
            stats.skipped += 1
            continue
        prepared.append((zpid, row, parse_price(row.get("price"))))

    zpids = list(dict.fromkeys(zpid for zpid, _, _ in prepared))
    existing, prior_prices = _prefetch(conn, zpids, kind, seen_utc)

    listing_params: list[tuple[Any, ...]] = []
    history_params: list[tuple[int, str, int, str]] = []
    added_in_batch: set[int] = set()
    for zpid, row, price_int in prepared:
        if zpid in existing:
            stats.updated += 1
            old_price, old_kind = existing[zpid]
            prior_price = prior_prices.get(zpid)
            if (
                price_int is not None
                and old_kind == kind
                and (
                    (prior_price is not None and prior_price != price_int)
                    or (prior_price is None and old_price is None)
                )
            ):
                stats.price_changes += 1
        elif zpid in added_in_batch:
            stats.updated += 1
        else:
            stats.added += 1
            added_in_batch.add(zpid)

        listing_params.append(
            (zpid, kind, price_int, *(row.get(column) for column in _DATA_COLUMNS), seen_utc, seen_utc)
        )
        if price_int is not None:
            history_params.append((zpid, kind, price_int, seen_utc))

    conn.executemany(_LISTING_UPSERT_SQL, listing_params)
    if history_params:
        conn.executemany(
            "INSERT INTO price_history (zpid, kind, price, seen_utc) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(zpid, kind, seen_utc) DO UPDATE SET price = excluded.price",
            history_params,
        )
    conn.commit()
    return stats


def load_listings(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Load all listings from the database.

    Returns:
        List of listing dicts (with first_seen_utc and last_seen_utc).
    """
    cursor = conn.execute("SELECT * FROM listings ORDER BY last_seen_utc DESC, zpid DESC")
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
