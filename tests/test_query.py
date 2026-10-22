import json
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from pyzill.cli.query import QueryValidationError, query_database, query_rows
from pyzill.db import connect, load_listings, upsert_listings


@st.composite
def _query_rows(draw: st.DrawFn) -> list[dict[str, Any]]:
    values = draw(
        st.lists(
            st.tuples(
                st.one_of(st.none(), st.integers(min_value=50_000, max_value=900_000)),
                st.sampled_from(["02134", "10001", "12305"]),
                st.sampled_from(["Albany", "Boston", "Schenectady"]),
                st.one_of(st.none(), st.integers(min_value=0, max_value=6)),
            ),
            min_size=1,
            max_size=8,
        )
    )
    return [
        {"zpid": index + 1, "price": price, "zipcode": zipcode, "city": city, "beds": beds}
        for index, (price, zipcode, city, beds) in enumerate(values)
    ]


@st.composite
def _where_json(draw: st.DrawFn) -> str:
    field = draw(st.sampled_from(["price", "zipcode", "city", "beds"]))
    value: int | str
    if field in {"price", "beds"}:
        operator = draw(st.sampled_from(["==", "!=", ">=", "<="]))
        value = draw(st.integers(min_value=0, max_value=900_000))
    elif field == "zipcode":
        operator = draw(st.sampled_from(["==", "!=", "~"]))
        value = draw(st.sampled_from(["02134", "10001", "12305"]))
    else:
        operator = draw(st.sampled_from(["==", "!=", "~"]))
        value = draw(st.sampled_from(["Albany", "Boston", "Schenectady"]))
    return json.dumps([{"field": field, "op": operator, "value": value}])


@settings(max_examples=50, deadline=None)
@given(
    rows=_query_rows(),
    where_json=_where_json(),
    sort_field=st.sampled_from(["zpid", "price", "zipcode", "city", "beds"]),
    descending=st.booleans(),
    limit=st.one_of(st.none(), st.integers(min_value=1, max_value=8)),
)
def test_json_file_and_database_query_results_match(
    rows: list[dict[str, Any]],
    where_json: str,
    sort_field: str,
    descending: bool,
    limit: int | None,
) -> None:
    connection = connect(":memory:")
    try:
        upsert_listings(connection, rows, seen_utc="2026-10-01T00:00:00Z")
        sort = f"{sort_field}:{'desc' if descending else 'asc'}"
        columns = "zpid,price,zipcode,city,beds"
        file_result, _ = query_rows(
            rows,
            where_json=where_json,
            sort=sort,
            cols=columns,
            limit=limit,
        )
        database_result, _ = query_database(
            connection,
            where_json=where_json,
            sort=sort,
            cols=columns,
            limit=limit,
        )
        assert file_result == database_result
    finally:
        connection.close()


def test_identifier_sort_uses_numeric_order_in_both_sources() -> None:
    rows = [{"zpid": 2}, {"zpid": 10}]
    connection = connect(":memory:")
    try:
        upsert_listings(connection, rows, seen_utc="2026-10-01T00:00:00Z")
        file_result, _ = query_rows(rows, sort="zpid:asc")
        database_result, _ = query_database(connection, sort="zpid:asc")
        assert [row["zpid"] for row in file_result] == [2, 10]
        assert [row["zpid"] for row in database_result] == [2, 10]
    finally:
        connection.close()


def test_zpid_range_filter_is_numeric() -> None:
    rows = [{"zpid": 99}, {"zpid": 101}]
    connection = connect(":memory:")
    try:
        upsert_listings(connection, rows, seen_utc="2026-10-01T00:00:00Z")
        database_result, _ = query_database(connection, where=["zpid>100"])
        assert [row["zpid"] for row in database_result] == [101]
    finally:
        connection.close()


def test_kind_is_a_queryable_database_field() -> None:
    connection = connect(":memory:")
    try:
        upsert_listings(connection, [{"zpid": 1, "price": 300000}], kind="sale", seen_utc="2026-10-01T00:00:00Z")
        upsert_listings(connection, [{"zpid": 2, "price": 2000}], kind="rent", seen_utc="2026-10-01T00:00:00Z")
        rows, selected = query_database(connection, where=["kind==rent"])
        assert [row["zpid"] for row in rows] == [2]
        assert selected is None
        projected, _ = query_database(connection, cols="zpid,kind")
        assert {row["kind"] for row in projected} == {"sale", "rent"}
    finally:
        connection.close()


