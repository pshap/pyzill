"""Read queries: listing rows, price history, and change events."""

import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from pyzill.db.schema import _LISTING_TABLE_COLUMNS

__all__ = ["get_changes", "get_price_history", "load_listings"]


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
    fields = set(_LISTING_TABLE_COLUMNS)
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


def get_changes(conn: sqlite3.Connection, since: str) -> list[dict[str, Any]]:
    """Summarize new listings, price/status transitions, and complete-run absences."""
    events: list[dict[str, Any]] = []
    listings = {(row["zpid"], row["kind"]): row for row in load_listings(conn)}

    def observed_days(key: tuple[int, str]) -> int:
        first_seen = listings[key]["first_seen_utc"]
        first = datetime.fromisoformat(first_seen)
        return max(0, (datetime.now(UTC) - first).days)

    for key, listing in listings.items():
        if listing["first_seen_utc"] >= since:
            events.append(
                {
                    "event": "new",
                    "zpid": key[0],
                    "kind": key[1],
                    "price": listing["price"],
                    "days_observed": observed_days(key),
                }
            )

    history = conn.execute(
        "SELECT zpid, kind, price, seen_utc FROM price_history ORDER BY zpid, kind, seen_utc"
    ).fetchall()
    latest: dict[tuple[int, str], int] = {}
    for zpid, kind, price, seen_utc in history:
        key = (zpid, kind)
        previous = latest.get(key)
        if seen_utc >= since and previous is not None and previous != price:
            events.append(
                {
                    "event": "price_drop" if price < previous else "price_increase",
                    "zpid": zpid,
                    "kind": kind,
                    "old_price": previous,
                    "price": price,
                    "seen_utc": seen_utc,
                    "days_observed": observed_days(key) if key in listings else None,
                }
            )
        latest[key] = price

    for zpid, kind, status, first_seen_utc in conn.execute(
        "SELECT zpid, kind, status, first_seen_utc FROM status_history "
        "WHERE first_seen_utc >= ? ORDER BY first_seen_utc",
        (since,),
    ):
        events.append(
            {
                "event": "status_change",
                "zpid": zpid,
                "kind": kind,
                "status": status,
                "seen_utc": first_seen_utc,
                "days_observed": observed_days((zpid, kind)) if (zpid, kind) in listings else None,
            }
        )

    # Only search runs are compared. A run with no query text is an ingest of
    # a file snapshot (``db import``) or an anonymous upsert, where an absence
    # means "not in this batch", not "delisted". Comparing two of those would
    # report every listing missing from the second batch as delisted.
    run_rows = conn.execute(
        "SELECT id, started_utc, finished_utc, kind, query, bbox_json, filters_json FROM sync_runs "
        "WHERE query != '' AND finished_utc IS NOT NULL AND truncated = 0 ORDER BY started_utc, id"
    ).fetchall()
    comparable: dict[tuple[str, str, str | None, str | None], list[tuple[int, str]]] = {}
    for run_id, started_utc, _, kind, query, bbox_json, filters_json in run_rows:
        # A run is only comparable to another with the same kind, query,
        # viewport, *and* filters: an unfiltered run after a --max-price run
        # would otherwise report every excluded listing as delisted.
        comparable.setdefault((kind, query, bbox_json, filters_json), []).append((run_id, started_utc))
    for run_list in comparable.values():
        prior_sets: list[set[tuple[int, str]]] = []
        for run_id, started_utc in run_list:
            present = {
                (zpid, kind)
                for zpid, kind in conn.execute("SELECT zpid, kind FROM sightings WHERE run_id = ?", (run_id,))
            }
            if prior_sets and started_utc >= since:
                prev_set = prior_sets[-1]
                events.extend(
                    {
                        "event": "delisted",
                        "zpid": key[0],
                        "kind": key[1],
                        "seen_utc": started_utc,
                        "days_observed": observed_days(key) if key in listings else None,
                    }
                    for key in sorted(prev_set - present)
                )
                all_prior = set().union(*prior_sets)
                events.extend(
                    {
                        "event": "relisted",
                        "zpid": key[0],
                        "kind": key[1],
                        "seen_utc": started_utc,
                        "days_observed": observed_days(key) if key in listings else None,
                    }
                    for key in sorted((present - prev_set) & all_prior)
                )
            prior_sets.append(present)

    return sorted(events, key=lambda event: (event.get("seen_utc", since), event["zpid"], event["event"]))
