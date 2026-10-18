import time

import pytest

from pyzill import details, http
from pyzill.exceptions import ZillowBlockedError


def test_get_from_home_id(fake_http, home_detail_body):
    fake_http.install("GET", content=home_detail_body)

    data = details.get_from_home_id(348914888)

    assert data["zpid"] == 348914888
    assert data["address"]["streetAddress"] == "27 Washington Ave"

    call = fake_http.calls[-1]
    assert "348914888_zpid" in call["url"]
    assert call["impersonate"] == "chrome136"
    assert call["timeout"] == 30.0


def test_details_use_default_retries(fake_http, monkeypatch):
    fake_http.install("GET", status_code=503, text="Service Unavailable")
    monkeypatch.setattr(time, "sleep", lambda *args, **kwargs: None)

    with pytest.raises(ZillowBlockedError):
        details.get_from_home_url("https://www.zillow.com/homedetails/bad_zpid/")

    assert len(fake_http.calls) == http.DEFAULT_MAX_RETRIES + 1


def test_get_from_home_url(fake_http, home_detail_body):
    fake_http.install("GET", content=home_detail_body)

    data = details.get_from_home_url("https://www.zillow.com/homedetails/27-Washington-Ave/348914888_zpid/")
    assert data["bedrooms"] == 3
    assert data["bathrooms"] == 2


def test_get_from_apartment_id(fake_http, department_detail_body):
    fake_http.install("GET", content=department_detail_body)

    data = details.get_from_apartment_id("CgKZT4")
    assert data["id"] == "CgKZT4"
    assert data["name"] == "The Lennox"
    assert fake_http.calls[-1]["url"] == "https://www.zillow.com/apartments/CgKZT4/"


def test_get_from_apartment_url(fake_http, department_detail_body):
    fake_http.install("GET", content=department_detail_body)

    data = details.get_from_apartment_url(
        "https://www.zillow.com/apartments/kissimmee-fl/the-nexus-at-overbrook/9DSWrh/"
    )
    assert data["id"] == "CgKZT4"


@pytest.mark.parametrize("fetch", [details.get_from_home_url, details.get_from_apartment_url])
def test_detail_url_rejects_non_zillow_hosts(monkeypatch, fake_http, fetch):
    monkeypatch.setattr("pyzill.details.http.get_page", lambda *args, **kwargs: pytest.fail("unexpected fetch"))

    with pytest.raises(ValueError, match=r"zillow\.com"):
        fetch("https://example.com/fake-zillow-page")

    assert fake_http.calls == []


@pytest.mark.parametrize(
    "alias",
    [
        details.get_from_department_id,
        details.get_from_deparment_id,
    ],
)
def test_department_id_aliases_warn(fake_http, department_detail_body, alias):
    fake_http.install("GET", content=department_detail_body)

    with pytest.warns(DeprecationWarning, match="deprecated"):
        assert alias("CgKZT4")["id"] == "CgKZT4"


@pytest.mark.parametrize(
    "alias",
    [
        details.get_from_department_url,
        details.get_from_deparment_url,
    ],
)
def test_department_url_aliases_warn(fake_http, department_detail_body, alias):
    fake_http.install("GET", content=department_detail_body)

    with pytest.warns(DeprecationWarning, match="deprecated"):
        assert alias("https://www.zillow.com/apartments/CgKZT4/")["id"] == "CgKZT4"


def test_detail_http_error_raises(fake_http):
    fake_http.install("GET", status_code=404, text="Not Found")

    with pytest.raises(ZillowBlockedError):
        details.get_from_home_url("https://www.zillow.com/homedetails/bad_zpid/")
