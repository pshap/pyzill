"""Tests for pyzill.db module."""

import itertools
import sqlite3
from pathlib import Path
from typing import Any

from pyzill.db import (
    connect,
    db_stats,
    default_db_path,
    get_price_history,
    load_listings,
    upsert_listings,
    utc_now,
)


def test_default_db_path():
    """Test default_db_path returns ./pyzill.db."""
    path = default_db_path()
    assert path.name == "pyzill.db"
    assert path.parent == Path.cwd()


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
    assert cursor.fetchone()[0] == 2

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


def test_upsert_new_listing(tmp_path: Path):
    """Test upsert_listings inserts new listings."""
    db_path = tmp_path / "test.db"
    conn = connect(db_path)

    rows = [
        {
            "zpid": 123,
            "price": 250000,
            "beds": 3,
            "baths": 2,
            "area": 1500,
            "address": "123 Main St",
            "city": "Schenectady",
            "state": "NY",
            "zipcode": "12305",
        }
    ]

    stats = upsert_listings(conn, rows, seen_utc="2026-10-01T12:00:00Z")

    assert stats.added == 1
    assert stats.updated == 0
    assert stats.price_changes == 0
    assert stats.skipped == 0

    # Verify listing was inserted
    cursor = conn.execute("SELECT * FROM listings WHERE zpid = 123")
    row = cursor.fetchone()
    assert row is not None
    assert row[2] == 250000  # price
    assert row[7] == "123 Main St"  # address

    # Verify price history was created
    cursor = conn.execute("SELECT * FROM price_history WHERE zpid = 123")
    hist = cursor.fetchone()
    assert hist is not None
    assert hist[2] == 250000  # price
    assert hist[3] == "2026-10-01T12:00:00Z"  # seen_utc

    conn.close()


def test_upsert_existing_listing(tmp_path: Path):
    """Test upsert_listings updates existing listings."""
    db_path = tmp_path / "test.db"
    conn = connect(db_path)

    # Insert initial listing
    rows: list[dict[str, Any]] = [
        {
            "zpid": 123,
            "price": 250000,
            "beds": 3,
            "baths": 2,
            "area": 1500,
            "address": "123 Main St",
            "city": "Schenectady",
            "state": "NY",
            "zipcode": "12305",
        }
    ]
    upsert_listings(conn, rows, seen_utc="2026-10-01T12:00:00Z")

    # Update with new data
    rows[0]["price"] = 260000
    rows[0]["beds"] = 4
    stats = upsert_listings(conn, rows, seen_utc="2026-10-02T12:00:00Z")

    assert stats.added == 0
    assert stats.updated == 1
    assert stats.price_changes == 1  # price changed
    assert stats.skipped == 0

    # Verify fields updated
    cursor = conn.execute("SELECT price, beds, first_seen_utc, last_seen_utc FROM listings WHERE zpid = 123")
    row = cursor.fetchone()
    assert row[0] == 260000  # price updated
    assert row[1] == 4  # beds updated
    assert row[2] == "2026-10-01T12:00:00Z"  # first_seen unchanged
    assert row[3] == "2026-10-02T12:00:00Z"  # last_seen updated

    # Verify price history has both entries
    cursor = conn.execute("SELECT price, seen_utc FROM price_history WHERE zpid = 123 ORDER BY seen_utc")
    history = cursor.fetchall()
    assert len(history) == 2
    assert history[0] == (250000, "2026-10-01T12:00:00Z")
    assert history[1] == (260000, "2026-10-02T12:00:00Z")

    conn.close()


def test_upsert_preserves_first_seen(tmp_path: Path):
    """Test upsert_listings preserves first_seen_utc on updates."""
    db_path = tmp_path / "test.db"
    conn = connect(db_path)

    rows = [{"zpid": 123, "price": 250000}]
    upsert_listings(conn, rows, seen_utc="2026-10-01T12:00:00Z")

    rows[0]["price"] = 260000
    upsert_listings(conn, rows, seen_utc="2026-10-02T12:00:00Z")

    cursor = conn.execute("SELECT first_seen_utc, last_seen_utc FROM listings WHERE zpid = 123")
    row = cursor.fetchone()
    assert row[0] == "2026-10-01T12:00:00Z"  # first_seen preserved
    assert row[1] == "2026-10-02T12:00:00Z"  # last_seen updated

    conn.close()


