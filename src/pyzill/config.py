import os

DEFAULT_IMPERSONATE = "chrome136"
DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_RETRIES = 1

# Canonical Zillow origin. Lives here (the core layer) so the data layer
# (normalize) and the transport layer (http) can both use it without either
# importing the other.
ZILLOW_HOME = "https://www.zillow.com"

_CANONICAL_NAME_BY_ALIAS = {
    "ZILLOW_PROXY": "PYZILL_PROXY",
    "ZILLOW_MIN_INTERVAL": "PYZILL_RATE_LIMIT",
}
_LEGACY_ENV = {
    "PYZILL_PROXY": "ZILLOW_PROXY",
    "PYZILL_RATE_LIMIT": "ZILLOW_MIN_INTERVAL",
}


def read_env(name: str) -> str | None:
    """Read a canonical setting first, then its documented legacy fallback.

    A variable that is present but empty or whitespace-only is treated as
    unset, so an accidental ``PYZILL_PROXY=`` in the environment (common in
    CI) does not turn into a proxy validation error.
    """
    canonical = _CANONICAL_NAME_BY_ALIAS.get(name, name)
    legacy = _LEGACY_ENV.get(canonical)
    for candidate in (canonical, legacy):
        if candidate is None:
            continue
        value = os.environ.get(candidate)
        if value is not None and value.strip():
            return value
    value = os.environ.get(name)
    return value if value is not None and value.strip() else None
