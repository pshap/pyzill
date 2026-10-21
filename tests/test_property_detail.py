"""The normalized property-detail shape."""

import pytest

from pyzill.property_detail import normalize_property_detail


def test_normalize_property_detail_reports_lot_size_in_square_feet():
    detail = normalize_property_detail({"zpid": 1, "lotSize": 0.25})
    assert detail["lot_size"] == pytest.approx(10_890.0)
    assert detail["lot_size_unit"] == "sqft (from acres)"


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
