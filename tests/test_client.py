from typing import Any

from curl_cffi import requests as cffi_requests

from pyzill import BBox
from pyzill.client import ZillowClient
from tests.conftest import FakeResponse


def test_client_reuses_session_and_owns_independent_resources(fake_http, search_success_body):
    fake_http.install("PUT", json_data=search_success_body)
    client = ZillowClient(rate_limit=0)
    other = ZillowClient(rate_limit=0)
    session = client.session

    client.search_sale(search_value="12305", bbox=BBox(north=43, east=-73, south=42, west=-74))
    client.search_sale(search_value="12305", bbox=BBox(north=43, east=-73, south=42, west=-74))

    assert client.session is session
    assert client.session is not other.session
    assert client.limiter is not other.limiter
    assert len(fake_http.calls) == 2


def test_client_retries_one_block_with_next_proxy_and_fresh_session(monkeypatch, search_success_body):
    responses = [
        FakeResponse(status_code=403, text="blocked"),
        FakeResponse(
            status_code=200,
            json_data={"cat1": {"searchResults": search_success_body["cat1"]["searchResults"]}},
        ),
    ]
    attempts: list[tuple[object, dict[str, Any]]] = []

    def respond(session: object, **kwargs: Any) -> FakeResponse:
        attempts.append((session, kwargs))
        return responses[len(attempts) - 1]

    monkeypatch.setattr(cffi_requests.Session, "request", respond)
    client = ZillowClient(proxy=["http://proxy-a:1", "http://proxy-b:2"], rate_limit=0, max_retries=0)

    result = client.search_sale(search_value="12305", bbox=BBox(north=43, east=-73, south=42, west=-74))

    assert len(result["mapResults"]) == 2
    assert len(attempts) == 2
    assert attempts[0][0] is not attempts[1][0]
    assert attempts[0][1]["proxies"]["http"] == "http://proxy-a:1"
    assert attempts[1][1]["proxies"]["http"] == "http://proxy-b:2"


def test_client_home_selects_requested_property_from_cache(fake_http):
    import json

    from tests.conftest import _next_data_html

    cache = {
        "first": {"property": {"zpid": 1, "address": "First"}},
        "requested": {"property": {"zpid": 2, "address": "Requested"}},
    }
    fake_http.install(
        "GET",
        content=_next_data_html({"gdpClientCache": json.dumps(cache)}),
    )
    client = ZillowClient(rate_limit=0)

    assert client.home(2)["address"] == "Requested"