def test_load_listings_and_the_query_engine_agree() -> None:
    """The library read helper and the CLI query must answer the same question alike.

    ``load_listings`` used to run its own SQL: a case-sensitive equality
    filter, and an order that did not match the engine's. The same filter now
    means the same thing wherever it is asked.
    """
    rows = [
        {"zpid": 1, "price": 250000, "city": "Albany"},
        {"zpid": 2, "price": 100000, "city": "Schenectady"},
        {"zpid": 3, "price": 900000, "city": "albany"},
    ]
    connection = connect(":memory:")
    try:
        upsert_listings(connection, rows, seen_utc="2026-10-01T00:00:00Z")

        assert (
            load_listings(connection, filters={"city": "albany"})
            == query_database(connection, where=["city==albany"])[0]
        )
        assert (
            load_listings(connection, filters={"price": 250000})
            == query_database(connection, where=["price==250000"])[0]
        )
        # A null value matches a null column in both.
        assert load_listings(connection, filters={"city": None}) == query_database(connection, where=["city==null"])[0]
        # And the row order is the same.
        assert [row["zpid"] for row in load_listings(connection)] == [
            row["zpid"] for row in query_database(connection)[0]
        ]
    finally:
        connection.close()


def test_load_listings_rejects_an_unknown_filter() -> None:
    connection = connect(":memory:")
    try:
        with pytest.raises(ValueError, match="Unknown listing filter"):
            load_listings(connection, filters={"not_a_column": 1})
    finally:
        connection.close()


def test_load_listings_rejects_a_non_numeric_value_for_a_numeric_column() -> None:
    """A numeric column compared with a value that is not a number is an error.

    The engine raises rather than compiling a comparison that could never be
    true, so the library helper raises too instead of matching nothing.
    """
    connection = connect(":memory:")
    try:
        upsert_listings(connection, [{"zpid": 1, "price": 250000}], seen_utc="2026-10-01T00:00:00Z")
        with pytest.raises(ValueError, match="is not a number"):
            load_listings(connection, filters={"price": "not a price"})
    finally:
        connection.close()


def test_numeric_field_rejects_non_numeric_literal() -> None:
    with pytest.raises(QueryValidationError):
        query_rows([{"price": 100}], where=["price<abc"])


def test_numeric_field_accepts_k_and_m_suffixes() -> None:
    rows = [{"price": 400000}, {"price": 600000}]
    result, _ = query_rows(rows, where=["price>=500k"])
    assert [row["price"] for row in result] == [600000]
    result, _ = query_rows([{"price": 1_200_000}], where=["price<=1.5m"])
    assert [row["price"] for row in result] == [1_200_000]


@pytest.mark.parametrize("expression", ["price>null", "price>=null", "price<null", "price<=null", "city~null"])
def test_null_rejected_by_operators_that_cannot_match_it(expression: str) -> None:
    """Comparing with null can never be true, so it must be a usage error."""
    with pytest.raises(QueryValidationError, match="cannot be compared with null"):
        query_rows([{"price": 100, "city": "Denver"}], where=[expression])


def test_null_equality_tests_for_a_null_value() -> None:
    rows: list[dict[str, Any]] = [{"zpid": 1, "price": 100}, {"zpid": 2, "price": None}]
    result, _ = query_rows(rows, where=["price=null"])
    assert [row["zpid"] for row in result] == [2]
    result, _ = query_rows(rows, where=["price!=null"])
    assert [row["zpid"] for row in result] == [1]


def test_null_rejected_in_where_json_conditions() -> None:
    with pytest.raises(QueryValidationError, match="cannot be compared with null"):
        query_rows([{"price": 100}], where_json='[{"field": "price", "op": ">", "value": null}]')


