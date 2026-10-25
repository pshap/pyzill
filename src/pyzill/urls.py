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
    "apartment_id_from_url",
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


def apartment_id_from_url(url: str) -> str | None:
    """Return the apartment (building) id in a Zillow apartment URL.

    Zillow writes the id as the last path segment of an
    ``/apartments/<place>/<name>/<id>/`` URL, which may be absolute or
    relative. A home URL, or anything that is not an apartment URL, yields
    ``None`` so the caller can fall back to the home-detail endpoint.
    """
    try:
        path = urlsplit(url).path
    except ValueError:
        return None
    segments = [segment for segment in path.split("/") if segment]
    if "apartments" not in segments:
        return None
    tail = segments[segments.index("apartments") + 1 :]
    # A building URL has at least a place/name segment and the id.
    if len(tail) < _APARTMENT_ID_MIN_SEGMENTS:
        return None
    return tail[-1]


_APARTMENT_ID_MIN_SEGMENTS = 2
