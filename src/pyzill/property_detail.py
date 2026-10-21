"""Normalize a raw Zillow home-detail page into a flat record.

The detail page is a different payload from a search result and a different
shape from a search listing: it carries the address in one object, the
living area under another name, the lot size in acres, and the HOA fee with
its own unit.  This module owns that shape; :mod:`pyzill.normalize` owns the
search listing shape.  The scalar rules both read are in
:mod:`pyzill.values`.
"""

from collections.abc import Mapping
from typing import Any, TypedDict

from pyzill.values import parse_price, to_number, to_text, to_zpid

__all__ = ["PropertyDetail", "normalize_property_detail"]


class PropertyDetail(TypedDict):
    """Normalized representation of a Zillow property detail page."""

    zpid: int | None
    price: int | None
    beds: int | float | None
    baths: int | float | None
    area: int | float | None
    address: str | None
    street: str | None
    city: str | None
    state: str | None
    zipcode: str | None
    latitude: float | None
    longitude: float | None
    home_type: str | None
    year_built: int | None
    lot_size: int | float | None
    lot_size_unit: str | None
    hoa_fee: int | float | None
    hoa_fee_unit: str | None
    description: str | None
    photos_count: int | None
    price_history: list[dict[str, Any]] | None
    tax_history: list[dict[str, Any]] | None
    schools: list[dict[str, Any]] | None


_ACRES_TO_SQFT = 43_560.0

_LotSizeResult = tuple[int | float | None, str | None]


def _lot_size_in_sqft(property_data: Mapping[str, Any], reso_dict: Mapping[str, Any]) -> _LotSizeResult:
    """Return the lot size in square feet and the unit it was reported in.

    Zillow reports ``lotSize`` in acres on home-detail payloads (with the unit
    spelled out nearby), while ``lotAreaValue``/``lotAreaUnit`` from search
    results are already in the stated unit. To avoid mixing units in one field,
    everything here is normalized to square feet, matching the ``Listing``
    convention. The original unit is returned alongside the value so callers can
    tell an acre-derived figure from a raw square-foot one.
    """
    for container, key in ((reso_dict, "lotSize"), (property_data, "lotSize")):
        acres = to_number(container.get(key))
        if acres is not None:
            return round(acres * _ACRES_TO_SQFT, 2), "sqft (from acres)"
    area = to_number(property_data.get("lotAreaValue"))
    if area is None:
        return None, None
    unit = str(property_data.get("lotAreaUnit") or "").lower()
    if unit.startswith("acre"):
        return round(area * _ACRES_TO_SQFT, 2), "sqft (from acres)"
    return area, unit or "sqft"


def normalize_property_detail(property_data: Mapping[str, Any]) -> PropertyDetail:
    """Flatten a raw Zillow home detail page dictionary into a normalized PropertyDetail."""
    if not isinstance(property_data, Mapping):
        return {
            "zpid": None,
            "price": None,
            "beds": None,
            "baths": None,
            "area": None,
            "address": None,
            "street": None,
            "city": None,
            "state": None,
            "zipcode": None,
            "latitude": None,
            "longitude": None,
            "home_type": None,
            "year_built": None,
            "lot_size": None,
            "lot_size_unit": None,
            "hoa_fee": None,
            "hoa_fee_unit": None,
            "description": None,
            "photos_count": None,
            "price_history": None,
            "tax_history": None,
            "schools": None,
        }

    addr = property_data.get("address")
    addr_dict: Mapping[str, Any] = addr if isinstance(addr, Mapping) else {}
    street = addr_dict.get("streetAddress") or property_data.get("streetAddress")
    city = addr_dict.get("city") or property_data.get("city")
    state = addr_dict.get("state") or property_data.get("state")
    zipcode = addr_dict.get("zipcode") or property_data.get("zipcode")
    full_address: str | None = None
    if isinstance(addr, str) and addr:
        full_address = addr
    elif street or city or state or zipcode:
        parts = [p for p in (street, city, f"{state} {zipcode}".strip() if (state or zipcode) else None) if p]
        full_address = ", ".join(parts) if parts else None

    reso = property_data.get("resoFacts")
    reso_dict: Mapping[str, Any] = reso if isinstance(reso, Mapping) else {}

    year_built_val = (
        reso_dict.get("yearBuilt") if reso_dict.get("yearBuilt") is not None else property_data.get("yearBuilt")
    )
    year_built_number = to_number(year_built_val)
    year_built = int(year_built_number) if year_built_number is not None and year_built_number > 0 else None

    lot_size, lot_size_unit = _lot_size_in_sqft(property_data, reso_dict)

    hoa_fee_val = property_data.get("monthlyHoaFee")
    hoa_fee_unit: str | None = None
    if hoa_fee_val is not None:
        hoa_fee = to_number(hoa_fee_val)
        hoa_fee_unit = "monthly"
    else:
        hoa_fee = to_number(reso_dict.get("hoaFeeTotal"))
        if hoa_fee is not None:
            # ``hoaFeeTotal`` is an annual figure on Zillow detail payloads;
            # record the unit so callers do not mistake it for a monthly fee.
            hoa_fee_unit = "annual"

    photo_count_val = property_data.get("photoCount")
    photo_count_number = to_number(photo_count_val)
    if photo_count_number is not None:
        photos_count = int(photo_count_number)
    elif isinstance(property_data.get("originalPhotos"), list):
        photos_count = len(property_data["originalPhotos"])
    elif isinstance(property_data.get("responsivePhotos"), list):
        photos_count = len(property_data["responsivePhotos"])
    else:
        photos_count = None

    price_hist = property_data.get("priceHistory")
    tax_hist = property_data.get("taxHistory")
    schools = property_data.get("schools") or reso_dict.get("schools")

    return {
        "zpid": to_zpid(property_data.get("zpid")),
        "price": parse_price(property_data.get("price")),
        "beds": to_number(property_data.get("bedrooms")),
        "baths": to_number(property_data.get("bathrooms")),
        "area": to_number(property_data.get("livingAreaValue") or property_data.get("livingArea")),
        "address": full_address,
        "street": to_text(street),
        "city": to_text(city),
        "state": to_text(state),
        "zipcode": to_text(zipcode),
        "latitude": to_number(property_data.get("latitude")),
        "longitude": to_number(property_data.get("longitude")),
        "home_type": to_text(property_data.get("homeType")),
        "year_built": year_built,
        "lot_size": lot_size,
        "lot_size_unit": lot_size_unit,
        "hoa_fee": hoa_fee,
        "hoa_fee_unit": hoa_fee_unit,
        "description": to_text(property_data.get("description")),
        "photos_count": photos_count,
        "price_history": list(price_hist) if isinstance(price_hist, list) else None,
        "tax_history": list(tax_hist) if isinstance(tax_hist, list) else None,
        "schools": list(schools) if isinstance(schools, list) else None,
    }
