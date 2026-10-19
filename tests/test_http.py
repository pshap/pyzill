import time
from typing import Any

import pytest
from curl_cffi import requests as cffi_requests
from curl_cffi.requests.exceptions import RequestException

from pyzill import http
from pyzill.exceptions import ZillowBlockedError, ZillowNetworkError, ZillowNotFoundError, ZillowServerError
from pyzill.ratelimit import _limiter


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
