"""Pulling the real listings out of a Zillow response.

A search response holds the listings in one of two places, and one of them
is often nothing but building cards.  This module digs the listing list out
of the response and applies the one rule that decides which of its rows are
listings.
"""

from collections.abc import Mapping
from typing import Any

from pyzill.normalize import Listing, is_building_result, normalize_listing

__all__ = ["extract_listings", "query_zipcode", "select_listings", "usable_row_count"]

_ZIPCODE_LENGTH = 5


def query_zipcode(query: str) -> str | None:
    """Return the query's zipcode, if it is one."""
    zipcode = query.strip()
    return zipcode if len(zipcode) == _ZIPCODE_LENGTH and zipcode.isascii() and zipcode.isdecimal() else None


def usable_row_count(rows: list[Any], *, zipcode: str | None = None) -> int:
    """Count rows that normalize to a real listing with an integer zpid.

    ``isBuilding`` cards are the common case, but Zillow sometimes stamps a
    building card with a non-numeric ``zpid`` (a ``buildingId`` like
    ``"47.61503--122.34274"``), so the check is on the normalized zpid
    rather than the flag alone.
    """
    return len(_usable_listings(rows, zipcode=zipcode))


def _usable_listings(rows: list[Any], *, zipcode: str | None = None) -> list[tuple[Mapping[str, Any], Listing]]:
    """Return the (raw, normalized) pairs of the rows that are real listings.

    A row counts as a listing when it is a dict, is not a building-only
    card, and normalizes to a listing with an integer zpid.  Rows whose
    zipcode is not the one searched for are dropped as well.

    Every caller that needs "the real listings in these rows" goes through
    this function, so the rule that picks between ``mapResults`` and
    ``listResults`` and the filter that follows it can never disagree.
    """
    usable: list[tuple[Mapping[str, Any], Listing]] = []
    for row in rows:
        if not isinstance(row, dict) or is_building_result(row):
            continue
        normalized = normalize_listing(row)
        if normalized is None or normalized["zpid"] is None:
            continue
        if zipcode is not None and normalized["zipcode"] != zipcode:
            continue
        usable.append((row, normalized))
    return usable


def select_listings(
    data: Any, *, wrap_single: bool, zipcode: str | None
) -> tuple[list[Any], list[tuple[Mapping[str, Any], Listing]]]:
    """Return a response's listing list and the real listings in it.

    Digs through nested response containers (``cat1`` / ``cat2`` /
    ``searchResults``) so a full saved search body works, not just the
    unwrapped ``mapResults`` shape. With wrap_single=True, a dict that has no
    results key is treated as one listing; otherwise it yields an empty list.

    When both ``mapResults`` and ``listResults`` are present, the list with
    real listings wins. Zillow's ``mapResults`` often contain only
    building-level cards (``isBuilding``, no zpid), while ``listResults``
    hold the individual units, so returning the first key blindly would drop
    every usable row in those areas. A tie goes to the sidebar list: the map
    rows carry an abbreviated property (a ``daysOnZillow`` stamped ``-1``)
    while the sidebar rows carry the same properties with their real values.

    Raises:
        ValueError: If the payload is neither an object nor an array.
    """
    if isinstance(data, list):
        return data, _usable_listings(data, zipcode=zipcode)
    if isinstance(data, dict):
        candidates: list[list[Any]] = []
        # The sidebar list is listed first so it wins a tie: ``max`` keeps
        # the first of equally sized candidates.
        for key in ("listResults", "mapResults"):
            if key not in data:
                continue
            value = data[key]
            candidates.append(value if isinstance(value, list) else [])
        if candidates:
            # Each candidate is normalized once, and the winner's result is
            # reused, so no row is normalized twice.
            scored = [(_usable_listings(rows, zipcode=zipcode), rows) for rows in candidates]
            usable, rows = max(scored, key=lambda candidate: len(candidate[0]))
            return rows, usable
        for key in ("cat1", "cat2", "searchResults"):
            if key in data:
                # It's a response envelope: honor its (possibly empty) contents
                # rather than treating the envelope itself as a listing.
                return select_listings(data[key], wrap_single=wrap_single, zipcode=zipcode)
        if wrap_single:
            return [data], _usable_listings([data], zipcode=zipcode)
        return [], []
    raise ValueError("Input must be a JSON object or array")


def extract_listings(data: Any, *, wrap_single: bool = False, zipcode: str | None = None) -> list[Any]:
    """Pull the listing list out of a Zillow response (or a bare list).

    The list is returned as Zillow sent it, building cards included; use
    :func:`~pyzill.normalize.normalize_listings` to drop those.

    Raises:
        ValueError: If the payload is neither an object nor an array.
    """
    rows, _ = select_listings(data, wrap_single=wrap_single, zipcode=zipcode)
    return rows
