"""Home/apartment detail fetching and URL/ID validation.

``pyzill.client`` imports this module and this module calls back into the
default client, so ``get_default_client`` is imported lazily inside each entry
point to avoid an import cycle (client -> details -> client).
"""

import warnings
from collections.abc import Callable
from typing import Any, cast
from urllib.parse import urlsplit

from pyzill import http

_Parser = Callable[[bytes], dict[str, Any]]


def _fetch(url: str, parser: _Parser, options: http.RequestOptions) -> dict[str, Any]:
    """Fetch and parse one Zillow detail page."""
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname or ""
    except ValueError as exc:
        raise ValueError("Detail URL must be an HTTPS Zillow URL") from exc
    if parsed.scheme != "https" or not (hostname == "zillow.com" or hostname.endswith(".zillow.com")):
        raise ValueError("Detail URL must be an HTTPS Zillow URL on a zillow.com host")
    return parser(http.get_page(url, options=options).content)


def _options(
    options: http.RequestOptions | None,
    proxy_url: str | None,
    impersonate: str,
    timeout: float,
    max_retries: int,
) -> http.RequestOptions:
    return options or http.RequestOptions(proxy_url, impersonate, timeout, max_retries)


def _client_call(  # noqa: PLR0913, PLR0917 - shared detail-dispatch helper
    method: str,
    target: Any,
    options: http.RequestOptions | None,
    proxy_url: str | None,
    impersonate: str,
    timeout: float,
    max_retries: int,
) -> dict[str, Any]:
    """Lazy-import the default client and dispatch one detail fetch.

    The four public ``get_from_*`` wrappers share this body; each keeps its
    own signature so Typer/CLI and docstrings stay intact. The import is
    local to break the ``client -> details -> client`` cycle.
    """
    from pyzill.client import get_default_client

    return cast(
        dict[str, Any],
        getattr(get_default_client(), method)(
            target, options=_options(options, proxy_url, impersonate, timeout, max_retries)
        ),
    )


def get_from_home_id(  # noqa: PLR0913
    property_id: int,
    proxy_url: str | None = None,
    impersonate: str = http.DEFAULT_IMPERSONATE,
    timeout: float = http.DEFAULT_TIMEOUT,
    max_retries: int = http.DEFAULT_MAX_RETRIES,
    *,
    options: http.RequestOptions | None = None,
) -> dict[str, Any]:
    return _client_call("home", property_id, options, proxy_url, impersonate, timeout, max_retries)


def get_from_apartment_id(  # noqa: PLR0913
    apartment_id: str,
    proxy_url: str | None = None,
    impersonate: str = http.DEFAULT_IMPERSONATE,
    timeout: float = http.DEFAULT_TIMEOUT,
    max_retries: int = http.DEFAULT_MAX_RETRIES,
    *,
    options: http.RequestOptions | None = None,
) -> dict[str, Any]:
    return _client_call("apartment", apartment_id, options, proxy_url, impersonate, timeout, max_retries)


def get_from_apartment_url(  # noqa: PLR0913
    apartment_url: str,
    proxy_url: str | None = None,
    impersonate: str = http.DEFAULT_IMPERSONATE,
    timeout: float = http.DEFAULT_TIMEOUT,
    max_retries: int = http.DEFAULT_MAX_RETRIES,
    *,
    options: http.RequestOptions | None = None,
) -> dict[str, Any]:
    return _client_call("apartment", apartment_url, options, proxy_url, impersonate, timeout, max_retries)


def get_from_home_url(  # noqa: PLR0913
    home_url: str,
    proxy_url: str | None = None,
    impersonate: str = http.DEFAULT_IMPERSONATE,
    timeout: float = http.DEFAULT_TIMEOUT,
    max_retries: int = http.DEFAULT_MAX_RETRIES,
    *,
    options: http.RequestOptions | None = None,
) -> dict[str, Any]:
    return _client_call("home", home_url, options, proxy_url, impersonate, timeout, max_retries)


def _deprecated_alias(alias_name: str, target: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
    def deprecated(*args: Any, **kwargs: Any) -> dict[str, Any]:
        warnings.warn(f"{alias_name} is deprecated; use {target.__name__}", DeprecationWarning, stacklevel=2)
        return target(*args, **kwargs)

    deprecated.__name__ = alias_name
    deprecated.__doc__ = f"Deprecated alias for :func:`{target.__name__}`."
    return deprecated


get_from_department_id = _deprecated_alias("get_from_department_id", get_from_apartment_id)
get_from_department_url = _deprecated_alias("get_from_department_url", get_from_apartment_url)
