"""Database tests: upsert."""

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
    get_price_history,
    load_listings,
    upsert_listings,
)


def test_upsert_takes_the_write_lock_before_reading_prior_state(tmp_path: Path, monkeypatch):
    """The pre-reads and the writes must be one atomic transaction.

    Another process could write the same zpids between the read that decides
    what changed and the write that records it. The write lock has to be held
    before the first read, not just before the first write.
    """
    from pyzill.db import upsert as upsert_module

    db_path = tmp_path / "write_lock.db"
    conn = connect(db_path)
    other = sqlite3.connect(str(db_path), timeout=0)
    other.execute("PRAGMA busy_timeout = 0")
    original = upsert_module._prefetch
    observed: list[bool] = []

    def prefetch_under_lock(*args, **kwargs):
        observed.append(conn.in_transaction)
        # A concurrent writer must already be excluded here.
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            other.execute(
                "INSERT INTO listings (zpid, kind, first_seen_utc, last_seen_utc) "
                "VALUES (99, 'sale', '2026-10-01T00:00:00Z', '2026-10-01T00:00:00Z')"
            )
        return original(*args, **kwargs)

    monkeypatch.setattr(upsert_module, "_prefetch", prefetch_under_lock)

    upsert_listings(conn, [{"zpid": 1, "price": 100}], seen_utc="2026-10-01T00:00:00Z")

    assert observed == [True]
    other.close()
    conn.close()


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


def test_failed_upsert_rolls_the_batch_back(tmp_path: Path):
    """A failure part-way through a batch must leave nothing behind.

    The listings rows are written before the price history, so a failure in
    the price history leaves the listings written and the rest not. The write
    path owns the transaction it commits, so it has to undo the whole batch.
    """
    conn = connect(tmp_path / "rollback.db")
    conn.execute(
        "CREATE TRIGGER fail_mid_batch BEFORE INSERT ON price_history "
        "WHEN NEW.zpid = 2 BEGIN SELECT RAISE(ABORT, 'boom'); END"
    )
    conn.commit()

    with pytest.raises(sqlite3.IntegrityError):
        upsert_listings(
            conn,
            [
                {"zpid": 1, "price": 100, "city": "Albany"},
                {"zpid": 2, "price": 200, "city": "Schenectady"},
                {"zpid": 3, "price": 300, "city": "Troy"},
            ],
            seen_utc="2026-10-01T00:00:00Z",
        )

    # The connection is left clean: no transaction is open for the caller to
    # commit by accident.
    assert not conn.in_transaction
    assert conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM price_history").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM sightings").fetchone()[0] == 0
    # The run record is undone with the rest of the batch, so no unfinished
    # run is left behind.
    assert conn.execute("SELECT COUNT(*) FROM sync_runs").fetchone()[0] == 0

    # The connection still works after the failure.
    upsert_listings(conn, [{"zpid": 4, "price": 400}], seen_utc="2026-10-02T00:00:00Z")
    assert load_listings(conn)[0]["zpid"] == 4
    conn.close()


def test_failed_upsert_rolls_back_the_run_it_created(tmp_path: Path):
    """A run created by the caller is undone with the batch it belongs to."""
    conn = connect(tmp_path / "rollback_run.db")
    conn.execute(
        "CREATE TRIGGER fail_mid_batch BEFORE INSERT ON price_history "
        "WHEN NEW.zpid = 1 BEGIN SELECT RAISE(ABORT, 'boom'); END"
    )
    conn.commit()
    run_id = begin_sync_run(conn, kind="sale", query="Albany", started_utc="2026-10-01T00:00:00Z")

    with pytest.raises(sqlite3.IntegrityError):
        upsert_listings(conn, [{"zpid": 1, "price": 100}], run_id=run_id, seen_utc="2026-10-01T00:00:00Z")

    assert conn.execute("SELECT COUNT(*) FROM sync_runs").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 0
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


def test_price_history_matches_the_listing_when_a_batch_carries_two_sightings(tmp_path: Path):
    """A batch with two sightings of one zpid must not leave the history disagreeing.

    The second sighting is compared against the price the first one staged,
    so the newest history row is the price the listing actually ends up
    holding -- otherwise the change report would announce a price move
    that the listing contradicts.
    """
    conn = connect(tmp_path / "staged.db")

    upsert_listings(conn, [{"zpid": 1, "price": 100}], seen_utc="2026-10-01T00:00:00Z")
    upsert_listings(
        conn,
        [{"zpid": 1, "price": 200}, {"zpid": 1, "price": 100}],
        seen_utc="2026-10-02T00:00:00Z",
    )

    listing_price = conn.execute("SELECT price FROM listings WHERE zpid = 1").fetchone()[0]
    newest_history = conn.execute(
        "SELECT price FROM price_history WHERE zpid = 1 ORDER BY seen_utc DESC LIMIT 1"
    ).fetchone()[0]

    assert listing_price == 100
    assert newest_history == listing_price

    conn.close()


