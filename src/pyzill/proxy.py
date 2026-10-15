from urllib.parse import quote, urlparse

from pyzill.config import read_env


def parse_proxy(ip_or_domain: str, port: str, username: str, password: str) -> str:
    """Build a proxy URL with URL-encoded credentials."""
    encoded_username = quote(username, safe="")
    encoded_password = quote(password, safe="")
    return f"http://{encoded_username}:{encoded_password}@{ip_or_domain}:{port}"


def resolve_proxy(proxy_url: str | None = None) -> str | None:
    """Resolve and validate an explicit or environment-configured proxy URL."""
    if proxy_url is None:
        proxy_url = read_env("ZILLOW_PROXY")
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
