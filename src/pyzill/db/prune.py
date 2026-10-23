"""Pruning old sync runs and their sightings.

The listings and their price history are the durable record; a sync run's
sightings (which carry the raw payload) are the part that grows with every
search. Pruning drops whole old runs, and the foreign key cascades their
sightings, so a database cannot be left with sightings that have no run.
"""

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from pyzill.db.runs import utc_now

__all__ = ["PruneStats", "prune_database"]

_CHUNK = 500


@dataclass(frozen=True)
class PruneStats:
    """What a prune removed."""

    runs_deleted: int
    sightings_deleted: int
    vacuumed: bool


def _cutoff(older_than_days: int, now: str) -> str:
    """Return the UTC timestamp ``older_than_days`` before ``now``."""
    moment = datetime.strptime(now, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    return (moment - timedelta(days=older_than_days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run_ids_to_delete(
    conn: sqlite3.Connection,
    *,
    cutoff: str | None,
    keep_runs: int | None,
) -> list[int]:
    """Resolve the runs selected by the age and retention options."""
    selected: set[int] = set()
    if cutoff is not None:
        selected.update(row[0] for row in conn.execute("SELECT id FROM sync_runs WHERE started_utc < ?", (cutoff,)))
    if keep_runs is not None:
        kept = {
            row[0]
            for row in conn.execute(
                "SELECT id FROM sync_runs ORDER BY started_utc DESC, id DESC LIMIT ?",
                (keep_runs,),
            )
        }
        selected.update(row[0] for row in conn.execute("SELECT id FROM sync_runs") if row[0] not in kept)
    return sorted(selected)


def _delete_runs(conn: sqlite3.Connection, run_ids: list[int]) -> tuple[int, int]:
    """Delete the runs and return how many runs and sightings went with them."""
    sightings = 0
    for start in range(0, len(run_ids), _CHUNK):
        chunk = run_ids[start : start + _CHUNK]
        marks = ", ".join("?" * len(chunk))
        sightings += conn.execute(
            f"SELECT COUNT(*) FROM sightings WHERE run_id IN ({marks})",  # noqa: S608 - values are bound
            chunk,
        ).fetchone()[0]
        conn.execute(
            f"DELETE FROM sync_runs WHERE id IN ({marks})",  # noqa: S608 - values are bound
            chunk,
        )
    return len(run_ids), sightings


def prune_database(  # noqa: PLR0913 - one prune entry point with its options
    conn: sqlite3.Connection,
    *,
    older_than_days: int | None = None,
    keep_runs: int | None = None,
    vacuum: bool = False,
    now: str | None = None,
    dry_run: bool = False,
) -> PruneStats:
    """Delete old sync runs (and their sightings) from the database.

    Args:
        conn: The database connection.
        older_than_days: Delete runs that started more than this many days ago.
        keep_runs: Keep only the newest N runs; the rest are deleted.
        vacuum: Rebuild the database file after deleting, reclaiming its space.
        now: Override the current UTC timestamp (for tests).
        dry_run: When True, resolve the runs to delete and count their sightings
            but issue no DELETE statements. The returned ``PruneStats``
            describes what a real run would remove.

    Returns:
        PruneStats: the number of runs and sightings deleted, and whether a
        vacuum ran.

    Both selectors may be combined; a run selected by either one is deleted.
    The listings and the price history are kept, so a pruned database still
    answers "what do I know about this property?" even after the raw payloads
    of old searches are gone.
    """
    if older_than_days is not None and older_than_days < 0:
        raise ValueError("older_than_days must be non-negative")
    if keep_runs is not None and keep_runs < 0:
        raise ValueError("keep_runs must be non-negative")

    cutoff = _cutoff(older_than_days, now or utc_now()) if older_than_days is not None else None
    run_ids = _run_ids_to_delete(conn, cutoff=cutoff, keep_runs=keep_runs)
    if dry_run:
        sightings = 0
        for start in range(0, len(run_ids), _CHUNK):
            chunk = run_ids[start : start + _CHUNK]
            marks = ", ".join("?" * len(chunk))
            sightings += conn.execute(
                f"SELECT COUNT(*) FROM sightings WHERE run_id IN ({marks})",  # noqa: S608 - values are bound
                chunk,
            ).fetchone()[0]
        return PruneStats(runs_deleted=len(run_ids), sightings_deleted=sightings, vacuumed=False)
    runs_deleted, sightings_deleted = _delete_runs(conn, run_ids)
    conn.commit()
    if vacuum:
        conn.execute("VACUUM")
    return PruneStats(runs_deleted=runs_deleted, sightings_deleted=sightings_deleted, vacuumed=vacuum)
