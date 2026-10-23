from typing import Any

import pytest
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


def test_per_call_timeout_does_not_leak_into_client_after_rotation(monkeypatch, search_success_body):
    from pyzill.http import DEFAULT_TIMEOUT

    responses = [
        FakeResponse(status_code=403, text="blocked"),
        FakeResponse(
            status_code=200,
            json_data={"cat1": {"searchResults": search_success_body["cat1"]["searchResults"]}},
        ),
    ]
    attempts: list[dict[str, Any]] = []

    def respond(session: object, **kwargs: Any) -> FakeResponse:
        attempts.append(kwargs)
        return responses[len(attempts) - 1]

    monkeypatch.setattr(cffi_requests.Session, "request", respond)
    client = ZillowClient(proxy=["http://proxy-a:1", "http://proxy-b:2"], rate_limit=0, max_retries=0)

    client.search_sale(search_value="12305", bbox=BBox(north=43, east=-73, south=42, west=-74), timeout=5)

    # The one-off timeout applies to this request (and its rotation retry) but
    # must not become the client-wide default.
    assert attempts[0]["timeout"] == 5
    assert attempts[1]["timeout"] == 5
    assert client.options.timeout == DEFAULT_TIMEOUT


def test_client_uses_a_distinct_session_per_proxy_url(monkeypatch, search_success_body):
    calls: list[tuple[object, object]] = []

    def respond(session: object, **kwargs: Any) -> FakeResponse:
        calls.append((session, kwargs["proxies"]["http"]))
        return FakeResponse(status_code=200, json_data=search_success_body)

    monkeypatch.setattr(cffi_requests.Session, "request", respond)
    client = ZillowClient(rate_limit=0)
    bbox = BBox(north=43, east=-73, south=42, west=-74)

    client.search_sale(search_value="12305", bbox=bbox, proxy_url="http://a:1")
    client.search_sale(search_value="12305", bbox=bbox, proxy_url="http://b:2")
    client.search_sale(search_value="12305", bbox=bbox, proxy_url="http://a:1")

    assert calls[0][0] is not calls[1][0]
    assert calls[0][0] is calls[2][0]


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


def test_client_does_not_rotate_away_from_a_pinned_proxy(monkeypatch, search_success_body):
    """A caller-supplied proxy_url pins the transport; a 403 must not rotate."""
    from pyzill.exceptions import ZillowBlockedError

    attempts: list[str] = []

    def respond(session: object, **kwargs: Any) -> FakeResponse:
        attempts.append(kwargs["proxies"]["http"])
        return FakeResponse(status_code=403, text="blocked")

    monkeypatch.setattr(cffi_requests.Session, "request", respond)
    client = ZillowClient(proxy=["http://proxy-a:1", "http://proxy-b:2"], rate_limit=0, max_retries=0)

    with pytest.raises(ZillowBlockedError):
        client.search_sale(
            search_value="12305",
            bbox=BBox(north=43, east=-73, south=42, west=-74),
            proxy_url="http://pinned:9",
        )

    assert attempts == ["http://pinned:9"]


def test_client_keeps_its_pool_when_proxy_url_is_none(monkeypatch, search_success_body):
    """proxy_url=None means "not specified"; it must not drop the pool proxy."""
    attempts: list[str] = []

    def respond(session: object, **kwargs: Any) -> FakeResponse:
        attempts.append(kwargs["proxies"]["http"])
        return FakeResponse(status_code=200, json_data=search_success_body)

    monkeypatch.setattr(cffi_requests.Session, "request", respond)
    client = ZillowClient(proxy=["http://proxy-a:1"], rate_limit=0)
    bbox = BBox(north=43, east=-73, south=42, west=-74)

    client.search_sale(search_value="12305", bbox=bbox, proxy_url=None)

    assert attempts == ["http://proxy-a:1"]


def test_duplicate_proxies_do_not_count_as_a_rotation_pair(monkeypatch, search_success_body):
    """Rotating onto the same proxy would burn the retry without changing transport."""
    from pyzill.exceptions import ZillowBlockedError

    attempts: list[str] = []

    def respond(session: object, **kwargs: Any) -> FakeResponse:
        attempts.append(kwargs["proxies"]["http"])
        return FakeResponse(status_code=403, text="blocked")

    monkeypatch.setattr(cffi_requests.Session, "request", respond)
    client = ZillowClient(proxy=["http://proxy-a:1", "http://proxy-a:1"], rate_limit=0, max_retries=0)

    with pytest.raises(ZillowBlockedError):
        client.search_sale(search_value="12305", bbox=BBox(north=43, east=-73, south=42, west=-74))

    assert attempts == ["http://proxy-a:1"]


def test_clients_share_the_process_wide_rate_limit_budget():
    """Several clients in one process must not multiply the request rate."""
    from pyzill.ratelimit import default_limiter

    assert ZillowClient().limiter is default_limiter()
    assert ZillowClient().limiter is ZillowClient().limiter


def test_explicit_rate_limit_is_never_looser_than_the_shared_budget():
    from pyzill.ratelimit import SharedBudgetLimiter, default_limiter

    client = ZillowClient(rate_limit=2.5)

    assert isinstance(client.limiter, SharedBudgetLimiter)
    assert client.limiter.min_interval == 2.5
    assert client.limiter._shared is default_limiter()


def test_explicit_zero_rate_limit_opts_out_of_throttling():
    from pyzill.ratelimit import SharedBudgetLimiter

    client = ZillowClient(rate_limit=0)

    assert not isinstance(client.limiter, SharedBudgetLimiter)
    assert client.limiter.min_interval == 0


def test_reset_default_client_replaces_it_and_closes_the_old(monkeypatch):
    from pyzill.client import get_default_client, reset_default_client

    first = get_default_client()
    assert get_default_client() is first
    closed: list[ZillowClient] = []

    def record(client: ZillowClient) -> None:
        closed.append(client)

    monkeypatch.setattr(ZillowClient, "close", record)

    reset_default_client()
    assert closed == [first]

    replacement = get_default_client()
    assert replacement is not first
    reset_default_client()
    assert closed == [first, replacement]


def test_reset_default_client_can_leave_it_open(monkeypatch):
    from pyzill.client import get_default_client, reset_default_client

    closed: list[ZillowClient] = []

    def record(client: ZillowClient) -> None:
        closed.append(client)

    monkeypatch.setattr(ZillowClient, "close", record)
    first = get_default_client()

    reset_default_client(close=False)

    assert closed == []
    assert get_default_client() is not first
    reset_default_client()
