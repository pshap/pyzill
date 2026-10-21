import json
import time
from typing import Any

import pytest
from curl_cffi import requests as cffi_requests
from curl_cffi.requests.exceptions import HTTPError

from pyzill import ratelimit


class NetworkAccessError(RuntimeError):
    """Raised when an offline test reaches for the network."""


@pytest.fixture(autouse=True)
def _disable_rate_limit():
    """Keep tests fast: no throttling between mocked requests."""
    original = ratelimit.get_min_interval()
    ratelimit.set_min_interval(0)
    yield
    ratelimit.set_min_interval(original)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch, request):
    """Fail an offline test that reaches for the network.

    The offline suite must never talk to Zillow or Nominatim: a live
    request makes the run slow and flaky, and it hides the fact that a
    test depends on a site it does not control. The ``live`` tests are the
    only ones allowed out.

    The guard is installed before any other fixture patches the same
    attribute, so a test that stubs the transport (``fake_http``) still
    wins over it. The attempt is reported when the test ends rather than
    where it happens, because the CLI turns every failure of a request
    into a clean exit code: a test that phoned home would otherwise pass.
    """
    attempts: list[str] = []

    def deny(*_args: Any, **_kwargs: Any) -> None:
        attempts.append(request.node.nodeid)
        raise NetworkAccessError("an offline test tried to reach the network")

    if request.node.get_closest_marker("live"):
        yield
        return
    monkeypatch.setattr(cffi_requests, "request", deny)
    monkeypatch.setattr(cffi_requests.Session, "request", deny)
    yield
    assert not attempts, (
        f"{request.node.nodeid} reached the network: it must stub the transport "
        "with the fake_http fixture, or be marked live"
    )


@pytest.fixture
def no_sleep(monkeypatch):
    """Make the rate limiters' delays instant.

    A test that checks *what* the limiter was asked to do does not need
    to wait for it. Tests that measure the delay itself ask for the
    ``time.sleep`` patch themselves.
    """
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", slept.append)
    return slept


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
                    {"zpid": 348914888, "address": "27 Washington Ave", "addressZipcode": "12305"},
                    {"zpid": 348914889, "address": "29 Washington Ave", "addressZipcode": "12305"},
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
