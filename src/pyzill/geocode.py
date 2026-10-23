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

from pyzill.bbox import BBox
from pyzill.proxy import resolve_proxy
from pyzill.ratelimit import RateLimiter
from pyzill.responses import json_body, raise_for_status

try:  # pragma: no cover - Windows has no fcntl
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"

__all__ = [
    "GeoResult",
    "GeocodeError",
    "GeocodeUnavailableError",
    "default_cache_path",
    "geocode",
]


class GeocodeError(ValueError):
    """A geocoding lookup failed.

    A subclass of :class:`ValueError`, so the callers that already handle a
    failed lookup keep working. ``retryable`` says whether trying again
    could succeed: a place that does not resolve will not resolve on a
    second try, but an unreachable service might.
    """

    retryable = False


class GeocodeUnavailableError(GeocodeError):
    """The geocoding service could not be reached, or returned an error."""

    retryable = True


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
# How many places the cache keeps. The cache is one JSON file that is parsed
# on every lookup and rewritten on every new place, so it has to stay small:
# 1000 entries are about 300 KiB and parse in a few milliseconds.
_CACHE_MAX_ENTRIES = 1000
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


def _read_cache_file(path: Path) -> dict[str, Any]:
    """Read and parse the cache file."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _cache_stamp(path: Path) -> tuple[str, int, int, int] | None:
    """Return a cheap fingerprint of the cache file, or None if it is gone.

    The fingerprint changes whenever the file does, so the parsed cache can
    be reused until another process (or this one) writes to it.
    """
    try:
        stat = path.stat()
    except OSError:
        return None
    return (str(path), stat.st_mtime_ns, stat.st_size, stat.st_ino)


class _CacheMemo:
    """The parsed cache, reused until the file changes on disk."""

    def __init__(self) -> None:
        self.entries: dict[str, Any] = {}
        self.stamp: tuple[str, int, int, int] | None = None


# Re-parsing the whole file on every lookup is wasteful when the file has
# not changed since the last read.
_cache_memo = _CacheMemo()


def _load_cache(path: Path) -> dict[str, Any]:
    """Return the cached entries, re-parsing the file only when it changed.

    The returned mapping is the shared in-memory copy: treat it as read-only
    (callers that change it copy it first).
    """
    stamp = _cache_stamp(path)
    if stamp is not None and stamp == _cache_memo.stamp:
        return _cache_memo.entries
    cache = _read_cache_file(path)
    _cache_memo.entries = cache
    _cache_memo.stamp = stamp
    return cache


def _entry_timestamp(entry: Any) -> float:
    """Return an entry's age stamp, or 0 for an entry that has none."""
    if not isinstance(entry, dict):
        return 0.0
    value = entry.get("cached_at", 0)
    return float(value) if isinstance(value, (int, float)) else 0.0


def _prune_cache(cache: dict[str, Any]) -> dict[str, Any]:
    """Drop expired entries, then the oldest ones over the entry cap.

    Expiry is lazy: the TTL is checked when an entry is read, and the stale
    ones are removed the next time the file is written. Pruning on write
    keeps the file (and the time spent parsing it) bounded.
    """
    now = time.time()
    fresh = {
        key: entry
        for key, entry in cache.items()
        if isinstance(entry, dict) and now - _entry_timestamp(entry) < _CACHE_TTL_SECONDS
    }
    overflow = len(fresh) - _CACHE_MAX_ENTRIES
    if overflow > 0:
        oldest = sorted(fresh.items(), key=lambda item: _entry_timestamp(item[1]))[:overflow]
        for key, _ in oldest:
            del fresh[key]
    return fresh


@contextmanager
def _cache_lock(path: Path) -> Iterator[None]:
    """Hold an exclusive lock while the cache file is updated.

    The cache file is replaced atomically (a fresh inode takes its place), so
    a lock held on the file itself would not survive the swap. The lock is
    taken on the cache *directory* instead, whose inode is stable -- which
    also keeps the cache directory free of a lock file.

    Locking is best-effort: on a platform without ``fcntl``, or when the
    directory cannot be opened, the update still proceeds.
    """
    if fcntl is None:
        yield
        return
    try:
        handle = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        yield
        return
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield
    finally:
        os.close(handle)


def _remove_legacy_lock(path: Path) -> None:
    """Remove the lock file older versions of pyzill left next to the cache.

    The lock now lives on the cache directory, so the sibling file is dead
    weight. Removing it is best-effort: a pyzill process of an older version
    that is still running would lose its mutual exclusion, which costs at
    most one cache entry.
    """
    with suppress(OSError):
        path.with_name(f"{path.name}.lock").unlink()


def _store_cache(path: Path, key: str, entry: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _cache_lock(path):
            # Copy: the loaded cache is the shared in-memory copy.
            cache = dict(_load_cache(path))
            cache[key] = {**entry, "cached_at": time.time()}
            cache = _prune_cache(cache)
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
            _remove_legacy_lock(path)
    except OSError:
        pass


def _geocode_error(place: str, detail: str, hint: str = _NO_GEOCODE_HINT) -> GeocodeError:
    """Build the error for a failed lookup.

    Every failure points at the same hint, so the wording lives in one place
    instead of being copied into each message.
    """
    return GeocodeError(f"Geocoding {place!r} {detail}. {hint}")


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
    resolved_proxy = resolve_proxy(proxy_url) if proxy_url else None
    try:
        _nominatim_limiter.wait()
        response = requests.get(
            NOMINATIM_URL,
            params={"q": place, "format": "jsonv2", "limit": 1, "countrycodes": "us"},
            headers={"User-Agent": _user_agent()},
            proxies={"http": resolved_proxy, "https": resolved_proxy} if resolved_proxy else None,
            timeout=_TIMEOUT,
        )
        raise_for_status(response)
        hits = json_body(response)
    except GeocodeError:
        raise
    except ValueError as exc:
        # The request reached the service, but its body was not JSON. That is
        # an unavailable lookup, not a place that does not resolve.
        raise GeocodeUnavailableError(f"Geocoding {place!r} failed: {exc}. {_NO_GEOCODE_HINT}") from exc
    except OSError as exc:
        # Network failures, HTTP errors, and unparseable bodies all mean the
        # same thing to the caller: the place could not be resolved now. They
        # are retryable, unlike a place that simply does not resolve.
        raise GeocodeUnavailableError(f"Geocoding {place!r} failed: {exc}. {_NO_GEOCODE_HINT}") from exc
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
        GeocodeError: the place did not resolve, or the response was
            malformed. Not retryable.
        GeocodeUnavailableError: the request failed. Retryable.
    """
    place = place.strip()
    if not place:
        raise GeocodeError("Cannot geocode an empty place name")

    cache_path = cache_path or default_cache_path()
    key = f"us:jsonv2:{place.lower()}"

    cached = _cached_result(cache_path, key, place)
    if cached is not None:
        return cached

    hits = _fetch_hits(place, proxy_url)
    if not hits:
        raise GeocodeError(f"No geocoding result for {place!r}. {_NO_RESULT_HINT}")

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
