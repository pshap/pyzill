from typing import Any

from curl_cffi import requests

from pyzill.parse import parse_body_deparments, parse_body_home
from pyzill.ratelimit import wait_for_slot
from pyzill.utils import resolve_proxy

headers = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",  # noqa: E501
    "Accept-Language": "en",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Sec-Ch-Ua": '"Chromium";v="136", "Google Chrome";v="136", "Not.A/Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",  # noqa: E501
}


def _get(url: str, proxy_url: str | None, impersonate: str, timeout: float) -> Any:
    proxy_url = resolve_proxy(proxy_url)
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
    wait_for_slot()
    response = requests.get(
        url=url,
        headers=headers,
        proxies=proxies,
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


def get_from_deparment_id(
    deparment_id: str,
    proxy_url: str | None = None,
    impersonate: str = "chrome136",
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Scrape data for an apartment/deparment based on its Zillow ID.

    Args:
        deparment_id: Zillow apartment complex ID.
        proxy_url: Proxy URL (falls back to ZILLOW_PROXY env var).
        impersonate: curl_cffi TLS impersonation profile.
        timeout: Request timeout in seconds.

    Returns:
        dict[str, Any]: parsed parment information
    """
    home_url = f"https://www.zillow.com/apartments/texas/the-lennox/{deparment_id}"
    response = _get(home_url, proxy_url, impersonate, timeout)
    return parse_body_deparments(response.content)


def get_from_deparment_url(
    deparment_url: str,
    proxy_url: str | None = None,
    impersonate: str = "chrome136",
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Scrape data for an apartment/deparment based on its Zillow URL.

    Args:
        deparment_url: Full Zillow apartment URL.
        proxy_url: Proxy URL (falls back to ZILLOW_PROXY env var).
        impersonate: curl_cffi TLS impersonation profile.
        timeout: Request timeout in seconds.

    Returns:
        dict[str, Any]: parsed parment information
    """
    response = _get(deparment_url, proxy_url, impersonate, timeout)
    return parse_body_deparments(response.content)


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
