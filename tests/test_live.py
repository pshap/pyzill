"""Opt-in smoke checks against Zillow's current public response shapes."""

import os

import pytest

from pyzill import BBox, for_sale, get_from_home_id

pytestmark = pytest.mark.live


def _live_enabled() -> bool:
    return os.environ.get("PYZILL_LIVE") == "1"


def test_live_home_detail_shape() -> None:
    if not _live_enabled():
        pytest.skip("set PYZILL_LIVE=1 to enable live Zillow checks")
    property_id = os.environ.get("PYZILL_LIVE_HOME_ID")
    if not property_id or not property_id.isdigit():
        pytest.skip("set PYZILL_LIVE_HOME_ID to a current home zpid")

    result = get_from_home_id(int(property_id), max_retries=0)

    assert isinstance(result, dict)
    assert "zpid" in result or "address" in result


def test_live_small_search_shape() -> None:
    if not _live_enabled():
        pytest.skip("set PYZILL_LIVE=1 to enable live Zillow checks")

    result = for_sale(
        search_value="Schenectady NY",
        bbox=BBox(north=42.83, east=-73.92, south=42.81, west=-73.95),
        max_retries=0,
    )

    assert isinstance(result, dict)
    assert isinstance(result.get("mapResults"), list)
    assert isinstance(result.get("listResults"), list)
