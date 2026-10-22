"""Browser identity for Zillow requests.

curl_cffi sets the TLS/JA3 fingerprint; these are the HTTP-side facts that
must agree with it. Presenting a Chrome User-Agent over a Safari
fingerprint is a classic bot signal, so every header here is derived from
the requested impersonation profile.
"""

from dataclasses import dataclass

from pyzill.config import DEFAULT_IMPERSONATE

__all__ = ["JSON_ACCEPT", "build_headers"]


_HTML_ACCEPT = (
    "text/html,application/xhtml+xml,application/xml;q=0.9,"
    "image/avif,image/webp,image/apng,*/*;q=0.8,"
    "application/signed-exchange;v=b3;q=0.7"
)
JSON_ACCEPT = "application/json, text/plain, */*"
_ACCEPT_LANGUAGE = "en-US,en;q=0.9"

_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/{version}.0.0.0 Safari/537.36"
)
_ANDROID_UA = (
    "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/{version}.0.0.0 Mobile Safari/537.36"
)
_EDGE_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/{version}.0.0.0 Safari/537.36 Edg/{version}.0.0.0"
)
_FIREFOX_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:{version}.0) Gecko/20100101 Firefox/{version}.0"
_SAFARI_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/{version} Safari/605.1.15"
)
_SAFARI_IOS_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS {os_version} like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/{version} Mobile/15E148 Safari/604.1"
)

_DEFAULT_CHROME_VERSION = DEFAULT_IMPERSONATE.removeprefix("chrome") or "136"
_DEFAULT_SAFARI_VERSION = "18.0"
_DEFAULT_FIREFOX_VERSION = "133"

# Safari packs "major.patch" into its profile name: "153" is 15.3, "170" is
# 17.0.  Two digits is a bare major, three is major+patch.
_SAFARI_MAJOR_ONLY_DIGITS = 2
_SAFARI_MAJOR_PATCH_DIGITS = 3


@dataclass(frozen=True, slots=True)
class _BrowserProfile:
    """The browser identity an impersonation profile should present.

    curl_cffi sets the TLS/JA3 fingerprint; these are the HTTP-side facts that
    must agree with it. Presenting a Chrome User-Agent over a Safari
    fingerprint is a classic bot signal, so every field here is derived from
    the requested profile.
    """

    user_agent: str
    sec_ch_ua: str | None
    sec_ch_ua_platform: str | None
    mobile: bool
    send_fetch_metadata: bool
    send_fetch_user: bool


def _digits(text: str) -> str:
    return "".join(char for char in text if char.isdigit())


def _sec_ch_ua(version: str, brand: str = '"Google Chrome"') -> str:
    """Build the Chromium client-hint brand list for a browser version.

    The list names the browser that sends it: Chrome sends Chromium and
    Chrome, Edge sends Chromium and Edge. A list that named both Chrome and
    Edge would be a fingerprint mismatch, so the brand is a parameter.
    """
    return f'"Chromium";v="{version}", {brand};v="{version}", "Not.A/Brand";v="99"'


def _safari_version(profile: str) -> tuple[str, bool]:
    """Return the (version, is_ios) pair for a Safari impersonation profile.

    Safari version numbers are packed into the profile name: ``safari184`` is
    18.4, ``safari15_3`` is 15.3, and ``safari172_ios`` is iOS 17.2.
    """
    body = profile.removeprefix("safari").lstrip("_")
    is_ios = body.endswith("_ios")
    if is_ios:
        body = body[: -len("_ios")]
    if "_" in body:
        major, _, minor = body.partition("_")
        return (f"{major}.{minor}" if minor else major), is_ios
    digits = _digits(body)
    if not digits:
        return _DEFAULT_SAFARI_VERSION, is_ios
    if len(digits) <= _SAFARI_MAJOR_ONLY_DIGITS:
        return f"{digits}.0", is_ios
    if len(digits) == _SAFARI_MAJOR_PATCH_DIGITS:
        return f"{digits[:2]}.{digits[2]}", is_ios
    return f"{digits[:2]}." + ".".join(digits[2:]), is_ios


