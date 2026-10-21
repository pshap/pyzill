"""Normalize the raw (and inconsistent) listing dicts Zillow returns.

Zillow's ``mapResults`` and ``listResults`` are messy: prices arrive as
preformatted strings (``"$329,900"``), some fields are missing entirely, and
others are ``None`` or ``0`` when unknown.  These helpers turn a raw listing
into a flat dict of predictable, numeric-or-``None`` values.
"""

import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, TypedDict, get_args

from pyzill.config import ZILLOW_HOME
from pyzill.utils import get_nested_value

_BASE_URL = ZILLOW_HOME


class Listing(TypedDict):
    """Normalized, flat representation of a Zillow listing.

    Every key is always present.  Numeric fields are ``int``/``float`` or
    ``None`` (never the ``"$0"``/missing sentinels Zillow uses); string fields
    are ``str`` or ``None``.  This is a static shape only -- it is not
    validated at runtime.
    """

    zpid: int | None
    price: int | None
    price_label: str | None
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
    status: str | None
    status_type: str | None
    detail_url: str | None
    image_url: str | None
    zestimate: int | None
    rent_zestimate: int | None
    days_on_zillow: int | None
    price_change: int | None
    price_change_date: str | None
    price_reduction: str | None
    lot_area_value: float | None
    lot_area_unit: str | None
    year_built: int | None
    tax_assessed_value: int | None
    listing_sub_type: str | None
    unit: str | None
    broker_name: str | None


def _is_numeric(annotation: Any) -> bool:
    """True when a field's annotation allows a number."""
    return any(argument is int or argument is float for argument in get_args(annotation))


# The listing fields, in declaration order. This is the single source for
# every list derived from the listing shape: the database columns, the
# columns the write path fills, and the numeric fields of the query engine.
LISTING_FIELDS: tuple[str, ...] = tuple(Listing.__annotations__)

# The listing fields whose values are numbers (or None).
NUMERIC_LISTING_FIELDS: frozenset[str] = frozenset(
    name for name, annotation in Listing.__annotations__.items() if _is_numeric(annotation)
)


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


# Anything that is not a digit, minus sign or decimal point.
_NOT_NUMBER = re.compile(r"[^0-9.\-]")
_PRICE_SUFFIXES = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}
# Trailing rental / "starting at" adornments attached to a price.
_RENTAL_SUFFIX = re.compile(r"(?:/month(?:ly)?|/mo|\+)\s*$", re.IGNORECASE)


def _to_number(value: Any) -> int | float | None:
    """Coerce a Zillow scalar into an int/float, or None if not numeric."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        cleaned = _NOT_NUMBER.sub("", value.strip())
        if cleaned in ("", "-", ".", "-."):
            return None
        try:
            num = float(cleaned)
        except ValueError:
            return None
        return int(num) if num.is_integer() else num
    return None


def _as_str(value: Any) -> str | None:
    """Coerce a Zillow scalar into a string, or None if it is not one.

    Zillow sometimes sends a number where a string belongs (a numeric
    ``addressZipcode``, a numeric ``priceLabel``).  Rendering those as
    strings keeps the normalized shape honest: a zipcode that stayed an
    int would never match the zipcode a search was made for, and the
    ``Listing`` shape promises ``str`` or ``None`` for every string
    field.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    return None


