import time

import pytest

from pyzill import CONTINENTAL_US, BBox, exceptions, search


def test_bbox_maps_and_splits():
    bbox = BBox(north=10, east=10, south=0, west=0)

    assert bbox.to_map_bounds() == {"north": 10, "east": 10, "south": 0, "west": 0}
    assert bbox.split() == (
        BBox(north=10, east=5, south=5, west=0),
        BBox(north=10, east=10, south=5, west=5),
        BBox(north=5, east=5, south=0, west=0),
        BBox(north=5, east=10, south=0, west=5),
    )


def test_continental_us_is_the_default_bbox():
    assert search._resolve_bbox(None) == CONTINENTAL_US


@pytest.mark.parametrize(
    ("north", "east", "south", "west"),
    [(0, 10, 1, 0), (10, 0, 0, 1)],
)
def test_bbox_rejects_inverted_bounds(north, east, south, west):
    with pytest.raises(ValueError, match="north/east"):
        BBox(north=north, east=east, south=south, west=west)


def test_search_accepts_bbox(fake_http, search_success_body):
    fake_http.install("PUT", json_data=search_success_body)
    search.for_sale(bbox=BBox(north=42.85, east=-73.90, south=42.78, west=-74.00))

    assert fake_http.calls[-1]["json"]["searchQueryState"]["mapBounds"] == {
        "north": 42.85,
        "east": -73.90,
        "south": 42.78,
        "west": -74.00,
    }


def test_for_sale_success(fake_http, search_success_body):
    fake_http.install("PUT", json_data=search_success_body)

    result = search.for_sale(
        search_value="Schenectady NY",
        bbox=BBox(north=42.85, east=-73.90, south=42.78, west=-74.00),
        zoom_value=12,
    )

    assert len(result["mapResults"]) == 2
    assert result["mapResults"][0]["zpid"] == 348914888

    call = fake_http.calls[-1]
    assert call["url"] == search._SEARCH_URL
    assert call["impersonate"] == "chrome136"
    assert call["timeout"] == 30.0
    assert call["headers"]["Referer"] == "https://www.zillow.com/"
    assert call["json"]["searchQueryState"]["usersSearchTerm"] == "Schenectady NY"


def test_for_sale_403_raises_zillow_blocked_error(fake_http):
    px_html = "<html><body>Please verify you are a human. perimeterx px-captcha</body></html>"
    fake_http.install("PUT", status_code=403, text=px_html, headers={"server": "PerimeterX"})

    with pytest.raises(exceptions.ZillowBlockedError) as exc_info:
        search.for_sale(search_value="Schenectady NY")

    err = exc_info.value
    assert err.status_code == 403
    assert "PerimeterX" in str(err) or "px-captcha" in str(err).lower()


def test_search_missing_results_raises_zillow_blocked_error(fake_http):
    fake_http.install("PUT", json_data={"cat1": {}})

    with pytest.raises(exceptions.ZillowBlockedError):
        search.for_sale(search_value="Schenectady NY")


def test_search_invalid_json_raises_zillow_blocked_error(fake_http):
    fake_http.install("PUT", text="not json")

    with pytest.raises(exceptions.ZillowBlockedError):
        search.for_sale(search_value="Schenectady NY")


def test_search_5xx_retries_then_raises(fake_http, monkeypatch):
    fake_http.install("PUT", status_code=503, text="Service Unavailable")
    monkeypatch.setattr(time, "sleep", lambda *a, **k: None)

    with pytest.raises(exceptions.ZillowBlockedError):
        search.for_sale(search_value="Schenectady NY", max_retries=2)

    assert len(fake_http.calls) == 3  # initial + 2 retries


def test_search_defaults_applied(fake_http, search_success_body):
    fake_http.install("PUT", json_data=search_success_body)
    search.search(search_value="anywhere")

    query_state = fake_http.calls[-1]["json"]["searchQueryState"]
    bounds = query_state["mapBounds"]
    assert bounds["north"] == 49.38
    assert bounds["south"] == 24.54
    assert "mapZoom" not in query_state


def test_search_does_not_mutate_filter_state(fake_http, search_success_body):
    fake_http.install("PUT", json_data=search_success_body)
    filter_state = {"isAllHomes": {"value": True}}

    search.search(filter_state=filter_state, min_beds=2)

    assert filter_state == {"isAllHomes": {"value": True}}


