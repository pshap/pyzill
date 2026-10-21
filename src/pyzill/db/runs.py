"""Sync-run bookkeeping: run records and the timestamps they carry.

The run lifecycle lives here: :func:`begin_sync_run` creates a run and
:func:`finish_sync_run` completes it. Callers that want a comparable run
(complete search results) create it with the query text; the write path
finishes the run it was given.
"""

import json
import sqlite3
from collections.abc import Mapping
from typing import Any

from pyzill.db.timestamps import utc_now

__all__ = [
    "begin_sync_run",
    "comparable_run_had_results",
    "finish_sync_run",
    "utc_now",
]


def begin_sync_run(  # noqa: PLR0913
    conn: sqlite3.Connection,
    *,
    kind: str,
    query: str = "",
    bbox: Mapping[str, Any] | None = None,
    filters: Mapping[str, Any] | None = None,
    started_utc: str | None = None,
) -> int:
    """Create a run record and return its database ID.

    A run with a non-empty ``query`` is a complete snapshot of that query's
    results, so two such runs can be compared to detect delistings. A run with
    no query text records an ingest (``db import``) or an anonymous upsert,
    whose absences mean "not in this batch" rather than "delisted"; change
    detection never compares those runs.
    """
    cursor = conn.execute(
        "INSERT INTO sync_runs (started_utc, kind, query, bbox_json, filters_json) VALUES (?, ?, ?, ?, ?)",
        (
            started_utc or utc_now(),
            kind,
            query,
            json.dumps(bbox, sort_keys=True) if bbox is not None else None,
            json.dumps(filters, sort_keys=True) if filters is not None else None,
        ),
    )
    if cursor.lastrowid is None:
        raise sqlite3.DatabaseError("Failed to retrieve sync_run id")
    return int(cursor.lastrowid)


def finish_sync_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    result_count: int,
    truncated: bool = False,
    finished_utc: str | None = None,
) -> None:
    """Complete a run after its results have been stored."""
    conn.execute(
        "UPDATE sync_runs SET finished_utc = ?, result_count = ?, truncated = ? WHERE id = ?",
        (finished_utc or utc_now(), result_count, int(truncated), run_id),
    )


def comparable_run_had_results(
    conn: sqlite3.Connection,
    *,
    kind: str,
    query: str = "",
    bbox: Mapping[str, Any] | None = None,
    filters: Mapping[str, Any] | None = None,
) -> bool:
    """Return True if a prior complete run with the same key returned any rows.

    Used to flag an empty result for a search that previously matched as
    suspect (a soft block or transient glitch), so it is not mistaken for a
    complete run in which every listing genuinely disappeared.
    """
    row = conn.execute(
        "SELECT 1 FROM sync_runs WHERE kind = ? AND query = ? AND bbox_json IS ? AND filters_json IS ? "
        "AND finished_utc IS NOT NULL AND truncated = 0 AND result_count > 0 LIMIT 1",
        (
            kind,
            query,
            json.dumps(bbox, sort_keys=True) if bbox is not None else None,
            json.dumps(filters, sort_keys=True) if filters is not None else None,
        ),
    ).fetchone()
    return row is not None
