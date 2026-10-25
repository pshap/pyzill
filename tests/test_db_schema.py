"""Database tests: schema."""

import sqlite3
from pathlib import Path

import pytest

from pyzill.db import (
    begin_sync_run,
    connect,
    connect_readonly,
    default_db_path,
    load_listings,
    upsert_listings,
    utc_now,
)


def test_default_db_path():
    """Test default_db_path uses the XDG data directory."""
    path = default_db_path()
    assert path.name == "pyzill.db"
    assert path.parent.name == "pyzill"
    assert path.parent.parent.name == "share"


def test_default_db_path_honors_xdg_data_home(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert default_db_path() == tmp_path / "xdg" / "pyzill" / "pyzill.db"


def test_utc_now_format():
    """Test utc_now returns fixed-width ISO string."""
    ts = utc_now()
    assert isinstance(ts, str)
    assert len(ts) == 20  # 2026-10-18T12:34:56Z
    assert ts.endswith("Z")
    assert "T" in ts


def test_connect_creates_schema(tmp_path: Path):
    """Test connect creates schema on first run."""
    db_path = tmp_path / "test.db"
    conn = connect(db_path)

    # Check tables exist
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = [row[0] for row in cursor.fetchall()]
    assert "listings" in tables
    assert "price_history" in tables

    # Check user_version is set
    cursor = conn.execute("PRAGMA user_version")
    assert cursor.fetchone()[0] == 5

    conn.close()


def test_fresh_database_enforces_the_value_constraints(tmp_path: Path):
    """The DDL rejects a kind or truncated flag the parser never produces."""
    conn = connect(tmp_path / "checks.db")
    now = "2026-10-01T00:00:00Z"

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO listings (zpid, kind, first_seen_utc, last_seen_utc) VALUES (1, 'bogus', ?, ?)",
            (now, now),
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO price_history (zpid, kind, price, seen_utc) VALUES (1, 'bogus', 100, ?)",
            (now,),
        )
    run_id = begin_sync_run(conn, kind="sale", started_utc=now)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE sync_runs SET truncated = 2 WHERE id = ?", (run_id,))
    conn.close()


def test_backfill_definition_keeps_a_check_constraint_with_commas():
    """A CHECK's commas belong to the definition, not to the column list."""
    from pyzill.db.schema import LISTING_COLUMN_DEFINITIONS

    assert LISTING_COLUMN_DEFINITIONS["kind"] == "TEXT NOT NULL DEFAULT 'sale' CHECK (kind IN ('sale', 'rent', 'sold'))"


