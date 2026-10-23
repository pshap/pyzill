"""CLI tests: query."""

import json

import pytest

from pyzill.cli import app
from pyzill.cli.query import coerce_value
from pyzill.db import connect, upsert_listings
from tests.cli_helpers import run_query, runner, write_json

pytestmark = pytest.mark.usefixtures("_stub_geocode")


class TestQueryCommand:
    """Offline re-slicing of saved search results."""

    @pytest.fixture
    def saved(self, tmp_path):
        rows = [
            {
                "zpid": 1,
                "price": 150000,
                "beds": 2,
                "baths": 1,
                "area": 1000,
                "city": "Schenectady",
                "zipcode": "12304",
            },
            {
                "zpid": 2,
                "price": 250000,
                "beds": 3,
                "baths": 2,
                "area": 1500,
                "city": "Schenectady",
                "zipcode": "12308",
            },
            {"zpid": 3, "price": 350000, "beds": 4, "baths": 2, "area": 2000, "city": "Scotia", "zipcode": "12302"},
            {
                "zpid": 4,
                "price": 200000,
                "beds": 3,
                "baths": 1,
                "area": None,
                "city": "Schenectady",
                "zipcode": "12303",
            },
        ]
        return write_json(tmp_path / "saved.json", rows)

    def test_where_filter(self, saved):
        assert {r["zpid"] for r in run_query(saved, "--where", "price<=250000")} == {1, 2, 4}

    def test_multiple_where_anded(self, saved):
        rows = run_query(saved, "--where", "price<=250000", "--where", "beds>=3")
        assert {r["zpid"] for r in rows} == {2, 4}

    def test_where_string_equality_with_numeric_literal(self, saved):
        assert [r["zpid"] for r in run_query(saved, "--where", "zipcode==12304")] == [1]

    @pytest.mark.parametrize("expression", ["zipcode==02134", 'zipcode=="02134"'])
    def test_where_preserves_leading_zero_zipcodes(self, tmp_path, expression):
        path = write_json(tmp_path / "zipcodes.json", [{"zpid": 1, "zipcode": "02134"}])

        assert [row["zpid"] for row in run_query(path, "--where", expression)] == [1]

    def test_where_json_filter(self, saved):
        rows = run_query(saved, "--where-json", '[{"field":"price","op":"<=","value":250000}]')
        assert {row["zpid"] for row in rows} == {1, 2, 4}

    def test_where_json_rejects_unknown_operators(self, saved):
        result = runner.invoke(
            app,
            ["query", str(saved), "--where-json", '[{"field":"price","op":"like","value":1}]'],
        )

        assert result.exit_code == 2
        assert "Unknown operator" in result.stderr

    def test_db_query_preserves_leading_zero_zipcodes_during_write(self, tmp_path):
        db_path = tmp_path / "zipcodes.db"
        writer = connect(db_path)
        upsert_listings(writer, [{"zpid": 1, "zipcode": "02134"}], seen_utc="2026-10-01T00:00:00Z")
        writer.execute("BEGIN IMMEDIATE")
        try:
            result = runner.invoke(
                app,
                ["db", "query", "--where", "zipcode==02134", "--db", str(db_path)],
            )
            assert result.exit_code == 0
            assert [row["zpid"] for row in json.loads(result.stdout)] == [1]
        finally:
            writer.rollback()
            writer.close()

    def test_where_contains(self, saved):
        assert len(run_query(saved, "--where", "city~schen")) == 3

    def test_where_money_string_value(self, saved):
        assert {r["zpid"] for r in run_query(saved, "--where", "price>=$200,000")} == {2, 3, 4}

    def test_sort_asc_and_desc(self, saved):
        rows = run_query(saved, "--sort", "price:desc")
        assert [r["price"] for r in rows] == [350000, 250000, 200000, 150000]

    def test_sort_missing_values_last(self, saved):
        rows = run_query(saved, "--sort", "area")
        assert rows[-1]["zpid"] == 4  # area=None goes last

    def test_ppsf_computed_field(self, saved):
        assert run_query(saved, "--where", "ppsf==150", "--cols", "zpid,ppsf") == [{"zpid": 1, "ppsf": 150.0}]

    def test_ppsf_missing_area_excluded(self, saved):
        rows = run_query(saved, "--where", "ppsf>=50")
        assert 4 not in {r["zpid"] for r in rows}

    def test_cols_selection(self, saved):
        rows = run_query(saved, "--cols", "zpid,price")
        assert list(rows[0]) == ["zpid", "price"]

    def test_limit(self, saved):
        rows = run_query(saved, "--sort", "price:asc", "--limit", "2")
        assert [r["zpid"] for r in rows] == [1, 4]

    def test_csv_format(self, saved):
        result = runner.invoke(app, ["query", str(saved), "--format", "csv", "--cols", "zpid,price"])
        assert result.exit_code == 0
        assert result.stdout.splitlines()[0] == "zpid,price"

    def test_table_honors_cols(self, saved):
        result = runner.invoke(app, ["query", str(saved), "--format", "table", "--cols", "zpid,price"])
        assert result.exit_code == 0
        assert "zpid" in result.stdout
        assert "price" in result.stdout
        assert "city" not in result.stdout  # in the data and concise set, but not in --cols

    def test_stdin(self, saved):
        result = runner.invoke(app, ["query", "-"], input=saved.read_text())
        assert result.exit_code == 0
        assert len(json.loads(result.stdout)) == 4

    @pytest.mark.parametrize(
        "args",
        [
            ["--where", "just a word"],
            ["--sort", "nope"],
            ["--cols", "zpid,nope"],
            ["--limit", "0"],
        ],
    )
    def test_query_rejects_unknown_or_invalid_arguments(self, saved, args):
        result = runner.invoke(app, ["query", str(saved), *args])
        assert result.exit_code == 2

    def test_unknown_where_field_is_named_in_the_error(self, saved):
        result = runner.invoke(app, ["query", str(saved), "--where", "sqft>=1000"])
        assert result.exit_code == 2
        assert "Unknown field" in result.stderr

    def test_non_list_input(self, tmp_path):
        path = write_json(tmp_path / "obj.json", {"zpid": 1})
        result = runner.invoke(app, ["query", str(path)])
        assert result.exit_code == 2
        assert "JSON array" in result.stderr

    def test_missing_file(self):
        result = runner.invoke(app, ["query", "/nonexistent/file.json"])
        assert result.exit_code == 2

    def test_empty_result_is_success(self, saved):
        assert run_query(saved, "--where", "price>999999") == []

    def test_where_rejects_null_with_ordering_operator(self, saved):
        result = runner.invoke(app, ["query", str(saved), "--where", "price>null"])
        assert result.exit_code == 2
        assert "cannot be compared with null" in result.stderr


def test_where_coercion_only_accepts_strict_numeric_literals():
    assert coerce_value("-$1,234.50") == -1234.5
    assert coerce_value("nan") == "nan"
    assert coerce_value("inf") == "inf"
    assert coerce_value("1e3") == "1e3"