def _format_epoch_ms(value: Any) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and value > 0:
        try:
            return datetime.fromtimestamp(value / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        except (ValueError, OSError):
            return None
    if isinstance(value, str):
        return value
    return None


def _extract_listing_sub_type(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        flags = [str(k).removeprefix("is_") for k, v in value.items() if v]
        return ", ".join(flags) if flags else None
    return None


def parse_price(value: Any) -> int | None:
    """Parse a Zillow price into an int.

    Handles preformatted strings (``"$329,900"`` -> ``329900``), K/M/B
    suffixes, numeric values, and ``None``.  Non-positive results (Zillow uses
    ``0``/``"$0"`` to mean "unknown") are returned as ``None``.

    Args:
        value: Raw price, e.g. ``"$329,900"``, ``329900``, ``None``.

    Returns:
        int | None: The price in whole dollars, or None if unknown.
    """
    if isinstance(value, str):
        cleaned = value.strip().replace(",", "").replace("$", "")
        # Strip trailing rental ("/mo", "/month") and "starting at" ("+")
        # adornments before the letter check, so "$2,500/mo" parses as 2500.
        while True:
            stripped = _RENTAL_SUFFIX.sub("", cleaned).strip()
            if stripped == cleaned:
                break
            cleaned = stripped
        suffix = cleaned[-1].upper() if cleaned else ""
        if suffix in _PRICE_SUFFIXES:
            numeric_text = cleaned[:-1].strip()
            if re.search(r"[A-Za-z]", numeric_text):
                return None
            num = _to_number(numeric_text)
            if num is not None:
                num *= _PRICE_SUFFIXES[suffix]
        elif re.search(r"[A-Za-z]", cleaned):
            return None
        else:
            num = _to_number(cleaned)
    else:
        num = _to_number(value)
    if num is None:
        return None
    price = round(num)
    return price if price > 0 else None


def _first(*values: Any) -> Any:
    """Return the first value that is not None."""
    for v in values:
        if v is not None:
            return v
    return None


def _first_number(value: Any, fallback: Any) -> int | float | None:
    number = _to_number(value)
    return number if number is not None else _to_number(fallback)


def _number_with_zero_fallback(value: Any, fallback: Any, *, preserve_zero: bool) -> int | float | None:
    number = _to_number(value)
    fallback_number = _to_number(fallback)
    if number is None or (number == 0 and fallback_number is not None and fallback_number > 0):
        number = fallback_number
    return None if number == 0 and not preserve_zero else number


def _integer_zpid(value: Any) -> int | None:
    number = _to_number(value)
    if isinstance(number, float):
        return int(number) if number.is_integer() else None
    return number


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
        acres = _to_number(container.get(key))
        if acres is not None:
            return round(acres * _ACRES_TO_SQFT, 2), "sqft (from acres)"
    area = _to_number(property_data.get("lotAreaValue"))
    if area is None:
        return None, None
    unit = str(property_data.get("lotAreaUnit") or "").lower()
    if unit.startswith("acre"):
        return round(area * _ACRES_TO_SQFT, 2), "sqft (from acres)"
    return area, unit or "sqft"


def is_building_result(listing: Any) -> bool:
    """Return True for a building-only map/list result that has no ``zpid``.

    Zillow's search ``mapResults`` and ``listResults`` mix real listings with
    building-level cards (``isBuilding: True``, a ``buildingId``, ``unitCount``
    and a "starting at" ``price``, and no ``zpid`` at the top level or under
    ``hdpData.homeInfo``). Live captures show rental and even sale/sold results
    can be *entirely* building cards in some areas, so callers that want real
    listings must drop these. A card that is flagged ``isBuilding`` but does
    carry a ``zpid`` is treated as a real listing and kept.

    Args:
        listing: A single raw entry from ``mapResults`` / ``listResults``.

    Returns:
        bool: True if the entry is a building card with no usable ``zpid``.
    """
    if not isinstance(listing, Mapping):
        return False
    if not listing.get("isBuilding"):
        return False
    if _integer_zpid(listing.get("zpid")) is not None:
        return False
    info = get_nested_value(listing, "hdpData.homeInfo", {})
    return not (isinstance(info, dict) and _integer_zpid(info.get("zpid")) is not None)


def normalize_listing(listing: Any) -> Listing | None:
    """Flatten a raw Zillow listing into a normalized ``Listing``.

    Prefers top-level fields and falls back to ``hdpData.homeInfo`` when a
    field is missing.  Numeric fields are coerced; unknown values become
    ``None`` so callers never have to deal with ``"$0"`` strings or missing
    keys.  String fields are coerced to ``str``, so the shape matches the
    :class:`Listing` contract even when Zillow sends a number.

    Args:
        listing: A single raw entry from ``mapResults`` / ``listResults``.

    Returns:
        Listing | None: A normalized listing, or None for non-dict entries.
    """
    if not isinstance(listing, dict):
        return None

    info = get_nested_value(listing, "hdpData.homeInfo", {})
    if not isinstance(info, dict):
        info = {}
    lat_long = listing.get("latLong") or {}
    if not isinstance(lat_long, dict):
        lat_long = {}

    price = parse_price(listing.get("price"))
    if price is None:
        price = parse_price(info.get("price"))

    detail_url = listing.get("detailUrl")
    if isinstance(detail_url, str):
        detail_url = _BASE_URL + detail_url if detail_url.startswith("/") else detail_url
    else:
        detail_url = None

    days_on_zillow_raw = (
        info.get("daysOnZillow") if info.get("daysOnZillow") is not None else listing.get("daysOnZillow")
    )
    days_on_zillow_number = _to_number(days_on_zillow_raw)
    days_on_zillow = int(days_on_zillow_number) if days_on_zillow_number is not None else None

    price_change_raw = info.get("priceChange") if info.get("priceChange") is not None else listing.get("priceChange")
    price_change_number = _to_number(price_change_raw)
    price_change = int(price_change_number) if price_change_number is not None else None

    date_price_changed_raw = info.get("datePriceChanged") or listing.get("datePriceChanged")
    price_change_date = _format_epoch_ms(date_price_changed_raw)
    price_reduction = info.get("priceReduction") or listing.get("priceReduction")

    lot_area_val = _number_with_zero_fallback(
        info.get("lotAreaValue"), listing.get("lotAreaValue"), preserve_zero=False
    )
    lot_area_unit = info.get("lotAreaUnit") or listing.get("lotAreaUnit")

    year_built_raw = info.get("yearBuilt") if info.get("yearBuilt") is not None else listing.get("yearBuilt")
    year_built_number = _to_number(year_built_raw)
    year_built = int(year_built_number) if year_built_number is not None and year_built_number > 0 else None

    tax_assessed = parse_price(info.get("taxAssessedValue") or listing.get("taxAssessedValue"))
    listing_sub_type = _extract_listing_sub_type(info.get("listing_sub_type") or listing.get("listing_sub_type"))
    unit = info.get("unit") or listing.get("unit")
    broker_name = listing.get("brokerName") or info.get("brokerName")

    return {
        "zpid": _integer_zpid(listing.get("zpid")),
        "price": price,
        "price_label": _as_str(listing.get("priceLabel")),
        "beds": _number_with_zero_fallback(listing.get("beds"), info.get("bedrooms"), preserve_zero=True),
        "baths": _number_with_zero_fallback(listing.get("baths"), info.get("bathrooms"), preserve_zero=True),
        "area": _number_with_zero_fallback(listing.get("area"), info.get("livingArea"), preserve_zero=False),
        "address": _as_str(listing.get("address")),
        "street": _as_str(_first(info.get("streetAddress"), listing.get("addressStreet"))),
        "city": _as_str(_first(info.get("city"), listing.get("addressCity"))),
        "state": _as_str(_first(listing.get("addressState"), info.get("state"))),
        "zipcode": _as_str(_first(info.get("zipcode"), listing.get("addressZipcode"))),
        "latitude": _first_number(lat_long.get("latitude"), info.get("latitude")),
        "longitude": _first_number(lat_long.get("longitude"), info.get("longitude")),
        "home_type": _as_str(info.get("homeType")),
        "status": _as_str(_first(listing.get("statusText"), info.get("homeStatus"))),
        "status_type": _as_str(listing.get("statusType")),
        "detail_url": detail_url,
        "image_url": _as_str(listing.get("imgSrc")),
        "zestimate": parse_price(info.get("zestimate")),
        "rent_zestimate": parse_price(info.get("rentZestimate")),
        "days_on_zillow": days_on_zillow,
        "price_change": price_change,
        "price_change_date": price_change_date,
        "price_reduction": _as_str(price_reduction),
        "lot_area_value": float(lot_area_val) if lot_area_val is not None else None,
        "lot_area_unit": _as_str(lot_area_unit),
        "year_built": year_built,
        "tax_assessed_value": tax_assessed,
        "listing_sub_type": listing_sub_type,
        "unit": _as_str(unit),
        "broker_name": _as_str(broker_name),
    }


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
    year_built_number = _to_number(year_built_val)
    year_built = int(year_built_number) if year_built_number is not None and year_built_number > 0 else None

    lot_size, lot_size_unit = _lot_size_in_sqft(property_data, reso_dict)

    hoa_fee_val = property_data.get("monthlyHoaFee")
    hoa_fee_unit: str | None = None
    if hoa_fee_val is not None:
        hoa_fee = _to_number(hoa_fee_val)
        hoa_fee_unit = "monthly"
    else:
        hoa_fee = _to_number(reso_dict.get("hoaFeeTotal"))
        if hoa_fee is not None:
            # ``hoaFeeTotal`` is an annual figure on Zillow detail payloads;
            # record the unit so callers do not mistake it for a monthly fee.
            hoa_fee_unit = "annual"

    photo_count_val = property_data.get("photoCount")
    photo_count_number = _to_number(photo_count_val)
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
        "zpid": _integer_zpid(property_data.get("zpid")),
        "price": parse_price(property_data.get("price")),
        "beds": _to_number(property_data.get("bedrooms")),
        "baths": _to_number(property_data.get("bathrooms")),
        "area": _to_number(property_data.get("livingAreaValue") or property_data.get("livingArea")),
        "address": full_address,
        "street": _as_str(street),
        "city": _as_str(city),
        "state": _as_str(state),
        "zipcode": _as_str(zipcode),
        "latitude": _to_number(property_data.get("latitude")),
        "longitude": _to_number(property_data.get("longitude")),
        "home_type": _as_str(property_data.get("homeType")),
        "year_built": year_built,
        "lot_size": lot_size,
        "lot_size_unit": lot_size_unit,
        "hoa_fee": hoa_fee,
        "hoa_fee_unit": hoa_fee_unit,
        "description": _as_str(property_data.get("description")),
        "photos_count": photos_count,
        "price_history": list(price_hist) if isinstance(price_hist, list) else None,
        "tax_history": list(tax_hist) if isinstance(tax_hist, list) else None,
        "schools": list(schools) if isinstance(schools, list) else None,
    }


def normalize_listings(listings: Any) -> list[Listing]:
    """Normalize an iterable of raw listings, skipping unusable entries.

    Non-dict entries and building-only cards (see :func:`is_building_result`)
    are dropped, so the result contains one :class:`Listing` per real listing.

    Args:
        listings: Iterable of raw listing dicts.

    Returns:
        list[Listing]: Normalized listings.
    """
    return [
        normalized
        for item in (listings or [])
        if not is_building_result(item) and (normalized := normalize_listing(item)) is not None
    ]
