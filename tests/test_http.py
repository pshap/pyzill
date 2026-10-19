import time
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import Any

import pytest
from curl_cffi import requests as cffi_requests
from curl_cffi.requests.exceptions import RequestException

from pyzill import http
from pyzill.exceptions import ZillowBlockedError, ZillowNetworkError, ZillowNotFoundError, ZillowServerError
from pyzill.ratelimit import _limiter


class _FrozenDatetime:
    """Stands in for the ``datetime`` class, fixing ``now()`` for parsing tests."""

    def __init__(self, fixed: datetime) -> None:
        self._fixed = fixed

    def now(self, _tz: Any = None) -> datetime:
        return self._fixed


def test_build_headers_derives_chrome_version() -> None:
    headers = http.build_headers("chrome145")

    assert '"Chromium";v="145"' in headers["Sec-Ch-Ua"]


@pytest.mark.parametrize(
    "options_factory",
    [lambda: http.RequestOptions(max_retries=-1), lambda: http.RequestOptions(timeout=0)],
)
def test_request_options_reject_invalid_limits(options_factory) -> None:
    with pytest.raises(ValueError, match=r"timeout|retries"):
        options_factory()


def test_http_helpers_build_request_specific_headers(fake_http) -> None:
    fake_http.install("GET")
    http.get_page("https://www.zillow.com/", options=http.RequestOptions())
    page_headers = fake_http.calls[-1]["headers"]
    assert page_headers["Sec-Fetch-Site"] == "none"
    assert "Content-Type" not in page_headers

    fake_http.install("PUT")
    http.put_json("https://www.zillow.com/search", {"search": True}, options=http.RequestOptions())
    xhr_headers = fake_http.calls[-1]["headers"]
    assert xhr_headers["Content-Type"] == "application/json"
    assert xhr_headers["Sec-Fetch-Site"] == "same-site"


def test_request_rejects_non_2xx_status(fake_http) -> None:
    fake_http.install("GET", status_code=302, text="Found")

    with pytest.raises(ZillowBlockedError):
        http.request("GET", "https://www.zillow.com/", headers={})


def test_request_distinguishes_not_found(fake_http) -> None:
    fake_http.install("GET", status_code=404, text="Not Found")

    with pytest.raises(ZillowNotFoundError):
        http.request("GET", "https://www.zillow.com/missing", headers={})


def test_request_distinguishes_server_failure(fake_http, monkeypatch) -> None:
    fake_http.install("GET", status_code=503, text="Unavailable")
    monkeypatch.setattr(time, "sleep", lambda *args, **kwargs: None)

    with pytest.raises(ZillowServerError):
        http.request("GET", "https://www.zillow.com/", headers={}, max_retries=0)

    assert not issubclass(ZillowServerError, ZillowBlockedError)


def test_request_detects_200_html_challenge(fake_http) -> None:
    fake_http.install("GET", text="<html>Please verify. PerimeterX px-captcha</html>")

    with pytest.raises(ZillowBlockedError):
        http.request("GET", "https://www.zillow.com/", headers={})


def test_request_allows_html_that_only_embeds_perimeterx_script(fake_http) -> None:
    """A normal Zillow page embeds a scripts.perimeterx tag; that is not a block."""
    fake_http.install(
        "GET",
        text='<html><script id="scripts.perimeterx">var _pxAppId=1;</script>'
        '<script id="__NEXT_DATA__">{"props":{}}</script></html>',
        headers={"content-type": "text/html"},
    )

    response = http.request("GET", "https://www.zillow.com/homedetails/x/1_zpid/", headers={})

    assert response.status_code == 200


def test_request_does_not_scan_json_content_type_for_markers(fake_http) -> None:
    fake_http.install(
        "PUT",
        json_data={"note": "px-captcha"},
        headers={"content-type": "application/json"},
    )

    response = http.request("PUT", "https://www.zillow.com/search", headers={})

    assert response.json() == {"note": "px-captcha"}  # type: ignore[no-untyped-call]


