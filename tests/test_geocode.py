"""Tests for the Nominatim geocoding helper."""

import json
import time
from importlib.metadata import version
from typing import Any

import pytest

from pyzill.bbox import BBox
from pyzill.geocode import GeoResult, _expand_viewport, default_cache_path, geocode


class _Resp:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self) -> Any:
        return self._payload


@pytest.fixture
def cache_path(tmp_path):
    return tmp_path / "geocode.json"


def _install_get(monkeypatch: pytest.MonkeyPatch, payload: Any = None, exc: Exception | None = None) -> list[dict]:
    calls: list[dict] = []

    def fake_get(url: str, **kwargs: Any) -> _Resp:
        calls.append(kwargs)
        if exc is not None:
            raise exc
        return _Resp(payload or [])

    monkeypatch.setattr("pyzill.geocode.requests.get", fake_get)
    return calls


HIT = [
    {
        "display_name": "Schenectady, New York, United States",
        "boundingbox": ["42.7634", "42.8438", "-73.9834", "-73.8931"],
    }
]


class TestGeocode:
    def test_user_agent_falls_back_when_metadata_missing(self, monkeypatch):
        from importlib.metadata import PackageNotFoundError

        import pyzill.geocode as geocode_module

        def _missing(_name: str) -> str:
            raise PackageNotFoundError("pyzill")

        monkeypatch.setattr(geocode_module, "version", _missing)
        assert geocode_module._user_agent() == "pyzill/unknown (Zillow research CLI; personal use)"

    def test_successful_lookup(self, monkeypatch, cache_path):
        calls = _install_get(monkeypatch, HIT)
        result = geocode("Schenectady NY", cache_path=cache_path)
        assert isinstance(result, GeoResult)
        assert result.bbox == BBox(north=42.8438, south=42.7634, east=-73.8931, west=-73.9834)
        assert "Schenectady" in result.display_name
        assert calls[0]["headers"]["User-Agent"].startswith(f"pyzill/{version('pyzill')} ")

    def test_result_cached_on_disk(self, monkeypatch, cache_path):
        _install_get(monkeypatch, HIT)
        geocode("Schenectady NY", cache_path=cache_path)
        calls = _install_get(monkeypatch, HIT)
        result = geocode("schenectady ny", cache_path=cache_path)  # case-insensitive key
        assert calls == []  # served from cache
        assert result.display_name.startswith("Schenectady")

    def test_cache_survives_corruption(self, monkeypatch, cache_path):
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text("{not json")
        _install_get(monkeypatch, HIT)
        result = geocode("Schenectady NY", cache_path=cache_path)
        assert result.bbox.north == 42.8438

    def test_no_result_raises(self, monkeypatch, cache_path):
        _install_get(monkeypatch, [])
        with pytest.raises(ValueError, match="No geocoding result"):
            geocode("zzzznowhere", cache_path=cache_path)

    def test_non_list_response_raises_value_error(self, monkeypatch, cache_path):
        """A dict-shaped error body must not escape as a raw KeyError."""
        _install_get(monkeypatch, {"error": "Unable to geocode"})
        with pytest.raises(ValueError, match="unexpected response shape"):
            geocode("Schenectady NY", cache_path=cache_path)

    def test_hit_without_bounding_box_raises_value_error(self, monkeypatch, cache_path):
        _install_get(monkeypatch, [{"display_name": "Schenectady"}])
        with pytest.raises(ValueError, match="no usable bounding box"):
            geocode("Schenectady NY", cache_path=cache_path)

    def test_malformed_bounding_box_raises_value_error(self, monkeypatch, cache_path):
        _install_get(monkeypatch, [{"display_name": "x", "boundingbox": ["a", "b", "c", "d"]}])
        with pytest.raises(ValueError, match="malformed bounding box"):
            geocode("Schenectady NY", cache_path=cache_path)

    def test_inverted_bounding_box_is_recovered(self, monkeypatch, cache_path):
        """An inverted box is repaired by the viewport expansion, not rejected."""
        _install_get(monkeypatch, [{"display_name": "x", "boundingbox": ["43.0", "42.0", "-73.0", "-74.0"]}])
        result = geocode("Schenectady NY", cache_path=cache_path)
        assert result.bbox.north > result.bbox.south
        assert result.bbox.east > result.bbox.west

    def test_non_finite_bounding_box_raises_value_error(self, monkeypatch, cache_path):
        """NaN bounds pass every BBox comparison and must be rejected outright."""
        _install_get(monkeypatch, [{"display_name": "x", "boundingbox": ["nan", "nan", "-73.0", "-74.0"]}])
        with pytest.raises(ValueError, match="non-finite bounding box"):
            geocode("Schenectady NY", cache_path=cache_path)

    def test_network_error_raises_with_hint(self, monkeypatch, cache_path):
        _install_get(monkeypatch, exc=OSError("DNS failure"))
        with pytest.raises(ValueError, match="--north"):
            geocode("Schenectady NY", cache_path=cache_path)

    def test_empty_place_raises(self, cache_path):
        with pytest.raises(ValueError, match="empty"):
            geocode("   ", cache_path=cache_path)

    def test_small_viewport_expanded(self, monkeypatch, cache_path):
        # Address-level hit: ~10m box gets grown to a neighbourhood.
        tiny = [{"display_name": "1 Main St", "boundingbox": ["42.7900", "42.7901", "-73.9400", "-73.9399"]}]
        _install_get(monkeypatch, tiny)
        result = geocode("1 Main St, Schenectady NY", cache_path=cache_path)
        assert result.bbox.north - result.bbox.south == pytest.approx(0.02)
        assert result.bbox.east - result.bbox.west == pytest.approx(0.02)
        assert result.synthetic_bbox is True

    def test_cache_write_failure_not_fatal(self, monkeypatch, tmp_path):
        _install_get(monkeypatch, HIT)
        ro_dir = tmp_path / "ro"
        ro_dir.mkdir()
        ro_dir.chmod(0o500)
        try:
            result = geocode("Schenectady NY", cache_path=ro_dir / "sub" / "geocode.json")
            assert result.bbox.north == 42.8438
        finally:
            ro_dir.chmod(0o700)

    def test_default_cache_path_respects_xdg(self, monkeypatch, tmp_path):
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
        assert default_cache_path() == tmp_path / "pyzill" / "geocode.json"
        monkeypatch.delenv("XDG_CACHE_HOME")
        assert "pyzill" in str(default_cache_path())


