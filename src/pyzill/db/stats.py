"""Database statistics: row counts, timestamps, and file size."""

import sqlite3
from dataclasses import dataclass
from pathlib import Path

__all__ = ["DBStats", "db_stats"]


@dataclass
class DBStats:
    """Database statistics."""

    total_listings: int
    oldest_seen: str | None
    newest_seen: str | None
    db_path: str
    db_size_bytes: int


def db_stats(conn: sqlite3.Connection) -> DBStats:
    """Get database statistics.

    Args:
        conn: Database connection.

    Returns:
        DBStats with counts, timestamps, and file info.
    """
    cursor = conn.execute("SELECT COUNT(*) FROM listings")
    total_listings = cursor.fetchone()[0]

    cursor = conn.execute("SELECT MIN(first_seen_utc), MAX(last_seen_utc) FROM listings")
    oldest_seen, newest_seen = cursor.fetchone()

    db_path = conn.execute("PRAGMA database_list").fetchone()[2]
    db_size_bytes = Path(db_path).stat().st_size if db_path and Path(db_path).exists() else 0

    return DBStats(
        total_listings=total_listings,
        oldest_seen=oldest_seen,
        newest_seen=newest_seen,
        db_path=db_path,
        db_size_bytes=db_size_bytes,
    )
