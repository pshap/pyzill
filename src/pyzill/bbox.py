from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BBox:
    """A validated north/east/south/west search viewport."""

    north: float
    east: float
    south: float
    west: float

    def __post_init__(self) -> None:
        if self.north <= self.south or self.east <= self.west:
            raise ValueError("north/east bounds must be greater than south/west bounds")

    def to_map_bounds(self) -> dict[str, float]:
        """Return Zillow's map-bounds representation."""
        return {
            "north": self.north,
            "east": self.east,
            "south": self.south,
            "west": self.west,
        }

    def split(self) -> tuple[BBox, BBox, BBox, BBox]:
        """Split this viewport into four quadrants."""
        mid_lat = (self.north + self.south) / 2
        mid_long = (self.east + self.west) / 2
        return (
            BBox(self.north, mid_long, mid_lat, self.west),
            BBox(self.north, self.east, mid_lat, mid_long),
            BBox(mid_lat, mid_long, self.south, self.west),
            BBox(mid_lat, self.east, self.south, mid_long),
        )


CONTINENTAL_US = BBox(north=49.38, east=-66.94, south=24.54, west=-124.79)
