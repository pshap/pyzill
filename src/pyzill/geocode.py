"""Resolve place names to bounding boxes via OpenStreetMap Nominatim.

Zillow searches need a map viewport; this turns a place name, ZIP, or street
address into one. Results are cached on disk so repeated queries for the same
place cost no network traffic.

Nominatim's usage policy requires a descriptive User-Agent and at most one
request per second; this module's own limiter plus the on-disk cache keep us
well under that.

This module talks to Nominatim directly instead of going through
``pyzill.http``: it is a different host with a different identity (a
descriptive tool User-Agent, not a browser fingerprint), its own one-request
per-second policy, and its own cache. The shared transport is for Zillow.
"""

import json
import math
import os
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from curl_cffi import requests

from pyzill import http
from pyzill.bbox import BBox
from pyzill.ratelimit import RateLimiter
from pyzill.utils import resolve_proxy

try:  # pragma: no cover - Windows has no fcntl
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"

# The viewport flags a caller can use instead of geocoding. Every error
# message points at them, so the list lives in one place.
_VIEWPORT_FLAGS = "--north/--south/--east/--west"
_NO_GEOCODE_HINT = f"Pass {_VIEWPORT_FLAGS} to search without geocoding."
_NO_RESULT_HINT = f"Try a more specific name, or pass {_VIEWPORT_FLAGS}."


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
# Nominatim's boundingbox is [south, north, west, east].
_BBOX_LENGTH = 4
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


@contextmanager
def _cache_lock(path: Path) -> Iterator[None]:
    """Hold an exclusive lock on the geocode cache for one update.

    The cache is a read-modify-write of a whole JSON file, so two processes
    geocoding at the same time would otherwise lose one another's entries.
    The lock lives in a sibling file because the cache itself is replaced
    atomically (swapping the inode would drop a lock held on it).

    Locking is best-effort: on a platform without ``fcntl``, or when the lock
    file cannot be created, the update still proceeds.
    """
    lock_path = path.with_name(f"{path.name}.lock")
    try:
        handle = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:
        yield
        return
    try:
        if fcntl is not None:
            fcntl.flock(handle, fcntl.LOCK_EX)
        yield
    finally:
        os.close(handle)


def _store_cache(path: Path, key: str, entry: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _cache_lock(path):
            cache = _load_cache(path)
            cache[key] = {**entry, "cached_at": time.time()}
            temporary_path: Path | None = None
            try:
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
    except OSError:
        pass


def _geocode_error(place: str, detail: str, hint: str = _NO_GEOCODE_HINT) -> ValueError:
    """Build the error for a failed lookup.

    Every failure points at the same hint, so the wording lives in one place
    instead of being copied into each message.
    """
    return ValueError(f"Geocoding {place!r} {detail}. {hint}")


def _cached_result(cache_path: Path, key: str, place: str) -> GeoResult | None:
    """Return the cached result for a place, or None if there is no fresh one.

    A corrupt or expired entry is treated as no entry: the lookup is redone.
    """
    cached = _load_cache(cache_path).get(key)
    if not (
        isinstance(cached, dict)
        and isinstance(cached.get("cached_at"), (int, float))
        and time.time() - cached["cached_at"] < _CACHE_TTL_SECONDS
        and {"north", "south", "east", "west"} <= cached.keys()
    ):
        return None
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
        return None  # Corrupt entry; re-fetch.


def _fetch_hits(place: str, proxy_url: str | None) -> list[Any]:
    """Ask Nominatim for the hits matching a place.

    Raises:
        ValueError: If the request failed, or the response was not a list.
    """
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
        http.raise_for_status(response)
        hits = http.json_body(response)
    except (OSError, ValueError) as exc:
        # Network failures, HTTP errors, and unparseable bodies all mean the
        # same thing to the caller: the place could not be resolved.
        raise _geocode_error(place, f"failed: {exc}") from exc
    if not isinstance(hits, list):
        raise _geocode_error(place, "returned an unexpected response shape")
    return hits


def _result_from_hit(hit: Any, place: str) -> GeoResult:
    """Build the search viewport for one Nominatim hit.

    Raises:
        ValueError: If the hit does not carry a usable bounding box.
    """
    if not isinstance(hit, dict):
        raise _geocode_error(place, "returned an unexpected result")
    # Nominatim's boundingbox is [south, north, west, east], all strings.
    raw_bbox = hit.get("boundingbox")
    if not isinstance(raw_bbox, (list, tuple)) or len(raw_bbox) != _BBOX_LENGTH:
        raise _geocode_error(place, "returned no usable bounding box")
    try:
        south, north, west, east = (float(x) for x in raw_bbox)
    except (TypeError, ValueError) as exc:
        raise _geocode_error(place, "returned a malformed bounding box") from exc
    if not all(math.isfinite(value) for value in (north, south, east, west)):
        # NaN/inf bounds survive the BBox comparison (every comparison with
        # NaN is False) and would silently produce a nonsense search viewport.
        raise _geocode_error(place, "returned a non-finite bounding box")
    synthetic_bbox = north - south < 2 * _MIN_HALF_EXTENT or east - west < 2 * _MIN_HALF_EXTENT
    north, south, east, west = _expand_viewport(north, south, east, west)

    try:
        bbox = BBox(north=north, south=south, east=east, west=west)
    except ValueError as exc:
        raise _geocode_error(place, f"returned an unusable viewport ({exc})") from exc

    return GeoResult(
        display_name=str(hit.get("display_name", place)),
        bbox=bbox,
        synthetic_bbox=synthetic_bbox,
    )


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
        ValueError: no result, the response was malformed, or the network
            request failed.
    """
    place = place.strip()
    if not place:
        raise ValueError("Cannot geocode an empty place name")

    cache_path = cache_path or default_cache_path()
    key = f"us:jsonv2:{place.lower()}"

    cached = _cached_result(cache_path, key, place)
    if cached is not None:
        return cached

    hits = _fetch_hits(place, proxy_url)
    if not hits:
        raise ValueError(f"No geocoding result for {place!r}. {_NO_RESULT_HINT}")

    result = _result_from_hit(hits[0], place)
    _store_cache(
        cache_path,
        key,
        {
            "display_name": result.display_name,
            "north": result.bbox.north,
            "south": result.bbox.south,
            "east": result.bbox.east,
            "west": result.bbox.west,
            "synthetic_bbox": result.synthetic_bbox,
        },
    )
    return result
