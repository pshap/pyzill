"""Compatibility re-exports for the search helpers.

The search entry points live in :mod:`pyzill.api` (the module above the
client) and the request building lives in :mod:`pyzill.search_request`.
This module keeps the historical import paths (``pyzill.search.for_sale``
and friends) working.
"""

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

# Historical private aliases, kept so existing code that reached into this
# module for them keeps working.
_SEARCH_URL = SEARCH_URL
_resolve_bbox = resolve_bbox

__all__ = [
    "TiledSearchError",
    "for_rent",
    "for_sale",
    "iter_rent",
    "iter_sale",
    "iter_search",
    "iter_sold",
    "search",
    "sold",
    "split_bbox",
    "tiled_search",
]
