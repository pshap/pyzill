"""Normalize the raw (and inconsistent) listing dicts Zillow returns.

Zillow's ``mapResults`` and ``listResults`` are messy: prices arrive as
preformatted strings (``"$329,900"``), some fields are missing entirely, and
others are ``None`` or ``0`` when unknown.  These helpers turn a raw listing
into a flat dict of predictable, numeric-or-``None`` values.
"""

import re
from typing import Any

_BASE_URL = "https://www.zillow.com"

# Anything that is not a digit, minus sign or decimal point.
_NOT_NUMBER = re.compile(r"[^0-9.\-]")


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

    Handles preformatted strings (``"$329,900"`` -> ``329900``), numeric
    values, and ``None``.  Non-positive results (Zillow uses ``0``/``"$0"``
    to mean "unknown") are returned as ``None``.

    Args:
        value: Raw price, e.g. ``"$329,900"``, ``329900``, ``None``.

    Returns:
        int | None: The price in whole dollars, or None if unknown.
    """
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


def normalize_listing(listing: dict[str, Any]) -> dict[str, Any]:
    """Flatten a raw Zillow listing into a normalized dict.

    Prefers top-level fields and falls back to ``hdpData.homeInfo`` when a
    field is missing.  Numeric fields are coerced; unknown values become
    ``None`` so callers never have to deal with ``"$0"`` strings or missing
    keys.

    Args:
        listing: A single raw entry from ``mapResults`` / ``listResults``.

    Returns:
        dict[str, Any]: A new dict with these keys: ``zpid``, ``price``,
        ``price_label``, ``beds``, ``baths``, ``area``, ``address``,
        ``street``, ``city``, ``state``, ``zipcode``, ``latitude``,
        ``longitude``, ``home_type``, ``status``, ``status_type``,
        ``detail_url``, ``image_url``, ``zestimate``, ``rent_zestimate``.
    """
    if not isinstance(listing, dict):
        raise TypeError(f"expected a listing dict, got {type(listing).__name__}")

    info = (listing.get("hdpData") or {}).get("homeInfo") or {}
    lat_long = listing.get("latLong") or {}

    price = parse_price(listing.get("price"))
    if price is None:
        price = parse_price(info.get("price"))

    detail_url = listing.get("detailUrl")
    if detail_url and detail_url.startswith("/"):
        detail_url = _BASE_URL + detail_url

    return {
        "zpid": _to_number(listing.get("zpid")),
        "price": price,
        "price_label": listing.get("priceLabel"),
        "beds": _first(_to_number(listing.get("beds")), _to_number(info.get("bedrooms"))),
        "baths": _first(_to_number(listing.get("baths")), _to_number(info.get("bathrooms"))),
        "area": _first(_to_number(listing.get("area")), _to_number(info.get("livingArea"))),
        "address": listing.get("address"),
        "street": info.get("streetAddress"),
        "city": info.get("city"),
        "state": _first(listing.get("addressState"), info.get("state")),
        "zipcode": info.get("zipcode"),
        "latitude": _first(lat_long.get("latitude"), info.get("latitude")),
        "longitude": _first(lat_long.get("longitude"), info.get("longitude")),
        "home_type": info.get("homeType"),
        "status": _first(listing.get("statusText"), info.get("homeStatus")),
        "status_type": listing.get("statusType"),
        "detail_url": detail_url,
        "image_url": listing.get("imgSrc"),
        "zestimate": parse_price(info.get("zestimate")),
        "rent_zestimate": parse_price(info.get("rentZestimate")),
    }


def normalize_listings(listings: Any) -> list[dict[str, Any]]:
    """Normalize an iterable of raw listings, skipping non-dict entries.

    Args:
        listings: Iterable of raw listing dicts.

    Returns:
        list[dict[str, Any]]: Normalized listings.
    """
    return [normalize_listing(item) for item in (listings or []) if isinstance(item, dict)]
