import time

import pytest
from curl_cffi import requests as cffi_requests
from curl_cffi.requests.exceptions import RequestException

from pyzill import http
from pyzill.exceptions import ZillowBlockedError


def test_build_headers_derives_chrome_version() -> None:
    headers = http.build_headers("chrome145")

    assert '"Chromium";v="145"' in headers["Sec-Ch-Ua"]


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

    monkeypatch.setattr(cffi_requests, "get", fail)
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

    monkeypatch.setattr(cffi_requests, "get", fail)
    monkeypatch.setattr(time, "sleep", lambda *args, **kwargs: None)

    with pytest.raises(RuntimeError):
        http.request("GET", "https://www.zillow.com/", headers={}, max_retries=2)

    assert attempts == 1