def test_upsert_price_newly_appearing(tmp_path: Path):
    """Test upsert_listings tracks price when it newly appears."""
    db_path = tmp_path / "test.db"
    conn = connect(db_path)

    # Insert without price
    rows = [{"zpid": 123, "price": None}]
    stats = upsert_listings(conn, rows, seen_utc="2026-10-01T12:00:00Z")
    assert stats.price_changes == 0

    # Update with price
    rows[0]["price"] = 250000
    stats = upsert_listings(conn, rows, seen_utc="2026-10-02T12:00:00Z")
    assert stats.price_changes == 1

    cursor = conn.execute("SELECT COUNT(*) FROM price_history WHERE zpid = 123")
    assert cursor.fetchone()[0] == 1

    conn.close()


def test_price_change_counting_rules(tmp_path: Path):
    """Pin the documented SyncStats.price_changes semantics."""
    conn = connect(tmp_path / "rules.db")

    # New zpid never counts.
    stats = upsert_listings(conn, [{"zpid": 1, "price": 300000}], seen_utc="2026-10-01T00:00:00Z")
    assert stats.price_changes == 0

    # Same price, same kind: no change.
    stats = upsert_listings(conn, [{"zpid": 1, "price": 300000}], seen_utc="2026-10-02T00:00:00Z")
    assert stats.price_changes == 0

    # Different price, same kind: change.
    stats = upsert_listings(conn, [{"zpid": 1, "price": 310000}], seen_utc="2026-10-03T00:00:00Z")
    assert stats.price_changes == 1

    # Kind change never counts, even with a different price.
    stats = upsert_listings(conn, [{"zpid": 1, "price": 2200}], kind="rent", seen_utc="2026-10-04T00:00:00Z")
    assert stats.price_changes == 0

    # First-ever price on an existing listing counts.
    upsert_listings(conn, [{"zpid": 2}], seen_utc="2026-10-01T00:00:00Z")  # no price
    stats = upsert_listings(conn, [{"zpid": 2, "price": 500000}], seen_utc="2026-10-05T00:00:00Z")
    assert stats.price_changes == 1

    conn.close()


def test_upsert_skip_no_zpid(tmp_path: Path):
    """Test upsert_listings skips rows without zpid."""
    db_path = tmp_path / "test.db"
    conn = connect(db_path)

    rows: list[dict[str, Any]] = [
        {"zpid": 123, "price": 250000},
        {"price": 300000},  # No zpid
        {"zpid": None, "price": 350000},  # Explicit None
    ]

    stats = upsert_listings(conn, rows, seen_utc="2026-10-01T12:00:00Z")

    assert stats.added == 1
    assert stats.skipped == 2

    cursor = conn.execute("SELECT COUNT(*) FROM listings")
    assert cursor.fetchone()[0] == 1

    conn.close()


def test_upsert_skip_invalid_zpid(tmp_path: Path):
    """Test upsert_listings skips rows with invalid zpid."""
    db_path = tmp_path / "test.db"
    conn = connect(db_path)

    rows: list[dict[str, Any]] = [
        {"zpid": "not-a-number", "price": 250000},
        {"zpid": 123.5, "price": 300000},  # Float should be castable
    ]

    stats = upsert_listings(conn, rows, seen_utc="2026-10-01T12:00:00Z")

    assert stats.skipped == 1  # "not-a-number" skipped
    assert stats.added == 1  # 123.5 cast to 123

    conn.close()


def test_upsert_float_zpid_cast(tmp_path: Path):
    """Test upsert_listings casts float zpid to int."""
    db_path = tmp_path / "test.db"
    conn = connect(db_path)

    rows = [{"zpid": 123.0, "price": 250000}]
    stats = upsert_listings(conn, rows, seen_utc="2026-10-01T12:00:00Z")

    assert stats.added == 1
    assert stats.skipped == 0

    cursor = conn.execute("SELECT zpid FROM listings WHERE zpid = 123")
    assert cursor.fetchone() is not None

    conn.close()


def test_upsert_no_price_change(tmp_path: Path):
    """Unchanged priced sightings remain recorded without a price-change count."""
    db_path = tmp_path / "test.db"
    conn = connect(db_path)

    rows = [{"zpid": 123, "price": 250000}]
    upsert_listings(conn, rows, seen_utc="2026-10-01T12:00:00Z")

    # Update with same price
    rows[0]["beds"] = 4  # Change other field
    stats = upsert_listings(conn, rows, seen_utc="2026-10-02T12:00:00Z")

    assert stats.updated == 1
    assert stats.price_changes == 0

    cursor = conn.execute("SELECT COUNT(*) FROM price_history WHERE zpid = 123")
    assert cursor.fetchone()[0] == 2  # Both priced sightings

    conn.close()


