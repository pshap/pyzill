"""The data the workflows speak in: requests, results, notices, and errors.

These are the shapes that travel between the workflows and their callers.
They hold no behavior beyond the validation a shape can carry itself, so
every module in the package can depend on them without depending on each
other.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from pyzill.bbox import BBox
from pyzill.db import SyncStats
from pyzill.normalize import Listing

__all__ = [
    "BoundingBoxError",
    "Notice",
    "RangeError",
    "SearchFilters",
    "SearchRequest",
    "SearchResult",
    "SyncResult",
    "Viewport",
]


class BoundingBoxError(ValueError):
    """The viewport bounds are incomplete or invalid.

    ``missing`` names the bounds that were not given, so the caller can
    render a message in its own vocabulary.
    """

    def __init__(self, message: str, missing: Sequence[str] = ()) -> None:
        self.missing = tuple(missing)
        super().__init__(message)


class RangeError(ValueError):
    """A minimum filter is larger than its matching maximum."""

    def __init__(self, name: str, low: int, high: int) -> None:
        self.name = name
        self.low = low
        self.high = high
        super().__init__(f"{name} minimum {low} is greater than maximum {high}")


@dataclass(frozen=True, slots=True)
class Notice:
    """A non-fatal condition the caller may report to the user.

    The workflow layer reports conditions as structured notices instead of
    messages, so the wording belongs to the caller (the CLI) and not to the
    library.
    """

    code: str
    values: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SearchFilters:
    """The filters of one search (what to look for, not how to send it)."""

    north: float | None = None
    south: float | None = None
    east: float | None = None
    west: float | None = None
    zoom: int | None = None
    min_beds: int | None = None
    max_beds: int | None = None
    min_bathrooms: int | None = None
    max_bathrooms: int | None = None
    min_price: int | None = None
    max_price: int | None = None
    tile: bool = False
    max_depth: int = 4
    max_requests: int = 50


# How each search filter reaches the search request: the filter's name in
# :class:`SearchFilters` and the query field it fills.  The search request
# owns the wire names, so this table is the only place the two vocabularies
# meet.  Every filter is either listed here, is a viewport bound, or is a
# tiling knob; a test keeps that true.
FILTER_PARAMS: Mapping[str, str] = {
    "zoom": "zoom_value",
    "min_beds": "min_beds",
    "max_beds": "max_beds",
    "min_bathrooms": "min_bathrooms",
    "max_bathrooms": "max_bathrooms",
    "min_price": "min_price",
    "max_price": "max_price",
}

# The filters that describe the searched area.  They resolve to a viewport
# instead of reaching the search request.
VIEWPORT_FILTERS: tuple[str, ...] = ("north", "south", "east", "west")

# The filters that bound the tiling run.  They describe how the run is
# carried out, not what it searches, so they never reach the search request
# and are not part of the comparable-filter key.
BUDGET_FILTERS: tuple[str, ...] = ("tile", "max_depth", "max_requests")


@dataclass(frozen=True, slots=True)
class SearchRequest:
    """One search: the query, its filters, and the transport to use."""

    query: str
    filters: SearchFilters = field(default_factory=SearchFilters)
    is_room: bool = False
    is_entire_place: bool = False
    transport: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Viewport:
    """The resolved viewport of a search."""

    bbox: BBox
    display_name: str | None = None
    notices: list[Notice] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class SearchResult:
    """The outcome of one search."""

    raw: dict[str, Any]
    listings: list[Listing]
    raw_listings: list[Mapping[str, Any]]
    truncated: bool
    bbox: BBox
    zipcode: str | None = None
    notices: list[Notice] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class SyncResult:
    """The outcome of a sync or an import."""

    stats: SyncStats
    rows: Sequence[Mapping[str, Any]] = field(default_factory=list)
    truncated: bool = False
    run_id: int = 0
    notices: list[Notice] = field(default_factory=list)
