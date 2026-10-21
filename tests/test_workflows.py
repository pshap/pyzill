"""The workflow layer: search, sync, and import.

These are unit tests for the rules the workflows share: which rows count as
listings, how a filter reaches the search request, and what makes two sync
runs comparable.
"""

import json
import sqlite3
from typing import Any

import pytest

from pyzill import BBox
from pyzill.db.schema import ensure_schema
from pyzill.normalize import normalize_listing
from pyzill.search_request import SearchQuery
from pyzill.workflows import (
    SearchFilters,
    SearchRequest,
    extract_listings,
    fetch_listings,
    import_rows,
    prepare_import,
    sync_to_db,
    usable_row_count,
)
from pyzill.workflows.model import BUDGET_FILTERS, FILTER_PARAMS, VIEWPORT_FILTERS

BBOX = BBox(north=43.0, south=42.0, east=-73.0, west=-74.0)


class FakeClient:
    """A client that returns a canned response instead of calling Zillow."""

    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    def search_sale(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return self.response

    def search_rent(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return self.response

    def search_sold(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return self.response


def _listing(zpid: int | str, **extra: Any) -> dict[str, Any]:
    return {"zpid": zpid, "price": 100, "hdpData": {"homeInfo": {"zipcode": "12305"}}, **extra}


class TestFilterVocabulary:
    """Every search filter must reach the search request."""

    def test_every_filter_is_classified(self):
        """A new filter must be a query filter, a viewport bound, or a tiling knob."""
        classified = set(FILTER_PARAMS) | set(VIEWPORT_FILTERS) | set(BUDGET_FILTERS)
        assert classified == set(SearchFilters.__annotations__)

    def test_every_query_param_is_a_real_search_field(self):
        assert set(FILTER_PARAMS.values()) <= set(SearchQuery.__annotations__)

    def test_filters_reach_the_search_request(self, monkeypatch):
        client = FakeClient({"mapResults": [], "listResults": []})
        request = SearchRequest(
            query="12305",
            filters=SearchFilters(
                zoom=12,
                min_beds=2,
                max_beds=4,
                min_bathrooms=1,
                max_bathrooms=3,
                min_price=100_000,
                max_price=500_000,
            ),
            transport={"proxy_url": "http://proxy:8080"},
        )
        monkeypatch.setattr("pyzill.workflows.search.get_default_client", lambda: client)

        fetch_listings("sale", request, bbox=BBOX)

        assert client.calls[0]["search_value"] == "12305"
        assert client.calls[0]["zoom_value"] == 12
        assert client.calls[0]["min_beds"] == 2
        assert client.calls[0]["max_beds"] == 4
        assert client.calls[0]["min_bathrooms"] == 1
        assert client.calls[0]["max_bathrooms"] == 3
        assert client.calls[0]["min_price"] == 100_000
        assert client.calls[0]["max_price"] == 500_000
        assert client.calls[0]["proxy_url"] == "http://proxy:8080"

    def test_the_viewport_bounds_do_not_reach_the_search_request(self, monkeypatch):
        client = FakeClient({"mapResults": [], "listResults": []})
        request = SearchRequest(
            query="12305",
            filters=SearchFilters(north=43.0, south=42.0, east=-73.0, west=-74.0),
        )
        monkeypatch.setattr("pyzill.workflows.search.get_default_client", lambda: client)

        fetch_listings("sale", request, bbox=BBOX)

        assert not {"north", "south", "east", "west"} & set(client.calls[0])


class TestUsableListings:
    """One rule decides what counts as a listing, everywhere."""

    def test_building_cards_are_not_listings(self):
        rows = [{"isBuilding": True, "buildingId": "47.61503--122.34274"}, _listing(1)]
        assert usable_row_count(rows) == 1

    def test_a_row_without_a_usable_zpid_is_not_a_listing(self):
        assert usable_row_count([_listing("47.61503--122.34274")]) == 0

    def test_fetch_listings_drops_rows_without_a_usable_zpid(self, monkeypatch):
        client = FakeClient(
            {
                "mapResults": [_listing(1), _listing("47.61503--122.34274")],
                "listResults": [],
            }
        )
        monkeypatch.setattr("pyzill.workflows.search.get_default_client", lambda: client)

        result = fetch_listings("sale", SearchRequest(query="12305"), bbox=BBOX)

        assert [listing["zpid"] for listing in result.listings] == [1]
        assert len(result.raw_listings) == 1

    def test_fetch_listings_keeps_what_usable_row_count_counts(self, monkeypatch):
        """The rule that picks the result list is the rule that filters it."""
        rows = [_listing(1), _listing("47.61503--122.34274"), {"isBuilding": True}]
        client = FakeClient({"mapResults": rows, "listResults": []})
        monkeypatch.setattr("pyzill.workflows.search.get_default_client", lambda: client)

        result = fetch_listings("sale", SearchRequest(query="12305"), bbox=BBOX)

        assert len(result.listings) == usable_row_count(rows, zipcode="12305")

    def test_each_row_is_normalized_once(self, monkeypatch):
        """Selecting the result list and filtering it share one normalization."""
        client = FakeClient({"mapResults": [_listing(1), _listing(2)], "listResults": [_listing(3)]})
        monkeypatch.setattr("pyzill.workflows.search.get_default_client", lambda: client)

        calls: list[Any] = []
        original = normalize_listing

        def counting_normalize(listing: Any) -> Any:
            calls.append(listing)
            return original(listing)

        monkeypatch.setattr("pyzill.workflows.extract.normalize_listing", counting_normalize)

        fetch_listings("sale", SearchRequest(query="12305"), bbox=BBOX)

        assert len(calls) == 3

    def test_a_numeric_zipcode_still_matches_the_searched_zipcode(self, monkeypatch):
        """Zillow sometimes sends the zipcode as a number."""
        client = FakeClient({"mapResults": [{"zpid": 1, "price": 100, "addressZipcode": 12305}], "listResults": []})
        monkeypatch.setattr("pyzill.workflows.search.get_default_client", lambda: client)

        result = fetch_listings("sale", SearchRequest(query="12305"), bbox=BBOX)

        assert len(result.listings) == 1


class TestExtractListings:
    """The response envelope is unwrapped the same way everywhere."""

    def test_wrap_single_survives_the_envelope_path(self):
        assert extract_listings({"cat1": {"zpid": 1}}, wrap_single=True) == [{"zpid": 1}]
        assert extract_listings({"cat2": {"zpid": 1}}, wrap_single=True) == [{"zpid": 1}]
        assert extract_listings({"searchResults": {"zpid": 1}}, wrap_single=True) == [{"zpid": 1}]

    def test_without_wrap_single_an_envelope_yields_nothing(self):
        assert extract_listings({"cat1": {"zpid": 1}}) == []

    def test_a_non_list_result_is_treated_as_empty(self):
        assert extract_listings({"mapResults": "not a list"}) == []

    def test_a_bare_list_is_returned_as_it_is(self):
        rows = [_listing(1)]
        assert extract_listings(rows) == rows


class TestImportRows:
    """An import records a run of its own and names it."""

    def test_the_result_names_the_run_it_wrote(self):
        conn = sqlite3.connect(":memory:")
        ensure_schema(conn)

        result = import_rows(conn, [{"zpid": 1, "price": 100}], [], kind="sale")

        assert result.run_id > 0
        stored = conn.execute("SELECT id FROM sync_runs WHERE id = ?", (result.run_id,)).fetchone()
        assert stored is not None

    def test_an_import_run_carries_no_query(self):
        conn = sqlite3.connect(":memory:")
        ensure_schema(conn)

        result = import_rows(conn, [{"zpid": 1, "price": 100}], [], kind="sale")

        stored = conn.execute("SELECT query FROM sync_runs WHERE id = ?", (result.run_id,)).fetchone()
        assert stored == ("",)

    def test_an_empty_import_writes_no_run(self):
        conn = sqlite3.connect(":memory:")
        ensure_schema(conn)

        result = import_rows(conn, [], [], kind="sale")

        assert result.run_id == 0
        assert conn.execute("SELECT COUNT(*) FROM sync_runs").fetchone()[0] == 0


class TestComparableRuns:
    """Two runs are comparable when they searched the same thing."""

    def _seed_run(self, conn: sqlite3.Connection, filters: dict[str, Any], result_count: int) -> None:
        conn.execute(
            "INSERT INTO sync_runs (started_utc, finished_utc, kind, query, bbox_json, filters_json, "
            "result_count, truncated) VALUES ('2026-01-01T00:00:00Z', '2026-01-01T00:01:00Z', "
            "'sale', '12305', ?, ?, ?, 0)",
            (json.dumps(BBOX.to_map_bounds(), sort_keys=True), json.dumps(filters, sort_keys=True), result_count),
        )
        conn.commit()

    def test_the_budget_knobs_are_not_part_of_the_key(self, monkeypatch):
        conn = sqlite3.connect(":memory:")
        ensure_schema(conn)
        client = FakeClient({"mapResults": [], "listResults": []})
        monkeypatch.setattr("pyzill.workflows.search.get_default_client", lambda: client)

        # A prior run of the same search, recorded with the old filter shape.
        self._seed_run(
            conn,
            {
                "max_bathrooms": None,
                "max_beds": None,
                "max_price": None,
                "min_bathrooms": None,
                "min_beds": None,
                "min_price": None,
                "zoom": None,
                "is_room": False,
                "is_entire_place": False,
            },
            result_count=10,
        )

        request = SearchRequest(query="12305", filters=SearchFilters(max_requests=10))
        result = sync_to_db(conn, "sale", request, bbox=BBOX)

        assert "suspect_empty" in [notice.code for notice in result.notices]

    def test_the_stored_filters_are_the_comparable_ones(self, monkeypatch):
        conn = sqlite3.connect(":memory:")
        ensure_schema(conn)
        client = FakeClient({"mapResults": [], "listResults": []})
        monkeypatch.setattr("pyzill.workflows.search.get_default_client", lambda: client)

        request = SearchRequest(
            query="12305",
            filters=SearchFilters(min_beds=2, max_requests=10, max_depth=2, tile=True),
        )
        sync_to_db(conn, "sale", request, bbox=BBOX)

        stored = json.loads(conn.execute("SELECT filters_json FROM sync_runs").fetchone()[0])
        assert stored == {
            "max_bathrooms": None,
            "max_beds": None,
            "max_price": None,
            "min_bathrooms": None,
            "min_beds": 2,
            "min_price": None,
            "zoom": None,
            "is_room": False,
            "is_entire_place": False,
        }

    def test_different_filters_are_not_comparable(self, monkeypatch):
        conn = sqlite3.connect(":memory:")
        ensure_schema(conn)
        client = FakeClient({"mapResults": [], "listResults": []})
        monkeypatch.setattr("pyzill.workflows.search.get_default_client", lambda: client)

        self._seed_run(
            conn,
            {
                "max_bathrooms": None,
                "max_beds": None,
                "max_price": None,
                "min_bathrooms": None,
                "min_beds": 2,
                "min_price": None,
                "zoom": None,
                "is_room": False,
                "is_entire_place": False,
            },
            result_count=10,
        )

        request = SearchRequest(query="12305", filters=SearchFilters(min_beds=4))
        result = sync_to_db(conn, "sale", request, bbox=BBOX)

        assert "suspect_empty" not in [notice.code for notice in result.notices]


class _TiledClient:
    """A client whose first call fills Zillow's cap and whose later calls do not.

    The first call is the parent tile: it comes back full, so the tiling loop
    subdivides it. Every later call is a smaller tile whose rows are distinct,
    so the union of the tiles is larger than the cap that a single search
    would have been cut off at.
    """

    def __init__(self, first: int, rest: int) -> None:
        self.first = first
        self.rest = rest
        self.calls = 0

    def _response(self) -> dict[str, Any]:
        self.calls += 1
        count = self.first if self.calls == 1 else self.rest
        start = 1_000 * self.calls
        return {"mapResults": [_listing(start + i) for i in range(count)], "listResults": []}

    def search_sale(self, **_kwargs: Any) -> dict[str, Any]:
        return self._response()

    def search_rent(self, **_kwargs: Any) -> dict[str, Any]:
        return self._response()

    def search_sold(self, **_kwargs: Any) -> dict[str, Any]:
        return self._response()


class TestTruncation:
    """A run is truncated only when Zillow's cap actually cut it short."""

    def test_a_single_search_at_the_cap_is_truncated(self, monkeypatch):
        client = FakeClient({"mapResults": [{"zpid": i} for i in range(500)], "listResults": []})
        monkeypatch.setattr("pyzill.workflows.search.get_default_client", lambda: client)

        result = fetch_listings("sale", SearchRequest(query="12305"), bbox=BBOX)

        assert result.truncated is True

    def test_a_single_search_below_the_cap_is_not_truncated(self, monkeypatch):
        client = FakeClient({"mapResults": [{"zpid": 1}], "listResults": []})
        monkeypatch.setattr("pyzill.workflows.search.get_default_client", lambda: client)

        result = fetch_listings("sale", SearchRequest(query="12305"), bbox=BBOX)

        assert result.truncated is False

    def test_a_tiled_run_larger_than_the_cap_is_not_truncated(self, monkeypatch):
        """The union of the tiles is expected to exceed the per-search cap.

        A tiled run is capped by its own budget, not by the union's size: a
        union larger than the cap is the whole point of tiling.
        """
        client = _TiledClient(first=500, rest=200)
        monkeypatch.setattr("pyzill.workflows.search.get_default_client", lambda: client)

        request = SearchRequest(query="12305", filters=SearchFilters(tile=True))
        result = fetch_listings("sale", request, bbox=BBOX)

        assert len(result.listings) > 500
        assert result.truncated is False

    def test_a_tiled_run_that_could_not_subdivide_further_is_truncated(self, monkeypatch):
        client = _TiledClient(first=500, rest=500)
        monkeypatch.setattr("pyzill.workflows.search.get_default_client", lambda: client)

        request = SearchRequest(query="12305", filters=SearchFilters(tile=True, max_depth=1))
        result = fetch_listings("sale", request, bbox=BBOX)

        assert result.truncated is True

    def test_a_tiled_run_that_ran_out_of_its_budget_is_truncated(self, monkeypatch):
        client = _TiledClient(first=500, rest=500)
        monkeypatch.setattr("pyzill.workflows.search.get_default_client", lambda: client)

        request = SearchRequest(query="12305", filters=SearchFilters(tile=True, max_requests=2))
        result = fetch_listings("sale", request, bbox=BBOX)

        assert result.truncated is True

    def test_a_tiled_run_is_recorded_as_complete(self, monkeypatch):
        """A complete tiled run must be comparable with the next one.

        Change detection only compares runs that were not truncated, so a
        tiled run wrongly marked truncated would never report a delisting.
        """
        conn = sqlite3.connect(":memory:")
        ensure_schema(conn)
        client = _TiledClient(first=500, rest=200)
        monkeypatch.setattr("pyzill.workflows.search.get_default_client", lambda: client)

        request = SearchRequest(query="12305", filters=SearchFilters(tile=True))
        result = sync_to_db(conn, "sale", request, bbox=BBOX)

        stored = conn.execute("SELECT truncated FROM sync_runs WHERE id = ?", (result.run_id,)).fetchone()
        assert stored == (0,)


def test_prepare_import_drops_rows_without_a_zpid():
    rows, _ = prepare_import([_listing(1), _listing("47.61503--122.34274")])
    assert [row["zpid"] for row in rows] == [1]


def test_prepare_import_keeps_a_row_whose_zpid_the_write_path_can_coerce():
    """An import only needs a zpid; the write path coerces it and counts failures."""
    rows, _ = prepare_import([{"zpid": "12345", "price": 100}])
    assert [row["zpid"] for row in rows] == ["12345"]


@pytest.mark.parametrize("kind", ["sale", "rent", "sold"])
def test_fetch_listings_returns_the_listings_of_a_response(kind, monkeypatch):
    client = FakeClient({"mapResults": [_listing(1)], "listResults": []})
    monkeypatch.setattr("pyzill.workflows.search.get_default_client", lambda: client)

    result = fetch_listings(kind, SearchRequest(query="12305"), bbox=BBOX)

    assert len(result.listings) == 1
    assert result.bbox == BBOX
