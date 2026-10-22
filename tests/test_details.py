import time

import pytest

from pyzill import details, http
from pyzill.detail_request import home_target
from pyzill.exceptions import ZillowBlockedError, ZillowNotFoundError, ZillowServerError


@pytest.mark.parametrize("value", ["12_3", " 123 ", True, 0, -5, "http://www.zillow.com/homedetails/123_zpid/"])
def test_home_target_rejects_invalid_targets(value):
    with pytest.raises(ValueError, match="positive integer zpid"):
        home_target(value)


@pytest.mark.parametrize(
    ("url", "expected_zpid"),
    [
        ("https://www.zillow.com/homedetails/27-Washington-Ave/123_zpid/", "123"),
        ("https://www.zillow.com/homedetails/no-id/?ref=123_zpid", None),
    ],
)
def test_home_target_extracts_only_a_zpid_path_segment(url, expected_zpid):
    assert home_target(url) == (url, expected_zpid)


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

    with pytest.raises(ZillowServerError):
        details.get_from_home_url("https://www.zillow.com/homedetails/bad_zpid/")

    assert len(fake_http.calls) == http.DEFAULT_MAX_RETRIES + 1


def test_get_from_home_url(fake_http, home_detail_body):
    fake_http.install("GET", content=home_detail_body)

    data = details.get_from_home_url("https://www.zillow.com/homedetails/27-Washington-Ave/348914888_zpid/")
    assert data["bedrooms"] == 3
    assert data["bathrooms"] == 2


def test_detail_accepts_request_options(fake_http, home_detail_body):
    fake_http.install("GET", content=home_detail_body)
    options = http.RequestOptions(impersonate="chrome145", timeout=12, max_retries=0)

    details.get_from_home_id(348914888, options=options)

    call = fake_http.calls[-1]
    assert call["impersonate"] == "chrome145"
    assert call["timeout"] == 12


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
    monkeypatch.setattr("pyzill.detail_request.http.get_page", lambda *args, **kwargs: pytest.fail("unexpected fetch"))

    with pytest.raises(ValueError, match=r"zillow\.com"):
        fetch("https://example.com/fake-zillow-page")

    assert fake_http.calls == []


def test_department_aliases_warn_and_delegate(fake_http, department_detail_body):
    """The deprecated `department` aliases warn and delegate to the apartment fetchers."""
    fake_http.install("GET", content=department_detail_body)

    with pytest.warns(DeprecationWarning, match="deprecated"):
        assert details.get_from_department_id("CgKZT4")["id"] == "CgKZT4"
    with pytest.warns(DeprecationWarning, match="deprecated"):
        assert details.get_from_department_url("https://www.zillow.com/apartments/CgKZT4/")["id"] == "CgKZT4"
    assert fake_http.calls[-1]["url"] == "https://www.zillow.com/apartments/CgKZT4/"


def test_deprecated_misspelling_removed():
    """The historical `deparment` misspellings are gone from the public API."""
    assert not hasattr(details, "get_from_deparment_id")
    assert not hasattr(details, "get_from_deparment_url")


def test_detail_http_error_raises(fake_http):
    fake_http.install("GET", status_code=404, text="Not Found")

    with pytest.raises(ZillowNotFoundError):
        details.get_from_home_url("https://www.zillow.com/homedetails/bad_zpid/")


def test_detail_pages_with_ordinary_perimeterx_settings_are_not_blocked(
    fake_http, home_detail_body, department_detail_body
):
    for body, kind in ((home_detail_body, "home"), (department_detail_body, "apartment")):
        html = body.replace(b"<html>", b'<html><script>var _pxhd = "cookie";</script><div px-appid="app"></div>')
        fake_http.install("GET", text=html.decode(), content=html, headers={"content-type": "text/html"})

        if kind == "home":
            assert details.get_from_home_id(348914888)["zpid"] == 348914888
        else:
            assert details.get_from_apartment_id("CgKZT4")["id"] == "CgKZT4"


def test_detail_200_html_challenge_raises_blocked(fake_http):
    fake_http.install("GET", text="<html>PerimeterX px-captcha</html>")

    with pytest.raises(ZillowBlockedError):
        details.get_from_home_url("https://www.zillow.com/homedetails/348914888_zpid/")
