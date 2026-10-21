"""Normalize the raw (and inconsistent) listing dicts Zillow returns.

Zillow's ``mapResults`` and ``listResults`` are messy: prices arrive as
preformatted strings (``"$329,900"``), some fields are missing entirely, and
others are ``None`` or ``0`` when unknown.  These helpers turn a raw listing
into a flat dict of predictable, numeric-or-``None`` values.

This module owns the search listing shape.  The property-detail shape lives
in :mod:`pyzill.property_detail`, and the scalar rules both read live in
:mod:`pyzill.values`.
"""

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, TypedDict, get_args

from pyzill.config import ZILLOW_HOME
from pyzill.utils import get_nested_value
from pyzill.values import parse_price, to_number, to_text, to_zpid

__all__ = [
    "LISTING_FIELDS",
    "NUMERIC_LISTING_FIELDS",
    "Listing",
    "is_building_result",
    "normalize_listing",
    "normalize_listings",
    "parse_price",
]

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


def _first(*values: Any) -> Any:
    """Return the first value that is not None."""
    for v in values:
        if v is not None:
            return v
    return None


def _first_number(value: Any, fallback: Any) -> int | float | None:
    number = to_number(value)
    return number if number is not None else to_number(fallback)


def _number_with_zero_fallback(value: Any, fallback: Any, *, preserve_zero: bool) -> int | float | None:
    number = to_number(value)
    fallback_number = to_number(fallback)
    if number is None or (number == 0 and fallback_number is not None and fallback_number > 0):
        number = fallback_number
    return None if number == 0 and not preserve_zero else number


def _resolve_zpid(listing: Mapping[str, Any]) -> int | None:
    """Return the listing's zpid, from the top level or from ``homeInfo``.

    Zillow carries the zpid in two places: at the top level of the row and
    under ``hdpData.homeInfo``.  A building card sometimes stamps a
    ``buildingId`` (a lat-lng string such as ``"47.61503--122.34274"``) into
    the top-level ``zpid``, which is not a zpid at all, so the nested copy is
    consulted as well.  Both the building-card test and the normalizer read
    the zpid through this one function, so they cannot disagree about which
    rows are listings.
    """
    zpid = to_zpid(listing.get("zpid"))
    if zpid is not None:
        return zpid
    info = get_nested_value(listing, "hdpData.homeInfo", {})
    if isinstance(info, dict):
        return to_zpid(info.get("zpid"))
    return None


def _days_on_zillow(*values: Any) -> int | None:
    """Return how many days a listing has been on Zillow.

    Zillow stamps ``-1`` when it does not know the value.  Live captures show
    the sentinel on the nested ``hdpData.homeInfo`` copy of a search row
    while the sidebar's copy of the same property carries the real count, so
    a negative value is treated as unknown and the first known count wins.
    Zero is a real count (a listing added today).
    """
    for value in values:
        number = to_number(value)
        if number is not None and number >= 0:
            return int(number)
    return None


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
    return _resolve_zpid(listing) is None


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

    days_on_zillow = _days_on_zillow(info.get("daysOnZillow"), listing.get("daysOnZillow"))

    price_change_raw = info.get("priceChange") if info.get("priceChange") is not None else listing.get("priceChange")
    price_change_number = to_number(price_change_raw)
    price_change = int(price_change_number) if price_change_number is not None else None

    date_price_changed_raw = info.get("datePriceChanged") or listing.get("datePriceChanged")
    price_change_date = _format_epoch_ms(date_price_changed_raw)
    price_reduction = info.get("priceReduction") or listing.get("priceReduction")

    lot_area_val = _number_with_zero_fallback(
        info.get("lotAreaValue"), listing.get("lotAreaValue"), preserve_zero=False
    )
    lot_area_unit = info.get("lotAreaUnit") or listing.get("lotAreaUnit")

    year_built_raw = info.get("yearBuilt") if info.get("yearBuilt") is not None else listing.get("yearBuilt")
    year_built_number = to_number(year_built_raw)
    year_built = int(year_built_number) if year_built_number is not None and year_built_number > 0 else None

    tax_assessed = parse_price(info.get("taxAssessedValue") or listing.get("taxAssessedValue"))
    listing_sub_type = _extract_listing_sub_type(info.get("listing_sub_type") or listing.get("listing_sub_type"))
    unit = info.get("unit") or listing.get("unit")
    broker_name = listing.get("brokerName") or info.get("brokerName")

    return {
        "zpid": _resolve_zpid(listing),
        "price": price,
        "price_label": to_text(listing.get("priceLabel")),
        "beds": _number_with_zero_fallback(listing.get("beds"), info.get("bedrooms"), preserve_zero=True),
        "baths": _number_with_zero_fallback(listing.get("baths"), info.get("bathrooms"), preserve_zero=True),
        "area": _number_with_zero_fallback(listing.get("area"), info.get("livingArea"), preserve_zero=False),
        "address": to_text(listing.get("address")),
        "street": to_text(_first(info.get("streetAddress"), listing.get("addressStreet"))),
        "city": to_text(_first(info.get("city"), listing.get("addressCity"))),
        "state": to_text(_first(listing.get("addressState"), info.get("state"))),
        "zipcode": to_text(_first(info.get("zipcode"), listing.get("addressZipcode"))),
        "latitude": _first_number(lat_long.get("latitude"), info.get("latitude")),
        "longitude": _first_number(lat_long.get("longitude"), info.get("longitude")),
        "home_type": to_text(info.get("homeType")),
        "status": to_text(_first(listing.get("statusText"), info.get("homeStatus"))),
        "status_type": to_text(listing.get("statusType")),
        "detail_url": detail_url,
        "image_url": to_text(listing.get("imgSrc")),
        "zestimate": parse_price(info.get("zestimate")),
        "rent_zestimate": parse_price(info.get("rentZestimate")),
        "days_on_zillow": days_on_zillow,
        "price_change": price_change,
        "price_change_date": price_change_date,
        "price_reduction": to_text(price_reduction),
        "lot_area_value": float(lot_area_val) if lot_area_val is not None else None,
        "lot_area_unit": to_text(lot_area_unit),
        "year_built": year_built,
        "tax_assessed_value": tax_assessed,
        "listing_sub_type": listing_sub_type,
        "unit": to_text(unit),
        "broker_name": to_text(broker_name),
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
