"""Read queries: listing rows and price history."""

import sqlite3
from collections.abc import Mapping
from typing import Any

from pyzill.db.schema import LISTING_TABLE_COLUMNS

__all__ = ["get_price_history", "load_listings"]


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
    fields = set(LISTING_TABLE_COLUMNS)
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
