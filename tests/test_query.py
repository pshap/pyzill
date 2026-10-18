import json
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from pyzill.cli.query import query_database, query_rows
from pyzill.db import connect, upsert_listings


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


def test_identifier_sort_uses_string_order_in_both_sources() -> None:
    rows = [{"zpid": 2}, {"zpid": 10}]
    connection = connect(":memory:")
    try:
        upsert_listings(connection, rows, seen_utc="2026-10-01T00:00:00Z")
        file_result, _ = query_rows(rows, sort="zpid:asc")
        database_result, _ = query_database(connection, sort="zpid:asc")
        assert [row["zpid"] for row in file_result] == [10, 2]
        assert [row["zpid"] for row in database_result] == [10, 2]
    finally:
        connection.close()
