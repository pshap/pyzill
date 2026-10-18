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


def test_parse_body_departments_rejects_missing_gdp() -> None:
    body = (
        b'<script id="__NEXT_DATA__" type="application/json">'
        b'{"props":{"pageProps":{"componentProps":{"initialReduxState":{}}}}}'
        b"</script>"
    )

    with pytest.raises(ZillowParseError):
        parse_body_departments(body)
