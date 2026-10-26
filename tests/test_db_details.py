"""Database tests: property detail persistence."""

import json
import sqlite3

from pyzill._kinds import Kind
from pyzill.db import (
    clear_detail_failure,
    connect,
    detail_normalization_failed,
    get_detail_failure,
    get_property_details,
    record_detail_failure,
    upsert_listings,
    upsert_property_details,
)


def _seed_listing(conn: sqlite3.Connection, zpid: int = 1, kind: Kind = "sale") -> None:
    upsert_listings(
        conn,
        [{"zpid": zpid, "price": 300000, "city": "Albany"}],
        kind=kind,
        seen_utc="2026-10-01T00:00:00Z",
    )


def _raw_detail() -> dict:
    return {
        "zpid": 1,
        "monthlyHoaFee": 150,
        "description": "A bright home with a garage",
        "photoCount": 4,
        "yearBuilt": 2001,
        "taxHistory": [{"year": 2020, "tax": 3000}],
        "priceHistory": [{"date": "2020-01-01", "price": 200000}],
        "schools": [{"name": "Elementary"}],
    }


def test_upsert_and_get_property_details_round_trip(tmp_path):
    conn = connect(tmp_path / "details.db")
    try:
        _seed_listing(conn)
        upsert_property_details(conn, _raw_detail(), kind="sale")
        row = get_property_details(conn, 1)
        assert row is not None
        assert row["zpid"] == 1
        assert row["kind"] == "sale"
        assert row["hoa_fee"] == 150
        assert row["hoa_fee_unit"] == "monthly"
        assert row["description"] == "A bright home with a garage"
        assert row["photos_count"] == 4
        assert row["year_built"] == 2001
        assert row["hoa_monthly"] == 150
        assert row["tax_assessed_latest"] is None
        assert row["tax_year_latest"] == 2020
        assert row["tax_paid_latest"] == 3000
        assert row["normalization_failed"] == 0
        assert json.loads(row["tax_history"]) == [{"year": 2020, "tax": 3000}]
        assert json.loads(row["schools"]) == [{"name": "Elementary"}]
        assert row["raw_json"] is not None
        assert row["enriched_at_utc"] is not None
    finally:
        conn.close()


def test_get_property_details_missing_returns_none(tmp_path):
    conn = connect(tmp_path / "missing.db")
    try:
        assert get_property_details(conn, 999) is None
        assert get_property_details(conn, 999, kind="rent") is None
    finally:
        conn.close()


def test_upsert_unknown_zpid_is_skipped(tmp_path):
    conn = connect(tmp_path / "skip.db")
    try:
        upsert_property_details(conn, {"description": "no zpid"}, kind="sale")
        assert get_property_details(conn, 1) is None
        assert conn.execute("SELECT COUNT(*) FROM property_details").fetchone()[0] == 0
    finally:
        conn.close()


def test_upsert_updates_existing_detail(tmp_path):
    conn = connect(tmp_path / "update.db")
    try:
        _seed_listing(conn)
        upsert_property_details(conn, {"zpid": 1, "monthlyHoaFee": 150}, kind="sale")
        upsert_property_details(conn, {"zpid": 1, "monthlyHoaFee": 250}, kind="sale")
        row = get_property_details(conn, 1)
        assert row is not None
        assert row["hoa_fee"] == 250
    finally:
        conn.close()


def test_detail_fields_are_queryable(tmp_path):
    conn = connect(tmp_path / "query.db")
    try:
        from pyzill.db import upsert_listings

        upsert_listings(
            conn, [{"zpid": 1, "price": 300000, "city": "Albany"}], kind="sale", seen_utc="2026-10-01T00:00:00Z"
        )
        upsert_property_details(
            conn,
            {"zpid": 1, "monthlyHoaFee": 0, "description": "Detached garage", "yearBuilt": 2005},
            kind="sale",
        )

        from pyzill.query import query_database

        no_hoa, _ = query_database(conn, where=["hoa_fee=0"])
        assert [row["zpid"] for row in no_hoa] == [1]

        garage, _ = query_database(conn, where=["description ~ garage"])
        assert [row["zpid"] for row in garage] == [1]

        not_garage, _ = query_database(conn, where=["description ~ basement"])
        assert not_garage == []

        year, _ = query_database(conn, where=["year_built>2000"])
        assert [row["zpid"] for row in year] == [1]
    finally:
        conn.close()


