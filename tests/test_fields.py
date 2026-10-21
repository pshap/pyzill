"""The listing field lists must never drift apart.

The normalized listing shape (``Listing``) is the single source: the
database columns, the columns the write path fills, and the numeric fields
of the query engine are all derived from it. These tests fail when one of
them is edited without the others.
"""

import re

from pyzill.db import connect
from pyzill.db.schema import _CREATE_LISTINGS_SQL, LISTING_TABLE_COLUMNS
from pyzill.db.upsert import _DATA_COLUMNS
from pyzill.normalize import LISTING_FIELDS, NUMERIC_LISTING_FIELDS, Listing
from pyzill.query import _NUMERIC_FIELDS

# The column definitions inside the CREATE TABLE statement.
_DDL_COLUMN_RE = re.compile(r"^\s{4}(\w+)\s+(?:INTEGER|REAL|TEXT)", re.MULTILINE)


def _ddl_columns() -> list[str]:
    """Return the column names the CREATE TABLE statement declares."""
    return _DDL_COLUMN_RE.findall(_CREATE_LISTINGS_SQL.format(name="listings"))


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
