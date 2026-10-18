from json import JSONDecodeError, loads
from typing import Any

from bs4 import BeautifulSoup

from pyzill.exceptions import ZillowParseError
from pyzill.utils import get_nested_value


def _parse_error(body: bytes) -> ZillowParseError:
    snippet = body.decode(errors="replace")[:500]
    return ZillowParseError(f"Zillow response did not match the expected page schema. Body starts with: {snippet!r}")


def parse_body_home(body: bytes) -> dict[str, Any]:
    """parse HTML content to retrieve JSON data

    Args:
        body (bytes): HTML content of web page

    Returns:
        dict[str, Any]: parsed property information
    """
    component_props = parse_body(body)
    data_raw = get_nested_value(component_props, "gdpClientCache")
    if not isinstance(data_raw, str):
        raise _parse_error(body)
    try:
        property_json = loads(data_raw)
    except JSONDecodeError as exc:
        raise _parse_error(body) from exc
    if not isinstance(property_json, dict):
        raise _parse_error(body)
    for value in property_json.values():
        property_data = value.get("property") if isinstance(value, dict) else None
        if isinstance(property_data, dict):
            return dict(property_data)
    raise _parse_error(body)


def parse_body_departments(body: bytes) -> dict[str, Any]:
    """parse HTML content to retrieve JSON data

    Args:
        body (bytes): HTML content of web page

    Returns:
        dict[str, Any]: parsed property information
    """
    component_props = parse_body(body)
    gdp = get_nested_value(component_props, "initialReduxState.gdp")
    if not isinstance(gdp, dict):
        raise _parse_error(body)
    return gdp


def parse_body(body: bytes) -> dict[str, Any]:
    """parse HTML content to retrieve JSON data

    Args:
        body (bytes): HTML content of web page

    Returns:
        dict[str, Any]: parsed property information
    """
    soup = BeautifulSoup(body, "html.parser")
    selection = soup.select_one("#__NEXT_DATA__")
    if not selection:
        raise _parse_error(body)
    try:
        data = loads(selection.string or "")
    except JSONDecodeError as exc:
        raise _parse_error(body) from exc
    component_props = get_nested_value(data, "props.pageProps.componentProps")
    if not isinstance(component_props, dict):
        raise _parse_error(body)
    return component_props
