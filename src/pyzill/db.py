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

    for row in rows:
        zpid = row.get("zpid")
        if zpid is None:
            stats.skipped += 1
            continue

        # Cast zpid to int (might be float from API)
        try:
            zpid = int(zpid)
        except (ValueError, TypeError):
            stats.skipped += 1
            continue

        # Check if listing exists
        cursor = conn.execute(
            "SELECT price, first_seen_utc, last_seen_utc, kind FROM listings WHERE zpid = ?",
            (zpid,),
        )
        existing = cursor.fetchone()

        # parse_price tolerates raw "$459,000" strings from unnormalized rows
        # and maps 0/"$0" (Zillow's unknown sentinel) to None.
        price_int = parse_price(row.get("price"))

        if existing is None:
            # New listing
            stats.added += 1
            conn.execute(
                """
                INSERT INTO listings (
                    zpid, kind, price, price_label, beds, baths, area,
                    address, street, city, state, zipcode,
                    latitude, longitude, home_type, status, status_type,
                    detail_url, image_url, zestimate, rent_zestimate,
                    first_seen_utc, last_seen_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    zpid,
                    kind,
                    price_int,
                    row.get("price_label"),
                    row.get("beds"),
                    row.get("baths"),
                    row.get("area"),
                    row.get("address"),
                    row.get("street"),
                    row.get("city"),
                    row.get("state"),
                    row.get("zipcode"),
                    row.get("latitude"),
                    row.get("longitude"),
                    row.get("home_type"),
                    row.get("status"),
                    row.get("status_type"),
                    row.get("detail_url"),
                    row.get("image_url"),
                    row.get("zestimate"),
                    row.get("rent_zestimate"),
                    seen_utc,
                    seen_utc,
                ),
            )

        else:
            # Existing listing
            stats.updated += 1
            old_price, _first_seen, last_seen, old_kind = existing
            is_current_sighting = seen_utc >= last_seen
            previous = conn.execute(
                "SELECT price FROM price_history WHERE zpid = ? AND kind = ? AND seen_utc < ? "
                "ORDER BY seen_utc DESC LIMIT 1",
                (zpid, kind, seen_utc),
            ).fetchone()
            same_timestamp = conn.execute(
                "SELECT price FROM price_history WHERE zpid = ? AND kind = ? AND seen_utc = ?",
                (zpid, kind, seen_utc),
            ).fetchone()
            previous_price = same_timestamp[0] if same_timestamp is not None else (previous[0] if previous else None)
            descriptive_values = tuple(
                value
                for key in (
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
                for value in (is_current_sighting, row.get(key))
            )

            conn.execute(
                """
                UPDATE listings SET
                    kind = CASE WHEN ? THEN ? ELSE kind END,
                    price = CASE WHEN ? THEN
                        CASE WHEN kind != ? THEN ? ELSE COALESCE(?, price) END
                        ELSE price END,
                    price_label = CASE WHEN ? THEN COALESCE(?, price_label) ELSE price_label END,
                    beds = CASE WHEN ? THEN COALESCE(?, beds) ELSE beds END,
                    baths = CASE WHEN ? THEN COALESCE(?, baths) ELSE baths END,
                    area = CASE WHEN ? THEN COALESCE(?, area) ELSE area END,
                    address = CASE WHEN ? THEN COALESCE(?, address) ELSE address END,
                    street = CASE WHEN ? THEN COALESCE(?, street) ELSE street END,
                    city = CASE WHEN ? THEN COALESCE(?, city) ELSE city END,
                    state = CASE WHEN ? THEN COALESCE(?, state) ELSE state END,
                    zipcode = CASE WHEN ? THEN COALESCE(?, zipcode) ELSE zipcode END,
                    latitude = CASE WHEN ? THEN COALESCE(?, latitude) ELSE latitude END,
                    longitude = CASE WHEN ? THEN COALESCE(?, longitude) ELSE longitude END,
                    home_type = CASE WHEN ? THEN COALESCE(?, home_type) ELSE home_type END,
                    status = CASE WHEN ? THEN COALESCE(?, status) ELSE status END,
                    status_type = CASE WHEN ? THEN COALESCE(?, status_type) ELSE status_type END,
                    detail_url = CASE WHEN ? THEN COALESCE(?, detail_url) ELSE detail_url END,
                    image_url = CASE WHEN ? THEN COALESCE(?, image_url) ELSE image_url END,
                    zestimate = CASE WHEN ? THEN COALESCE(?, zestimate) ELSE zestimate END,
                    rent_zestimate = CASE WHEN ? THEN COALESCE(?, rent_zestimate) ELSE rent_zestimate END,
                    first_seen_utc = MIN(first_seen_utc, ?),
                    last_seen_utc = MAX(last_seen_utc, ?)
                WHERE zpid = ?
                """,
                (
                    is_current_sighting,
                    kind,
                    is_current_sighting,
                    kind,
                    price_int,
                    price_int,
                    *descriptive_values,
                    seen_utc,
                    seen_utc,
                    zpid,
                ),
            )

            if (
                price_int is not None
                and old_kind == kind
                and (
                    (previous_price is not None and previous_price != price_int)
                    or (previous_price is None and old_price is None)
                )
            ):
                stats.price_changes += 1

        if price_int is not None:
            conn.execute(
                "INSERT INTO price_history (zpid, kind, price, seen_utc) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(zpid, kind, seen_utc) DO UPDATE SET price = excluded.price",
                (zpid, kind, price_int, seen_utc),
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
