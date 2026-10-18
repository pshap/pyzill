"""Opt-in smoke checks against Zillow's current public response shapes.

Run with ``PYZILL_LIVE=1 pytest -m live``. These hit real, rate-limited
endpoints; their job is to catch payload drift between releases, not to run
in CI. The Schenectady NY bbox is residential enough to return real
listings -- a dense downtown patch can come back as *only* building cards,
which normalization now drops (see ``is_building_result``).
"""

import os

import pytest

from pyzill import BBox, for_rent, for_sale, get_from_home_id, normalize_listings, sold
from pyzill.geocode import geocode

pytestmark = pytest.mark.live

# Residential patch used by scripts/capture_fixture.py: dense enough for
# results, residential enough to include individual listings.
_SMOKE_BBOX = BBox(north=42.83, east=-73.92, south=42.81, west=-73.95)


def _live_enabled() -> bool:
    return os.environ.get("PYZILL_LIVE") == "1"


def _assert_search_shape(result: dict) -> None:
    assert isinstance(result, dict)
    assert isinstance(result.get("mapResults"), list)
    assert isinstance(result.get("listResults"), list)


def _first_real_zpid(result: dict) -> int | None:
    for row in normalize_listings(result.get("mapResults") or []):
        zpid = row["zpid"]
        if isinstance(zpid, int):
            return zpid
    return None


def test_live_sale_search_yields_normalized_listings() -> None:
    if not _live_enabled():
        pytest.skip("set PYZILL_LIVE=1 to enable live Zillow checks")

    result = for_sale(search_value="Schenectady NY", bbox=_SMOKE_BBOX, max_retries=0)
    _assert_search_shape(result)

    listings = normalize_listings(result.get("mapResults") or [])
    assert listings, "sale search returned no usable listings"
    assert all(isinstance(listing["zpid"], int) for listing in listings)
    assert any(listing["address"] or listing["price"] is not None for listing in listings)


def test_live_rent_search_yields_only_real_listings() -> None:
    if not _live_enabled():
        pytest.skip("set PYZILL_LIVE=1 to enable live Zillow checks")

    result = for_rent(search_value="Schenectady NY", bbox=_SMOKE_BBOX, max_retries=0)
    _assert_search_shape(result)

    # Rental areas can be building-dominated; an empty normalized list is a
    # valid outcome, junk rows (zpid=None) are not.
    listings = normalize_listings(result.get("mapResults") or [])
    assert all(isinstance(listing["zpid"], int) for listing in listings)


def test_live_sold_search_yields_normalized_listings() -> None:
    if not _live_enabled():
        pytest.skip("set PYZILL_LIVE=1 to enable live Zillow checks")

    result = sold(search_value="Schenectady NY", bbox=_SMOKE_BBOX, max_retries=0)
    _assert_search_shape(result)

    listings = normalize_listings(result.get("mapResults") or [])
    assert listings, "sold search returned no usable listings"
    assert all(isinstance(listing["zpid"], int) for listing in listings)


def test_live_home_detail_shape() -> None:
    if not _live_enabled():
        pytest.skip("set PYZILL_LIVE=1 to enable live Zillow checks")

    search_result = for_sale(search_value="Schenectady NY", bbox=_SMOKE_BBOX, max_retries=0)
    zpid = _first_real_zpid(search_result)
    if zpid is None:
        pytest.skip("sale search returned only building cards; no zpid to fetch")

    result = get_from_home_id(zpid, max_retries=0)
    assert isinstance(result, dict)
    assert result.get("zpid") or result.get("address")


def test_live_geocode_smoke(tmp_path) -> None:
    if not _live_enabled():
        pytest.skip("set PYZILL_LIVE=1 to enable live Nominatim checks")

    # Bypass the on-disk cache so this exercises the live Nominatim path.
    result = geocode("Schenectady NY", cache_path=tmp_path / "geocode.json")
    assert result.bbox.north > result.bbox.south
    assert result.bbox.east > result.bbox.west  # US longitudes: east is greater (less negative)