def test_price_change_counted_when_the_listing_has_a_price_but_no_history(tmp_path: Path):
    """A change is counted against the recorded state, even without a history row.

    The listing holds a price but the history has no row for it (an
    out-of-order batch, or a database whose history predates the row).
    The counter and the history must agree: either both report the
    change or neither does.
    """
    conn = connect(tmp_path / "no-history.db")

    upsert_listings(conn, [{"zpid": 1, "price": 100}], seen_utc="2026-10-01T00:00:00Z")
    conn.execute("DELETE FROM price_history")
    conn.commit()

    stats = upsert_listings(conn, [{"zpid": 1, "price": 200}], seen_utc="2026-10-02T00:00:00Z")

    history = [row["price"] for row in get_price_history(conn, 1)]
    assert stats.price_changes == 1
    assert history[-1] == 200

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
    """Test upsert_listings skips values that are not integer zpids."""
    db_path = tmp_path / "test.db"
    conn = connect(db_path)

    rows: list[dict[str, Any]] = [
        {"zpid": "not-a-number", "price": 250000},
        {"zpid": 123.5, "price": 300000},
        {"zpid": True, "price": 350000},
        {"zpid": 2**63, "price": 400000},
    ]

    stats = upsert_listings(conn, rows, seen_utc="2026-10-01T12:00:00Z")

    assert stats.skipped == 4
    assert stats.added == 0

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


