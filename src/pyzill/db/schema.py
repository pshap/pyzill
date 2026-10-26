"""Schema definition, migrations, and column backfills.

The DDL, the canonical column list, and the migration steps live here so the
fresh-schema path and the upgrade path can never drift apart.
"""

import re
import sqlite3
from collections.abc import Callable
from typing import Any

from pyzill.db.derive import hoa_monthly, int_if_whole, tax_latest_from_json
from pyzill.normalize import LISTING_FIELDS
from pyzill.values import to_number

__all__ = [
    "LISTING_COLUMN_DEFINITIONS",
    "LISTING_TABLE_COLUMNS",
    "PROPERTY_DETAIL_COLUMN_DEFINITIONS",
    "SCHEMA_VERSION",
    "ensure_schema",
    "schema_needs_update",
]


# The current schema version.
SCHEMA_VERSION = 5

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
    kind TEXT NOT NULL DEFAULT 'sale' CHECK (kind IN ('sale', 'rent', 'sold')),
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
    kind TEXT NOT NULL DEFAULT 'sale' CHECK (kind IN ('sale', 'rent', 'sold')),
    price INTEGER NOT NULL,
    seen_utc TEXT NOT NULL,
    PRIMARY KEY (zpid, kind, seen_utc),
    FOREIGN KEY (zpid, kind) REFERENCES listings(zpid, kind) ON DELETE CASCADE
);"""


# Each listings column as the DDL declares it: the storage type plus any
# constraint. The DDL is the one authority for a column's type, so the
# backfill parses it instead of keeping a second list of its own. The
# constraint may contain commas inside a CHECK, so the line is read to its
# end and only the trailing column separator is dropped.
_DDL_COLUMN_RE = re.compile(r"^[ \t]{4}(\w+)[ \t]+(INTEGER|REAL|TEXT)(.*?),?$", re.MULTILINE)

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

_CREATE_PROPERTY_DETAILS_SQL = """CREATE TABLE IF NOT EXISTS {name} (
    zpid INTEGER NOT NULL,
    kind TEXT NOT NULL DEFAULT 'sale' CHECK (kind IN ('sale', 'rent', 'sold')),
    hoa_fee INTEGER,
    hoa_fee_unit TEXT,
    hoa_monthly REAL,
    description TEXT,
    photos_count INTEGER,
    year_built INTEGER,
    lot_size REAL,
    lot_size_unit TEXT,
    tax_assessed_latest INTEGER,
    tax_year_latest INTEGER,
    tax_paid_latest INTEGER,
    tax_history TEXT,
    schools TEXT,
    raw_json TEXT,
    enriched_at_utc TEXT NOT NULL,
    normalization_failed INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (zpid, kind),
    FOREIGN KEY (zpid, kind) REFERENCES listings(zpid, kind) ON DELETE CASCADE
);"""

_CREATE_PROPERTY_DETAILS_INDEX_SQL = ("CREATE INDEX IF NOT EXISTS ix_property_details_kind ON property_details(kind)",)

_CREATE_DETAIL_FAILURES_SQL = """CREATE TABLE IF NOT EXISTS {name} (
    zpid INTEGER NOT NULL,
    kind TEXT NOT NULL DEFAULT 'sale' CHECK (kind IN ('sale', 'rent', 'sold')),
    recorded_at_utc TEXT NOT NULL,
    reason TEXT NOT NULL,
    status_code INTEGER,
    PRIMARY KEY (zpid, kind),
    FOREIGN KEY (zpid, kind) REFERENCES listings(zpid, kind) ON DELETE CASCADE
);"""

_CREATE_DETAIL_FAILURES_INDEX_SQL = ("CREATE INDEX IF NOT EXISTS ix_detail_failures_kind ON detail_failures(kind)",)

# The property_details columns, parsed from the same DDL the table is built
# with, so the schema command and the table can never drift apart.
PROPERTY_DETAIL_COLUMN_DEFINITIONS: dict[str, str] = {
    name: f"{column_type}{constraint}".strip()
    for name, column_type, constraint in _DDL_COLUMN_RE.findall(
        _CREATE_PROPERTY_DETAILS_SQL.format(name="property_details")
    )
}

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
                truncated INTEGER NOT NULL DEFAULT 0 CHECK (truncated IN (0, 1))
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


def _migrate_v3_to_v4(conn: sqlite3.Connection) -> None:
    """Add property details table."""
    conn.execute(_CREATE_PROPERTY_DETAILS_SQL.format(name="property_details"))
    for statement in _CREATE_PROPERTY_DETAILS_INDEX_SQL:
        conn.execute(statement)


_V5_PROPERTY_DETAIL_INSERT_SQL = """INSERT INTO property_details (
    zpid, kind, hoa_fee, hoa_fee_unit, hoa_monthly, description, photos_count,
    year_built, lot_size, lot_size_unit, tax_assessed_latest, tax_year_latest,
    tax_paid_latest, tax_history, schools, raw_json, enriched_at_utc,
    normalization_failed
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""


def _migrate_v4_to_v5(conn: sqlite3.Connection) -> None:
    """Rebuild property_details around derived numeric columns and add detail_failures.

    The v4 table carried ``tax_assessed_value`` (always NULL) and
    ``price_history_detail`` (a JSON duplicate of tax_history).  Neither is
    kept: the v5 table derives ``hoa_monthly`` from ``hoa_fee``/``hoa_fee_unit``
    and ``tax_*_latest`` from the newest ``tax_history`` entry at write time,
    and the migration backfills those derivations from stored JSON.
    """
    old_columns = {row[1] for row in conn.execute("PRAGMA table_info(property_details)")}
    conn.execute("CREATE TABLE property_details_v5_staging AS SELECT * FROM property_details")
    conn.execute("DROP TABLE property_details")
    conn.execute(_CREATE_PROPERTY_DETAILS_SQL.format(name="property_details"))
    for statement in _CREATE_PROPERTY_DETAILS_INDEX_SQL:
        conn.execute(statement)

    if old_columns:
        old_col_list = sorted(old_columns)
        rows = conn.execute(
            f"SELECT {', '.join(f's."{col}"' for col in old_col_list)} FROM property_details_v5_staging s"  # noqa: S608 - column names come from PRAGMA table_info
        ).fetchall()
        conn.executemany(
            _V5_PROPERTY_DETAIL_INSERT_SQL,
            (_v5_property_detail_row(dict(zip(old_col_list, row, strict=True))) for row in rows),
        )
    conn.execute("DROP TABLE property_details_v5_staging")

    conn.execute(_CREATE_DETAIL_FAILURES_SQL.format(name="detail_failures"))
    for statement in _CREATE_DETAIL_FAILURES_INDEX_SQL:
        conn.execute(statement)


def _v5_property_detail_row(values: dict[str, Any]) -> tuple[Any, ...]:
    """Return the parameter tuple for one rebuilt property_details row.

    Derived columns come from the stored JSON: ``hoa_monthly`` normalizes
    ``hoa_fee`` by its unit, and ``tax_*_latest`` come from the newest entry
    of ``tax_history``.
    """
    hoa_fee = int_if_whole(to_number(values.get("hoa_fee")))
    hoa_fee_unit = values.get("hoa_fee_unit")
    monthly_hoa = hoa_monthly(hoa_fee, hoa_fee_unit)
    tax_latest = tax_latest_from_json(values.get("tax_history"))
    normalization_failed = values.get("normalization_failed")
    normalization_failed = 0 if normalization_failed is None else 1 if int(normalization_failed) else 0

    return (
        values.get("zpid"),
        values.get("kind"),
        hoa_fee,
        hoa_fee_unit,
        monthly_hoa,
        values.get("description"),
        values.get("photos_count"),
        values.get("year_built"),
        values.get("lot_size"),
        values.get("lot_size_unit"),
        tax_latest.assessed,
        tax_latest.year,
        tax_latest.paid,
        values.get("tax_history"),
        values.get("schools"),
        values.get("raw_json"),
        values.get("enriched_at_utc"),
        normalization_failed,
    )


_MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {
    1: _migrate_v1_to_v2,
    2: _migrate_v2_to_v3,
    3: _migrate_v3_to_v4,
    4: _migrate_v4_to_v5,
}


def _addable_definition(definition: str) -> str:
    """Return a column definition SQLite can add to an existing table.

    SQLite refuses to add a NOT NULL column that has no default, so such a
    column is added as a nullable one. The write path fills it anyway, and a
    column that cannot be added at all is better added plain than not at all.
    """
    if "NOT NULL" in definition and "DEFAULT" not in definition:
        return definition.split(" NOT NULL", 1)[0]
    return definition


# The value a column takes when the old table does not have it. The kind
# falls back to the DDL's default, and the two timestamps to the moment the
# database was rebuilt; every other column is nullable.
_NOW_SQL = "strftime('%Y-%m-%dT%H:%M:%SZ','now')"
_LEGACY_COLUMN_FALLBACKS = {
    "kind": "'sale'",
    "first_seen_utc": _NOW_SQL,
    "last_seen_utc": _NOW_SQL,
}


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    """True when the database already holds a table of the given name."""
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone()
    return row is not None


def _legacy_column_sql(column: str, old_columns: set[str]) -> str:
    """Return the SQL that reads one column out of a legacy listings table.

    A column the old table has is read from it. A column it does not have
    takes the fallback: the kind becomes a sale, the timestamps become the
    moment of the rebuild, and anything else becomes null.
    """
    if column in old_columns:
        return f'"{column}"'
    return _LEGACY_COLUMN_FALLBACKS.get(column, "NULL")


def _rebuild_legacy_tables(conn: sqlite3.Connection) -> None:
    """Rebuild a listings database of unknown provenance to the current schema.

    A database that has a ``listings`` table but no schema version predates
    the versioning, or had its version stamp cleared. Its shape is whatever
    it was, so the tables are rebuilt from the current DDL: the columns the
    old table has are carried across, and a column it does not have takes
    the fallback above. A price history row for a listing that is not in the
    table is dropped, because the rebuilt history references the listings.
    """
    old_columns = {row[1] for row in conn.execute("PRAGMA table_info(listings)")}
    has_history = _table_exists(conn, "price_history")
    history_columns: set[str] = set()
    if has_history:
        history_columns = {row[1] for row in conn.execute("PRAGMA table_info(price_history)")}

    conn.execute("DROP TABLE IF EXISTS listings_legacy_staging")
    conn.execute("CREATE TABLE listings_legacy_staging AS SELECT * FROM listings")
    if has_history:
        conn.execute("DROP TABLE IF EXISTS price_history_legacy_staging")
        conn.execute("CREATE TABLE price_history_legacy_staging AS SELECT * FROM price_history")

    conn.execute("DROP TABLE IF EXISTS price_history")
    conn.execute("DROP TABLE IF EXISTS listings")
    conn.execute(_CREATE_LISTINGS_SQL.format(name="listings"))

    columns_sql = ", ".join(f'"{column}"' for column in LISTING_TABLE_COLUMNS)
    values_sql = ", ".join(_legacy_column_sql(column, old_columns) for column in LISTING_TABLE_COLUMNS)
    # The column list is the fixed LISTING_TABLE_COLUMNS constant, not user input.
    conn.execute(f"INSERT INTO listings ({columns_sql}) SELECT {values_sql} FROM listings_legacy_staging")  # noqa: S608
    conn.execute("DROP TABLE listings_legacy_staging")

    conn.execute(_CREATE_PRICE_HISTORY_SQL.format(name="price_history"))
    if has_history:
        # A history row keeps the kind it had; a history table that has no
        # kind column holds sale prices.
        kind = 'h."kind"' if "kind" in history_columns else "'sale'"
        zpid_select = 'h."zpid"' if "zpid" in history_columns else "NULL"
        price_select = 'h."price"' if "price" in history_columns else "NULL"
        seen_select = 'h."seen_utc"' if "seen_utc" in history_columns else _NOW_SQL
        # The column names come from the fixed schema constants, not user input.
        conn.execute(
            "INSERT INTO price_history (zpid, kind, price, seen_utc) "  # noqa: S608
            f"SELECT {zpid_select}, {kind}, {price_select}, {seen_select} "
            "FROM price_history_legacy_staging h "
            f"WHERE EXISTS (SELECT 1 FROM listings l WHERE l.zpid = h.zpid AND l.kind = {kind})"
        )
        conn.execute("DROP TABLE price_history_legacy_staging")

    for statement in _CREATE_INDEX_SQL + _CREATE_TRACKING_SQL:
        conn.execute(statement)


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


def schema_needs_update(conn: sqlite3.Connection) -> bool:
    """Whether opening the database has schema work to do.

    A current-version database whose columns are all present needs no write
    lock. Only a version mismatch or a missing additive column requires the
    migration/backfill path.
    """
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version < SCHEMA_VERSION:
        return True
    existing_columns = {row[1] for row in conn.execute("PRAGMA table_info(listings)")}
    if any(column not in existing_columns for column in LISTING_TABLE_COLUMNS):
        return True
    # Also need property_details and detail_failures tables
    return not (_table_exists(conn, "property_details") and _table_exists(conn, "detail_failures"))


def _create_detail_tables(conn: sqlite3.Connection) -> None:
    """Create the property_details and detail_failures tables and their indexes."""
    conn.execute(_CREATE_PROPERTY_DETAILS_SQL.format(name="property_details"))
    for statement in _CREATE_PROPERTY_DETAILS_INDEX_SQL:
        conn.execute(statement)
    conn.execute(_CREATE_DETAIL_FAILURES_SQL.format(name="detail_failures"))
    for statement in _CREATE_DETAIL_FAILURES_INDEX_SQL:
        conn.execute(statement)


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
            if _table_exists(conn, "listings"):
                # A listings table with no version stamp is a database of
                # unknown provenance, not a fresh file. Assuming it is fresh
                # would leave the old table in place and then fail to build
                # an index over a column it does not have, so it is rebuilt.
                _rebuild_legacy_tables(conn)
            else:
                conn.execute(_CREATE_LISTINGS_SQL.format(name="listings"))
                conn.execute(_CREATE_PRICE_HISTORY_SQL.format(name="price_history"))
                for statement in _CREATE_INDEX_SQL + _CREATE_TRACKING_SQL:
                    conn.execute(statement)
        else:
            while version < SCHEMA_VERSION:
                migration = _MIGRATIONS.get(version)
                if migration is None:
                    raise sqlite3.DatabaseError(f"No migration available from schema version {version}")
                migration(conn)
                version += 1
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    # The details table is part of the current schema: create it when missing.
    # Idempotent on fresh, migrated, and already-current databases alike.
    _create_detail_tables(conn)
    # Backfill any columns a newer release added. Runs even when the
    # version is already current so a database that predates an additive
    # column (but not a version bump) is repaired on open.
    _backfill_listing_columns(conn)
