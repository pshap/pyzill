import inspect
import time
import warnings

import pytest

from pyzill import CONTINENTAL_US, BBox, exceptions, normalize_listings, search


def test_for_rent_exposes_rental_flags_as_keyword_parameters():
    parameters = inspect.signature(search.for_rent).parameters

    assert parameters["is_room"].default is False
    assert parameters["is_entire_place"].default is False


def test_bbox_maps_and_splits():
    bbox = BBox(north=10, east=10, south=0, west=0)

    assert bbox.to_map_bounds() == {"north": 10, "east": 10, "south": 0, "west": 0}
    assert bbox.split() == (
        BBox(north=10, east=5, south=5, west=0),
        BBox(north=10, east=10, south=5, west=5),
        BBox(north=5, east=5, south=0, west=0),
        BBox(north=5, east=10, south=0, west=5),
    )


def test_missing_bbox_requires_explicit_nationwide_opt_in():
    with pytest.raises(ValueError, match="bbox is required"):
        search._resolve_bbox(None)

    assert search._resolve_bbox(None, allow_nationwide=True) == CONTINENTAL_US


def test_search_without_bbox_raises_before_any_request(fake_http):
    fake_http.install("PUT", json_data={})

    with pytest.raises(ValueError, match="bbox is required"):
        search.for_sale(search_value="12305")

    assert fake_http.calls == []


def test_allow_nationwide_sends_continental_us_bounds(fake_http, search_success_body):
    fake_http.install("PUT", json_data=search_success_body)

    search.for_sale(search_value="12305", allow_nationwide=True)

    query_state = fake_http.calls[-1]["json"]["searchQueryState"]
    assert query_state["mapBounds"] == CONTINENTAL_US.to_map_bounds()


@pytest.mark.parametrize("fn", [search.for_rent, search.sold])
def test_rent_and_sold_also_require_bbox_or_nationwide_opt_in(fn):
    with pytest.raises(ValueError, match="bbox is required"):
        fn(search_value="x")


def test_iter_sale_propagates_missing_bbox_error():
    with pytest.raises(ValueError, match="bbox is required"):
        list(search.iter_sale(search_value="x"))


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
        search.for_sale(search_value="Schenectady NY", allow_nationwide=True)

    err = exc_info.value
    assert err.status_code == 403
    assert "PerimeterX" in str(err) or "px-captcha" in str(err).lower()


def test_search_missing_results_raises_zillow_blocked_error(fake_http):
    fake_http.install("PUT", json_data={"cat1": {}})

    with pytest.raises(exceptions.ZillowBlockedError):
        search.for_sale(search_value="Schenectady NY", allow_nationwide=True)


def test_search_invalid_json_raises_zillow_blocked_error(fake_http):
    fake_http.install("PUT", text="not json")

    with pytest.raises(exceptions.ZillowBlockedError):
        search.for_sale(search_value="Schenectady NY", allow_nationwide=True)


def test_search_5xx_retries_then_raises(fake_http, monkeypatch):
    fake_http.install("PUT", status_code=503, text="Service Unavailable")
    monkeypatch.setattr(time, "sleep", lambda *a, **k: None)

    with pytest.raises(exceptions.ZillowBlockedError):
        search.for_sale(search_value="Schenectady NY", allow_nationwide=True, max_retries=2)

    assert len(fake_http.calls) == 3  # initial + 2 retries


def test_search_defaults_applied(fake_http, search_success_body):
    fake_http.install("PUT", json_data=search_success_body)
    search.search(search_value="anywhere", allow_nationwide=True)

    query_state = fake_http.calls[-1]["json"]["searchQueryState"]
    bounds = query_state["mapBounds"]
    assert bounds["north"] == 49.38
    assert bounds["south"] == 24.54
    assert "mapZoom" not in query_state


def test_search_does_not_mutate_filter_state(fake_http, search_success_body):
    fake_http.install("PUT", json_data=search_success_body)
    filter_state = {"isAllHomes": {"value": True}}

    search.search(filter_state=filter_state, min_beds=2, allow_nationwide=True)

    assert filter_state == {"isAllHomes": {"value": True}}


def test_search_rejects_unknown_runtime_keywords():
    with pytest.raises(TypeError, match="bogus") as exc_info:
        search.for_sale(search_value="x", min_bed=2, bogus=1)  # type: ignore[call-arg]

    assert "min_beds" in str(exc_info.value)


def test_proxy_env_fallback(fake_http, monkeypatch, search_success_body):
    fake_http.install("PUT", json_data=search_success_body)
    monkeypatch.setenv("ZILLOW_PROXY", "http://user:pass@proxy.example:8080")

    search.for_sale(search_value="x", allow_nationwide=True)
    assert fake_http.calls[-1]["proxies"] == {
        "http": "http://user:pass@proxy.example:8080",
        "https": "http://user:pass@proxy.example:8080",
    }

    search.for_sale(search_value="x", allow_nationwide=True, proxy_url="http://explicit:1@proxy.example:9090")
    assert fake_http.calls[-1]["proxies"]["http"] == "http://explicit:1@proxy.example:9090"


def test_iter_search_yields_and_limits(fake_http, search_success_body):
    fake_http.install("PUT", json_data=search_success_body)

    items = list(search.iter_sale(search_value="x", allow_nationwide=True))
    assert len(items) == 2

    items = list(search.iter_sale(max_items=1, search_value="x", allow_nationwide=True))
    assert len(items) == 1
    assert items[0]["zpid"] == 348914888


def test_iter_search_default_fn(fake_http, search_success_body):
    fake_http.install("PUT", json_data=search_success_body)

    items = list(search.iter_search(search_value="x", max_items=1, allow_nationwide=True))
    assert len(items) == 1
    assert items[0]["zpid"] == 348914888


