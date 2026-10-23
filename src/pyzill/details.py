"""Compatibility re-exports for the detail fetchers.

The ``get_from_*`` entry points live in :mod:`pyzill.api` (the module above
the client) and the page fetching lives in :mod:`pyzill.detail_request`.
This module keeps the historical import paths (``pyzill.details.get_from_home_id``
and friends) working.
"""

import warnings

from pyzill.api import (
    get_from_apartment_id,
    get_from_apartment_url,
    get_from_department_id,
    get_from_department_url,
    get_from_home_id,
    get_from_home_url,
)

warnings.warn(
    "pyzill.details is deprecated; import the entry points from pyzill.api",
    DeprecationWarning,
    stacklevel=2,
)


__all__ = [
    "get_from_apartment_id",
    "get_from_apartment_url",
    "get_from_department_id",
    "get_from_department_url",
    "get_from_home_id",
    "get_from_home_url",
]
