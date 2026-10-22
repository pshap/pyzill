import re
from json import JSONDecodeError, loads
from typing import Any

from pyzill.exceptions import ZillowParseError
from pyzill.utils import get_nested_value

_SCRIPT_RE = re.compile(r"<script\b([^>]*)>(.*?)</script\s*>", re.IGNORECASE | re.DOTALL)
# The id attribute, and not an attribute that ends in "id" (data-id,
# aria-labelledid): the lookbehind keeps those out.
_SCRIPT_ID_RE = re.compile(r"(?<![\w-])id\s*=\s*(?:['\"]([^'\"]*)['\"]|([^\s>]+))", re.IGNORECASE)


def _parse_error(body: bytes) -> ZillowParseError:
    snippet = body.decode(errors="replace")[:500]
    return ZillowParseError(f"Zillow response did not match the expected page schema. Body starts with: {snippet!r}")


def parse_body_home(body: bytes, zpid: int | str | None = None) -> dict[str, Any]:
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
    first_property: dict[str, Any] | None = None
    for value in property_json.values():
        property_data = value.get("property") if isinstance(value, dict) else None
        if isinstance(property_data, dict):
            if first_property is None:
                first_property = property_data
            if zpid is not None and str(property_data.get("zpid")) == str(zpid):
                return dict(property_data)
    if zpid is not None:
        # The requested zpid is not in the cache; returning some other property
        # would silently attribute the wrong home to the caller.
        raise ZillowParseError(f"Zillow response did not contain requested property zpid {zpid}")
    if first_property is not None:
        return dict(first_property)
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
    html = body.decode("utf-8", errors="replace")
    script_data: str | None = None
    # finditer, not findall: a Zillow page carries a dozen script bodies and
    # only one of them is wanted, so the rest need not be held in memory.
    for match in _SCRIPT_RE.finditer(html):
        attributes, content = match.group(1), match.group(2)
        identifier = _SCRIPT_ID_RE.search(attributes)
        if identifier and next(value for value in identifier.groups() if value is not None) == "__NEXT_DATA__":
            script_data = content
            break
    if script_data is None:
        raise _parse_error(body)
    try:
        data = loads(script_data)
    except JSONDecodeError as exc:
        raise _parse_error(body) from exc
    component_props = get_nested_value(data, "props.pageProps.componentProps")
    if not isinstance(component_props, dict):
        raise _parse_error(body)
    return component_props
