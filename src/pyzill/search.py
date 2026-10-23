"""Compatibility re-exports for the search helpers.

The search entry points live in :mod:`pyzill.api` (the module above the
client) and the request building lives in :mod:`pyzill.search_request`.
This module keeps the historical import paths (``pyzill.search.for_sale``
and friends) working.
"""

import warnings

from pyzill.api import (
    for_rent,
    for_sale,
    iter_rent,
    iter_sale,
    iter_search,
    iter_sold,
    search,
    sold,
)
from pyzill.search_request import SEARCH_URL, resolve_bbox
from pyzill.search_request import SearchParams as SearchParams
from pyzill.tiling import TiledSearchError, split_bbox, tiled_search

warnings.warn(
    "pyzill.search is deprecated; import the entry points from pyzill.api",
    DeprecationWarning,
    stacklevel=2,
)


__all__ = [
    "SEARCH_URL",
    "TiledSearchError",
    "for_rent",
    "for_sale",
    "iter_rent",
    "iter_sale",
    "iter_search",
    "iter_sold",
    "resolve_bbox",
    "search",
    "sold",
    "split_bbox",
    "tiled_search",
]
