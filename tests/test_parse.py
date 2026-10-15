import json

from pyzill.parse import parse_body


def test_next_data_preserves_html_entity_text() -> None:
    payload = {
        "props": {
            "pageProps": {
                "componentProps": {
                    "value": 'literal &quot; text and &amp; text',
                }
            }
        }
    }
    body = (
        '<script id="__NEXT_DATA__" type="application/json">'
        f"{json.dumps(payload)}"
        "</script>"
    ).encode()

    parsed = parse_body(body)

    assert parsed["value"] == 'literal &quot; text and &amp; text'
