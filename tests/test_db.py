"""Tests for pyzill.db module."""

import datetime as dt
import itertools
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from pyzill.db import (
    begin_sync_run,
    connect,
    connect_readonly,
    db_stats,
    default_db_path,
    finish_sync_run,
    get_changes,
    get_price_history,
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
    assert cursor.fetchone()[0] == 3

    conn.close()


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
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
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


def test_same_zpid_can_have_sale_and_rent_rows(tmp_path: Path):
    conn = connect(tmp_path / "kinds.db")
    upsert_listings(conn, [{"zpid": 1, "price": 300000}], kind="sale", seen_utc="2026-10-01T00:00:00Z")
    upsert_listings(conn, [{"zpid": 1, "price": 2200}], kind="rent", seen_utc="2026-10-02T00:00:00Z")

    rows = conn.execute("SELECT zpid, kind, price FROM listings ORDER BY kind").fetchall()

    assert rows == [(1, "rent", 2200), (1, "sale", 300000)]
    assert conn.execute("SELECT COUNT(*) FROM price_history WHERE zpid = 1").fetchone()[0] == 2
    conn.close()


def test_upsert_records_raw_payload_and_sync_run(tmp_path: Path):
    conn = connect(tmp_path / "raw.db")
    raw = {"zpid": "123", "price": "$250,000", "statusText": "Active"}

    upsert_listings(
        conn,
        [{"zpid": 123, "price": 250000, "status": "Active"}],
        raw_rows=[raw],
        seen_utc="2026-10-01T00:00:00Z",
    )

    run = conn.execute("SELECT kind, result_count, truncated FROM sync_runs").fetchone()
    raw_json = conn.execute("SELECT raw_json FROM sightings WHERE zpid = 123").fetchone()[0]

    assert run == ("sale", 1, 0)
    assert json.loads(raw_json) == raw
    conn.close()


def test_upsert_records_non_finite_raw_values_as_null(tmp_path: Path):
    """json.loads accepts NaN; storing it must not abort the write transaction."""
    conn = connect(tmp_path / "nan.db")
    raw = {"zpid": "123", "price": float("nan"), "nested": [float("inf"), {"x": float("-inf")}]}

    upsert_listings(
        conn,
        [{"zpid": 123, "price": None, "status": None}],
        raw_rows=[raw],
        seen_utc="2026-10-01T00:00:00Z",
    )

    raw_json = conn.execute("SELECT raw_json FROM sightings WHERE zpid = 123").fetchone()[0]
    stored = json.loads(raw_json)
    assert stored["price"] is None
    assert stored["nested"] == [None, {"x": None}]
    conn.close()


def test_status_history_records_only_transitions(tmp_path: Path):
    conn = connect(tmp_path / "status.db")
    for timestamp, status in [
        ("2026-10-01T00:00:00Z", "Active"),
        ("2026-10-02T00:00:00Z", "Active"),
        ("2026-10-03T00:00:00Z", "Pending"),
        ("2026-10-04T00:00:00Z", "Active"),
    ]:
        upsert_listings(conn, [{"zpid": 1, "status": status}], seen_utc=timestamp)

    history = conn.execute(
        "SELECT status, first_seen_utc FROM status_history WHERE zpid = 1 ORDER BY first_seen_utc"
    ).fetchall()

    assert history == [
        ("Active", "2026-10-01T00:00:00Z"),
        ("Pending", "2026-10-03T00:00:00Z"),
        ("Active", "2026-10-04T00:00:00Z"),
    ]
    conn.close()


def test_status_history_tracks_later_status_within_one_batch(tmp_path: Path):
    """A batch with repeated zpids must compare each row to the status staged just before it."""
    conn = connect(tmp_path / "batch_status.db")

    # Pre-batch status is Active; the batch first confirms Active then moves to
    # Pending, and only the transition should be recorded (using a batched
    # pre-read rather than a per-row SELECT).
    upsert_listings(conn, [{"zpid": 1, "status": "Active"}], seen_utc="2026-10-01T00:00:00Z")
    upsert_listings(
        conn,
        [{"zpid": 1, "status": "Active"}, {"zpid": 1, "status": "Pending"}],
        seen_utc="2026-10-02T00:00:00Z",
    )

    history = conn.execute(
        "SELECT status, first_seen_utc FROM status_history WHERE zpid = 1 ORDER BY first_seen_utc"
    ).fetchall()
    assert history == [("Active", "2026-10-01T00:00:00Z"), ("Pending", "2026-10-02T00:00:00Z")]
    conn.close()


def test_upsert_does_not_change_kind_for_existing_pair(tmp_path: Path):
    """The (zpid, kind) primary key means a sighting never rewrites another kind's row."""
    conn = connect(tmp_path / "kinds.db")
    upsert_listings(conn, [{"zpid": 1, "price": 300000}], kind="sale", seen_utc="2026-10-01T00:00:00Z")
    upsert_listings(conn, [{"zpid": 1, "price": 2200}], kind="rent", seen_utc="2026-10-01T00:00:00Z")

    rows = conn.execute("SELECT kind, price FROM listings WHERE zpid = 1 ORDER BY kind").fetchall()
    assert rows == [("rent", 2200), ("sale", 300000)]
    conn.close()


def test_load_listings_supports_filters_and_limit(tmp_path: Path):
    conn = connect(tmp_path / "filtered.db")
    upsert_listings(
        conn,
        [
            {"zpid": 1, "city": "Albany", "zipcode": "01234"},
            {"zpid": 2, "city": "Boston", "zipcode": "02134"},
            {"zpid": 3, "city": "Albany", "zipcode": "01235"},
        ],
        seen_utc="2026-10-01T00:00:00Z",
    )

    rows = load_listings(conn, filters={"city": "Albany"}, limit=1)

    assert len(rows) == 1
    assert rows[0]["city"] == "Albany"
    conn.close()


def test_get_changes_reports_price_status_and_comparable_delisting(tmp_path: Path):
    conn = connect(tmp_path / "changes.db")
    query_args: dict[str, Any] = {"kind": "sale", "query": "Albany", "bbox": {"north": 43, "south": 42}}

    first_run = begin_sync_run(conn, **query_args, started_utc="2026-10-01T00:00:00Z")
    upsert_listings(
        conn,
        [{"zpid": 1, "price": 300000, "status": "Active"}, {"zpid": 2, "price": 200000}],
        seen_utc="2026-10-01T00:00:00Z",
        run_id=first_run,
    )
    finish_sync_run(conn, first_run, result_count=2, finished_utc="2026-10-01T00:00:00Z")
    second_run = begin_sync_run(conn, **query_args, started_utc="2026-10-02T00:00:00Z")
    upsert_listings(
        conn,
        [{"zpid": 1, "price": 250000, "status": "Pending"}],
        seen_utc="2026-10-02T00:00:00Z",
        run_id=second_run,
    )
    finish_sync_run(conn, second_run, result_count=1, finished_utc="2026-10-02T00:00:00Z")

    changes = get_changes(conn, "2026-10-02T00:00:00Z")
    event_types = {(change["event"], change["zpid"]) for change in changes}

    assert ("price_drop", 1) in event_types
    assert ("status_change", 1) in event_types
    assert ("delisted", 2) in event_types
    conn.close()


def test_upsert_listings_persists_truncated_flag(tmp_path: Path):
    db_path = tmp_path / "truncated.db"
    conn = connect(db_path)
    run_id = begin_sync_run(conn, kind="sale", query="Albany", started_utc="2026-10-01T00:00:00Z")
    upsert_listings(
        conn,
        [{"zpid": 1, "price": 300000}],
        seen_utc="2026-10-01T00:00:00Z",
        run_id=run_id,
        truncated=True,
    )
    conn.close()

    reopened = connect(db_path)
    row = reopened.execute("SELECT result_count, finished_utc, truncated FROM sync_runs").fetchone()
    reopened.close()
    assert row == (1, "2026-10-01T00:00:00Z", 1)


def test_get_changes_ignores_runs_with_different_filters(tmp_path: Path):
    conn = connect(tmp_path / "filters.db")
    bbox = {"north": 43, "south": 42}
    filtered = begin_sync_run(
        conn,
        kind="sale",
        query="Albany",
        bbox=bbox,
        filters={"max_price": 500000},
        started_utc="2026-10-01T00:00:00Z",
    )
    upsert_listings(
        conn,
        [{"zpid": 1, "price": 300000}, {"zpid": 2, "price": 450000}],
        seen_utc="2026-10-01T00:00:00Z",
        run_id=filtered,
    )
    finish_sync_run(conn, filtered, result_count=2, finished_utc="2026-10-01T00:00:00Z")

    unfiltered = begin_sync_run(conn, kind="sale", query="Albany", bbox=bbox, started_utc="2026-10-02T00:00:00Z")
    upsert_listings(
        conn,
        [{"zpid": 1, "price": 300000}],
        seen_utc="2026-10-02T00:00:00Z",
        run_id=unfiltered,
    )
    finish_sync_run(conn, unfiltered, result_count=1, finished_utc="2026-10-02T00:00:00Z")

    events = {(change["event"], change["zpid"]) for change in get_changes(conn, "2026-10-02T00:00:00Z")}

    assert ("delisted", 2) not in events
    assert ("relisted", 1) not in events
    conn.close()


def test_get_changes_skips_truncated_runs(tmp_path: Path):
    conn = connect(tmp_path / "skip_truncated.db")
    query_args: dict[str, Any] = {"kind": "sale", "query": "Albany", "bbox": {"north": 43, "south": 42}}
    first = begin_sync_run(conn, **query_args, started_utc="2026-10-01T00:00:00Z")
    upsert_listings(
        conn,
        [{"zpid": 1, "price": 300000}, {"zpid": 2, "price": 200000}],
        seen_utc="2026-10-01T00:00:00Z",
        run_id=first,
    )
    finish_sync_run(conn, first, result_count=2, finished_utc="2026-10-01T00:00:00Z")

    truncated = begin_sync_run(conn, **query_args, started_utc="2026-10-02T00:00:00Z")
    upsert_listings(
        conn,
        [{"zpid": 1, "price": 300000}],
        seen_utc="2026-10-02T00:00:00Z",
        run_id=truncated,
        truncated=True,
    )

    events = {(change["event"], change["zpid"]) for change in get_changes(conn, "2026-10-02T00:00:00Z")}

    assert ("delisted", 2) not in events
    conn.close()


def test_get_changes_ignores_import_runs(tmp_path: Path):
    """Two imports of different files must not look like a delisting.

    An import ingests a file snapshot: a listing missing from the second file
    was never observed as gone, so change detection must not compare the two
    runs against each other.
    """
    conn = connect(tmp_path / "imports.db")

    upsert_listings(
        conn,
        [{"zpid": 1, "price": 100}, {"zpid": 2, "price": 200}],
        seen_utc="2026-10-01T00:00:00Z",
        kind="sale",
    )
    upsert_listings(conn, [{"zpid": 1, "price": 100}], seen_utc="2026-10-02T00:00:00Z", kind="sale")

    events = get_changes(conn, "2026-10-01T12:00:00Z")

    assert [event for event in events if event["event"] == "delisted"] == []
    assert [event for event in events if event["event"] == "relisted"] == []
    conn.close()


def test_get_changes_compares_search_runs_with_the_same_query(tmp_path: Path):
    """Search runs of the same query still detect delistings."""
    conn = connect(tmp_path / "searches.db")
    query_args: dict[str, Any] = {"kind": "sale", "query": "Albany", "bbox": {"north": 43, "south": 42}}

    first = begin_sync_run(conn, **query_args, started_utc="2026-10-01T00:00:00Z")
    upsert_listings(
        conn,
        [{"zpid": 1, "price": 300000}, {"zpid": 2, "price": 200000}],
        seen_utc="2026-10-01T00:00:00Z",
        run_id=first,
    )
    finish_sync_run(conn, first, result_count=2, finished_utc="2026-10-01T00:00:00Z")

    second = begin_sync_run(conn, **query_args, started_utc="2026-10-02T00:00:00Z")
    upsert_listings(conn, [{"zpid": 1, "price": 300000}], seen_utc="2026-10-02T00:00:00Z", run_id=second)
    finish_sync_run(conn, second, result_count=1, finished_utc="2026-10-02T00:00:00Z")

    events = {(event["event"], event["zpid"]) for event in get_changes(conn, "2026-10-02T00:00:00Z")}

    assert ("delisted", 2) in events
    conn.close()


def test_comparable_run_had_results(tmp_path: Path):
    from pyzill.db import comparable_run_had_results

    conn = connect(tmp_path / "comparable.db")
    run_id = begin_sync_run(
        conn,
        kind="sale",
        query="Albany",
        bbox={"north": 43, "south": 42},
        filters={"max_price": 500000},
        started_utc="2026-10-01T00:00:00Z",
    )
    assert not comparable_run_had_results(
        conn, kind="sale", query="Albany", bbox={"north": 43, "south": 42}, filters={"max_price": 500000}
    )

    upsert_listings(conn, [{"zpid": 1, "price": 300000}], seen_utc="2026-10-01T00:00:00Z", run_id=run_id)

    assert comparable_run_had_results(
        conn, kind="sale", query="Albany", bbox={"north": 43, "south": 42}, filters={"max_price": 500000}
    )
    assert not comparable_run_had_results(conn, kind="sale", query="Albany", bbox={"north": 43, "south": 42})
    conn.close()


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

    assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
    assert conn.execute("SELECT zpid, kind, price, city, zipcode FROM listings").fetchall() == [
        (7, "sale", 450000, "Albany", "01234")
    ]
    assert conn.execute("SELECT zpid, kind, price FROM price_history").fetchall() == [(7, "sale", 450000)]
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"sync_runs", "sightings", "status_history"} <= tables
    conn.close()


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
    assert cursor.fetchone()[0] == 1  # Unchanged prices do not add history

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

    assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
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

    monkeypatch.setattr("pyzill.db.schema._MIGRATIONS", {2: boom})

    with pytest.raises(_FakeBaseException):
        connect(db_path)

    # A subsequent connect must succeed: no leaked write lock.
    reopened = connect(db_path)
    assert reopened.execute("PRAGMA user_version").fetchone()[0] == 3
    reopened.close()