class TestExpandViewport:
    @pytest.mark.parametrize(
        ("north", "south", "east", "west"),
        [
            (42.7901, 42.7900, -73.9399, -73.9400),  # both too small
            (42.8438, 42.7634, -73.8931, -73.9834),  # already large: untouched
        ],
    )
    def test_expand(self, north, south, east, west):
        n, s, e, w = _expand_viewport(north, south, east, west)
        assert n > s
        assert e > w
        if north - south < 0.02:
            assert n - s == pytest.approx(0.02)
        else:
            assert (n, s) == (north, south)


def test_cache_entry_roundtrip(tmp_path, monkeypatch):
    """Stored entries come back as GeoResults without a network call."""
    cache = tmp_path / "geocode.json"
    cache.write_text(
        json.dumps(
            {
                "us:jsonv2:schenectady ny": {
                    "display_name": "cached",
                    "north": 42.9,
                    "south": 42.7,
                    "east": -73.8,
                    "west": -74.0,
                    "cached_at": time.time(),
                }
            }
        )
    )
    calls = _install_get(monkeypatch, HIT)
    result = geocode("Schenectady NY", cache_path=cache)
    assert calls == []
    assert result.display_name == "cached"
    assert result.bbox.north == 42.9


def test_geocode_uses_separate_rate_limiter_and_no_implicit_proxy(monkeypatch, cache_path):
    calls = _install_get(monkeypatch, HIT)
    waits: list[bool] = []
    monkeypatch.setattr("pyzill.geocode._nominatim_limiter.wait", lambda: waits.append(True))
    monkeypatch.setenv("ZILLOW_PROXY", "http://proxy.example:9000")

    geocode("Schenectady NY", cache_path=cache_path)

    assert waits == [True]
    assert calls[0]["proxies"] is None
    assert "us:jsonv2:" in next(iter(json.loads(cache_path.read_text())))


def test_expired_cache_entry_is_refetched(monkeypatch, cache_path):
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps(
            {
                "us:jsonv2:schenectady ny": {
                    "display_name": "stale",
                    "north": 42.9,
                    "south": 42.7,
                    "east": -73.8,
                    "west": -74.0,
                    "cached_at": 0,
                }
            }
        )
    )
    calls = _install_get(monkeypatch, HIT)

    result = geocode("Schenectady NY", cache_path=cache_path)

    assert len(calls) == 1
    assert result.display_name.startswith("Schenectady")


def test_cache_updates_are_serialized_across_writers(tmp_path):
    """Concurrent writers must not lose each other's cache entries."""
    import threading

    from pyzill.geocode import _store_cache

    cache_path = tmp_path / "geocode.json"
    writers = 8
    per_writer = 20

    def writer(index: int) -> None:
        for item in range(per_writer):
            _store_cache(cache_path, f"key-{index}-{item}", {"north": 1.0})

    threads = [threading.Thread(target=writer, args=(index,)) for index in range(writers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    cache = json.loads(cache_path.read_text())
    assert len(cache) == writers * per_writer


def test_cache_lock_is_best_effort_when_it_cannot_be_created(tmp_path, monkeypatch):
    """A cache dir that refuses the lock file must not break geocoding."""
    import os

    class _RefusesOpen:
        def __getattr__(self, name: str) -> object:
            return getattr(os, name)

        def open(self, *_args: object, **_kwargs: object) -> None:
            raise OSError("read-only")

    monkeypatch.setattr("pyzill.geocode.os", _RefusesOpen())
    _install_get(monkeypatch, HIT)

    result = geocode("Schenectady NY", cache_path=tmp_path / "geocode.json")

    assert result.bbox.north == 42.8438


def test_geocode_is_reachable_as_a_module_attribute():
    """`pyzill.geocode.geocode(...)` must resolve, as the docs promise."""
    import pyzill

    assert callable(pyzill.geocode.geocode)
    assert pyzill.GeoResult is pyzill.geocode.GeoResult
