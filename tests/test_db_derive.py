"""Deriving HOA and tax figures from a property-detail payload."""

import pytest

from pyzill.db.derive import TaxLatest, hoa_monthly, int_if_whole, tax_latest, tax_latest_from_json


def test_tax_latest_field_order_is_fixed():
    """One order, spelled in the type: callers use attributes, never positions."""
    assert TaxLatest._fields == ("year", "paid", "assessed")


@pytest.mark.parametrize(
    ("hoa_fee", "hoa_fee_unit", "expected"),
    [
        (150, "monthly", 150),
        (150.0, "monthly", 150.0),
        (1200, "annual", 100.0),
        (1200.0, "annual", 100.0),
        (300, "quarterly", 100.0),
        (301, "quarterly", 100.33),
        (150, "weekly", None),
        (150, None, None),
        (None, "monthly", None),
    ],
)
def test_hoa_monthly(hoa_fee, hoa_fee_unit, expected):
    assert hoa_monthly(hoa_fee, hoa_fee_unit) == expected


@pytest.mark.parametrize("value", [3, 3.5, 3.0, None, "3"])
def test_int_if_whole_only_rewrites_a_whole_float(value):
    expected = 3 if value == 3.0 else value
    assert int_if_whole(value) == expected


def test_int_if_whole_returns_an_int_for_a_whole_float():
    assert isinstance(int_if_whole(2023.0), int)
    assert isinstance(int_if_whole(2023), int)
    assert isinstance(int_if_whole(2023.5), float)


@pytest.mark.parametrize("entries", [None, [], "not a list", {}, [{"year": 2023}, "not a dict"]])
def test_tax_latest_without_a_usable_entry_is_all_none(entries):
    assert tax_latest(entries) == TaxLatest(None, None, None)


@pytest.mark.parametrize("key", ["taxPaid", "taxPaidAmount", "tax", "taxAmount"])
def test_tax_latest_paid_aliases(key):
    assert tax_latest([{key: 4500}]).paid == 4500


@pytest.mark.parametrize("key", ["taxAssessedValue", "assessedValue", "taxAssessed"])
def test_tax_latest_assessed_aliases(key):
    assert tax_latest([{key: 5000}]).assessed == 5000


def test_tax_latest_reads_the_last_entry_only():
    latest = tax_latest(
        [
            {"year": 2020, "taxPaid": 1000, "taxAssessedValue": 2000},
            {"year": 2023, "taxPaid": 3000, "taxAssessedValue": 4000},
        ]
    )
    assert latest == TaxLatest(year=2023, paid=3000, assessed=4000)


def test_tax_latest_whole_float_year_becomes_int():
    latest = tax_latest([{"year": 2023.0}])
    assert latest.year == 2023
    assert isinstance(latest.year, int)


def test_tax_latest_null_alias_falls_through_to_the_next_one():
    """A present-but-null alias must not hide the figure Zillow did send."""
    latest = tax_latest([{"taxPaid": None, "taxPaidAmount": 2500, "taxAssessedValue": None, "assessedValue": 5000}])
    assert latest.paid == 2500
    assert latest.assessed == 5000


def test_tax_latest_non_numeric_alias_falls_through_to_the_next_one():
    latest = tax_latest([{"taxPaid": "n/a", "tax": "3,000", "taxAssessedValue": "unknown", "taxAssessed": 6000}])
    assert latest.paid == 3000
    assert latest.assessed == 6000


def test_tax_latest_coerces_string_numbers_to_numbers():
    latest = tax_latest([{"year": "2023", "taxPaid": "$3,000", "taxAssessedValue": "4,500.50"}])
    assert latest == TaxLatest(year=2023, paid=3000, assessed=4500.5)


@pytest.mark.parametrize("raw", [None, "", "{not json", "null", "[]", '"a string"'])
def test_tax_latest_from_json_without_usable_entries(raw):
    assert tax_latest_from_json(raw) == TaxLatest(None, None, None)


def test_tax_latest_from_json_reads_a_stored_list():
    assert tax_latest_from_json('[{"year": 2021}, {"year": 2022, "tax": 3100}]') == TaxLatest(
        year=2022, paid=3100, assessed=None
    )
