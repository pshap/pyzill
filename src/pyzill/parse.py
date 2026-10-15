from html import unescape
from json import loads
from typing import Any, cast

from bs4 import BeautifulSoup

from pyzill.utils import get_nested_value, remove_space


def parse_body_home(body: bytes) -> dict[str, Any]:
    """parse HTML content to retrieve JSON data

    Args:
        body (bytes): HTML content of web page

    Returns:
        dict[str, Any]: parsed property information
    """
    component_props = parse_body(body)
    data_raw = get_nested_value(component_props, "gdpClientCache")
    if not data_raw:
        return {}
    property_json = loads(data_raw)
    return next(
        (
            cast("dict[str, Any]", value["property"])
            for value in property_json.values()
            if isinstance(value, dict) and "property" in value
        ),
        {},
    )


def parse_body_departments(body: bytes) -> dict[str, Any]:
    """parse HTML content to retrieve JSON data

    Args:
        body (bytes): HTML content of web page

    Returns:
        dict[str, Any]: parsed property information
    """
    component_props = parse_body(body)
    return cast("dict[str, Any]", get_nested_value(component_props, "initialReduxState.gdp"))


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
        return {}
    html_data = selection.getText()
    html_data = remove_space(unescape(html_data))
    data = loads(html_data)
    return cast("dict[str, Any]", get_nested_value(data, "props.pageProps.componentProps"))
