"""Sync-run bookkeeping: timestamps, run records, and the default DB path."""

import json
import os
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

__all__ = [
    "begin_sync_run",
    "comparable_run_had_results",
    "default_db_path",
    "finish_sync_run",
    "utc_now",
]


def default_db_path() -> Path:
    """Return the XDG data directory's pyzill database path."""
    data_home = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return data_home / "pyzill" / "pyzill.db"


def utc_now() -> str:
    """Return current UTC time as ISO string (fixed-width %Y-%m-%dT%H:%M:%SZ)."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def begin_sync_run(  # noqa: PLR0913
    conn: sqlite3.Connection,
    *,
    kind: str,
    query: str = "",
    bbox: Mapping[str, Any] | None = None,
    filters: Mapping[str, Any] | None = None,
    started_utc: str | None = None,
) -> int:
    """Create a run record and return its database ID."""
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
