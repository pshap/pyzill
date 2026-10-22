"""Change events: what happened to the listings since a point in time."""

import sqlite3
from datetime import UTC, datetime
from typing import Any

__all__ = ["get_changes"]


def _observed_days(first_seen: str, now: datetime) -> int:
    """Return how many days ago a listing was first seen.

    A stored timestamp without a timezone is read as UTC, so a timestamp
    written by hand cannot make the whole report fail.
    """
    first = datetime.fromisoformat(first_seen)
    if first.tzinfo is None:
        first = first.replace(tzinfo=UTC)
    return max(0, (now - first).days)


def get_changes(conn: sqlite3.Connection, since: str) -> list[dict[str, Any]]:
    """Summarize new listings, price/status transitions, and complete-run absences.

    Every event carries the timestamp it happened at, and the returned list is
    ordered by that timestamp, so a consumer can replay the events in the
    order they occurred.
    """
    events: list[dict[str, Any]] = []
    now = datetime.now(UTC)

    # Only the two columns the events need are read: the report is about
    # changes, and loading every column of every listing made the cost of a
    # report grow with the size of the database rather than with the size of
    # the change.
    listings = {
        (zpid, kind): (price, first_seen)
        for zpid, kind, price, first_seen in conn.execute("SELECT zpid, kind, price, first_seen_utc FROM listings")
    }

    def observed_days(key: tuple[int, str]) -> int:
        return _observed_days(listings[key][1], now)

    for key, (price, first_seen) in listings.items():
        if first_seen >= since:
            events.append(
                {
                    "event": "new",
                    "zpid": key[0],
                    "kind": key[1],
                    "price": price,
                    # The listing was first seen when it first appeared; that
                    # is when the event happened, and it is what orders it
                    # against the price and status events below.
                    "seen_utc": first_seen,
                    "days_observed": observed_days(key),
                }
            )

    # The previous price of a listing is the price of the row before it, so
    # it is found in the database with a window function rather than by
    # reading the whole history into memory. The window is computed over
    # every row, so a price that moved before ``since`` is still the
    # baseline the change is measured against.
    for zpid, kind, price, seen_utc, previous in conn.execute(
        "SELECT zpid, kind, price, seen_utc, previous_price FROM ("
        "SELECT zpid, kind, price, seen_utc, "
        "LAG(price) OVER (PARTITION BY zpid, kind ORDER BY seen_utc) AS previous_price "
        "FROM price_history) WHERE seen_utc >= ?",
        (since,),
    ):
        if previous is None or previous == price:
            continue
        key = (zpid, kind)
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

    for zpid, kind, status, first_seen_utc in conn.execute(
        "SELECT zpid, kind, status, first_seen_utc FROM status_history "
        "WHERE first_seen_utc >= ? ORDER BY first_seen_utc",
        (since,),
    ):
        key = (zpid, kind)
        events.append(
            {
                "event": "status_change",
                "zpid": zpid,
                "kind": kind,
                "status": status,
                "seen_utc": first_seen_utc,
                "days_observed": observed_days(key) if key in listings else None,
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
        # The listings the run before this one saw. Only the run right before
        # the current one is compared, so one set is enough.
        prev_present: set[tuple[int, str]] | None = None
        # Every listing any earlier run of this group saw. It is accumulated
        # as the runs are walked, so the report costs one pass over the
        # sightings rather than a union of all of them for every run.
        all_prior: set[tuple[int, str]] = set()
        for run_id, started_utc in run_list:
            present = {
                (zpid, kind)
                for zpid, kind in conn.execute("SELECT zpid, kind FROM sightings WHERE run_id = ?", (run_id,))
            }
            if prev_present is not None and started_utc >= since:
                events.extend(
                    {
                        "event": "delisted",
                        "zpid": key[0],
                        "kind": key[1],
                        "seen_utc": started_utc,
                        "days_observed": observed_days(key) if key in listings else None,
                    }
                    for key in sorted(prev_present - present)
                )
                events.extend(
                    {
                        "event": "relisted",
                        "zpid": key[0],
                        "kind": key[1],
                        "seen_utc": started_utc,
                        "days_observed": observed_days(key) if key in listings else None,
                    }
                    for key in sorted((present - prev_present) & all_prior)
                )
            prev_present = present
            all_prior |= present

    return sorted(events, key=lambda event: (event["seen_utc"], event["zpid"], event["event"]))
