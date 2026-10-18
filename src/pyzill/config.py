import os

DEFAULT_IMPERSONATE = "chrome136"
DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_RETRIES = 1

_CANONICAL_NAME_BY_ALIAS = {
    "ZILLOW_PROXY": "PYZILL_PROXY",
    "ZILLOW_MIN_INTERVAL": "PYZILL_RATE_LIMIT",
}
_LEGACY_ENV = {
    "PYZILL_PROXY": "ZILLOW_PROXY",
    "PYZILL_RATE_LIMIT": "ZILLOW_MIN_INTERVAL",
}


def read_env(name: str) -> str | None:
    """Read a canonical setting first, then its documented legacy fallback."""
    canonical = _CANONICAL_NAME_BY_ALIAS.get(name, name)
    legacy = _LEGACY_ENV.get(canonical)
    for candidate in (canonical, legacy):
        if candidate is not None and candidate in os.environ:
            return os.environ[candidate]
    return os.environ.get(name)
