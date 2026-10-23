"""Database tests: changes."""

from pathlib import Path
from typing import Any

from pyzill.db import (
    begin_sync_run,
    connect,
    finish_sync_run,
    get_changes,
    upsert_listings,
)


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


def test_get_changes_survives_a_timestamp_without_a_timezone(tmp_path: Path):
    """A hand-written timestamp must not take the whole report down."""
    conn = connect(tmp_path / "naive.db")
    conn.execute(
        "INSERT INTO listings (zpid, kind, first_seen_utc, last_seen_utc) "
        "VALUES (1, 'sale', '2026-10-01T00:00:00', '2026-10-01T00:00:00')"
    )
    conn.commit()

    changes = get_changes(conn, "2026-10-01")

    assert [(change["event"], change["zpid"]) for change in changes] == [("new", 1)]
    conn.close()


def test_get_changes_reads_only_what_the_events_need(tmp_path: Path):
    """The report must not load every column of every listing.

    A database that holds a hundred thousand listings must not cost a
    hundred thousand rows of memory to answer "what changed since Friday".
    """
    conn = connect(tmp_path / "sized.db")
    upsert_listings(
        conn,
        [{"zpid": index, "price": 100_000 + index} for index in range(2000)],
        kind="sale",
        seen_utc="2026-10-01T00:00:00Z",
    )

    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    try:
        get_changes(conn, "2026-10-01T00:00:00Z")
    finally:
        conn.set_trace_callback(None)

    assert any("first_seen_utc" in statement and "FROM listings" in statement for statement in statements)
    assert not any("SELECT * FROM listings" in statement for statement in statements)
    conn.close()


def test_get_changes_finds_a_price_move_whose_baseline_predates_the_window(tmp_path: Path):
    """The previous price is found even when it was recorded before the window."""
    conn = connect(tmp_path / "baseline.db")
    upsert_listings(conn, [{"zpid": 1, "price": 300000}], kind="sale", seen_utc="2026-10-01T00:00:00Z")
    upsert_listings(conn, [{"zpid": 1, "price": 250000}], kind="sale", seen_utc="2026-10-05T00:00:00Z")

    changes = get_changes(conn, "2026-10-02T00:00:00Z")

    price_events = [change for change in changes if change["event"].startswith("price_")]
    assert [(event["event"], event["old_price"], event["price"]) for event in price_events] == [
        ("price_drop", 300000, 250000)
    ]
    conn.close()


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


def test_get_changes_orders_new_events_by_when_they_happened(tmp_path: Path):
    """A listing first seen later must not be reported before an earlier event."""
    conn = connect(tmp_path / "order.db")

    upsert_listings(conn, [{"zpid": 1, "price": 100}], seen_utc="2026-10-01T00:00:00Z", kind="sale")
    upsert_listings(conn, [{"zpid": 1, "price": 150}], seen_utc="2026-10-05T00:00:00Z", kind="sale")
    upsert_listings(
        conn,
        [{"zpid": 1, "price": 150}, {"zpid": 2, "price": 200}],
        seen_utc="2026-10-10T00:00:00Z",
        kind="sale",
    )

    events = get_changes(conn, "2026-10-02T00:00:00Z")

    assert [event["event"] for event in events] == ["price_increase", "new"]
    assert events[0]["seen_utc"] == "2026-10-05T00:00:00Z"
    # The new listing carries the timestamp it was first seen at.
    new_event = next(event for event in events if event["event"] == "new")
    assert new_event["seen_utc"] == "2026-10-10T00:00:00Z"
    conn.close()


def test_get_changes_events_all_carry_a_timestamp(tmp_path: Path):
    """Every event type reports when it happened."""
    conn = connect(tmp_path / "timestamps.db")
    query_args: dict[str, Any] = {"kind": "sale", "query": "Albany", "bbox": {"north": 43, "south": 42}}

    first = begin_sync_run(conn, **query_args, started_utc="2026-10-01T00:00:00Z")
    upsert_listings(
        conn,
        [{"zpid": 1, "price": 300000, "status": "Active"}, {"zpid": 2, "price": 200000}],
        seen_utc="2026-10-01T00:00:00Z",
        run_id=first,
    )
    finish_sync_run(conn, first, result_count=2, finished_utc="2026-10-01T00:00:00Z")

    second = begin_sync_run(conn, **query_args, started_utc="2026-10-02T00:00:00Z")
    upsert_listings(
        conn,
        [{"zpid": 1, "price": 250000, "status": "Pending"}],
        seen_utc="2026-10-02T00:00:00Z",
        run_id=second,
    )
    finish_sync_run(conn, second, result_count=1, finished_utc="2026-10-02T00:00:00Z")

    events = get_changes(conn, "2026-10-02T00:00:00Z")

    assert events
    assert all(event["seen_utc"] for event in events)
    conn.close()


def test_duplicate_zpids_in_one_batch_count_once(tmp_path: Path):
    """A zpid seen twice in one batch is one listing, counted once."""
    conn = connect(tmp_path / "dupes.db")

    stats = upsert_listings(
        conn,
        [{"zpid": 1, "price": 100}, {"zpid": 1, "price": 120}, {"zpid": 2, "price": 50}],
        seen_utc="2026-10-01T00:00:00Z",
        kind="sale",
    )

    assert stats.added == 2
    assert stats.updated == 0
    assert stats.price_changes == 0
    # The run counted the listings it stored, not the rows it was handed.
    assert conn.execute("SELECT result_count FROM sync_runs").fetchone() == (2,)
    assert conn.execute("SELECT COUNT(*) FROM sightings").fetchone() == (2,)
    conn.close()


def test_duplicate_zpids_count_one_price_change(tmp_path: Path):
    """One listing whose price changed counts one price change, not one per row."""
    conn = connect(tmp_path / "dupes.db")

    upsert_listings(conn, [{"zpid": 1, "price": 100}], seen_utc="2026-10-01T00:00:00Z", kind="sale")

    stats = upsert_listings(
        conn,
        [{"zpid": 1, "price": 150}, {"zpid": 1, "price": 160}],
        seen_utc="2026-10-02T00:00:00Z",
        kind="sale",
    )

    assert stats.updated == 1
    assert stats.price_changes == 1
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


def test_change_report_agrees_with_the_listing_price(tmp_path: Path):
    """The reported price moves must match the prices the listing holds."""
    conn = connect(tmp_path / "agrees.db")

    upsert_listings(conn, [{"zpid": 1, "price": 100}], seen_utc="2026-10-01T00:00:00Z")
    upsert_listings(
        conn,
        [{"zpid": 1, "price": 200}, {"zpid": 1, "price": 100}],
        seen_utc="2026-10-02T00:00:00Z",
    )

    listing_price = conn.execute("SELECT price FROM listings WHERE zpid = 1").fetchone()[0]
    price_events = [event for event in get_changes(conn, "2026-10-01T00:00:00Z") if event["event"].startswith("price_")]

    assert not price_events or price_events[-1]["price"] == listing_price

    conn.close()