def test_connect_survives_concurrent_first_open(tmp_path: Path):
    """Concurrent first opens must not both run the migration."""
    import threading

    db_path = tmp_path / "concurrent.db"
    barrier = threading.Barrier(4)
    errors: list[Exception] = []

    def open_once() -> None:
        try:
            barrier.wait(timeout=5)
            connect(db_path).close()
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=open_once) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == []
    conn = connect(db_path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
    conn.close()


def test_connect_idempotent(tmp_path: Path):
    """Test connect is idempotent (doesn't break on existing DB)."""
    db_path = tmp_path / "test.db"
    conn1 = connect(db_path)
    conn1.execute(
        "INSERT INTO listings (zpid, price, first_seen_utc, last_seen_utc) "
        "VALUES (1, 100, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
    )
    conn1.commit()
    conn1.close()

    # Reconnect should work and preserve data
    conn2 = connect(db_path)
    cursor = conn2.execute("SELECT COUNT(*) FROM listings")
    assert cursor.fetchone()[0] == 1
    conn2.close()


def test_readonly_connection_does_not_wait_for_writer(tmp_path: Path):
    db_path = tmp_path / "locked.db"
    writer = connect(db_path)
    upsert_listings(writer, [{"zpid": 1, "price": 100}], seen_utc="2026-10-01T00:00:00Z")
    writer.execute("BEGIN IMMEDIATE")

    try:
        reader = connect_readonly(db_path)
        assert reader.execute("SELECT price FROM listings WHERE zpid = 1").fetchone() == (100,)
        reader.close()
    finally:
        writer.rollback()
        writer.close()


def test_connect_current_schema_does_not_take_the_write_lock(tmp_path: Path):
    """Opening a current database is a read: it must not wait for its writer."""
    db_path = tmp_path / "current.db"
    connect(db_path).close()

    writer = connect(db_path)
    writer.execute("BEGIN IMMEDIATE")
    try:
        reader = connect(db_path)
        assert reader.execute("PRAGMA user_version").fetchone()[0] == 5
        reader.close()
    finally:
        writer.rollback()
        writer.close()


def test_v2_database_migrates_to_v3_and_preserves_data(tmp_path: Path):
    db_path = tmp_path / "v2.db"
    legacy = sqlite3.connect(db_path)
    legacy.executescript(
        """
        CREATE TABLE listings (
            zpid INTEGER PRIMARY KEY, kind TEXT NOT NULL DEFAULT 'sale', price INTEGER,
            price_label TEXT, beds REAL, baths REAL, area INTEGER, address TEXT,
            street TEXT, city TEXT, state TEXT, zipcode TEXT, latitude REAL,
            longitude REAL, home_type TEXT, status TEXT, status_type TEXT,
            detail_url TEXT, image_url TEXT, zestimate INTEGER, rent_zestimate INTEGER,
            first_seen_utc TEXT NOT NULL, last_seen_utc TEXT NOT NULL
        );
        CREATE TABLE price_history (
            zpid INTEGER NOT NULL REFERENCES listings(zpid) ON DELETE CASCADE,
            kind TEXT NOT NULL DEFAULT 'sale', price INTEGER NOT NULL, seen_utc TEXT NOT NULL,
            PRIMARY KEY (zpid, kind, seen_utc)
        );
        INSERT INTO listings (zpid, kind, price, address, city, zipcode, first_seen_utc, last_seen_utc)
            VALUES (7, 'sale', 450000, '7 Main St', 'Albany', '01234', '2026-01-01T00:00:00Z', '2026-01-02T00:00:00Z');
        INSERT INTO price_history VALUES (7, 'sale', 450000, '2026-01-01T00:00:00Z');
        PRAGMA user_version = 2;
        """
    )
    legacy.close()

    conn = connect(db_path)

    assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
    assert conn.execute("SELECT zpid, kind, price, city, zipcode FROM listings").fetchall() == [
        (7, "sale", 450000, "Albany", "01234")
    ]
    assert conn.execute("SELECT zpid, kind, price FROM price_history").fetchall() == [(7, "sale", 450000)]
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"sync_runs", "sightings", "status_history"} <= tables
    conn.close()


def test_backfilled_numeric_column_keeps_its_type(tmp_path: Path):
    """A numeric column added on open must not be added as text.

    A backfilled database that stored a numeric column as text would hand
    back strings where the listing shape promises numbers.
    """
    db_path = tmp_path / "numeric-backfill.db"
    conn = connect(db_path)
    conn.execute("ALTER TABLE listings DROP COLUMN beds")
    conn.commit()
    conn.close()

    conn = connect(db_path)
    columns = {row[1]: row[2] for row in conn.execute("PRAGMA table_info(listings)")}
    assert columns["beds"] == "REAL"

    upsert_listings(conn, [{"zpid": 1, "price": 329900, "beds": 3.5}], seen_utc="2026-10-01T12:00:00Z")
    listing = load_listings(conn)[0]
    conn.close()

    assert listing["beds"] == 3.5


