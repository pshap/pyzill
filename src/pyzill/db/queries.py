"""Read queries: listing rows, enrichment candidates, and price history."""

import sqlite3
from collections.abc import Mapping
from typing import Any, NamedTuple

from pyzill.db.schema import LISTING_TABLE_COLUMNS
from pyzill.query import DETAILS_JOIN, Condition, compile_listing_filter, query_database

__all__ = ["EnrichCandidate", "get_price_history", "load_listings", "select_enrich_candidates"]


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


class EnrichCandidate(NamedTuple):
    """One listing chosen for enrichment.

    zpid and kind come from the listings primary key, so neither can be missing.
    """

    zpid: int
    kind: str
    detail_url: str | None


def select_enrich_candidates(  # noqa: PLR0913 - the shortlist's whole filter surface
    conn: sqlite3.Connection,
    *,
    where: list[str] | None = None,
    where_json: str | None = None,
    sort: str | None = None,
    limit: int | None = None,
    max_age_days: int | None = None,
) -> list[EnrichCandidate]:
    """Select the listings whose property details need (re)fetching.

    A listing is a candidate when it has no ``property_details`` row, or its
    ``enriched_at_utc`` is older than ``max_age_days`` days, and it has no
    ``detail_failures`` row recorded within ``max_age_days`` days. When
    ``max_age_days`` is None every listing matching the filter is a
    candidate.

    The shortlist uses the same filter pipeline as ``db query`` (through
    :func:`pyzill.query.compile_listing_filter`), so ``--where``, ``--where-json``,
    and ``--sort`` mean exactly what they mean there. The details join is
    always applied, because the age filter reads its columns, and every
    listing column is therefore qualified with ``listings.``.
    """
    compiled = compile_listing_filter(where=where, where_json=where_json, sort=sort)
    clauses = list(compiled.clauses)
    parameters = list(compiled.parameters)
    if max_age_days is not None:
        cutoff = "strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?)"
        age = f"-{max_age_days} days"
        clauses.append(f'(property_details."zpid" IS NULL OR property_details."enriched_at_utc" < {cutoff})')
        parameters.append(age)
        clauses.append(f'(df."zpid" IS NULL OR df."recorded_at_utc" < {cutoff})')
        parameters.append(age)

    sql = 'SELECT listings."zpid", listings."kind", listings."detail_url" FROM "listings"'
    sql += DETAILS_JOIN
    sql += ' LEFT JOIN detail_failures df ON df."zpid" = listings."zpid" AND df."kind" = listings."kind"'
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY " + compiled.order_by
    if limit is not None:
        sql += " LIMIT ?"
        parameters.append(limit)

    rows = conn.execute(sql, parameters).fetchall()
    return [EnrichCandidate(zpid=row[0], kind=row[1], detail_url=row[2]) for row in rows]


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