class TestValueTypes:
    """A comparison must keep the value's type and the column's in step."""

    def _sources(self, rows: list[dict[str, Any]]):
        connection = connect(":memory:")
        upsert_listings(connection, rows, kind="sale", seen_utc="2026-10-01T00:00:00Z")
        return connection

    def test_a_quoted_number_matches_the_same_rows_as_a_bare_one(self) -> None:
        """``price=='250000'`` must find what ``price==250000`` finds."""
        rows = [{"zpid": 1, "price": 250000}, {"zpid": 2, "price": 300000}]
        connection = self._sources(rows)
        try:
            for expression in ("price==250000", "price=='250000'"):
                result, _ = query_rows(rows, where=[expression])
                database_result, _ = query_database(connection, where=[expression])
                assert [row["zpid"] for row in result] == [1], expression
                assert [row["zpid"] for row in database_result] == [1], expression
        finally:
            connection.close()

    def test_a_number_matches_a_text_column_that_holds_it(self) -> None:
        """``zipcode==12305`` must find the row whose zipcode is ``'12305'``."""
        rows = [{"zpid": 1, "zipcode": "12305"}, {"zpid": 2, "zipcode": "02134"}]
        connection = self._sources(rows)
        try:
            for expression in ("zipcode==12305", "zipcode=='12305'"):
                result, _ = query_rows(rows, where=[expression])
                database_result, _ = query_database(connection, where=[expression])
                assert [row["zpid"] for row in result] == [1], expression
                assert [row["zpid"] for row in database_result] == [1], expression
        finally:
            connection.close()

    def test_a_text_value_against_a_numeric_field_is_a_usage_error(self) -> None:
        """A value that is not a number must not silently match nothing."""
        with pytest.raises(QueryValidationError, match="is numeric"):
            query_rows([{"price": 100}], where=["price=='not a number'"])

    def test_a_text_value_against_a_numeric_field_is_a_usage_error_in_json(self) -> None:
        with pytest.raises(QueryValidationError, match="is numeric"):
            query_rows([{"price": 100}], where_json='[{"field": "price", "op": "==", "value": "abc"}]')

    def test_a_projected_computed_column_has_the_same_value_in_both_sources(self) -> None:
        """``ppsf`` is computed, not stored, and must read the same in both."""
        rows = [{"zpid": 1, "price": 250000, "area": 1500}]
        connection = self._sources(rows)
        try:
            file_result, _ = query_rows(rows, cols="zpid,ppsf")
            database_result, _ = query_database(connection, cols="zpid,ppsf")
            assert file_result == database_result == [{"zpid": 1, "ppsf": 166.67}]
        finally:
            connection.close()


@pytest.mark.parametrize("value", ["abc", True])
def test_json_numeric_filters_reject_non_numeric_and_boolean_values(value: Any) -> None:
    encoded = json.dumps([{"field": "price", "op": ">", "value": value}])
    with pytest.raises(QueryValidationError, match="is numeric"):
        query_rows([{"zpid": 1, "price": 100}], where_json=encoded)


def test_json_numeric_filter_coerces_quoted_numbers() -> None:
    rows = [{"zpid": 1, "price": 100}, {"zpid": 2, "price": 250}]
    filtered, _ = query_rows(
        rows,
        where_json='[{"field":"price","op":">=","value":"200"}]',
    )
    assert [row["zpid"] for row in filtered] == [2]


@pytest.mark.parametrize("limit", [-1, -10])
def test_query_entry_points_reject_negative_limits(limit: int) -> None:
    rows = [{"zpid": 1, "price": 100}]
    with pytest.raises(QueryValidationError, match="limit must be non-negative"):
        query_rows(rows, limit=limit)

    connection = connect(":memory:")
    try:
        upsert_listings(connection, rows, seen_utc="2026-10-01T00:00:00Z")
        with pytest.raises(QueryValidationError, match="limit must be non-negative"):
            query_database(connection, limit=limit)
    finally:
        connection.close()


def test_query_entry_points_allow_zero_limit() -> None:
    rows = [{"zpid": 1, "price": 100}]
    assert query_rows(rows, limit=0)[0] == []

    connection = connect(":memory:")
    try:
        upsert_listings(connection, rows, seen_utc="2026-10-01T00:00:00Z")
        assert query_database(connection, limit=0)[0] == []
    finally:
        connection.close()
