from typing import Any

from pyzill import http
from pyzill.parse import parse_body_departments, parse_body_home


def _get(url: str, proxy_url: str | None, impersonate: str, timeout: float) -> Any:
    """Fetch a detail page and enforce a 2xx response."""
    headers = {**http.build_headers(impersonate), "Sec-Fetch-Site": "none"}
    response = http.request(
        "GET",
        url,
        headers=headers,
        proxy_url=proxy_url,
        impersonate=impersonate,
        timeout=timeout,
    )
    response.raise_for_status()
    return response


def get_from_home_id(
    property_id: int,
    proxy_url: str | None = None,
    impersonate: str = "chrome136",
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Scrape data for a property based on its Zillow property ID (zpid).

    Args:
        property_id: Zillow property ID (zpid).
        proxy_url: Proxy URL (falls back to ZILLOW_PROXY env var).
        impersonate: curl_cffi TLS impersonation profile.
        timeout: Request timeout in seconds.

    Returns:
        dict[str, Any]: parsed property information
    """
    home_url = f"https://www.zillow.com/homedetails/any-title/{property_id}_zpid/"
    return get_from_home_url(home_url, proxy_url, impersonate, timeout)


def get_from_department_id(
    department_id: str,
    proxy_url: str | None = None,
    impersonate: str = "chrome136",
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Scrape data for an apartment/department based on its Zillow ID.

    Args:
        department_id: Zillow apartment complex ID.
        proxy_url: Proxy URL (falls back to ZILLOW_PROXY env var).
        impersonate: curl_cffi TLS impersonation profile.
        timeout: Request timeout in seconds.

    Returns:
        dict[str, Any]: parsed department information
    """
    home_url = f"https://www.zillow.com/apartments/{department_id}/"
    response = _get(home_url, proxy_url, impersonate, timeout)
    return parse_body_departments(response.content)


def get_from_department_url(
    department_url: str,
    proxy_url: str | None = None,
    impersonate: str = "chrome136",
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Scrape data for an apartment/departement based on its Zillow URL.

    Args:
        department_url: Full Zillow apartment URL.
        proxy_url: Proxy URL (falls back to ZILLOW_PROXY env var).
        impersonate: curl_cffi TLS impersonation profile.
        timeout: Request timeout in seconds.

    Returns:
        dict[str, Any]: parsed department information
    """
    response = _get(department_url, proxy_url, impersonate, timeout)
    return parse_body_departments(response.content)


def get_from_home_url(
    home_url: str,
    proxy_url: str | None = None,
    impersonate: str = "chrome136",
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Scrape a Zillow home-detail URL and parse the property data.

    Args:
        home_url: Full Zillow home-detail URL.
        proxy_url: Proxy URL (falls back to ZILLOW_PROXY env var).
        impersonate: curl_cffi TLS impersonation profile.
        timeout: Request timeout in seconds.

    Returns:
        dict[str, Any]: parsed property information
    """
    response = _get(home_url, proxy_url, impersonate, timeout)
    return parse_body_home(response.content)


# Backwards-compatible aliases for the historical "deparment" misspelling.
get_from_deparment_id = get_from_department_id
get_from_deparment_url = get_from_department_url