def test_listing_upsert_sql_is_pinned():
    """The generated UPSERT SQL is pinned so any drift is caught immediately."""
    from pyzill.db.upsert import _LISTING_UPSERT_SQL

    expected = (
        "INSERT INTO listings (zpid, kind, price, price_label, beds, baths, area, address, street, city, "
        "state, zipcode, latitude, longitude, home_type, status, status_type, detail_url, image_url, "
        "zestimate, rent_zestimate, days_on_zillow, price_change, price_change_date, price_reduction, "
        "lot_area_value, lot_area_unit, year_built, tax_assessed_value, listing_sub_type, unit, broker_name, "
        "first_seen_utc, last_seen_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
        "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(zpid, kind) DO UPDATE SET\n"
        "        price = CASE WHEN excluded.last_seen_utc >= listings.last_seen_utc THEN "
        "COALESCE(excluded.price, listings.price) ELSE listings.price END,\n"
        "        price_label = CASE WHEN excluded.last_seen_utc >= listings.last_seen_utc THEN "
        "COALESCE(excluded.price_label, listings.price_label) ELSE listings.price_label END,\n"
        "        beds = CASE WHEN excluded.last_seen_utc >= listings.last_seen_utc THEN "
        "COALESCE(excluded.beds, listings.beds) ELSE listings.beds END,\n"
        "        baths = CASE WHEN excluded.last_seen_utc >= listings.last_seen_utc THEN "
        "COALESCE(excluded.baths, listings.baths) ELSE listings.baths END,\n"
        "        area = CASE WHEN excluded.last_seen_utc >= listings.last_seen_utc THEN "
        "COALESCE(excluded.area, listings.area) ELSE listings.area END,\n"
        "        address = CASE WHEN excluded.last_seen_utc >= listings.last_seen_utc THEN "
        "COALESCE(excluded.address, listings.address) ELSE listings.address END,\n"
        "        street = CASE WHEN excluded.last_seen_utc >= listings.last_seen_utc THEN "
        "COALESCE(excluded.street, listings.street) ELSE listings.street END,\n"
        "        city = CASE WHEN excluded.last_seen_utc >= listings.last_seen_utc THEN "
        "COALESCE(excluded.city, listings.city) ELSE listings.city END,\n"
        "        state = CASE WHEN excluded.last_seen_utc >= listings.last_seen_utc THEN "
        "COALESCE(excluded.state, listings.state) ELSE listings.state END,\n"
        "        zipcode = CASE WHEN excluded.last_seen_utc >= listings.last_seen_utc THEN "
        "COALESCE(excluded.zipcode, listings.zipcode) ELSE listings.zipcode END,\n"
        "        latitude = CASE WHEN excluded.last_seen_utc >= listings.last_seen_utc THEN "
        "COALESCE(excluded.latitude, listings.latitude) ELSE listings.latitude END,\n"
        "        longitude = CASE WHEN excluded.last_seen_utc >= listings.last_seen_utc THEN "
        "COALESCE(excluded.longitude, listings.longitude) ELSE listings.longitude END,\n"
        "        home_type = CASE WHEN excluded.last_seen_utc >= listings.last_seen_utc THEN "
        "COALESCE(excluded.home_type, listings.home_type) ELSE listings.home_type END,\n"
        "        status = CASE WHEN excluded.last_seen_utc >= listings.last_seen_utc THEN "
        "COALESCE(excluded.status, listings.status) ELSE listings.status END,\n"
        "        status_type = CASE WHEN excluded.last_seen_utc >= listings.last_seen_utc THEN "
        "COALESCE(excluded.status_type, listings.status_type) ELSE listings.status_type END,\n"
        "        detail_url = CASE WHEN excluded.last_seen_utc >= listings.last_seen_utc THEN "
        "COALESCE(excluded.detail_url, listings.detail_url) ELSE listings.detail_url END,\n"
        "        image_url = CASE WHEN excluded.last_seen_utc >= listings.last_seen_utc THEN "
        "COALESCE(excluded.image_url, listings.image_url) ELSE listings.image_url END,\n"
        "        zestimate = CASE WHEN excluded.last_seen_utc >= listings.last_seen_utc THEN "
        "COALESCE(excluded.zestimate, listings.zestimate) ELSE listings.zestimate END,\n"
        "        rent_zestimate = CASE WHEN excluded.last_seen_utc >= listings.last_seen_utc THEN "
        "COALESCE(excluded.rent_zestimate, listings.rent_zestimate) ELSE listings.rent_zestimate END,\n"
        "        days_on_zillow = CASE WHEN excluded.last_seen_utc >= listings.last_seen_utc THEN "
        "COALESCE(excluded.days_on_zillow, listings.days_on_zillow) ELSE listings.days_on_zillow END,\n"
        "        price_change = CASE WHEN excluded.last_seen_utc >= listings.last_seen_utc THEN "
        "COALESCE(excluded.price_change, listings.price_change) ELSE listings.price_change END,\n"
        "        price_change_date = CASE WHEN excluded.last_seen_utc >= listings.last_seen_utc THEN "
        "COALESCE(excluded.price_change_date, listings.price_change_date) ELSE listings.price_change_date END,\n"
        "        price_reduction = CASE WHEN excluded.last_seen_utc >= listings.last_seen_utc THEN "
        "COALESCE(excluded.price_reduction, listings.price_reduction) ELSE listings.price_reduction END,\n"
        "        lot_area_value = CASE WHEN excluded.last_seen_utc >= listings.last_seen_utc THEN "
        "COALESCE(excluded.lot_area_value, listings.lot_area_value) ELSE listings.lot_area_value END,\n"
        "        lot_area_unit = CASE WHEN excluded.last_seen_utc >= listings.last_seen_utc THEN "
        "COALESCE(excluded.lot_area_unit, listings.lot_area_unit) ELSE listings.lot_area_unit END,\n"
        "        year_built = CASE WHEN excluded.last_seen_utc >= listings.last_seen_utc THEN "
        "COALESCE(excluded.year_built, listings.year_built) ELSE listings.year_built END,\n"
        "        tax_assessed_value = CASE WHEN excluded.last_seen_utc >= listings.last_seen_utc THEN "
        "COALESCE(excluded.tax_assessed_value, listings.tax_assessed_value) ELSE listings.tax_assessed_value END,\n"
        "        listing_sub_type = CASE WHEN excluded.last_seen_utc >= listings.last_seen_utc THEN "
        "COALESCE(excluded.listing_sub_type, listings.listing_sub_type) ELSE listings.listing_sub_type END,\n"
        "        unit = CASE WHEN excluded.last_seen_utc >= listings.last_seen_utc THEN "
        "COALESCE(excluded.unit, listings.unit) ELSE listings.unit END,\n"
        "        broker_name = CASE WHEN excluded.last_seen_utc >= listings.last_seen_utc THEN "
        "COALESCE(excluded.broker_name, listings.broker_name) ELSE listings.broker_name END,\n"
        "        first_seen_utc = MIN(listings.first_seen_utc, excluded.first_seen_utc),\n"
        "        last_seen_utc = MAX(listings.last_seen_utc, excluded.last_seen_utc)"
    )
    assert expected == _LISTING_UPSERT_SQL
