"""Resolve place names to bounding boxes via OpenStreetMap Nominatim.

Zillow searches need a map viewport; this turns a place name, ZIP, or street
address into one. Results are cached on disk so repeated queries for the same
place cost no network traffic.

Nominatim's usage policy requires a descriptive User-Agent and at most one
request per second; the CLI's caller-side throttle plus the on-disk cache keep
us well under that.
"""

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from curl_cffi import requests

from pyzill.bbox import BBox
from pyzill.http import build_proxies

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"

# Nominatim rejects generic library User-Agents; identify the tool and purpose.
_USER_AGENT = "pyzill/1.0 (Zillow research CLI; personal use)"

_TIMEOUT = 15.0

# Address-level hits come back with a viewport of a few meters, which is too
# small for a Zillow map search; expand anything smaller than a neighbourhood.
_MIN_HALF_EXTENT = 0.01  # degrees, roughly 1 km


@dataclass(frozen=True, slots=True)
class GeoResult:
    """A geocoding hit: display name plus the bounding box to search."""

    display_name: str
    bbox: BBox


def default_cache_path() -> Path:
    """Return the on-disk cache location, honoring XDG_CACHE_HOME."""
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "pyzill" / "geocode.json"


def _expand_viewport(north: float, south: float, east: float, west: float) -> tuple[float, float, float, float]:
    """Grow a too-small viewport to a minimum neighbourhood-sized box."""
    if north - south < 2 * _MIN_HALF_EXTENT:
        mid = (north + south) / 2
        north, south = mid + _MIN_HALF_EXTENT, mid - _MIN_HALF_EXTENT
    if east - west < 2 * _MIN_HALF_EXTENT:
        mid = (east + west) / 2
        east, west = mid + _MIN_HALF_EXTENT, mid - _MIN_HALF_EXTENT
    return north, south, east, west


def _load_cache(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _store_cache(path: Path, key: str, entry: dict[str, Any]) -> None:
    cache = _load_cache(path)
    cache[key] = entry
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cache, indent=2), encoding="utf-8")
    except OSError:
        pass  # A read-only cache dir is not fatal; the answer still gets returned.


def geocode(
    place: str,
    *,
    cache_path: Path | None = None,
    proxy_url: str | None = None,
) -> GeoResult:
    """Resolve a US place name, ZIP, or address to a Zillow-ready bounding box.

    Args:
        place: Free-form location text ('Schenectady NY', '12305', a street address).
        cache_path: JSON cache file; defaults to ``~/.cache/pyzill/geocode.json``.
        proxy_url: Optional proxy for the Nominatim request (falls back to ZILLOW_PROXY).

    Returns:
        GeoResult: display name plus a validated BBox.

    Raises:
        ValueError: no result, or the network request failed.
    """
    place = place.strip()
    if not place:
        raise ValueError("Cannot geocode an empty place name")

    cache_path = cache_path or default_cache_path()
    key = place.lower()

    cached = _load_cache(cache_path).get(key)
    if isinstance(cached, dict) and {"north", "south", "east", "west"} <= cached.keys():
        try:
            return GeoResult(
                display_name=str(cached.get("display_name", place)),
                bbox=BBox(
                    north=float(cached["north"]),
                    south=float(cached["south"]),
                    east=float(cached["east"]),
                    west=float(cached["west"]),
                ),
            )
        except (TypeError, ValueError):
            pass  # Corrupt entry; re-fetch.

    try:
        response = requests.get(
            NOMINATIM_URL,
            params={"q": place, "format": "jsonv2", "limit": 1, "countrycodes": "us"},
            headers={"User-Agent": _USER_AGENT},
            proxies=build_proxies(proxy_url),
            timeout=_TIMEOUT,
        )
        response.raise_for_status()  # type: ignore[no-untyped-call]
        hits = response.json()  # type: ignore[no-untyped-call]
    except Exception as e:
        raise ValueError(
            f"Geocoding {place!r} failed: {e}. Pass --north/--south/--east/--west to search without geocoding."
        ) from e

    if not hits:
        raise ValueError(
            f"No geocoding result for {place!r}. Try a more specific name, or pass --north/--south/--east/--west."
        )

    hit = hits[0]
    # Nominatim's boundingbox is [south, north, west, east], all strings.
    south, north, west, east = (float(x) for x in hit["boundingbox"])
    north, south, east, west = _expand_viewport(north, south, east, west)

    result = GeoResult(
        display_name=str(hit.get("display_name", place)),
        bbox=BBox(north=north, south=south, east=east, west=west),
    )
    _store_cache(
        cache_path,
        key,
        {
            "display_name": result.display_name,
            "north": north,
            "south": south,
            "east": east,
            "west": west,
        },
    )
    return result
