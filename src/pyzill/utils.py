"""Small text helpers, plus compatibility re-exports of the proxy helpers.

The proxy functions used to live here. They now live in :mod:`pyzill.proxy`;
this module keeps re-exporting them so existing imports
(``from pyzill.utils import resolve_proxy``) keep working. New code should
import them from :mod:`pyzill.proxy`.
"""

import re
from typing import Any

from pyzill.proxy import parse_proxy as parse_proxy
from pyzill.proxy import redact_credentials as redact_credentials
from pyzill.proxy import redact_proxy_url as redact_proxy_url
from pyzill.proxy import resolve_proxy as resolve_proxy


def collapse_whitespace(value: str) -> str:
    """Collapse runs of whitespace to a single space."""
    return re.sub(r"\s+", " ", value.strip())


remove_space = collapse_whitespace


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