def test_request_does_not_flag_parseable_json_without_content_type(fake_http) -> None:
    fake_http.install("PUT", json_data={"note": "px-captcha"})

    response = http.request("PUT", "https://www.zillow.com/search", headers={})

    assert response.json() == {"note": "px-captcha"}  # type: ignore[no-untyped-call]


def test_request_honors_retry_after_for_429(fake_http, monkeypatch) -> None:
    fake_http.install("GET", status_code=429, text="Slow down", headers={"Retry-After": "2"})
    delays: list[float] = []
    monkeypatch.setattr(_limiter, "wait", delays.append)

    with pytest.raises(ZillowBlockedError):
        http.request("GET", "https://www.zillow.com/", headers={}, max_retries=1)

    assert delays == [0.0, 2.0]


def test_request_honors_capped_retry_after_for_503(fake_http, monkeypatch) -> None:
    fake_http.install("GET", status_code=503, text="Unavailable", headers={"Retry-After": "120"})
    delays: list[float] = []
    monkeypatch.setattr(_limiter, "wait", delays.append)

    with pytest.raises(ZillowServerError):
        http.request("GET", "https://www.zillow.com/", headers={}, max_retries=1, max_retry_after=30)

    assert delays == [0.0, 30.0]


def test_retry_delay_parses_http_date_retry_after(monkeypatch) -> None:
    fixed_now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    monkeypatch.setattr(http, "datetime", _FrozenDatetime(fixed_now))
    retry_at = format_datetime(fixed_now + timedelta(seconds=45))

    assert http._retry_delay(0, retry_at) == pytest.approx(45.0, abs=1.0)


def test_retry_delay_caps_http_date_retry_after(monkeypatch) -> None:
    fixed_now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    monkeypatch.setattr(http, "datetime", _FrozenDatetime(fixed_now))
    retry_at = format_datetime(fixed_now + timedelta(seconds=600))

    assert http._retry_delay(0, retry_at, max_retry_after=30) == 30.0


def test_retry_delay_ignores_past_http_date(monkeypatch) -> None:
    fixed_now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    monkeypatch.setattr(http, "datetime", _FrozenDatetime(fixed_now))
    retry_at = format_datetime(fixed_now - timedelta(seconds=30))

    assert http._retry_delay(0, retry_at) == 0.0


def test_retry_delay_falls_back_to_backoff_for_garbage(monkeypatch) -> None:
    first = http._retry_delay(0, "not-a-delay")
    later = http._retry_delay(3, "not-a-delay")
    assert later >= first  # exponential backoff still applied


def test_request_retries_request_exceptions(monkeypatch) -> None:
    attempts = 0

    def fail(**kwargs: Any) -> None:
        nonlocal attempts
        attempts += 1
        raise RequestException("network down")

    monkeypatch.setattr(cffi_requests.Session, "request", lambda self, **kwargs: fail(**kwargs))
    monkeypatch.setattr(time, "sleep", lambda *args, **kwargs: None)

    with pytest.raises(ZillowNetworkError) as exc_info:
        http.request("GET", "https://www.zillow.com/", headers={}, max_retries=2)

    assert attempts == 3
    assert exc_info.value.code == "network"
    assert exc_info.value.retryable


def test_request_does_not_retry_other_exceptions(monkeypatch) -> None:
    attempts = 0

    def fail(**kwargs: Any) -> None:
        nonlocal attempts
        attempts += 1
        raise RuntimeError("unexpected")

    monkeypatch.setattr(cffi_requests.Session, "request", lambda self, **kwargs: fail(**kwargs))
    monkeypatch.setattr(time, "sleep", lambda *args, **kwargs: None)

    with pytest.raises(RuntimeError):
        http.request("GET", "https://www.zillow.com/", headers={}, max_retries=2)

    assert attempts == 1
