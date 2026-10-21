"""Coercing Zillow's raw values into the scalars the normalized shapes use.

Zillow is inconsistent about types: a price arrives as ``"$329,900"``, a
zipcode sometimes as the number ``12305``, a flag as the string ``"true"``.
These helpers apply one rule per value kind, so the listing shape and the
property-detail shape cannot drift apart on how a value is read.
"""

import re
from typing import Any

__all__ = ["parse_price", "to_number", "to_quantity", "to_text", "to_zpid"]


# Anything that is not a digit, minus sign or decimal point.
_NOT_NUMBER = re.compile(r"[^0-9.\-]")
_PRICE_SUFFIXES = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}
# Trailing rental / "starting at" adornments attached to a price.
_RENTAL_SUFFIX = re.compile(r"(?:/month(?:ly)?|/mo|\+)\s*$", re.IGNORECASE)
# A measurement with its unit spelled out: "1,742 sqft", "0.25 acres".
_QUANTITY_RE = re.compile(r"^(?P<number>[-+]?[\d,]*\.?\d+)\s*(?P<unit>[A-Za-z][A-Za-z.\s]*)?$")


def to_number(value: Any) -> int | float | None:
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


def to_quantity(value: Any) -> tuple[int | float | None, str | None]:
    """Split a Zillow measurement into its number and the unit it was stated in.

    Zillow states some measurements as a number with the unit spelled out in
    the same string ("1,742 sqft", "0.25 acres").  Splitting the two apart
    keeps the unit from being silently dropped: a number that arrived with
    "sqft" attached and a bare number are not the same measurement, and only
    the first one says what it is.

    A value that carries no unit (a number, or a string of digits) yields a
    ``None`` unit.  A value that is not a measurement at all yields
    ``(None, None)``.

    Args:
        value: Raw measurement, e.g. ``"1,742 sqft"``, ``1742``, ``None``.

    Returns:
        tuple: the number and the lower-cased unit, either of which may be
        ``None``.
    """
    if value is None or isinstance(value, bool):
        return None, None
    if isinstance(value, (int, float)):
        return value, None
    if not isinstance(value, str):
        return None, None
    match = _QUANTITY_RE.match(value.strip())
    if match is None:
        return None, None
    number = to_number(match.group("number"))
    if number is None:
        return None, None
    unit = (match.group("unit") or "").strip().lower() or None
    return number, unit


def to_text(value: Any) -> str | None:
    """Coerce a Zillow scalar into a string, or None if it is not one.

    Zillow sometimes sends a number where a string belongs (a numeric
    ``addressZipcode``, a numeric ``priceLabel``).  Rendering those as
    strings keeps the normalized shapes honest: a zipcode that stayed an
    int would never match the zipcode a search was made for, and the
    shapes promise ``str`` or ``None`` for every string field.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    return None


def to_zpid(value: Any) -> int | None:
    """Coerce a Zillow value into a zpid, or None when it is not one.

    A zpid is always an integer.  Zillow also stamps a building card's
    ``buildingId`` (a lat-lng string such as ``"47.61503--122.34274"``)
    into the ``zpid`` field, which is not a zpid at all.
    """
    number = to_number(value)
    if isinstance(number, float):
        return int(number) if number.is_integer() else None
    return number


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
            num = to_number(numeric_text)
            if num is not None:
                num *= _PRICE_SUFFIXES[suffix]
        elif re.search(r"[A-Za-z]", cleaned):
            return None
        else:
            num = to_number(cleaned)
    else:
        num = to_number(value)
    if num is None:
        return None
    price = round(num)
    return price if price > 0 else None
