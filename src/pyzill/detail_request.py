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
    error = "Home target must be a positive integer zpid or valid https Zillow URL on a zillow.com host"
    if isinstance(zpid_or_url, bool):
        raise ValueError(error)
    if isinstance(zpid_or_url, int):
        if zpid_or_url <= 0:
            raise ValueError(error)
        return home_detail_url(zpid_or_url), zpid_or_url
    if not isinstance(zpid_or_url, str):
        raise ValueError(error)
    if is_zillow_https_url(zpid_or_url):
        parsed = urlsplit(zpid_or_url)
        match = re.search(r"(?:^|/)([0-9]+)_zpid(?:/|$)", parsed.path)
        if match is None:
            return zpid_or_url, None
        zpid = match.group(1)
        if int(zpid) <= 0:
            raise ValueError(error)
        return zpid_or_url, zpid
    if not re.fullmatch(r"[0-9]+", zpid_or_url):
        raise ValueError(error)
    zpid = int(zpid_or_url)
    if zpid <= 0:
        raise ValueError(error)
    return home_detail_url(zpid), zpid


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
