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
import tempfile
import time
from contextlib import suppress
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from curl_cffi import requests

from pyzill.bbox import BBox
from pyzill.ratelimit import RateLimiter
from pyzill.utils import resolve_proxy

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"


def _user_agent() -> str:
    """Identify the tool to Nominatim, tolerating missing package metadata.

    ``version()`` raises ``PackageNotFoundError`` when pyzill is not installed
    (run from source, a zipapp, or a PyInstaller bundle), which must not break
    ``import pyzill.cli``.
    """
    try:
        ver = version("pyzill")
    except PackageNotFoundError:
        ver = "unknown"
    return f"pyzill/{ver} (Zillow research CLI; personal use)"


_TIMEOUT = 15.0

# Address-level hits come back with a viewport of a few meters, which is too
# small for a Zillow map search; expand anything smaller than a neighbourhood.
_MIN_HALF_EXTENT = 0.01  # degrees, roughly 1 km
_CACHE_TTL_SECONDS = 30 * 24 * 60 * 60
_nominatim_limiter = RateLimiter(min_interval=1.0, jitter=0)


@dataclass(frozen=True, slots=True)
class GeoResult:
    """A geocoding hit: display name plus the bounding box to search."""

    display_name: str
    bbox: BBox
    synthetic_bbox: bool = False


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
    cache[key] = {**entry, "cached_at": time.time()}
    temporary_path: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as temporary:
            temporary.write(json.dumps(cache, indent=2))
            temporary_path = Path(temporary.name)
        temporary_path.replace(path)
    except OSError:
        pass  # A read-only cache dir is not fatal; the answer still gets returned.
    finally:
        if temporary_path is not None:
            with suppress(OSError):
                temporary_path.unlink(missing_ok=True)


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
        proxy_url: Optional explicit proxy for the Nominatim request.

    Returns:
        GeoResult: display name plus a validated BBox.

    Raises:
        ValueError: no result, or the network request failed.
    """
    place = place.strip()
    if not place:
        raise ValueError("Cannot geocode an empty place name")

    cache_path = cache_path or default_cache_path()
    key = f"us:jsonv2:{place.lower()}"

    cached = _load_cache(cache_path).get(key)
    if (
        isinstance(cached, dict)
        and isinstance(cached.get("cached_at"), (int, float))
        and time.time() - cached["cached_at"] < _CACHE_TTL_SECONDS
        and {"north", "south", "east", "west"} <= cached.keys()
    ):
        try:
            return GeoResult(
                display_name=str(cached.get("display_name", place)),
                bbox=BBox(
                    north=float(cached["north"]),
                    south=float(cached["south"]),
                    east=float(cached["east"]),
                    west=float(cached["west"]),
                ),
                synthetic_bbox=bool(cached.get("synthetic_bbox", False)),
            )
        except (TypeError, ValueError):
            pass  # Corrupt entry; re-fetch.

    try:
        _nominatim_limiter.wait()
        resolved_proxy = resolve_proxy(proxy_url) if proxy_url else None
        response = requests.get(
            NOMINATIM_URL,
            params={"q": place, "format": "jsonv2", "limit": 1, "countrycodes": "us"},
            headers={"User-Agent": _user_agent()},
            proxies={"http": resolved_proxy, "https": resolved_proxy} if resolved_proxy else None,
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
    synthetic_bbox = north - south < 2 * _MIN_HALF_EXTENT or east - west < 2 * _MIN_HALF_EXTENT
    north, south, east, west = _expand_viewport(north, south, east, west)

    result = GeoResult(
        display_name=str(hit.get("display_name", place)),
        bbox=BBox(north=north, south=south, east=east, west=west),
        synthetic_bbox=synthetic_bbox,
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
            "synthetic_bbox": synthetic_bbox,
        },
    )
    return result
