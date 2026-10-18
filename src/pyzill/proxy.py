import ipaddress
from urllib.parse import quote, urlparse

from pyzill.config import read_env

_MAX_PORT = 65535


def parse_proxy(ip_or_domain: str, port: str, username: str, password: str, scheme: str = "http") -> str:
    """Build a proxy URL with URL-encoded credentials."""
    if scheme not in {"http", "https", "socks5", "socks5h"}:
        raise ValueError("scheme must be http, https, socks5, or socks5h")
    try:
        port_number = int(port)
    except ValueError as exc:
        raise ValueError("port must be an integer between 1 and 65535") from exc
    if not 1 <= port_number <= _MAX_PORT:
        raise ValueError("port must be an integer between 1 and 65535")
    host = ip_or_domain.strip()
    if ":" in host and not (host.startswith("[") and host.endswith("]")):
        try:
            ipaddress.IPv6Address(host)
        except ipaddress.AddressValueError as exc:
            raise ValueError("host must be a valid hostname or IPv6 address") from exc
        host = f"[{host}]"
    encoded_username = quote(username, safe="")
    encoded_password = quote(password, safe="")
    return f"{scheme}://{encoded_username}:{encoded_password}@{host}:{port_number}"


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
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"Invalid proxy URL {proxy_url!r}: invalid port") from exc
    if port is not None and not 1 <= port <= _MAX_PORT:
        raise ValueError(f"Invalid proxy URL {proxy_url!r}: port must be between 1 and 65535")
    return proxy_url
