import json
from typing import Any

import pytest
from curl_cffi import requests as cffi_requests
from curl_cffi.requests.exceptions import HTTPError

from pyzill import ratelimit


@pytest.fixture(autouse=True)
def _disable_rate_limit():
    """Keep tests fast: no throttling between mocked requests."""
    original = ratelimit.get_min_interval()
    ratelimit.set_min_interval(0)
    yield
    ratelimit.set_min_interval(original)


class FakeResponse:
    """Minimal stand-in for a curl_cffi response object."""

    def __init__(
        self,
        status_code: int = 200,
        json_data: Any = None,
        text: str = "",
        content: bytes = b"",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self._json = json_data
        self.text = text or (json.dumps(json_data) if json_data is not None else "")
        self.content = content or self.text.encode()
        self.headers = headers or {}

    def json(self) -> Any:
        if self._json is None:
            raise json.JSONDecodeError("Expecting value", "", 0)
        return self._json

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise HTTPError(f"HTTP Error {self.status_code}", 0, self)


class _FakeHTTP:
    """Records outgoing pyzill.http request calls and returns canned responses."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._monkeypatch = monkeypatch
        self.calls: list[dict[str, Any]] = []

    def install(
        self,
        method: str,
        *,
        status_code: int = 200,
        json_data: Any = None,
        text: str = "",
        content: bytes = b"",
        headers: dict[str, str] | None = None,
    ) -> FakeResponse:
        """Patch ``curl_cffi.requests.request`` to return a FakeResponse."""
        response = FakeResponse(
            status_code=status_code,
            json_data=json_data,
            text=text,
            content=content,
            headers=headers,
        )

        def handler(*_args: Any, **kwargs: Any) -> FakeResponse:
            call = dict(kwargs)
            actual_method = str(call["method"]).lower()
            if actual_method != method.lower():
                raise AssertionError(f"expected {method}, got {actual_method}")
            call["method"] = actual_method
            self.calls.append(call)
            return response

        self._monkeypatch.setattr(cffi_requests, "request", handler)
        self._monkeypatch.setattr(cffi_requests.Session, "request", handler)
        return response


@pytest.fixture
def fake_http(monkeypatch):
    """Intercept :func:`pyzill.http.request` at the curl_cffi boundary."""
    return _FakeHTTP(monkeypatch)


def _next_data_html(page_props: dict) -> bytes:
    """Build a minimal Zillow-like page with a #__NEXT_DATA__ script tag."""
    payload = {
        "props": {
            "pageProps": {
                "componentProps": page_props,
            }
        }
    }
    html = (
        "<html><head></head><body>"
        f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(payload)}</script>'
        "</body></html>"
    )
    return html.encode()


@pytest.fixture
def search_success_body():
    return {
        "cat1": {
            "searchResults": {
                "mapResults": [
                    {"zpid": 348914888, "address": "27 Washington Ave"},
                    {"zpid": 348914889, "address": "29 Washington Ave"},
                ],
                "listResults": [{"zpid": 348914888}],
            }
        }
    }


@pytest.fixture
def home_detail_body():
    """Recorded-shape fixture for a home detail page."""
    property_data = {
        "zpid": 348914888,
        "address": {"streetAddress": "27 Washington Ave", "city": "Schenectady"},
        "bedrooms": 3,
        "bathrooms": 2,
    }
    cache_key = "https://www.zillow.com/homedetails/348914888_zpid/"
    gdp_client_cache = json.dumps({cache_key: {"property": property_data}})
    return _next_data_html({"gdpClientCache": gdp_client_cache})


@pytest.fixture
def department_detail_body():
    department_data = {"id": "CgKZT4", "name": "The Lennox"}
    return _next_data_html({"initialReduxState": {"gdp": department_data}})
