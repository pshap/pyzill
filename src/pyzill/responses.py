"""Reading an HTTP response: the parts every client needs.

These helpers are host-agnostic: one turns a response body into JSON, the
other turns a non-2xx status into an exception. They live here, and not in
:mod:`pyzill.http`, because the Zillow transport is not the only client in
the package -- the geocoder talks to Nominatim.
"""

from typing import Any, Protocol


class ResponseLike(Protocol):
    """The part of an HTTP response these helpers need.

    Typing the helpers against this instead of a concrete client class keeps
    them usable with any response object that can parse itself and report a
    failed status.
    """

    def json(self) -> Any: ...

    def raise_for_status(self) -> None: ...


__all__ = ["ResponseLike", "json_body", "raise_for_status"]


def json_body(response: ResponseLike) -> Any:
    """Parse a response body as JSON."""
    return response.json()


def raise_for_status(response: ResponseLike) -> None:
    """Raise an HTTPError for unsuccessful responses."""
    response.raise_for_status()
