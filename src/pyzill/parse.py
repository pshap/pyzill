from json import JSONDecodeError, loads
from typing import Any

from bs4 import BeautifulSoup

from pyzill.exceptions import ZillowBlockedError
from pyzill.utils import get_nested_value


def _blocked(body: bytes) -> ZillowBlockedError:
    return ZillowBlockedError(status_code=200, url="response body", body_snippet=body.decode(errors="replace"))


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
        raise _blocked(body)
    try:
        property_json = loads(data_raw)
    except JSONDecodeError as exc:
        raise _blocked(body) from exc
    if not isinstance(property_json, dict):
        raise _blocked(body)
    for value in property_json.values():
        property_data = value.get("property") if isinstance(value, dict) else None
        if isinstance(property_data, dict):
            return dict(property_data)
    raise _blocked(body)


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
        raise _blocked(body)
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
        raise _blocked(body)
    try:
        data = loads(selection.string or "")
    except JSONDecodeError as exc:
        raise _blocked(body) from exc
    component_props = get_nested_value(data, "props.pageProps.componentProps")
    if not isinstance(component_props, dict):
        raise _blocked(body)
    return component_props
