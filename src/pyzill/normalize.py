"""Normalize the raw (and inconsistent) listing dicts Zillow returns.

Zillow's ``mapResults`` and ``listResults`` are messy: prices arrive as
preformatted strings (``"$329,900"``), some fields are missing entirely, and
others are ``None`` or ``0`` when unknown.  These helpers turn a raw listing
into a flat dict of predictable, numeric-or-``None`` values.
"""

import re
from collections.abc import Mapping
from typing import Any, TypedDict, overload

from pyzill.http import ZILLOW_HOME
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


def _integer_zpid(value: Any) -> int | None:
    number = _to_number(value)
    if isinstance(number, float):
        return int(number) if number.is_integer() else None
    return number


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


@overload
def normalize_listing(listing: dict[str, Any]) -> Listing: ...


@overload
def normalize_listing(listing: Any) -> Listing | None: ...


def normalize_listing(listing: Any) -> Listing | None:
    """Flatten a raw Zillow listing into a normalized ``Listing``.

    Prefers top-level fields and falls back to ``hdpData.homeInfo`` when a
    field is missing.  Numeric fields are coerced; unknown values become
    ``None`` so callers never have to deal with ``"$0"`` strings or missing
    keys.

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
    if detail_url and detail_url.startswith("/"):
        detail_url = _BASE_URL + detail_url

    return {
        "zpid": _integer_zpid(listing.get("zpid")),
        "price": price,
        "price_label": listing.get("priceLabel"),
        "beds": _first_number(listing.get("beds"), info.get("bedrooms")),
        "baths": _first_number(listing.get("baths"), info.get("bathrooms")),
        "area": _first_number(listing.get("area"), info.get("livingArea")),
        "address": listing.get("address"),
        "street": info.get("streetAddress"),
        "city": info.get("city"),
        "state": _first(listing.get("addressState"), info.get("state")),
        "zipcode": info.get("zipcode"),
        "latitude": _first_number(lat_long.get("latitude"), info.get("latitude")),
        "longitude": _first_number(lat_long.get("longitude"), info.get("longitude")),
        "home_type": info.get("homeType"),
        "status": _first(listing.get("statusText"), info.get("homeStatus")),
        "status_type": listing.get("statusType"),
        "detail_url": detail_url,
        "image_url": listing.get("imgSrc"),
        "zestimate": parse_price(info.get("zestimate")),
        "rent_zestimate": parse_price(info.get("rentZestimate")),
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