def test_detail_fields_can_be_projected_and_sorted(tmp_path):
    """A --cols projection and a --sort on detail fields read the joined table."""
    conn = connect(tmp_path / "project.db")
    try:
        from pyzill.db import upsert_listings
        from pyzill.query import query_database

        upsert_listings(
            conn, [{"zpid": 1, "price": 300000, "city": "Albany"}], kind="sale", seen_utc="2026-10-01T00:00:00Z"
        )
        upsert_property_details(
            conn,
            {"zpid": 1, "monthlyHoaFee": 0, "description": "Detached garage", "yearBuilt": 2005},
            kind="sale",
        )

        rows, selected = query_database(conn, cols="zpid,hoa_fee,description")
        assert selected == ["zpid", "hoa_fee", "description"]
        assert rows == [{"zpid": 1, "hoa_fee": 0, "description": "Detached garage"}]

        sorted_rows, _ = query_database(conn, sort="year_built:desc")
        assert [row["zpid"] for row in sorted_rows] == [1]

        filtered_sorted, _ = query_database(conn, where=["hoa_fee=0"], sort="year_built:desc")
        assert [row["zpid"] for row in filtered_sorted] == [1]
    finally:
        conn.close()


def test_listing_field_and_detail_field_where_together(tmp_path):
    """A WHERE that mixes listing and detail fields joins and qualifies both."""
    conn = connect(tmp_path / "mixed.db")
    try:
        from pyzill.db import upsert_listings
        from pyzill.query import query_database

        upsert_listings(
            conn,
            [
                {"zpid": 1, "price": 300000, "city": "Albany"},
                {"zpid": 2, "price": 100000, "city": "Schenectady"},
            ],
            kind="sale",
            seen_utc="2026-10-01T00:00:00Z",
        )
        upsert_property_details(conn, {"zpid": 1, "monthlyHoaFee": 0, "description": "Detached garage"}, kind="sale")
        upsert_property_details(conn, {"zpid": 2, "monthlyHoaFee": 300, "description": "Condo, no garage"}, kind="sale")

        # ``kind`` lives in both tables, so it must be qualified when the join
        # is active; a listing column and a detail column together still work.
        rows, _ = query_database(conn, where=["city==Albany", "hoa_fee=0"])
        assert [row["zpid"] for row in rows] == [1]

        rows, _ = query_database(conn, where=["kind==sale", "description ~ garage"])
        # ``~`` is a substring match, so "Condo, no garage" matches too.
        assert [row["zpid"] for row in rows] == [2, 1]

        rows, _ = query_database(conn, where=["kind==sale", "description ~ garage", "city==Schenectady"])
        assert [row["zpid"] for row in rows] == [2]
    finally:
        conn.close()


def test_taxes_under_threshold_uses_listing_value(tmp_path):
    """tax_assessed_value prefers the listings column when details lack it."""
    conn = connect(tmp_path / "tax.db")
    try:
        from pyzill.db import upsert_listings
        from pyzill.query import query_database

        upsert_listings(conn, [{"zpid": 1, "tax_assessed_value": 5000}], kind="sale", seen_utc="2026-10-01T00:00:00Z")
        upsert_property_details(conn, {"zpid": 1, "monthlyHoaFee": 0}, kind="sale")

        cheap, _ = query_database(conn, where=["tax_assessed_value<6000"])
        assert [row["zpid"] for row in cheap] == [1]

        expensive, _ = query_database(conn, where=["tax_assessed_value>=6000"])
        assert expensive == []
    finally:
        conn.close()


def test_clear_detail_failure_forgets_the_recorded_failure(tmp_path):
    conn = connect(tmp_path / "failure.db")
    try:
        _seed_listing(conn)
        record_detail_failure(conn, 1, "sale", reason="not_found", status_code=404)
        assert get_detail_failure(conn, 1) is not None

        clear_detail_failure(conn, 1, "sale")

        assert get_detail_failure(conn, 1) is None
    finally:
        conn.close()


def test_detail_normalization_failed_reads_the_stored_flag(tmp_path):
    conn = connect(tmp_path / "normalization.db")
    try:
        _seed_listing(conn)
        assert detail_normalization_failed(conn, 1) is False

        upsert_property_details(conn, _raw_detail(), kind="sale")
        assert detail_normalization_failed(conn, 1) is False

        conn.execute("UPDATE property_details SET normalization_failed = 1 WHERE zpid = 1 AND kind = 'sale'")
        assert detail_normalization_failed(conn, 1, "sale") is True
    finally:
        conn.close()
