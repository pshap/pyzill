"""Schema definition, migrations, and column backfills.

The DDL, the canonical column list, and the migration steps live here so the
fresh-schema path and the upgrade path can never drift apart.
"""

import contextlib
import sqlite3
from typing import Any

__all__ = ["ensure_schema"]


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


def _backfill_listing_columns(conn: sqlite3.Connection) -> None:
    """Add any ``_LISTING_TABLE_COLUMNS`` column missing from ``listings``."""
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(listings)")}
    for col in _LISTING_TABLE_COLUMNS:
        if col in existing_cols:
            continue
        _integer_cols = ("days_on_zillow", "price_change", "year_built", "tax_assessed_value")
        col_type = "REAL" if col == "lot_area_value" else ("INTEGER" if col in _integer_cols else "TEXT")
        with contextlib.suppress(sqlite3.OperationalError):
            conn.execute(f"ALTER TABLE listings ADD COLUMN {col} {col_type}")


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Bring the database up to the current schema version.

    Must run inside a write transaction: the version is re-read under the
    lock so a concurrent first open cannot migrate the same database twice.
    """
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version > _SCHEMA_VERSION:
        raise sqlite3.DatabaseError(
            f"Database schema version {version} is newer than supported version {_SCHEMA_VERSION}"
        )
    if version < _SCHEMA_VERSION:
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
        conn.execute(f"PRAGMA user_version = {version}")
    # Backfill any columns a newer release added. Runs even when the
    # version is already current so a database that predates an additive
    # column (but not a version bump) is repaired on open.
    _backfill_listing_columns(conn)
