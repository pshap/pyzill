import time

import pytest
from curl_cffi import requests as cffi_requests
from curl_cffi.requests.exceptions import RequestException

from pyzill import http
from pyzill.exceptions import ZillowBlockedError


def test_build_headers_derives_chrome_version() -> None:
    headers = http.build_headers("chrome145")

    assert '"Chromium";v="145"' in headers["Sec-Ch-Ua"]


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


def test_request_retries_request_exceptions(monkeypatch) -> None:
    attempts = 0

    def fail(**kwargs):
        nonlocal attempts
        attempts += 1
        raise RequestException("network down")

    monkeypatch.setattr(cffi_requests, "request", fail)
    monkeypatch.setattr(time, "sleep", lambda *args, **kwargs: None)

    with pytest.raises(RequestException):
        http.request("GET", "https://www.zillow.com/", headers={}, max_retries=2)

    assert attempts == 3


def test_request_does_not_retry_other_exceptions(monkeypatch) -> None:
    attempts = 0

    def fail(**kwargs):
        nonlocal attempts
        attempts += 1
        raise RuntimeError("unexpected")

    monkeypatch.setattr(cffi_requests, "request", fail)
    monkeypatch.setattr(time, "sleep", lambda *args, **kwargs: None)

    with pytest.raises(RuntimeError):
        http.request("GET", "https://www.zillow.com/", headers={}, max_retries=2)

    assert attempts == 1
