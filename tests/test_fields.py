"""The listing field lists must never drift apart.

The normalized listing shape (``Listing``) is the single source: the
database columns, the columns the write path fills, and the numeric fields
of the query engine are all derived from it. These tests fail when one of
them is edited without the others.

The column *types* are pinned too: the DDL is the one authority for them,
so a fresh database, a migrated one, and a backfilled one must all declare
the same type for every column.
"""

import re
import sqlite3
from pathlib import Path

from pyzill.db import connect
from pyzill.db.schema import (
    _CREATE_LISTINGS_SQL,
    LISTING_COLUMN_DEFINITIONS,
    LISTING_TABLE_COLUMNS,
)
from pyzill.db.upsert import _DATA_COLUMNS
from pyzill.normalize import LISTING_FIELDS, NUMERIC_LISTING_FIELDS, Listing
from pyzill.query import _NUMERIC_FIELDS

# The column definitions inside the CREATE TABLE statement.
_DDL_COLUMN_RE = re.compile(r"^[ \t]{4}(\w+)[ \t]+(INTEGER|REAL|TEXT)([^,\n]*)", re.MULTILINE)

# A column a backfilled database can be missing: SQLite refuses to drop a
# primary-key or indexed column.
_BACKFILLABLE_COLUMN = "beds"


def _ddl_definitions() -> list[tuple[str, str, str]]:
    """Return the (name, type, constraint) triples the CREATE TABLE declares."""
    return _DDL_COLUMN_RE.findall(_CREATE_LISTINGS_SQL.format(name="listings"))


def _ddl_columns() -> list[str]:
    """Return the column names the CREATE TABLE statement declares."""
    return [name for name, _, _ in _ddl_definitions()]


def _ddl_types() -> dict[str, str]:
    """Return the type the CREATE TABLE statement gives each column."""
    return {name: column_type for name, column_type, _ in _ddl_definitions()}


def _table_types(path: str) -> dict[str, str]:
    """Return the type a database gives each listings column."""
    connection = sqlite3.connect(path)
    try:
        return {row[1]: row[2] for row in connection.execute("PRAGMA table_info(listings)")}
    finally:
        connection.close()


def test_listing_fields_are_the_declared_listing_shape():
    assert tuple(Listing.__annotations__) == LISTING_FIELDS


def test_table_columns_are_the_listing_fields_plus_kind_and_timestamps():
    assert (LISTING_FIELDS[0], "kind", *LISTING_FIELDS[1:], "first_seen_utc", "last_seen_utc") == LISTING_TABLE_COLUMNS


def test_create_table_matches_the_derived_columns():
    assert _ddl_columns() == list(LISTING_TABLE_COLUMNS)


def test_created_database_matches_the_derived_columns():
    connection = connect(":memory:")
    try:
        columns = [row[1] for row in connection.execute("PRAGMA table_info(listings)")]
    finally:
        connection.close()

    assert columns == list(LISTING_TABLE_COLUMNS)


def test_upsert_data_columns_are_the_listing_fields():
    assert tuple(field for field in LISTING_FIELDS if field not in ("zpid", "price")) == _DATA_COLUMNS


def test_numeric_fields_come_from_the_listing_shape():
    assert NUMERIC_LISTING_FIELDS | {"ppsf"} == _NUMERIC_FIELDS


def test_every_numeric_field_is_annotated_as_a_number():
    numeric = NUMERIC_LISTING_FIELDS
    for name in numeric:
        annotation = Listing.__annotations__[name]
        assert "int" in str(annotation) or "float" in str(annotation), name


def test_the_column_types_come_from_the_ddl():
    """The DDL is the one authority for a column's type."""
    for name, column_type in _ddl_types().items():
        assert LISTING_COLUMN_DEFINITIONS[name].startswith(column_type), name


def test_a_backfilled_column_keeps_its_type(tmp_path: Path):
    """A column added on open must have the type the DDL declares.

    A backfilled database that stored a numeric column as text would
    return strings where the listing shape promises numbers.
    """
    db_path = tmp_path / "backfill.db"
    connection = connect(db_path)
    connection.execute(f"ALTER TABLE listings DROP COLUMN {_BACKFILLABLE_COLUMN}")
    connection.commit()
    connection.close()

    connection = connect(db_path)
    try:
        types = {row[1]: row[2] for row in connection.execute("PRAGMA table_info(listings)")}
    finally:
        connection.close()

    assert types[_BACKFILLABLE_COLUMN] == _ddl_types()[_BACKFILLABLE_COLUMN]


def test_the_schema_paths_agree_on_columns_and_types(tmp_path: Path):
    """A fresh, a backfilled, and a migrated database declare the same schema."""
    fresh_path = tmp_path / "fresh.db"
    connect(fresh_path).close()

    backfilled_path = tmp_path / "backfilled.db"
    connection = connect(backfilled_path)
    connection.execute(f"ALTER TABLE listings DROP COLUMN {_BACKFILLABLE_COLUMN}")
    connection.commit()
    connection.close()
    connect(backfilled_path).close()

    migrated_path = tmp_path / "migrated.db"
    legacy = sqlite3.connect(migrated_path)
    legacy.executescript(
        """
        CREATE TABLE listings (
            zpid INTEGER PRIMARY KEY,
            first_seen_utc TEXT NOT NULL,
            last_seen_utc TEXT NOT NULL
        );
        CREATE TABLE price_history (
            zpid INTEGER NOT NULL REFERENCES listings(zpid) ON DELETE CASCADE,
            price INTEGER NOT NULL,
            seen_utc TEXT NOT NULL,
            PRIMARY KEY (zpid, seen_utc)
        );
        PRAGMA user_version = 1;
        """
    )
    legacy.close()
    connect(migrated_path).close()

    fresh = _table_types(str(fresh_path))
    backfilled = _table_types(str(backfilled_path))
    migrated = _table_types(str(migrated_path))

    # A backfilled column is appended, so the order may differ; the
    # columns and their types may not.
    assert set(backfilled) == set(fresh)
    assert backfilled == fresh
    assert migrated == fresh
