"""Read queries: listing rows and price history."""

import sqlite3
from collections.abc import Mapping
from typing import Any

from pyzill.db.schema import LISTING_TABLE_COLUMNS
from pyzill.query import Condition, query_database

__all__ = ["get_price_history", "load_listings"]


def load_listings(
    conn: sqlite3.Connection,
    *,
    filters: Mapping[str, Any] | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Load listings with optional equality filters and a row bound.

    The filters are equality conditions on listing columns, and the rows come
    back newest first. The query itself is the shared engine in
    :mod:`pyzill.query`, so a filter here means exactly what the same filter
    means to ``pyzill db query``: text is compared without regard to case,
    and a numeric column compared with a value that is not a number is an
    error rather than a filter that matches nothing.

    Args:
        conn: Database connection.
        filters: Column name to value; a null value matches a null column.
        limit: Maximum number of rows to return.

    Returns:
        List of listing dicts (with first_seen_utc and last_seen_utc).
    """
    if limit is not None and limit < 1:
        raise ValueError("limit must be at least 1")
    fields = set(LISTING_TABLE_COLUMNS)
    conditions: list[Condition] = []
    for field, value in (filters or {}).items():
        if field not in fields:
            raise ValueError(f"Unknown listing filter: {field}")
        conditions.append(Condition(field, "=", value))
    rows, _ = query_database(conn, conditions=conditions, limit=limit)
    return rows


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
