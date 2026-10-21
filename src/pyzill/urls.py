"""Zillow URL rules: the one place that knows what a Zillow URL looks like.

The detail fetchers, the CLI's detail commands, and the URL builders all
need the same two facts -- "is this an HTTPS Zillow URL?" and "what is the
URL for this zpid/building?". Keeping them here means the host rule cannot
drift between the library and the CLI.
"""

from urllib.parse import quote, urlsplit

from pyzill.config import ZILLOW_HOME

__all__ = [
    "ZILLOW_HOST",
    "apartment_detail_url",
    "home_detail_url",
    "is_zillow_https_url",
]

ZILLOW_HOST = "zillow.com"


def is_zillow_https_url(url: str) -> bool:
    """Return True when ``url`` is an HTTPS URL on a ``zillow.com`` host.

    A URL that cannot be parsed is not a Zillow URL; callers that need a
    specific error message raise their own.
    """
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    hostname = parsed.hostname or ""
    return parsed.scheme == "https" and (hostname == ZILLOW_HOST or hostname.endswith(f".{ZILLOW_HOST}"))


def home_detail_url(zpid: int | str) -> str:
    """Return the canonical detail URL for a home zpid."""
    return f"{ZILLOW_HOME}/homedetails/any-title/{zpid}_zpid/"


def apartment_detail_url(apartment_id: str) -> str:
    """Return the canonical detail URL for an apartment (building) id."""
    return f"{ZILLOW_HOME}/apartments/{quote(apartment_id.strip(), safe='')}/"
