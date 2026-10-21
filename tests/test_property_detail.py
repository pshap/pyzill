"""The normalized property-detail shape."""

import pytest

from pyzill.property_detail import normalize_property_detail


def test_normalize_property_detail_reports_an_unlabeled_lot_size_as_sent():
    """An unlabeled number must not be multiplied by the acre conversion.

    Live detail payloads send ``lotSize`` in square feet ("1,742 sqft"), so
    treating a bare number as acres turned a 1,742 sqft lot into 75 million
    square feet. A number with no unit anywhere is reported as it arrived.
    """
    detail = normalize_property_detail({"zpid": 1, "lotSize": 0.25})
    assert detail["lot_size"] == pytest.approx(0.25)
    assert detail["lot_size_unit"] is None


def test_normalize_property_detail_reads_a_lot_size_that_states_its_unit():
    detail = normalize_property_detail({"zpid": 1, "resoFacts": {"lotSize": "1,742 sqft"}})
    assert detail["lot_size"] == pytest.approx(1742.0)
    assert detail["lot_size_unit"] == "sqft"


def test_normalize_property_detail_converts_a_lot_size_stated_in_acres():
    detail = normalize_property_detail({"zpid": 1, "resoFacts": {"lotSize": "0.25 acres"}})
    assert detail["lot_size"] == pytest.approx(10_890.0)
    assert detail["lot_size_unit"] == "sqft (from acres)"


def test_normalize_property_detail_reads_the_detail_lot_area_units_field():
    """A detail payload spells the unit field ``lotAreaUnits``, not ``lotAreaUnit``."""
    detail = normalize_property_detail({"zpid": 1, "lotAreaValue": 1742.4, "lotAreaUnits": "Square Feet"})
    assert detail["lot_size"] == pytest.approx(1742.4)
    assert detail["lot_size_unit"] == "sqft"


def test_normalize_property_detail_converts_acres_lot_area_unit():
    detail = normalize_property_detail({"zpid": 1, "lotAreaValue": 0.5, "lotAreaUnit": "acres"})
    assert detail["lot_size"] == pytest.approx(21_780.0)
    assert detail["lot_size_unit"] == "sqft (from acres)"


def test_normalize_property_detail_keeps_square_feet_lot_area_value():
    detail = normalize_property_detail({"zpid": 1, "lotAreaValue": 8000, "lotAreaUnit": "sqft"})
    assert detail["lot_size"] == 8000
    assert detail["lot_size_unit"] == "sqft"


def test_normalize_property_detail_reports_monthly_hoa_fee():
    detail = normalize_property_detail({"zpid": 1, "monthlyHoaFee": 250})
    assert detail["hoa_fee"] == 250
    assert detail["hoa_fee_unit"] == "monthly"


def test_normalize_property_detail_reports_annual_hoa_fee_total():
    detail = normalize_property_detail({"zpid": 1, "resoFacts": {"hoaFeeTotal": 3000}})
    assert detail["hoa_fee"] == 3000
    assert detail["hoa_fee_unit"] == "annual"


def test_normalize_property_detail_no_hoa_fee_is_none():
    detail = normalize_property_detail({"zpid": 1})
    assert detail["hoa_fee"] is None
    assert detail["hoa_fee_unit"] is None


def test_normalize_property_detail_non_mapping_returns_none_fields():
    detail = normalize_property_detail(None)  # type: ignore[arg-type]
    assert detail["lot_size"] is None
    assert detail["lot_size_unit"] is None
    assert detail["hoa_fee"] is None
    assert detail["hoa_fee_unit"] is None
    assert detail["zpid"] is None


def test_normalize_property_detail_coerces_string_fields():
    detail = normalize_property_detail({"zpid": 1, "homeType": 4, "description": 5})

    assert detail["home_type"] == "4"
    assert detail["description"] == "5"


def test_normalize_property_detail_keeps_string_fields_as_they_are():
    detail = normalize_property_detail({"zpid": 1, "homeType": "SINGLE_FAMILY", "description": "Nice house"})

    assert detail["home_type"] == "SINGLE_FAMILY"
    assert detail["description"] == "Nice house"