def _resolve_profile(impersonate: str) -> _BrowserProfile:
    """Map a curl_cffi impersonation profile onto a matching browser identity."""
    name = impersonate.lower()
    if name.startswith("edge"):
        version = _digits(name) or _DEFAULT_CHROME_VERSION
        return _BrowserProfile(
            user_agent=_EDGE_UA.format(version=version),
            sec_ch_ua=_sec_ch_ua(version, '"Microsoft Edge"'),
            sec_ch_ua_platform='"Windows"',
            mobile=False,
            send_fetch_metadata=True,
            send_fetch_user=True,
        )
    if name.startswith("chrome"):
        version = _digits(name) or _DEFAULT_CHROME_VERSION
        android = "android" in name
        return _BrowserProfile(
            user_agent=(_ANDROID_UA if android else _CHROME_UA).format(version=version),
            sec_ch_ua=_sec_ch_ua(version),
            sec_ch_ua_platform='"Android"' if android else '"Windows"',
            mobile=android,
            send_fetch_metadata=True,
            send_fetch_user=True,
        )
    if name.startswith(("firefox", "tor")):
        version = _digits(name) or _DEFAULT_FIREFOX_VERSION
        return _BrowserProfile(
            user_agent=_FIREFOX_UA.format(version=version),
            sec_ch_ua=None,
            sec_ch_ua_platform=None,
            mobile=False,
            # Firefox sends neither Sec-Ch-Ua client hints nor Sec-Fetch
            # metadata; presenting them is a fingerprint mismatch.
            send_fetch_metadata=False,
            send_fetch_user=False,
        )
    if name.startswith("safari"):
        version, is_ios = _safari_version(name)
        user_agent = (
            _SAFARI_IOS_UA.format(version=version, os_version=version.replace(".", "_"))
            if is_ios
            else _SAFARI_UA.format(version=version)
        )
        return _BrowserProfile(
            user_agent=user_agent,
            sec_ch_ua=None,
            sec_ch_ua_platform=None,
            mobile=is_ios,
            send_fetch_metadata=True,
            send_fetch_user=False,
        )
    return _resolve_profile(DEFAULT_IMPERSONATE)


def _base_headers(profile: _BrowserProfile, accept: str, *, navigation: bool) -> dict[str, str]:
    """Build the headers every Zillow request carries."""
    headers = {
        "Accept": accept,
        "Accept-Language": _ACCEPT_LANGUAGE,
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "User-Agent": profile.user_agent,
    }
    if profile.sec_ch_ua is not None:
        headers["Sec-Ch-Ua"] = profile.sec_ch_ua
        headers["Sec-Ch-Ua-Mobile"] = "?1" if profile.mobile else "?0"
        if profile.sec_ch_ua_platform is not None:
            headers["Sec-Ch-Ua-Platform"] = profile.sec_ch_ua_platform
    if navigation:
        # Only document navigations carry this; an XHR does not.
        headers["Upgrade-Insecure-Requests"] = "1"
    return headers


def _fetch_headers(
    profile: _BrowserProfile,
    *,
    dest: str,
    mode: str,
    site: str,
    user_initiated: bool = False,
) -> dict[str, str]:
    """Build the Sec-Fetch-* metadata for a request, if the browser sends it."""
    if not profile.send_fetch_metadata:
        return {}
    headers = {
        "Sec-Fetch-Dest": dest,
        "Sec-Fetch-Mode": mode,
        "Sec-Fetch-Site": site,
    }
    if user_initiated and profile.send_fetch_user:
        headers["Sec-Fetch-User"] = "?1"
    return headers


def build_headers(  # noqa: PLR0913 - one builder for every Zillow request shape
    impersonate: str = DEFAULT_IMPERSONATE,
    *,
    accept: str = _HTML_ACCEPT,
    navigation: bool = True,
    fetch_dest: str = "document",
    fetch_mode: str = "navigate",
    fetch_site: str = "none",
    user_initiated: bool = True,
) -> dict[str, str]:
    """Return request headers matching a curl_cffi impersonate profile.

    curl_cffi already sets the TLS/JA3 fingerprint and most headers; these fill
    in the HTTP headers Zillow's bot detection inspects.  The User-Agent,
    client hints, and Sec-Fetch metadata are all derived from the requested
    profile, so the HTTP identity never contradicts the TLS fingerprint.

    Args:
        impersonate: curl_cffi impersonation profile name.
        accept: Value for the ``Accept`` header.
        navigation: Whether this is a document navigation (which sends
            ``Upgrade-Insecure-Requests``) rather than a scripted request.
        fetch_dest: ``Sec-Fetch-Dest`` value.
        fetch_mode: ``Sec-Fetch-Mode`` value.
        fetch_site: ``Sec-Fetch-Site`` value.
        user_initiated: Whether the request came from a user gesture, which
            adds ``Sec-Fetch-User`` on Chromium browsers.

    Returns:
        dict[str, str]: base headers (callers add request-specific ones).
    """
    profile = _resolve_profile(impersonate)
    headers = _base_headers(profile, accept, navigation=navigation)
    headers.update(
        _fetch_headers(
            profile,
            dest=fetch_dest,
            mode=fetch_mode,
            site=fetch_site,
            user_initiated=user_initiated,
        )
    )
    return headers
