import warnings
from collections.abc import Callable
from typing import Any

from pyzill import http
from pyzill.parse import parse_body_departments, parse_body_home

_Parser = Callable[[bytes], dict[str, Any]]


def _fetch(url: str, parser: _Parser, options: http.RequestOptions) -> dict[str, Any]:
    """Fetch and parse one Zillow detail page."""
    return parser(http.get_page(url, options=options).content)


def get_from_home_id(
    property_id: int,
    proxy_url: str | None = None,
    impersonate: str = http.DEFAULT_IMPERSONATE,
    timeout: float = http.DEFAULT_TIMEOUT,
    max_retries: int = http.DEFAULT_MAX_RETRIES,
) -> dict[str, Any]:
    return _fetch(
        f"{http.ZILLOW_HOME}/homedetails/any-title/{property_id}_zpid/",
        parse_body_home,
        http.RequestOptions(proxy_url, impersonate, timeout, max_retries),
    )


def get_from_apartment_id(
    department_id: str,
    proxy_url: str | None = None,
    impersonate: str = http.DEFAULT_IMPERSONATE,
    timeout: float = http.DEFAULT_TIMEOUT,
    max_retries: int = http.DEFAULT_MAX_RETRIES,
) -> dict[str, Any]:
    return _fetch(
        f"{http.ZILLOW_HOME}/apartments/{department_id}/",
        parse_body_departments,
        http.RequestOptions(proxy_url, impersonate, timeout, max_retries),
    )


def get_from_apartment_url(
    department_url: str,
    proxy_url: str | None = None,
    impersonate: str = http.DEFAULT_IMPERSONATE,
    timeout: float = http.DEFAULT_TIMEOUT,
    max_retries: int = http.DEFAULT_MAX_RETRIES,
) -> dict[str, Any]:
    return _fetch(
        department_url,
        parse_body_departments,
        http.RequestOptions(proxy_url, impersonate, timeout, max_retries),
    )


def get_from_home_url(
    home_url: str,
    proxy_url: str | None = None,
    impersonate: str = http.DEFAULT_IMPERSONATE,
    timeout: float = http.DEFAULT_TIMEOUT,
    max_retries: int = http.DEFAULT_MAX_RETRIES,
) -> dict[str, Any]:
    return _fetch(
        home_url,
        parse_body_home,
        http.RequestOptions(proxy_url, impersonate, timeout, max_retries),
    )


def get_from_department_id(
    department_id: str,
    proxy_url: str | None = None,
    impersonate: str = http.DEFAULT_IMPERSONATE,
    timeout: float = http.DEFAULT_TIMEOUT,
    max_retries: int = http.DEFAULT_MAX_RETRIES,
) -> dict[str, Any]:
    """Deprecated alias for :func:`get_from_apartment_id`."""
    warnings.warn(
        "get_from_department_id is deprecated; use get_from_apartment_id",
        DeprecationWarning,
        stacklevel=2,
    )
    return get_from_apartment_id(department_id, proxy_url, impersonate, timeout, max_retries)


def get_from_department_url(
    department_url: str,
    proxy_url: str | None = None,
    impersonate: str = http.DEFAULT_IMPERSONATE,
    timeout: float = http.DEFAULT_TIMEOUT,
    max_retries: int = http.DEFAULT_MAX_RETRIES,
) -> dict[str, Any]:
    """Deprecated alias for :func:`get_from_apartment_url`."""
    warnings.warn(
        "get_from_department_url is deprecated; use get_from_apartment_url",
        DeprecationWarning,
        stacklevel=2,
    )
    return get_from_apartment_url(department_url, proxy_url, impersonate, timeout, max_retries)


def get_from_deparment_id(
    department_id: str,
    proxy_url: str | None = None,
    impersonate: str = http.DEFAULT_IMPERSONATE,
    timeout: float = http.DEFAULT_TIMEOUT,
    max_retries: int = http.DEFAULT_MAX_RETRIES,
) -> dict[str, Any]:
    """Deprecated alias for :func:`get_from_apartment_id`."""
    warnings.warn(
        "get_from_deparment_id is deprecated; use get_from_apartment_id",
        DeprecationWarning,
        stacklevel=2,
    )
    return get_from_apartment_id(department_id, proxy_url, impersonate, timeout, max_retries)


def get_from_deparment_url(
    department_url: str,
    proxy_url: str | None = None,
    impersonate: str = http.DEFAULT_IMPERSONATE,
    timeout: float = http.DEFAULT_TIMEOUT,
    max_retries: int = http.DEFAULT_MAX_RETRIES,
) -> dict[str, Any]:
    """Deprecated alias for :func:`get_from_apartment_url`."""
    warnings.warn(
        "get_from_deparment_url is deprecated; use get_from_apartment_url",
        DeprecationWarning,
        stacklevel=2,
    )
    return get_from_apartment_url(department_url, proxy_url, impersonate, timeout, max_retries)
