import os
import re
from typing import Any
from urllib.parse import quote, urlparse

regex_space = re.compile(r"[\s ]+")


def remove_space(value: str) -> str:
    """remove unwanted spaces in given string

    Args:
        value (str): input string with unwanted spaces

    Returns:
        str: string with single spaces
    """
    return regex_space.sub(" ", value.strip())


def get_nested_value(dic: Any, key_path: str, default: Any = None) -> Any:
    """Return the value at a dot-separated key path, or ``default`` if absent.

    Args:
        dic: Mapping to traverse (any non-mapping input yields ``default``).
        key_path: Dot-separated keys, e.g. ``"a.b.c"``.
        default: Value returned when a key is missing or a step is not a
            mapping.

    Returns:
        Any: the nested value, or ``default``.
    """
    current = dic
    for key in key_path.split("."):
        if not isinstance(current, dict) or key not in current:
            return default
        current = current[key]
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
    return f"http://{encoded_username}:{encoded_password}@{ip_or_domain}:{port}"


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
            f"Invalid proxy URL scheme {parsed.scheme!r} in {proxy_url!r}; expected http, https, socks5, or socks5h"
        )
    if not parsed.hostname:
        raise ValueError(f"Invalid proxy URL {proxy_url!r}: missing hostname")
    return proxy_url
