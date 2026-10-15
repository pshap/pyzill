import json

import pytest


class FakeResponse:
    """Minimal stand-in for a curl_cffi response object."""

    def __init__(self, status_code=200, json_data=None, text="", content=b"", headers=None):
        self.status_code = status_code
        self._json = json_data
        self.text = text or (json.dumps(json_data) if json_data is not None else "")
        self.content = content if content else self.text.encode()
        self.headers = headers or {}

    def json(self):
        if self._json is None:
            raise json.JSONDecodeError("Expecting value", "", 0)
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")


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
