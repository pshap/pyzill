"""Shared search options and validation primitives for CLI commands."""

from dataclasses import dataclass
from typing import cast

from pyzill.bbox import BBox


@dataclass(frozen=True, slots=True)
class SearchOptions:
    query: str
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
    is_room: bool = False
    is_entire_place: bool = False


def build_bbox(north: float | None, south: float | None, east: float | None, west: float | None) -> BBox | None:
    values = {"north": north, "south": south, "east": east, "west": west}
    missing = [name for name, value in values.items() if value is None]
    if len(missing) == len(values):
        return None
    if missing:
        flags = ", ".join(f"--{name}" for name in missing)
        raise ValueError(f"A bounding box needs all four of --north/--south/--east/--west (missing: {flags})")
    try:
        return BBox(
            north=cast(float, north),
            south=cast(float, south),
            east=cast(float, east),
            west=cast(float, west),
        )
    except ValueError as exc:
        raise ValueError(f"Invalid bounding box: {exc}") from exc


def check_range(name: str, low: int | None, high: int | None) -> None:
    if low is not None and high is not None and low > high:
        raise ValueError(f"--min-{name} ({low}) is greater than --max-{name} ({high})")