def test_proxy_env_fallback(fake_http, monkeypatch, search_success_body):
    fake_http.install("PUT", json_data=search_success_body)
    monkeypatch.setenv("ZILLOW_PROXY", "http://user:pass@proxy.example:8080")

    search.for_sale(search_value="x")
    assert fake_http.calls[-1]["proxies"] == {
        "http": "http://user:pass@proxy.example:8080",
        "https": "http://user:pass@proxy.example:8080",
    }

    search.for_sale(search_value="x", proxy_url="http://explicit:1@proxy.example:9090")
    assert fake_http.calls[-1]["proxies"]["http"] == "http://explicit:1@proxy.example:9090"


def test_iter_search_yields_and_limits(fake_http, search_success_body):
    fake_http.install("PUT", json_data=search_success_body)

    items = list(search.iter_search(search.for_sale, search_value="x"))
    assert len(items) == 2

    items = list(search.iter_search(search.for_sale, max_items=1, search_value="x"))
    assert len(items) == 1
    assert items[0]["zpid"] == 348914888


def test_iter_search_default_fn(fake_http, search_success_body):
    fake_http.install("PUT", json_data=search_success_body)

    items = list(search.iter_search(search_value="x", max_items=1))
    assert len(items) == 1
    assert items[0]["zpid"] == 348914888


def test_iter_search_normalize(fake_http, search_success_body):
    fake_http.install("PUT", json_data=search_success_body)

    items = list(search.iter_search(search.for_sale, normalize=True, search_value="x"))
    assert len(items) == 2
    # normalized shape: flat keys, numeric-or-None
    assert items[0]["zpid"] == 348914888
    assert items[0]["price"] is None
    assert items[0]["beds"] is None
    assert "hdpData" not in items[0]


def test_for_rent_sets_rent_filters(fake_http, search_success_body):
    fake_http.install("PUT", json_data=search_success_body)
    search.for_rent(search_value="x", is_room=True, is_entire_place=True)

    filter_state = fake_http.calls[-1]["json"]["searchQueryState"]["filterState"]
    assert filter_state["isForRent"] == {"value": True}
    assert filter_state["isRoomForRent"] == {"value": True}
    assert "isEntirePlaceForRent" not in filter_state


def test_sold_sets_sold_filters(fake_http, search_success_body):
    fake_http.install("PUT", json_data=search_success_body)
    search.sold(search_value="x")

    filter_state = fake_http.calls[-1]["json"]["searchQueryState"]["filterState"]
    assert filter_state["isRecentlySold"] == {"value": True}
    assert filter_state["isForRent"] == {"value": False}


def test_listing_type_filters_match_current_payloads(fake_http, search_success_body):
    fake_http.install("PUT", json_data=search_success_body)

    search.for_sale(search_value="x")
    search.for_rent(search_value="x")
    search.sold(search_value="x")

    states = [call["json"]["searchQueryState"]["filterState"] for call in fake_http.calls[-3:]]
    assert states == [
        {
            "sortSelection": {"value": "globalrelevanceex"},
            "isAllHomes": {"value": True},
        },
        {
            "sortSelection": {"value": "priorityscore"},
            "isNewConstruction": {"value": False},
            "isForSaleForeclosure": {"value": False},
            "isForSaleByOwner": {"value": False},
            "isForSaleByAgent": {"value": False},
            "isForRent": {"value": True},
            "isComingSoon": {"value": False},
            "isAuction": {"value": False},
            "isAllHomes": {"value": True},
            "isEntirePlaceForRent": {"value": False},
        },
        {
            "sortSelection": {"value": "globalrelevanceex"},
            "isNewConstruction": {"value": False},
            "isForSaleForeclosure": {"value": False},
            "isForSaleByOwner": {"value": False},
            "isForSaleByAgent": {"value": False},
            "isForRent": {"value": False},
            "isComingSoon": {"value": False},
            "isAuction": {"value": False},
            "isAllHomes": {"value": True},
            "isRecentlySold": {"value": True},
        },
    ]


def test_split_bbox_quadrants():
    quadrants = search.split_bbox(BBox(north=10, east=10, south=0, west=0))
    assert len(quadrants) == 4
    for q in quadrants:
        assert isinstance(q, BBox)