def test_upsert_same_second_price_change(tmp_path: Path):
    """A price change within the same second must not hit the history PK."""
    db_path = tmp_path / "test.db"
    conn = connect(db_path)

    upsert_listings(conn, [{"zpid": 123, "price": 250000}], seen_utc="2026-10-01T12:00:00Z")
    stats = upsert_listings(conn, [{"zpid": 123, "price": 260000}], seen_utc="2026-10-01T12:00:00Z")

    assert stats.updated == 1
    assert stats.price_changes == 1
    history = get_price_history(conn, 123)
    assert len(history) == 1
    assert history[0]["price"] == 260000

    conn.close()


def test_upsert_string_price(tmp_path: Path):
    """Raw '$459,000' strings coerce; '$0' is Zillow's unknown sentinel."""
    db_path = tmp_path / "test.db"
    conn = connect(db_path)

    stats = upsert_listings(
        conn,
        [{"zpid": 123, "price": "$459,000"}, {"zpid": 456, "price": "$0"}],
        seen_utc="2026-10-01T12:00:00Z",
    )
    assert stats.added == 2

    cursor = conn.execute("SELECT zpid, price FROM listings ORDER BY zpid")
    assert cursor.fetchall() == [(123, 459000), (456, None)]

    conn.close()


def test_load_listings_deterministic_tie_break(tmp_path: Path):
    """Rows last seen in the same second sort by zpid descending."""
    db_path = tmp_path / "test.db"
    conn = connect(db_path)

    upsert_listings(
        conn,
        [{"zpid": 123}, {"zpid": 456}, {"zpid": 789}],
        seen_utc="2026-10-01T12:00:00Z",
    )

    assert [row["zpid"] for row in load_listings(conn)] == [789, 456, 123]

    conn.close()


def test_load_listings(tmp_path: Path):
    """Test load_listings returns all listings."""
    db_path = tmp_path / "test.db"
    conn = connect(db_path)

    rows = [
        {"zpid": 123, "price": 250000, "city": "Schenectady"},
        {"zpid": 456, "price": 300000, "city": "Albany"},
    ]
    upsert_listings(conn, rows, seen_utc="2026-10-01T12:00:00Z")

    listings = load_listings(conn)
    assert len(listings) == 2
    assert listings[0]["zpid"] == 456  # Sorted by last_seen DESC
    assert listings[1]["zpid"] == 123

    conn.close()


def test_get_price_history(tmp_path: Path):
    """Test get_price_history returns price timeline."""
    db_path = tmp_path / "test.db"
    conn = connect(db_path)

    rows = [{"zpid": 123, "price": 250000}]
    upsert_listings(conn, rows, seen_utc="2026-10-01T12:00:00Z")

    rows[0]["price"] = 260000
    upsert_listings(conn, rows, seen_utc="2026-10-02T12:00:00Z")

    rows[0]["price"] = 270000
    upsert_listings(conn, rows, seen_utc="2026-10-03T12:00:00Z")

    history = get_price_history(conn, 123)
    assert len(history) == 3
    assert history[0] == {"price": 250000, "seen_utc": "2026-10-01T12:00:00Z", "kind": "sale"}
    assert history[1] == {"price": 260000, "seen_utc": "2026-10-02T12:00:00Z", "kind": "sale"}
    assert history[2] == {"price": 270000, "seen_utc": "2026-10-03T12:00:00Z", "kind": "sale"}

    conn.close()


def test_get_price_history_empty(tmp_path: Path):
    """Test get_price_history returns empty list for unknown zpid."""
    db_path = tmp_path / "test.db"
    conn = connect(db_path)

    history = get_price_history(conn, 999)
    assert history == []

    conn.close()


def test_db_stats(tmp_path: Path):
    """Test db_stats returns database statistics."""
    db_path = tmp_path / "test.db"
    conn = connect(db_path)

    rows = [
        {"zpid": 123, "price": 250000},
        {"zpid": 456, "price": 300000},
    ]
    upsert_listings(conn, rows, seen_utc="2026-10-01T12:00:00Z")

    stats = db_stats(conn)
    assert stats.total_listings == 2
    assert stats.oldest_seen == "2026-10-01T12:00:00Z"
    assert stats.newest_seen == "2026-10-01T12:00:00Z"
    assert stats.db_path == str(db_path)
    assert stats.db_size_bytes > 0

    conn.close()