def test_connect_backfills_missing_columns_at_current_version(tmp_path: Path):
    """A same-version database missing a newer column gains it with the right type."""
    db_path = tmp_path / "partial.db"
    legacy = sqlite3.connect(db_path)
    legacy.executescript(
        """
        CREATE TABLE listings (
            zpid INTEGER NOT NULL, kind TEXT NOT NULL DEFAULT 'sale', price INTEGER,
            price_label TEXT, beds REAL, baths REAL, area INTEGER, address TEXT,
            street TEXT, city TEXT, state TEXT, zipcode TEXT, latitude REAL,
            longitude REAL, home_type TEXT, status TEXT, status_type TEXT,
            detail_url TEXT, image_url TEXT, zestimate INTEGER, rent_zestimate INTEGER,
            first_seen_utc TEXT NOT NULL, last_seen_utc TEXT NOT NULL,
            PRIMARY KEY (zpid, kind)
        );
        INSERT INTO listings (zpid, kind, address, first_seen_utc, last_seen_utc)
            VALUES (7, 'sale', '7 Main St', '2026-01-01T00:00:00Z', '2026-01-02T00:00:00Z');
        PRAGMA user_version = 3;
        """
    )
    legacy.close()

    conn = connect(db_path)

    columns = {row[1]: row[2] for row in conn.execute("PRAGMA table_info(listings)")}
    assert columns["broker_name"] == "TEXT"
    assert columns["lot_area_value"] == "REAL"
    assert columns["year_built"] == "INTEGER"
    assert conn.execute("SELECT address FROM listings WHERE zpid = 7").fetchone()[0] == "7 Main St"
    conn.close()


def test_v1_database_migrates_kind_columns_and_history(tmp_path: Path):
    db_path = tmp_path / "v1.db"
    legacy = sqlite3.connect(db_path)
    legacy.executescript(
        """
        CREATE TABLE listings (
            zpid INTEGER PRIMARY KEY,
            first_seen_utc TEXT NOT NULL,
            last_seen_utc TEXT NOT NULL
        );
        CREATE TABLE price_history (
            zpid INTEGER NOT NULL REFERENCES listings(zpid) ON DELETE CASCADE,
            price INTEGER NOT NULL,
            seen_utc TEXT NOT NULL,
            PRIMARY KEY (zpid, seen_utc)
        );
        INSERT INTO listings VALUES (1, '2026-01-01T00:00:00Z', '2026-01-02T00:00:00Z');
        INSERT INTO price_history VALUES (1, 100, '2026-01-01T00:00:00Z');
        PRAGMA user_version = 1;
        """
    )
    legacy.close()

    conn = connect(db_path)

    assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
    assert conn.execute("SELECT kind FROM listings WHERE zpid = 1").fetchone()[0] == "sale"
    assert conn.execute("SELECT kind FROM price_history WHERE zpid = 1").fetchone()[0] == "sale"
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] > 0

    # A migrated database must have the same listings column order as a
    # freshly created one (kind second, not appended after last_seen_utc).
    migrated_cols = [row[1] for row in conn.execute("PRAGMA table_info(listings)")]
    fresh = connect(tmp_path / "fresh.db")
    fresh_cols = [row[1] for row in fresh.execute("PRAGMA table_info(listings)")]
    assert migrated_cols == fresh_cols
    assert migrated_cols.index("kind") == 1

    # Price history rows survive the listings rebuild (no cascade delete).
    assert conn.execute("SELECT COUNT(*) FROM price_history").fetchone()[0] == 1

    conn.close()
    fresh.close()


def test_unversioned_database_with_a_listings_table_is_rebuilt(tmp_path: Path):
    """A listings table with no version stamp is rebuilt, not treated as fresh.

    Assuming such a database was fresh left the old table in place and then
    failed to build an index over a column it did not have, so the database
    could not be opened at all.
    """
    db_path = tmp_path / "unversioned.db"
    legacy = sqlite3.connect(db_path)
    legacy.executescript(
        """
        CREATE TABLE listings (
            zpid INTEGER PRIMARY KEY,
            price INTEGER,
            city TEXT,
            first_seen_utc TEXT NOT NULL,
            last_seen_utc TEXT NOT NULL
        );
        CREATE TABLE price_history (
            zpid INTEGER NOT NULL REFERENCES listings(zpid) ON DELETE CASCADE,
            price INTEGER NOT NULL,
            seen_utc TEXT NOT NULL,
            PRIMARY KEY (zpid, seen_utc)
        );
        INSERT INTO listings VALUES (1, 100, 'Albany', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z');
        INSERT INTO listings VALUES (2, 200, 'Troy', '2026-01-02T00:00:00Z', '2026-01-02T00:00:00Z');
        INSERT INTO price_history VALUES (1, 100, '2026-01-01T00:00:00Z');
        INSERT INTO price_history VALUES (2, 200, '2026-01-02T00:00:00Z');
        -- A history row for a listing that is not there cannot be carried
        -- across: the rebuilt history references the listings table.
        INSERT INTO price_history VALUES (99, 999, '2026-01-03T00:00:00Z');
        """
    )
    legacy.close()

    conn = connect(db_path)

    assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
    # The rows are kept, and the kind the old table did not have is filled in.
    assert conn.execute("SELECT zpid, kind, price, city FROM listings ORDER BY zpid").fetchall() == [
        (1, "sale", 100, "Albany"),
        (2, "sale", 200, "Troy"),
    ]
    assert conn.execute("SELECT zpid, kind FROM price_history ORDER BY zpid").fetchall() == [
        (1, "sale"),
        (2, "sale"),
    ]
    # The rebuilt table has the current primary key and the current indexes.
    ddl = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'listings'").fetchone()[0]
    assert "PRIMARY KEY (zpid, kind)" in ddl
    indexes = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert "ix_listings_kind_zipcode" in indexes
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

    # The rebuilt database is usable.
    upsert_listings(conn, [{"zpid": 3, "price": 300}], seen_utc="2026-10-01T00:00:00Z")
    assert load_listings(conn)[0]["zpid"] == 3
    conn.close()


