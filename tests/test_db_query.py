"""Database tests: query."""

from pathlib import Path

import pytest

from pyzill.db import (
    PruneStats,
    begin_sync_run,
    connect,
    db_stats,
    load_listings,
    prune_database,
    upsert_listings,
)


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


def test_prune_database_removes_old_runs_and_keeps_listings(tmp_path: Path):
    """Pruning drops a run's sightings, never the durable listing record."""
    conn = connect(tmp_path / "prune.db")
    old_run = begin_sync_run(conn, kind="sale", query="old", started_utc="2026-01-01T00:00:00Z")
    upsert_listings(
        conn,
        [{"zpid": 1, "price": 100}],
        raw_rows=[{"zpid": "1", "price": "100"}],
        run_id=old_run,
        seen_utc="2026-01-01T00:00:00Z",
    )
    new_run = begin_sync_run(conn, kind="sale", query="new", started_utc="2026-10-01T00:00:00Z")
    upsert_listings(
        conn,
        [{"zpid": 2, "price": 200}],
        raw_rows=[{"zpid": "2", "price": "200"}],
        run_id=new_run,
        seen_utc="2026-10-01T00:00:00Z",
    )

    stats = prune_database(conn, older_than_days=30, now="2026-10-22T00:00:00Z")

    assert stats == PruneStats(runs_deleted=1, sightings_deleted=1, vacuumed=False)
    assert [row[0] for row in conn.execute("SELECT id FROM sync_runs")] == [new_run]
    assert conn.execute("SELECT zpid FROM sightings").fetchall() == [(2,)]
    assert conn.execute("SELECT zpid FROM listings ORDER BY zpid").fetchall() == [(1,), (2,)]
    assert conn.execute("SELECT COUNT(*) FROM price_history").fetchone()[0] == 2
    conn.close()


def test_prune_database_keep_runs_keeps_the_newest(tmp_path: Path):
    conn = connect(tmp_path / "keep.db")
    # Inserted out of order: the selection is by start time, not by id.
    begin_sync_run(conn, kind="sale", query="b", started_utc="2026-02-01T00:00:00Z")
    begin_sync_run(conn, kind="sale", query="a", started_utc="2026-01-01T00:00:00Z")
    newest = begin_sync_run(conn, kind="sale", query="c", started_utc="2026-03-01T00:00:00Z")
    conn.commit()

    stats = prune_database(conn, keep_runs=1)

    assert stats.runs_deleted == 2
    assert conn.execute("SELECT id FROM sync_runs").fetchall() == [(newest,)]
    conn.close()


def test_prune_database_combines_selectors_and_vacuums(tmp_path: Path):
    conn = connect(tmp_path / "vacuum.db")
    begin_sync_run(conn, kind="sale", query="a", started_utc="2020-01-01T00:00:00Z")
    begin_sync_run(conn, kind="sale", query="b", started_utc="2021-01-01T00:00:00Z")
    third = begin_sync_run(conn, kind="sale", query="c", started_utc="2026-10-01T00:00:00Z")
    conn.commit()

    # The first is old enough on its own; keep_runs=1 selects the second too.
    stats = prune_database(conn, older_than_days=365, keep_runs=1, vacuum=True, now="2026-10-22T00:00:00Z")

    assert stats.runs_deleted == 2
    assert stats.vacuumed is True
    assert [row[0] for row in conn.execute("SELECT id FROM sync_runs")] == [third]
    conn.close()


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [({"older_than_days": -1}, "older_than_days"), ({"keep_runs": -1}, "keep_runs")],
)
def test_prune_database_rejects_negative_limits(tmp_path: Path, kwargs, message):
    conn = connect(tmp_path / "invalid.db")

    with pytest.raises(ValueError, match=message):
        prune_database(conn, **kwargs)

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


def test_db_stats_memory_database_has_no_file_size():
    conn = connect(":memory:")

    assert db_stats(conn).db_size_bytes == 0

    conn.close()
