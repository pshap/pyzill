"""Connection management: opening, migrating, and read-only access."""

import contextlib
import sqlite3
from pathlib import Path

from pyzill.db.schema import SCHEMA_VERSION, ensure_schema, schema_needs_update

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
    try:
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
            raise sqlite3.DatabaseError(
                f"Database schema version {version} is newer than supported version {SCHEMA_VERSION}"
            )

        # A database that is already at the current version with every column
        # in place is only read, so opening it must not take the write lock.
        if schema_needs_update(conn):
            try:
                conn.execute("BEGIN IMMEDIATE")
                # Re-read under the write lock: a concurrent first run may have
                # migrated between our initial read and acquiring the lock.
                ensure_schema(conn)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
    except BaseException:
        # Any failure during the PRAGMAs or the migration leaves a connection
        # that nobody will close, so close it here before propagating.
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
