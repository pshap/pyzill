"""SQLite database for tracking Zillow listings with price history.

Uses stdlib sqlite3 for zero new dependencies. Database lives at ./pyzill.db
by default (configurable via --db or PYZILL_DB).
"""

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pyzill.normalize import parse_price

__all__ = [
    "DBStats",
    "SyncStats",
    "connect",
    "db_stats",
    "default_db_path",
    "get_price_history",
    "load_listings",
    "upsert_listings",
    "utc_now",
]


def default_db_path() -> Path:
    """Return the default database path (./pyzill.db)."""
    return Path.cwd() / "pyzill.db"


def utc_now() -> str:
    """Return current UTC time as ISO string (fixed-width %Y-%m-%dT%H:%M:%SZ)."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


_SCHEMA_VERSION = 1
_SCHEMA_SQL = """
PRAGMA user_version = 1;

CREATE TABLE IF NOT EXISTS listings (
  zpid INTEGER PRIMARY KEY,
  price INTEGER,
  price_label TEXT,
  beds REAL,
  baths REAL,
  area INTEGER,
  address TEXT,
  street TEXT,
  city TEXT,
  state TEXT,
  zipcode TEXT,
  latitude REAL,
  longitude REAL,
  home_type TEXT,
  status TEXT,
  status_type TEXT,
  detail_url TEXT,
  image_url TEXT,
  zestimate INTEGER,
  rent_zestimate INTEGER,
  first_seen_utc TEXT NOT NULL,
  last_seen_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS price_history (
  zpid INTEGER NOT NULL REFERENCES listings(zpid) ON DELETE CASCADE,
  price INTEGER NOT NULL,
  seen_utc TEXT NOT NULL,
  PRIMARY KEY (zpid, seen_utc)
);

CREATE INDEX IF NOT EXISTS ix_listings_last_seen ON listings(last_seen_utc);
"""


@dataclass
class SyncStats:
    """Statistics from a sync/import operation."""

    added: int
    updated: int
    price_changes: int
    skipped: int


def connect(path: Path | str) -> sqlite3.Connection:
    """Connect to the database, creating schema if needed.

    Args:
        path: Path to the SQLite database file.

    Returns:
        Connection with foreign_keys enabled.
    """
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA foreign_keys = ON")

    # Check if schema exists by checking user_version
    cursor = conn.execute("PRAGMA user_version")
    version = cursor.fetchone()[0]

    if version == 0:
        # Create schema
        conn.executescript(_SCHEMA_SQL)
        conn.commit()

    return conn


def upsert_listings(
    conn: sqlite3.Connection,
    rows: list[dict[str, Any]],
    *,
    seen_utc: str | None = None,
) -> SyncStats:
    """Upsert listing rows into the database.

    Skips rows without a zpid (counted in stats). For each zpid:
    - New: insert row + initial price_history row (if price known)
    - Existing: overwrite listing fields, bump last_seen_utc, keep first_seen_utc
    - Price changed: append price_history row

    Args:
        conn: Database connection.
        rows: List of normalized listing dicts (must have 'zpid' field).
        seen_utc: Timestamp for this batch (default: utc_now()).

    Returns:
        SyncStats with counts of added/updated/price_changes/skipped.
    """
    if seen_utc is None:
        seen_utc = utc_now()

    stats = SyncStats(added=0, updated=0, price_changes=0, skipped=0)

    for row in rows:
        zpid = row.get("zpid")
        if zpid is None:
            stats.skipped += 1
            continue

        # Cast zpid to int (might be float from API)
        try:
            zpid = int(zpid)
        except (ValueError, TypeError):
            stats.skipped += 1
            continue

        # Check if listing exists
        cursor = conn.execute(
            "SELECT price, first_seen_utc FROM listings WHERE zpid = ?",
            (zpid,),
        )
        existing = cursor.fetchone()

        # parse_price tolerates raw "$459,000" strings from unnormalized rows
        # and maps 0/"$0" (Zillow's unknown sentinel) to None.
        price_int = parse_price(row.get("price"))

        if existing is None:
            # New listing
            stats.added += 1
            conn.execute(
                """
                INSERT INTO listings (
                    zpid, price, price_label, beds, baths, area,
                    address, street, city, state, zipcode,
                    latitude, longitude, home_type, status, status_type,
                    detail_url, image_url, zestimate, rent_zestimate,
                    first_seen_utc, last_seen_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    zpid,
                    price_int,
                    row.get("price_label"),
                    row.get("beds"),
                    row.get("baths"),
                    row.get("area"),
                    row.get("address"),
                    row.get("street"),
                    row.get("city"),
                    row.get("state"),
                    row.get("zipcode"),
                    row.get("latitude"),
                    row.get("longitude"),
                    row.get("home_type"),
                    row.get("status"),
                    row.get("status_type"),
                    row.get("detail_url"),
                    row.get("image_url"),
                    row.get("zestimate"),
                    row.get("rent_zestimate"),
                    seen_utc,
                    seen_utc,
                ),
            )

            # Add initial price history if price known
            if price_int is not None:
                conn.execute(
                    "INSERT INTO price_history (zpid, price, seen_utc) VALUES (?, ?, ?) "
                    "ON CONFLICT(zpid, seen_utc) DO UPDATE SET price = excluded.price",
                    (zpid, price_int, seen_utc),
                )
        else:
            # Existing listing
            stats.updated += 1
            old_price, _first_seen = existing

            conn.execute(
                """
                UPDATE listings SET
                    price = ?, price_label = ?, beds = ?, baths = ?, area = ?,
                    address = ?, street = ?, city = ?, state = ?, zipcode = ?,
                    latitude = ?, longitude = ?, home_type = ?, status = ?, status_type = ?,
                    detail_url = ?, image_url = ?, zestimate = ?, rent_zestimate = ?,
                    last_seen_utc = ?
                WHERE zpid = ?
                """,
                (
                    price_int,
                    row.get("price_label"),
                    row.get("beds"),
                    row.get("baths"),
                    row.get("area"),
                    row.get("address"),
                    row.get("street"),
                    row.get("city"),
                    row.get("state"),
                    row.get("zipcode"),
                    row.get("latitude"),
                    row.get("longitude"),
                    row.get("home_type"),
                    row.get("status"),
                    row.get("status_type"),
                    row.get("detail_url"),
                    row.get("image_url"),
                    row.get("zestimate"),
                    row.get("rent_zestimate"),
                    seen_utc,
                    zpid,
                ),
            )

            # Track price changes (old and new both known, or price newly
            # appearing; a vanished price isn't a change). The
            # upsert-on-conflict keeps a second sighting in the same second
            # from crashing on the (zpid, seen_utc) primary key.
            if price_int is not None and old_price != price_int:
                stats.price_changes += 1
                conn.execute(
                    "INSERT INTO price_history (zpid, price, seen_utc) VALUES (?, ?, ?) "
                    "ON CONFLICT(zpid, seen_utc) DO UPDATE SET price = excluded.price",
                    (zpid, price_int, seen_utc),
                )

    conn.commit()
    return stats


def load_listings(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Load all listings from the database.

    Returns:
        List of listing dicts (with first_seen_utc and last_seen_utc).
    """
    cursor = conn.execute("SELECT * FROM listings ORDER BY last_seen_utc DESC, zpid DESC")
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
        "SELECT price, seen_utc FROM price_history WHERE zpid = ? ORDER BY seen_utc ASC",
        (zpid,),
    )
    columns = [desc[0] for desc in cursor.description]
    return [dict(zip(columns, row, strict=False)) for row in cursor.fetchall()]


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
    db_size_bytes = Path(db_path).stat().st_size if Path(db_path).exists() else 0

    return DBStats(
        total_listings=total_listings,
        oldest_seen=oldest_seen,
        newest_seen=newest_seen,
        db_path=db_path,
        db_size_bytes=db_size_bytes,
    )
