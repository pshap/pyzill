"""The enrichment shortlist and the filter compiler it is built on."""

import sqlite3
from collections.abc import Sequence
from typing import Any, cast

import pytest

from pyzill.db import (
    EnrichCandidate,
    connect,
    record_detail_failure,
    select_enrich_candidates,
    upsert_listings,
    upsert_property_details,
    utc_now,
)
from pyzill.query import QueryValidationError, compile_listing_filter, query_database

SEEN = "2026-10-01T00:00:00Z"
STALE = "2000-01-01T00:00:00Z"


@pytest.fixture
def conn(tmp_path):
    connection = connect(tmp_path / "candidates.db")
    try:
        yield connection
    finally:
        connection.close()


def _seed(conn: sqlite3.Connection, *rows: dict[str, Any]) -> None:
    upsert_listings(conn, list(rows), seen_utc=SEEN)


def _detail(conn: sqlite3.Connection, zpid: int, *, enriched_at: str, **fields: Any) -> None:
    upsert_property_details(conn, {"zpid": zpid, **fields}, kind="sale", enriched_at_utc=enriched_at)


def _candidate_zpids(conn: sqlite3.Connection, **kwargs: Any) -> list[int]:
    return [candidate.zpid for candidate in select_enrich_candidates(conn, **kwargs)]


def test_listing_without_details_is_a_candidate(conn):
    _seed(conn, {"zpid": 1, "detail_url": "https://www.zillow.com/homedetails/1_zpid/"})

    candidates = select_enrich_candidates(conn, max_age_days=7)

    assert candidates == [EnrichCandidate(zpid=1, kind="sale", detail_url="https://www.zillow.com/homedetails/1_zpid/")]


def test_recently_enriched_listing_is_skipped(conn):
    _seed(conn, {"zpid": 1})
    _detail(conn, 1, enriched_at=utc_now())

    assert _candidate_zpids(conn, max_age_days=7) == []


def test_stale_details_is_a_candidate(conn):
    _seed(conn, {"zpid": 1})
    _detail(conn, 1, enriched_at=STALE)

    assert _candidate_zpids(conn, max_age_days=7) == [1]


def test_recent_failure_is_skipped(conn):
    _seed(conn, {"zpid": 1})
    record_detail_failure(conn, 1, "sale", reason="not_found", recorded_at_utc=utc_now())

    assert _candidate_zpids(conn, max_age_days=7) == []


def test_old_failure_is_a_candidate(conn):
    _seed(conn, {"zpid": 1})
    record_detail_failure(conn, 1, "sale", reason="not_found", recorded_at_utc=STALE)

    assert _candidate_zpids(conn, max_age_days=7) == [1]


def test_no_max_age_selects_every_listing(conn):
    _seed(conn, {"zpid": 1}, {"zpid": 2})
    _detail(conn, 1, enriched_at=utc_now())
    record_detail_failure(conn, 2, "sale", reason="not_found", recorded_at_utc=utc_now())

    assert _candidate_zpids(conn, max_age_days=None) == [2, 1]


def test_cutoff_bound_parameter_matches_the_literal_form(conn):
    """The max-age cutoff is the same string whether bound or interpolated."""
    literal = conn.execute("SELECT strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-7 days')").fetchone()[0]
    bound = conn.execute("SELECT strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?)", ("-7 days",)).fetchone()[0]

    assert bound == literal


def test_where_on_a_listing_field(conn):
    _seed(conn, {"zpid": 1, "city": "Albany"}, {"zpid": 2, "city": "Boston"})

    assert _candidate_zpids(conn, max_age_days=None, where=["city==Albany"]) == [1]


def test_where_on_a_detail_field(conn):
    _seed(conn, {"zpid": 1}, {"zpid": 2})
    _detail(conn, 1, enriched_at=utc_now(), hoa_fee=0)
    _detail(conn, 2, enriched_at=utc_now(), hoa_fee=300)

    assert _candidate_zpids(conn, max_age_days=None, where=["hoa_fee=0"]) == [1]


def test_sort_on_a_detail_field(conn):
    _seed(conn, {"zpid": 1}, {"zpid": 2})
    _detail(conn, 1, enriched_at=utc_now(), hoa_fee=0)
    _detail(conn, 2, enriched_at=utc_now(), hoa_fee=300)

    assert _candidate_zpids(conn, max_age_days=None, sort="hoa_fee:desc") == [2, 1]


def test_sort_and_limit_are_respected(conn):
    _seed(conn, {"zpid": 1}, {"zpid": 2}, {"zpid": 3})

    assert _candidate_zpids(conn, max_age_days=None, sort="zpid:desc", limit=2) == [3, 2]


def test_default_order_is_newest_first(conn):
    _seed(conn, {"zpid": 1}, {"zpid": 2}, {"zpid": 3})

    assert _candidate_zpids(conn, max_age_days=None) == [3, 2, 1]


def test_unknown_filter_field_is_rejected(conn):
    _seed(conn, {"zpid": 1})

    with pytest.raises(QueryValidationError, match="Unknown field"):
        select_enrich_candidates(conn, max_age_days=None, where=["not_a_field=1"])


class _RecordingConnection:
    """Delegates to a real connection and records the SQL it is asked to run."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self.statements: list[str] = []

    def execute(self, sql: str, parameters: Sequence[Any] = ()) -> sqlite3.Cursor:
        self.statements.append(sql)
        return self._conn.execute(sql, parameters)


@pytest.mark.parametrize(
    ("where", "sort"),
    [
        (None, None),
        (None, "price:desc"),
        (None, "zpid:asc"),
        (None, "last_seen_utc:asc"),
        (["hoa_fee>=0"], "hoa_fee:desc"),
        (["city==Albany"], "zpid:desc"),
    ],
)
def test_shortlist_and_query_database_produce_the_same_order(conn, where, sort):
    """The shortlist and ``db query`` cannot drift on filter or sort meaning."""
    _seed(
        conn,
        {"zpid": 1, "price": 300000, "city": "Albany"},
        {"zpid": 2, "price": 100000, "city": "Boston"},
        {"zpid": 3, "price": 200000, "city": "Albany"},
    )
    _detail(conn, 1, enriched_at=utc_now(), hoa_fee=300)
    _detail(conn, 2, enriched_at=utc_now(), hoa_fee=0)
    _detail(conn, 3, enriched_at=utc_now(), hoa_fee=150)

    queried, _ = query_database(conn, where=where, sort=sort)
    shortlisted = select_enrich_candidates(conn, where=where, sort=sort, max_age_days=None)

    assert [candidate.zpid for candidate in shortlisted] == [row["zpid"] for row in queried]


@pytest.mark.parametrize("sort", ["price:desc", "zpid:asc", "hoa_fee:desc", "last_seen_utc:asc"])
def test_compiled_filter_emits_the_order_query_database_uses(conn, sort):
    """The compiler's ORDER BY is the one the query engine appends, verbatim."""
    _seed(conn, {"zpid": 1, "price": 300000}, {"zpid": 2, "price": 100000})
    _detail(conn, 1, enriched_at=utc_now(), hoa_fee=300)
    _detail(conn, 2, enriched_at=utc_now(), hoa_fee=0)
    recorder = _RecordingConnection(conn)

    query_database(cast(sqlite3.Connection, recorder), sort=sort)

    assert recorder.statements[-1].split(" ORDER BY ", 1)[1] == compile_listing_filter(sort=sort).order_by