_ZPID = 424242


def _complete_sighting(ts: str, price: int) -> dict[str, Any]:
    """A sighting with every data column filled and a non-None price.

    Completeness matters: sparse sightings make final state order-dependent
    by design (the newest batch COALESCEs over older ones, and an older
    backfill cannot refill a field the newest sighting omitted). The
    permutation property only holds for fully-specified sightings, which is
    what real sync batches provide after normalization.
    """
    return {
        "zpid": _ZPID,
        "price": price,
        "price_label": f"${price}",
        "beds": 3,
        "baths": 2.0,
        "area": 1500,
        "address": f"{price} Test St",
        "street": f"{price} Test St",
        "city": "Testville",
        "state": "TS",
        "zipcode": "00000",
        "latitude": 40.0,
        "longitude": -75.0,
        "home_type": "SINGLE_FAMILY",
        "status": "FOR_SALE",
        "status_type": "FOR_SALE",
        "detail_url": f"https://www.zillow.com/homedetails/{_ZPID}/",
        "image_url": "https://photos.example/x.jpg",
        "zestimate": price,
        "rent_zestimate": price // 2,
    }


@st.composite
def _sightings(draw: st.DrawFn) -> list[tuple[str, int]]:
    """A random set of (fixed-width UTC timestamp, price) sightings, timestamps distinct."""
    count = draw(st.integers(min_value=1, max_value=5))
    prices = draw(st.lists(st.integers(min_value=1, max_value=10_000_000), min_size=count, max_size=count))
    offsets = draw(st.lists(st.integers(min_value=0, max_value=86_400), min_size=count, max_size=count, unique=True))
    base = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    return [
        ((base + dt.timedelta(seconds=offset)).strftime("%Y-%m-%dT%H:%M:%SZ"), price)
        for offset, price in zip(offsets, prices, strict=True)
    ]


