"""Deriving the stored detail figures from a raw property-detail payload.

The v4->v5 migration and the property-detail write path derive the same
numbers from the same Zillow payload: an HOA fee normalized to a monthly
figure, and the newest tax-history entry split into its year, paid, and
assessed value.  Both live here so the two paths cannot drift apart on the
rule, the field order, or the value each rule yields.
"""

import json
from collections.abc import Mapping, Sequence
from typing import Any, NamedTuple

from pyzill.values import to_number

__all__ = [
    "TaxLatest",
    "hoa_monthly",
    "int_if_whole",
    "tax_latest",
    "tax_latest_from_json",
]

# The aliases Zillow uses for one figure, in the order they are preferred.  A
# key that is present but not a number (null, or text) falls through to the
# next alias instead of stopping the search.
_TAX_PAID_KEYS = ("taxPaid", "taxPaidAmount", "tax", "taxAmount")
_TAX_ASSESSED_KEYS = ("taxAssessedValue", "assessedValue", "taxAssessed")
_TAX_YEAR_KEYS = ("year",)


class TaxLatest(NamedTuple):
    """The newest tax-history entry, in one fixed field order.

    Attribute access (``latest.year``) is the point: the migration and the
    write path used to return their values in opposite tuple orders, so a
    caller could swap the year and the paid figure without noticing.
    """

    year: int | float | None
    paid: int | float | None
    assessed: int | float | None


def int_if_whole(value: Any) -> Any:
    """Return a whole float as an int, so 2023.0 is stored as 2023.

    The target columns have INTEGER affinity, so SQLite stores either value
    the same way; returning an int keeps the value the caller holds equal to
    the value the database reads back.
    """
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def hoa_monthly(hoa_fee: int | float | None, hoa_fee_unit: str | None) -> int | float | None:
    """Normalize an HOA fee to a monthly figure by the unit it was stated in.

    ``monthly`` stays as-is, ``annual`` divides by 12, ``quarterly`` divides
    by 3, and an unknown unit yields ``None`` rather than inventing a value.
    No HOA is represented by ``None``.
    """
    if hoa_fee is None:
        return None
    if hoa_fee_unit == "monthly":
        return hoa_fee
    if hoa_fee_unit == "annual":
        return round(float(hoa_fee) / 12.0, 2)
    if hoa_fee_unit == "quarterly":
        return round(float(hoa_fee) / 3.0, 2)
    return None


def _first_number(entry: Mapping[str, Any], keys: Sequence[str]) -> int | float | None:
    """Return the first alias in ``keys`` whose value is a number.

    A key that is present but holds ``None`` or something non-numeric is
    skipped: Zillow sends a null ``taxPaid`` alongside a populated ``tax`` on
    some pages, and stopping at the null would discard the figure it sent.
    """
    for key in keys:
        number = to_number(entry.get(key))
        if number is not None:
            return number
    return None


def tax_latest(entries: object) -> TaxLatest:
    """Return the year, paid, and assessed figures of the newest tax entry.

    ``entries`` is an already-parsed ``tax_history`` value.  Only the last
    entry is read, and only when it is a dict; anything else is a payload
    with no usable tax figures.
    """
    if not isinstance(entries, list) or not entries:
        return TaxLatest(None, None, None)
    latest = entries[-1]
    if not isinstance(latest, dict):
        return TaxLatest(None, None, None)
    return TaxLatest(
        year=int_if_whole(_first_number(latest, _TAX_YEAR_KEYS)),
        paid=int_if_whole(_first_number(latest, _TAX_PAID_KEYS)),
        assessed=int_if_whole(_first_number(latest, _TAX_ASSESSED_KEYS)),
    )


def tax_latest_from_json(raw: str | None) -> TaxLatest:
    """Parse a stored ``tax_history`` JSON string and return its newest entry.

    A missing, empty, or unparsable value is a payload with no tax figures,
    not an error: legacy rows carry whatever the old parser stored.
    """
    if not raw:
        return TaxLatest(None, None, None)
    try:
        entries = json.loads(raw)
    except (ValueError, TypeError):
        return TaxLatest(None, None, None)
    return tax_latest(entries)
