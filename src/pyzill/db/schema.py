"""Schema definition, migrations, and column backfills.

The DDL, the canonical column list, and the migration steps live here so the
fresh-schema path and the upgrade path can never drift apart.
"""

import re
import sqlite3
from typing import Any

from pyzill.normalize import LISTING_FIELDS

__all__ = [
    "LISTING_COLUMN_DEFINITIONS",
    "LISTING_TABLE_COLUMNS",
    "SCHEMA_VERSION",
    "ensure_schema",
]


# The current schema version.
SCHEMA_VERSION = 3

# Compatibility alias for the historical private name.
_SCHEMA_VERSION = SCHEMA_VERSION

# Canonical listings columns, in schema order. Derived from the normalized
# listing shape, so the DDL, the migrations, and the write path all agree on
# one list. The kind sits second (it is part of the primary key) and the two
# timestamps are added by the database itself.
LISTING_TABLE_COLUMNS: tuple[str, ...] = (
    LISTING_FIELDS[0],
    "kind",
    *LISTING_FIELDS[1:],
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


# Each listings column as the DDL declares it: the storage type plus any
# constraint. The DDL is the one authority for a column's type, so the
# backfill parses it instead of keeping a second list of its own.
_DDL_COLUMN_RE = re.compile(r"^[ \t]{4}(\w+)[ \t]+(INTEGER|REAL|TEXT)([^,\n]*)", re.MULTILINE)

LISTING_COLUMN_DEFINITIONS: dict[str, str] = {
    name: f"{column_type}{constraint}".strip()
    for name, column_type, constraint in _DDL_COLUMN_RE.findall(_CREATE_LISTINGS_SQL.format(name="listings"))
}


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
    carried = [col for col in LISTING_TABLE_COLUMNS if col != "kind" and col in old_columns]

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
    # columns_sql is built from the fixed LISTING_TABLE_COLUMNS constant, not user input.
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
    carried = [col for col in LISTING_TABLE_COLUMNS if col in old_columns]

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


def _addable_definition(definition: str) -> str:
    """Return a column definition SQLite can add to an existing table.

    SQLite refuses to add a NOT NULL column that has no default, so such a
    column is added as a nullable one. The write path fills it anyway, and a
    column that cannot be added at all is better added plain than not at all.
    """
    if "NOT NULL" in definition and "DEFAULT" not in definition:
        return definition.split(" NOT NULL", 1)[0]
    return definition


def _backfill_listing_columns(conn: sqlite3.Connection) -> None:
    """Add any ``LISTING_TABLE_COLUMNS`` column missing from ``listings``.

    The column is added with the definition the DDL gives it, so a
    backfilled database has the same column types as a fresh one.
    """
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(listings)")}
    if not existing_cols:
        # There is no listings table to repair. Only the schema path
        # creates one, so this is a database that never held listings.
        return
    for col in LISTING_TABLE_COLUMNS:
        if col in existing_cols:
            continue
        definition = LISTING_COLUMN_DEFINITIONS.get(col)
        if definition is None:
            raise sqlite3.DatabaseError(f"No column definition for {col!r} in the listings schema")
        try:
            conn.execute(f"ALTER TABLE listings ADD COLUMN {col} {_addable_definition(definition)}")
        except sqlite3.OperationalError as exc:
            # Another process may have added the column between the check
            # above and this statement; anything else is a real fault.
            if "duplicate column name" not in str(exc):
                raise


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Bring the database up to the current schema version.

    Must run inside a write transaction: the version is re-read under the
    lock so a concurrent first open cannot migrate the same database twice.
    """
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version > SCHEMA_VERSION:
        raise sqlite3.DatabaseError(
            f"Database schema version {version} is newer than supported version {SCHEMA_VERSION}"
        )
    if version < SCHEMA_VERSION:
        if version == 0:
            conn.execute(_CREATE_LISTINGS_SQL.format(name="listings"))
            conn.execute(_CREATE_PRICE_HISTORY_SQL.format(name="price_history"))
            for statement in _CREATE_INDEX_SQL + _CREATE_TRACKING_SQL:
                conn.execute(statement)
            version = SCHEMA_VERSION
        while version < SCHEMA_VERSION:
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
