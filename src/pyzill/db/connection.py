"""Connection management: opening, migrating, and read-only access."""

import contextlib
import sqlite3
from pathlib import Path

from pyzill.db.schema import SCHEMA_VERSION, ensure_schema

__all__ = ["connect", "connect_readonly"]


def connect(path: Path | str) -> sqlite3.Connection:
    """Connect to the database, creating schema if needed.

    Args:
        path: Path to the SQLite database file.

    Returns:
        Connection with foreign_keys enabled.
    """
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    # Enable WAL. This takes a lock and can race with a concurrent first open;
    # WAL is persistent in the file once set, so a "busy" result just means
    # another connection is already switching it (or has).
    if str(path) != ":memory:":
        with contextlib.suppress(sqlite3.OperationalError):
            conn.execute("PRAGMA journal_mode = wal")

    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version > SCHEMA_VERSION:
        conn.close()
        raise sqlite3.DatabaseError(
            f"Database schema version {version} is newer than supported version {SCHEMA_VERSION}"
        )

    try:
        conn.execute("BEGIN IMMEDIATE")
        # Re-read under the write lock: a concurrent first run may have
        # migrated between our initial read and acquiring the lock.
        ensure_schema(conn)
        conn.commit()

    except BaseException:
        conn.rollback()
        conn.close()
        raise

    return conn


def connect_readonly(path: Path | str) -> sqlite3.Connection:
    """Open an existing database without schema writes or migration attempts."""
    uri = f"{Path(path).resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA query_only = ON")
    return conn