def test_db_stats_empty(tmp_path: Path):
    """Test db_stats with empty database."""
    db_path = tmp_path / "test.db"
    conn = connect(db_path)

    stats = db_stats(conn)
    assert stats.total_listings == 0
    assert stats.oldest_seen is None
    assert stats.newest_seen is None
    assert stats.db_path == str(db_path)
    assert stats.db_size_bytes > 0  # Schema exists

    conn.close()


def test_roundtrip(tmp_path: Path):
    """Test full roundtrip: upsert -> load -> upsert -> load."""
    db_path = tmp_path / "test.db"
    conn = connect(db_path)

    # Initial load
    rows1 = [
        {"zpid": 123, "price": 250000, "city": "Schenectady"},
        {"zpid": 456, "price": 300000, "city": "Albany"},
    ]
    upsert_listings(conn, rows1, seen_utc="2026-10-01T12:00:00Z")

    listings1 = load_listings(conn)
    assert len(listings1) == 2

    # Update one, add one
    rows2 = [
        {"zpid": 123, "price": 260000, "city": "Schenectady"},  # Updated
        {"zpid": 789, "price": 350000, "city": "Troy"},  # New
    ]
    upsert_listings(conn, rows2, seen_utc="2026-10-02T12:00:00Z")

    listings2 = load_listings(conn)
    assert len(listings2) == 3

    # Verify 123 was updated
    listing_123 = next((listing for listing in listings2 if listing["zpid"] == 123), None)
    assert listing_123 is not None
    assert listing_123["price"] == 260000
    assert listing_123["last_seen_utc"] == "2026-10-02T12:00:00Z"

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

    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
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


def test_kind_transition_does_not_mix_price_history(tmp_path: Path):
    conn = connect(tmp_path / "kind.db")
    upsert_listings(conn, [{"zpid": 1, "price": 300000}], kind="sale", seen_utc="2026-10-01T00:00:00Z")

    stats = upsert_listings(conn, [{"zpid": 1, "price": 2200}], kind="rent", seen_utc="2026-10-02T00:00:00Z")

    listing = conn.execute("SELECT price, kind FROM listings WHERE zpid = 1").fetchone()
    assert listing == (2200, "rent")
    assert stats.price_changes == 0
    assert get_price_history(conn, 1) == [
        {"price": 300000, "seen_utc": "2026-10-01T00:00:00Z", "kind": "sale"},
        {"price": 2200, "seen_utc": "2026-10-02T00:00:00Z", "kind": "rent"},
    ]
    conn.close()


def test_sparse_sighting_preserves_known_fields_and_price(tmp_path: Path):
    conn = connect(tmp_path / "sparse.db")
    upsert_listings(
        conn,
        [{"zpid": 1, "price": 100, "city": "Albany", "beds": 3}],
        seen_utc="2026-10-01T00:00:00Z",
    )

    upsert_listings(conn, [{"zpid": 1, "price": None}], seen_utc="2026-10-02T00:00:00Z")

    assert conn.execute("SELECT price, city, beds FROM listings WHERE zpid = 1").fetchone() == (100, "Albany", 3.0)
    assert len(get_price_history(conn, 1)) == 1
    conn.close()


def test_out_of_order_sightings_are_order_independent(tmp_path: Path):
    sightings = [
        ("2026-09-01T00:00:00Z", 90, "Early"),
        ("2026-10-01T00:00:00Z", 95, "Middle"),
        ("2026-10-10T00:00:00Z", 100, "Latest"),
    ]
    final_states: list[tuple[int, str, str, str]] = []
    histories: list[list[dict[str, Any]]] = []

    for order in itertools.permutations(sightings):
        conn = connect(tmp_path / f"order-{len(final_states)}.db")
        for seen_utc, price, city in order:
            upsert_listings(conn, [{"zpid": 1, "price": price, "city": city}], seen_utc=seen_utc)
        listing = conn.execute(
            "SELECT price, city, first_seen_utc, last_seen_utc FROM listings WHERE zpid = 1"
        ).fetchone()
        final_states.append(listing)
        histories.append(get_price_history(conn, 1))
        conn.close()

    assert all(state == (100, "Latest", "2026-09-01T00:00:00Z", "2026-10-10T00:00:00Z") for state in final_states)
    assert all(history == histories[0] for history in histories)


def test_db_stats_memory_database_has_no_file_size():
    conn = connect(":memory:")

    assert db_stats(conn).db_size_bytes == 0

    conn.close()
