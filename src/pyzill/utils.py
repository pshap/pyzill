"""Small helpers shared by the package core.

The proxy helpers used to live here. They now live in :mod:`pyzill.proxy`;
the old import paths still resolve, but reach them through a deprecation
shim so existing code keeps working while new code imports the real module.
"""

import random
import re
import warnings
from typing import Any

from pyzill.proxy import parse_proxy as _parse_proxy
from pyzill.proxy import redact_credentials as _redact_credentials
from pyzill.proxy import redact_proxy_url as _redact_proxy_url
from pyzill.proxy import resolve_proxy as _resolve_proxy

# The public proxy helpers, as the old path exposed them. Reaching one through
# this module is deprecated; ``__getattr__`` warns and returns the proxy
# module's function.
_PROXY_EXPORTS: dict[str, Any] = {
    "parse_proxy": _parse_proxy,
    "redact_credentials": _redact_credentials,
    "redact_proxy_url": _redact_proxy_url,
    "resolve_proxy": _resolve_proxy,
}


def __getattr__(name: str) -> Any:
    """Resolve a historical proxy re-export, with a deprecation warning."""
    if name in _PROXY_EXPORTS:
        warnings.warn(
            f"pyzill.utils.{name} is deprecated; import it from pyzill.proxy",
            DeprecationWarning,
            stacklevel=2,
        )
        return _PROXY_EXPORTS[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def collapse_whitespace(value: str) -> str:
    """Collapse runs of whitespace to a single space."""
    return re.sub(r"\s+", " ", value.strip())


def remove_space(value: str) -> str:
    """Deprecated alias for :func:`collapse_whitespace`."""
    warnings.warn(
        "pyzill.utils.remove_space is deprecated; use pyzill.utils.collapse_whitespace",
        DeprecationWarning,
        stacklevel=2,
    )
    return collapse_whitespace(value)


def jitter(maximum: float) -> float:
    """Return a random delay in ``[0, maximum]`` seconds.

    This is the one place pyzill draws a random number: the rate limiter's
    spacing and the retry backoff. None of those values is a secret -- they
    only spread requests out in time -- so the standard library's ``random``
    is the right tool and ``secrets`` would only cost more.
    """
    return random.uniform(0.0, maximum)  # noqa: S311 - jitter is not cryptographic


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
