import os
from re import compile
from urllib.parse import quote, urlparse

regex_space = compile(r"[\s ]+")


def remove_space(value: str) -> str:
    """remove unwanted spaces in given string

    Args:
        value (str): input string with unwanted spaces

    Returns:
        str: string with single spaces
    """
    return regex_space.sub(" ", value.strip())


def get_nested_value(dic, key_path, default=None):
    keys = key_path.split(".")
    current = dic
    for key in keys:
        current = current.get(key, {})
        if current == {} or current is None:
            return default
    return current


def parse_proxy(ip_or_domain: str, port: str, username: str, password: str) -> str:
    """Build a proxy URL with URL-encoded credentials.

    Args:
        ip_or_domain: Proxy hostname or IP.
        port: Proxy port.
        username: Proxy username.
        password: Proxy password.

    Returns:
        str: proxy URL of the form http://user:pass@host:port
    """
    encoded_username = quote(username, safe="")
    encoded_password = quote(password, safe="")
    proxy_url = f"http://{encoded_username}:{encoded_password}@{ip_or_domain}:{port}"
    return proxy_url


def resolve_proxy(proxy_url: str | None = None) -> str | None:
    """Resolve the proxy URL to use, preferring an explicit argument.

    Falls back to the ``ZILLOW_PROXY`` environment variable when
    ``proxy_url`` is None.  Validates that the resolved URL has a scheme
    and hostname.

    Args:
        proxy_url: Explicit proxy URL, or None to fall back to the env var.

    Returns:
        str | None: A valid proxy URL, or None if no proxy is configured.

    Raises:
        ValueError: If the resolved proxy URL is malformed.
    """
    if proxy_url is None:
        proxy_url = os.environ.get("ZILLOW_PROXY")
    if proxy_url is None:
        return None
    parsed = urlparse(proxy_url)
    if parsed.scheme not in ("http", "https", "socks5", "socks5h"):
        raise ValueError(
            f"Invalid proxy URL scheme {parsed.scheme!r} in {proxy_url!r}; "
            "expected http, https, socks5, or socks5h"
        )
    if not parsed.hostname:
        raise ValueError(f"Invalid proxy URL {proxy_url!r}: missing hostname")
    return proxy_url
