import json

import pytest

from pyzill.exceptions import ZillowParseError
from pyzill.parse import parse_body, parse_body_departments, parse_body_home


def test_next_data_preserves_html_entity_text() -> None:
    payload = {
        "props": {
            "pageProps": {
                "componentProps": {
                    "value": "literal &quot; text and &amp; text",
                }
            }
        }
    }
    body = (f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(payload)}</script>').encode()

    parsed = parse_body(body)

    assert parsed["value"] == "literal &quot; text and &amp; text"


def test_parse_body_rejects_pages_without_next_data() -> None:
    with pytest.raises(ZillowParseError):
        parse_body(b"<html><body>blocked</body></html>")


def test_parse_body_rejects_invalid_json() -> None:
    body = b'<script id="__NEXT_DATA__">not json</script>'

    with pytest.raises(ZillowParseError):
        parse_body(body)


def test_parse_body_home_rejects_missing_gdp_cache() -> None:
    body = b'<script id="__NEXT_DATA__" type="application/json">{"props":{"pageProps":{"componentProps":{}}}}</script>'

    with pytest.raises(ZillowParseError):
        parse_body_home(body)


def test_parse_body_home_selects_requested_zpid_from_cache():
    cache = {
        "first": {"property": {"zpid": 1, "address": "First"}},
        "requested": {"property": {"zpid": 2, "address": "Requested"}},
    }
    payload = {
        "props": {
            "pageProps": {
                "componentProps": {"gdpClientCache": json.dumps(cache)},
            }
        }
    }
    body = f'<script type="application/json" id="__NEXT_DATA__">{json.dumps(payload)}</script>'.encode()

    assert parse_body_home(body, zpid=2)["address"] == "Requested"


def test_parse_body_departments_rejects_missing_gdp() -> None:
    body = (
        b'<script id="__NEXT_DATA__" type="application/json">'
        b'{"props":{"pageProps":{"componentProps":{"initialReduxState":{}}}}}'
        b"</script>"
    )

    with pytest.raises(ZillowParseError):
        parse_body_departments(body)


@pytest.mark.parametrize(
    "body",
    [
        b'<script id="__NEXT_DATA__">{"props":{"pageProps":{"componentProps":[]}}}</script>',
    ],
)
def test_parse_rejects_unexpected_component_shapes(body: bytes) -> None:
    with pytest.raises(ZillowParseError):
        parse_body(body)


def test_parse_departments_rejects_unexpected_gdp_shape() -> None:
    body = (
        b'<script id="__NEXT_DATA__">'
        b'{"props":{"pageProps":{"componentProps":{"initialReduxState":{"gdp":[]}}}}}</script>'
    )

    with pytest.raises(ZillowParseError):
        parse_body_departments(body)


def test_parse_home_rejects_malformed_gdp_cache_json() -> None:
    body = b'<script id="__NEXT_DATA__">{"props":{"pageProps":{"componentProps":{"gdpClientCache":"{"}}}}</script>'

    with pytest.raises(ZillowParseError):
        parse_body_home(body)


def test_parse_home_rejects_cache_without_property() -> None:
    body = b'<script id="__NEXT_DATA__">{"props":{"pageProps":{"componentProps":{"gdpClientCache":"{}"}}}}</script>'

    with pytest.raises(ZillowParseError):
        parse_body_home(body)
