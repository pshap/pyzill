"""Tests for the host-agnostic response helpers."""

import json

import pytest
from curl_cffi.requests.exceptions import HTTPError

from pyzill.responses import json_body, raise_for_status
from tests.conftest import FakeResponse


def test_json_body_parses_the_body():
    response = FakeResponse(json_data={"cat1": {"searchResults": {}}})

    assert json_body(response) == {"cat1": {"searchResults": {}}}


def test_json_body_raises_on_a_non_json_body():
    response = FakeResponse(text="not json")

    with pytest.raises(json.JSONDecodeError):
        json_body(response)


def test_raise_for_status_passes_a_successful_response():
    response = FakeResponse(status_code=200, json_data={})

    raise_for_status(response)  # must not raise


@pytest.mark.parametrize("status_code", [400, 404, 429, 500, 503])
def test_raise_for_status_raises_for_an_error_status(status_code):
    response = FakeResponse(status_code=status_code)

    with pytest.raises(HTTPError):
        raise_for_status(response)


def test_http_re_exports_the_response_helpers():
    """The helpers stay importable from pyzill.http for compatibility."""
    from pyzill import http

    assert http.json_body is json_body
    assert http.raise_for_status is raise_for_status
