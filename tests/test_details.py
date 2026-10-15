import pytest
from curl_cffi.requests.exceptions import HTTPError

from pyzill import details
from tests.conftest import FakeResponse


def test_get_from_home_id(monkeypatch, home_detail_body):
    captured = {}

    def fake_get(url, headers=None, proxies=None, impersonate=None, timeout=None):
        captured["url"] = url
        captured["impersonate"] = impersonate
        captured["timeout"] = timeout
        return FakeResponse(status_code=200, content=home_detail_body)

    monkeypatch.setattr(details.requests, "get", fake_get)

    data = details.get_from_home_id(348914888)

    assert data["zpid"] == 348914888
    assert data["address"]["streetAddress"] == "27 Washington Ave"
    assert "348914888_zpid" in captured["url"]
    assert captured["impersonate"] == "chrome136"


def test_get_from_home_url(monkeypatch, home_detail_body):
    def fake_get(url, headers=None, proxies=None, impersonate=None, timeout=None):
        return FakeResponse(status_code=200, content=home_detail_body)

    monkeypatch.setattr(details.requests, "get", fake_get)

    data = details.get_from_home_url("https://www.zillow.com/homedetails/27-Washington-Ave/348914888_zpid/")
    assert data["bedrooms"] == 3
    assert data["bathrooms"] == 2


def test_get_from_deparment_id(monkeypatch, department_detail_body):
    def fake_get(url, headers=None, proxies=None, impersonate=None, timeout=None):
        return FakeResponse(status_code=200, content=department_detail_body)

    monkeypatch.setattr(details.requests, "get", fake_get)

    data = details.get_from_deparment_id("CgKZT4")
    assert data["id"] == "CgKZT4"
    assert data["name"] == "The Lennox"


def test_get_from_deparment_url(monkeypatch, department_detail_body):
    def fake_get(url, headers=None, proxies=None, impersonate=None, timeout=None):
        return FakeResponse(status_code=200, content=department_detail_body)

    monkeypatch.setattr(details.requests, "get", fake_get)

    data = details.get_from_deparment_url(
        "https://www.zillow.com/apartments/kissimmee-fl/the-nexus-at-overbrook/9DSWrh/"
    )
    assert data["id"] == "CgKZT4"


def test_detail_http_error_raises(monkeypatch):
    def fake_get(url, headers=None, proxies=None, impersonate=None, timeout=None):
        return FakeResponse(status_code=404, text="Not Found")

    monkeypatch.setattr(details.requests, "get", fake_get)

    with pytest.raises(HTTPError):
        details.get_from_home_url("https://www.zillow.com/homedetails/bad_zpid/")