def _apply_batches(batches: list[tuple[str, int]]) -> tuple[Any, list[dict[str, Any]]]:
    """Upsert each (ts, price) sighting as its own batch; return final listing + history."""
    conn = connect(":memory:")
    try:
        for ts, price in batches:
            upsert_listings(conn, [_complete_sighting(ts, price)], seen_utc=ts)
        listing = conn.execute(
            "SELECT price, city, first_seen_utc, last_seen_utc FROM listings WHERE zpid = ?",
            (_ZPID,),
        ).fetchone()
        history = get_price_history(conn, _ZPID)
    finally:
        conn.close()
    assert listing is not None
    return listing, history


@settings(max_examples=50, deadline=None)
@given(sightings=_sightings())
def test_upsert_permutation_invariance(sightings: list[tuple[str, int]]) -> None:
    """Applying complete sightings in any batch order converges to the same state.

    The listings row must end at the newest sighting (price/city from the max
    timestamp, first_seen at the min, and last_seen at the max) for every
    permutation of batch order. Chronological history stores the first price
    and subsequent price changes; full history reconstruction for out-of-order
    sightings requires the sightings table. Stats are not asserted:
    price_changes is a documented per-batch heuristic and is legitimately
    order-sensitive.
    """
    baseline = _apply_batches(sorted(sightings))
    newest_ts, newest_price = max(sightings)
    oldest_ts = min(ts for ts, _ in sightings)

    listing, history = baseline
    assert listing[0] == newest_price
    assert listing[2] == oldest_ts
    assert listing[3] == newest_ts
    changes = [
        (ts, price)
        for index, (ts, price) in enumerate(sorted(sightings))
        if index == 0 or price != sorted(sightings)[index - 1][1]
    ]
    assert [(row["seen_utc"], row["price"]) for row in history] == changes

    for order in itertools.permutations(sightings):
        assert _apply_batches(list(order))[0] == listing
