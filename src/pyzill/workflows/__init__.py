"""Application workflows: the operations the CLI and library users share.

This layer sits above the client and the database.  It holds the search
request objects, the viewport resolution, the listing extraction, and the
sync/import pipelines.

Nothing here prints and nothing here exits the process: the workflows
return data plus structured notices, and the caller (the CLI, or a library
user) decides what to report and how to react.

The package is split by job:

- :mod:`pyzill.workflows.model` -- the shapes the workflows speak in.
- :mod:`pyzill.workflows.viewport` -- resolving a search's viewport.
- :mod:`pyzill.workflows.extract` -- pulling the real listings out of a
  Zillow response.
- :mod:`pyzill.workflows.search` -- running one search.
- :mod:`pyzill.workflows.sync` -- the sync and import pipelines.
"""

from pyzill.workflows.extract import extract_listings, query_zipcode, select_listings, usable_row_count
from pyzill.workflows.model import (
    BoundingBoxError,
    Notice,
    RangeError,
    SearchFilters,
    SearchRequest,
    SearchResult,
    SyncResult,
    Viewport,
)
from pyzill.workflows.search import fetch_listings
from pyzill.workflows.sync import import_rows, prepare_import, sync_to_db
from pyzill.workflows.viewport import build_viewport, check_range, resolve_viewport

__all__ = [
    "BoundingBoxError",
    "Notice",
    "RangeError",
    "SearchFilters",
    "SearchRequest",
    "SearchResult",
    "SyncResult",
    "Viewport",
    "build_viewport",
    "check_range",
    "extract_listings",
    "fetch_listings",
    "import_rows",
    "prepare_import",
    "query_zipcode",
    "resolve_viewport",
    "select_listings",
    "sync_to_db",
    "usable_row_count",
]
