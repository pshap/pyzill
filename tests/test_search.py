import pytest

from pyzill import exceptions, search
from tests.conftest import FakeResponse


def test_for_sale_success(monkeypatch, search_success_body):
    captured = {}

    def fake_put(url, json=None, headers=None, proxies=None, impersonate=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        captured["impersonate"] = impersonate
        captured["timeout"] = timeout
        return FakeResponse(status_code=200, json_data=search_success_body)

    monkeypatch.setattr(search.requests, "put", fake_put)

    result = search.for_sale(
        search_value="Schenectady NY",
        ne_lat=42.85,
        ne_long=-73.90,
        sw_lat=42.78,
        sw_long=-74.00,
        zoom_value=12,
    )

    assert len(result["mapResults"]) == 2
    assert result["mapResults"][0]["zpid"] == 348914888
    assert captured["impersonate"] == "chrome136"
    assert captured["timeout"] == 30.0
    assert captured["headers"]["Referer"] == "https://www.zillow.com/"
    assert captured["json"]["searchQueryState"]["usersSearchTerm"] == "Schenectady NY"


def test_for_sale_403_raises_zillow_blocked_error(monkeypatch):
    px_html = "<html><body>Please verify you are a human. perimeterx px-captcha</body></html>"

    def fake_put(*args, **kwargs):
        return FakeResponse(status_code=403, text=px_html, headers={"server": "PerimeterX"})

    monkeypatch.setattr(search.requests, "put", fake_put)

    with pytest.raises(exceptions.ZillowBlockedError) as exc_info:
        search.for_sale(search_value="Schenectady NY")

    err = exc_info.value
    assert err.status_code == 403
    assert "PerimeterX" in str(err) or "px-captcha" in str(err).lower()


def test_search_5xx_retries_then_raises(monkeypatch):
    calls = {"n": 0}

    def fake_put(*args, **kwargs):
        calls["n"] += 1
        return FakeResponse(status_code=503, text="Service Unavailable")

    monkeypatch.setattr(search.requests, "put", fake_put)
    monkeypatch.setattr(search.time, "sleep", lambda *a, **k: None)

    with pytest.raises(exceptions.ZillowBlockedError):
        search.for_sale(search_value="Schenectady NY", max_retries=2)

    assert calls["n"] == 3  # initial + 2 retries


def test_search_defaults_applied(monkeypatch, search_success_body):
    captured = {}

    def fake_put(url, json=None, headers=None, proxies=None, impersonate=None, timeout=None):
        captured["json"] = json
        return FakeResponse(status_code=200, json_data=search_success_body)

    monkeypatch.setattr(search.requests, "put", fake_put)
    search.search(search_value="anywhere")

    bounds = captured["json"]["searchQueryState"]["mapBounds"]
    assert bounds["north"] == 49.38
    assert bounds["south"] == 24.54
    assert "mapZoom" not in captured["json"]["searchQueryState"]


def test_proxy_env_fallback(monkeypatch, search_success_body):
    captured = {}

    def fake_put(url, json=None, headers=None, proxies=None, impersonate=None, timeout=None):
        captured["proxies"] = proxies
        return FakeResponse(status_code=200, json_data=search_success_body)

    monkeypatch.setattr(search.requests, "put", fake_put)
    monkeypatch.setenv("ZILLOW_PROXY", "http://user:pass@proxy.example:8080")

    search.for_sale(search_value="x")
    assert captured["proxies"] == {
        "http": "http://user:pass@proxy.example:8080",
        "https": "http://user:pass@proxy.example:8080",
    }

    captured.clear()
    search.for_sale(search_value="x", proxy_url="http://explicit:1@proxy.example:9090")
    assert captured["proxies"]["http"] == "http://explicit:1@proxy.example:9090"


def test_iter_search_yields_and_limits(monkeypatch, search_success_body):
    def fake_put(*args, **kwargs):
        return FakeResponse(status_code=200, json_data=search_success_body)

    monkeypatch.setattr(search.requests, "put", fake_put)

    items = list(search.iter_search(search.for_sale, search_value="x"))
    assert len(items) == 2

    items = list(search.iter_search(search.for_sale, max_items=1, search_value="x"))
    assert len(items) == 1
    assert items[0]["zpid"] == 348914888


def test_iter_search_normalize(monkeypatch, search_success_body):
    def fake_put(*args, **kwargs):
        return FakeResponse(status_code=200, json_data=search_success_body)

    monkeypatch.setattr(search.requests, "put", fake_put)

    items = list(search.iter_search(search.for_sale, normalize=True, search_value="x"))
    assert len(items) == 2
    # normalized shape: flat keys, numeric-or-None
    assert items[0]["zpid"] == 348914888
    assert items[0]["price"] is None
    assert items[0]["beds"] is None
    assert "hdpData" not in items[0]


def test_split_bbox_quadrants():
    quadrants = search.split_bbox(ne_lat=10, ne_long=10, sw_lat=0, sw_long=0)
    assert len(quadrants) == 4
    for q in quadrants:
        assert set(q.keys()) == {"ne_lat", "ne_long", "sw_lat", "sw_long"}
