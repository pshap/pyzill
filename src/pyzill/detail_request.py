"""Fetching Zillow detail pages and parsing them.

The pure request/response half of the detail API: the caller supplies the
transport options, so this module never picks a proxy, a session, or a rate
limiter, and it never prints or exits.
"""

import re
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from pyzill import http
from pyzill.parse import parse_body_departments, parse_body_home
from pyzill.urls import apartment_detail_url, home_detail_url, is_zillow_https_url

__all__ = [
    "apartment_target",
    "fetch_apartment",
    "fetch_detail",
    "fetch_home",
    "home_target",
]

_Parser = Callable[[bytes], dict[str, Any]]


def fetch_detail(url: str, parser: _Parser, options: http.RequestOptions) -> dict[str, Any]:
    """Fetch and parse one Zillow detail page.

    Raises:
        ValueError: If the URL is not an HTTPS Zillow URL.
    """
    try:
        urlsplit(url)
    except ValueError as exc:
        raise ValueError("Detail URL must be an HTTPS Zillow URL") from exc
    if not is_zillow_https_url(url):
        raise ValueError("Detail URL must be an HTTPS Zillow URL on a zillow.com host")
    return parser(http.get_page(url, options=options).content)


def home_target(zpid_or_url: int | str) -> tuple[str, int | str | None]:
    """Return the URL to fetch and the zpid to select from the page cache.

    A numeric zpid becomes the canonical detail URL; a URL is used as given,
    and the zpid embedded in it (if any) is the property to select.
    """
    if isinstance(zpid_or_url, str) and zpid_or_url.startswith("https://"):
        match = re.search(r"(\d+)_zpid", zpid_or_url)
        return zpid_or_url, (match.group(1) if match else None)
    return home_detail_url(zpid_or_url), int(zpid_or_url)


def apartment_target(apartment_id_or_url: str) -> str:
    """Return the URL to fetch for an apartment id or URL."""
    if apartment_id_or_url.startswith("https://"):
        return apartment_id_or_url
    return apartment_detail_url(apartment_id_or_url)


def fetch_home(
    url: str,
    requested_zpid: int | str | None,
    options: http.RequestOptions,
) -> dict[str, Any]:
    """Fetch one home detail page and return the requested property."""
    return fetch_detail(url, lambda body: parse_body_home(body, requested_zpid), options)


def fetch_apartment(url: str, options: http.RequestOptions) -> dict[str, Any]:
    """Fetch one apartment (building) detail page."""
    return fetch_detail(url, parse_body_departments, options)