def test_v1_migration_carries_wide_columns_across_rebuild(tmp_path: Path):
    db_path = tmp_path / "wide_v1.db"
    legacy = sqlite3.connect(db_path)
    legacy.executescript(
        """
        CREATE TABLE listings (
            zpid INTEGER PRIMARY KEY,
            price INTEGER,
            address TEXT,
            city TEXT,
            latitude REAL,
            longitude REAL,
            first_seen_utc TEXT NOT NULL,
            last_seen_utc TEXT NOT NULL
        );
        CREATE TABLE price_history (
            zpid INTEGER NOT NULL REFERENCES listings(zpid) ON DELETE CASCADE,
            price INTEGER NOT NULL,
            seen_utc TEXT NOT NULL,
            PRIMARY KEY (zpid, seen_utc)
        );
        INSERT INTO listings VALUES (7, 450000, '1 Main St', 'Schenectady', 42.8, -73.9,
            '2026-01-01T00:00:00Z', '2026-01-02T00:00:00Z');
        INSERT INTO price_history VALUES (7, 450000, '2026-01-01T00:00:00Z');
        PRAGMA user_version = 1;
        """
    )
    legacy.close()

    conn = connect(db_path)

    row = conn.execute(
        "SELECT zpid, kind, price, address, city, latitude, longitude, "
        "first_seen_utc, last_seen_utc FROM listings WHERE zpid = 7"
    ).fetchone()
    assert row == (
        7,
        "sale",
        450000,
        "1 Main St",
        "Schenectady",
        42.8,
        -73.9,
        "2026-01-01T00:00:00Z",
        "2026-01-02T00:00:00Z",
    )
    assert conn.execute("SELECT COUNT(*) FROM price_history").fetchone()[0] == 1
    conn.close()


def test_connect_closes_connection_on_keyboard_interrupt(tmp_path: Path, monkeypatch):
    """A BaseException during migration must not leak the connection.

    ``except Exception`` would let ``KeyboardInterrupt``/``SystemExit`` escape
    with the write connection still open, leaving a lock that blocks every
    later ``connect()``. A leaked lock surfaces here as a retryable
    ``sqlite3.OperationalError: database is locked``.
    """
    db_path = tmp_path / "ki.db"
    legacy = sqlite3.connect(str(db_path))
    legacy.execute("PRAGMA user_version = 2")
    legacy.commit()
    legacy.close()

    class _FakeBaseException(BaseException):
        """Stands in for KeyboardInterrupt/SystemExit: BaseException, not Exception."""

    calls = [0]

    def boom(conn):
        calls[0] += 1
        if calls[0] == 1:
            raise _FakeBaseException

    monkeypatch.setattr("pyzill.db.schema._MIGRATIONS", {2: boom, 3: boom, 4: boom})

    with pytest.raises(_FakeBaseException):
        connect(db_path)

    # A subsequent connect must succeed: no leaked write lock.
    reopened = connect(db_path)
    assert reopened.execute("PRAGMA user_version").fetchone()[0] == 5
    reopened.close()