def test_tiled_search_splits_capped_tiles_and_deduplicates():
    root = BBox(north=10, east=10, south=0, west=0)
    calls: list[BBox] = []

    def fake_search(*, bbox: BBox, **kwargs):
        calls.append(bbox)
        if bbox == root:
            return {"mapResults": [{"zpid": index} for index in range(500)]}
        return {"mapResults": [{"zpid": 500}, {"zpid": 501}], "listResults": []}

    result = search.tiled_search(fake_search, bbox=root, max_depth=1, search_value="x")

    assert len(calls) == 5
    assert len(result["mapResults"]) == 502
    assert [item["zpid"] for item in result["mapResults"]][-2:] == [500, 501]


def test_tiled_search_reports_cap_at_max_depth():
    capped: list[bool] = []
    search.tiled_search(
        lambda **kwargs: {"mapResults": [{"zpid": index} for index in range(500)]},
        bbox=BBox(north=10, east=10, south=0, west=0),
        max_depth=0,
        on_cap=lambda: capped.append(True),
    )

    assert capped == [True]


def test_tiled_search_stops_at_request_budget_and_warns():
    calls: list[BBox] = []

    def always_capped(**kwargs):
        calls.append(kwargs["bbox"])
        return {"mapResults": [{"zpid": len(calls) * 1000 + index} for index in range(500)]}

    with pytest.warns(UserWarning, match="request budget of 3 exhausted"):
        result = search.tiled_search(
            always_capped,
            bbox=BBox(north=10, east=10, south=0, west=0),
            max_depth=4,
            max_requests=3,
        )

    assert len(calls) == 3
    assert result["budget_exhausted"] is True
    assert len(result["mapResults"]) == 1500


def test_tiled_search_on_budget_callback_overrides_warning():
    def always_capped(**kwargs):
        return {"mapResults": [{"zpid": index} for index in range(500)]}

    seen: list[str] = []
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # no UserWarning should escape when on_budget is set
        result = search.tiled_search(
            always_capped,
            bbox=BBox(north=10, east=10, south=0, west=0),
            max_depth=4,
            max_requests=2,
            on_budget=lambda: seen.append("budget"),
        )

    assert result["budget_exhausted"] is True
    assert seen == ["budget"]


def test_tiled_search_default_budget_of_fifty_requests():
    calls = [0]

    def always_capped(**kwargs):
        calls[0] += 1
        return {"mapResults": [{"zpid": calls[0] * 1000 + index} for index in range(500)]}

    with pytest.warns(UserWarning, match="budget of 50 exhausted"):
        result = search.tiled_search(
            always_capped,
            bbox=BBox(north=10, east=10, south=0, west=0),
            max_depth=3,
        )

    assert calls[0] == 50
    assert result["budget_exhausted"] is True


def test_tiled_search_flags_partial_results_when_budget_unused():
    result = search.tiled_search(
        lambda **kwargs: {"mapResults": [{"zpid": 1}]},
        bbox=BBox(north=10, east=10, south=0, west=0),
    )

    assert result["budget_exhausted"] is False


def test_tiled_search_progress_callback_reports_tiles_and_listings():
    progress: list[tuple[int, int]] = []

    search.tiled_search(
        lambda **kwargs: {"mapResults": [{"zpid": 1}, {"zpid": 2}]},
        bbox=BBox(north=10, east=10, south=0, west=0),
        on_progress=lambda used, total: progress.append((used, total)),
    )

    assert progress == [(1, 2)]


def test_tiled_search_rejects_invalid_budget():
    with pytest.raises(ValueError, match="max_requests"):
        search.tiled_search(
            lambda **kwargs: {},
            bbox=BBox(north=10, east=10, south=0, west=0),
            max_requests=0,
        )


def test_normalize_listings_can_normalize_iteration(fake_http, search_success_body):
    fake_http.install("PUT", json_data=search_success_body)

    items = list(normalize_listings(search.iter_sale(search_value="x", allow_nationwide=True)))
    assert len(items) == 2
    # normalized shape: flat keys, numeric-or-None
    assert items[0]["zpid"] == 348914888
    assert items[0]["price"] is None
    assert items[0]["beds"] is None
    assert "hdpData" not in items[0]


def test_for_rent_sets_rent_filters(fake_http, search_success_body):
    fake_http.install("PUT", json_data=search_success_body)
    search.for_rent(search_value="x", allow_nationwide=True, is_room=True, is_entire_place=True)

    filter_state = fake_http.calls[-1]["json"]["searchQueryState"]["filterState"]
    assert filter_state["isForRent"] == {"value": True}
    assert filter_state["isRoomForRent"] == {"value": True}
    assert "isEntirePlaceForRent" not in filter_state


def test_sold_sets_sold_filters(fake_http, search_success_body):
    fake_http.install("PUT", json_data=search_success_body)
    search.sold(search_value="x", allow_nationwide=True)

    filter_state = fake_http.calls[-1]["json"]["searchQueryState"]["filterState"]
    assert filter_state["isRecentlySold"] == {"value": True}
    assert filter_state["isForRent"] == {"value": False}


def test_listing_type_filters_match_current_payloads(fake_http, search_success_body):
    fake_http.install("PUT", json_data=search_success_body)

    search.for_sale(search_value="x", allow_nationwide=True)
    search.for_rent(search_value="x", allow_nationwide=True)
    search.sold(search_value="x", allow_nationwide=True)

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


@pytest.mark.parametrize("fn", [search.for_sale, search.for_rent, search.sold])
def test_listing_type_functions_reject_filter_state(fake_http, fn):
    fake_http.install("PUT", json_data={"cat1": {"searchResults": {}}})

    with pytest.raises(ValueError, match="filter_state"):
        fn(filter_state={"isAllHomes": {"value": True}})
