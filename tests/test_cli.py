"""Tests for pyzill CLI."""

import json
import os
import subprocess
import sys
from dataclasses import fields
from inspect import signature
from pathlib import Path
from typing import Any, ClassVar

import pytest
import typer
from typer._click.exceptions import ClickException
from typer.testing import CliRunner, Result

from pyzill import ratelimit
from pyzill.bbox import BBox
from pyzill.cli import app, console, main_entry
from pyzill.cli import geocode as geocode_module
from pyzill.cli.console import _redact, stderr_line
from pyzill.cli.db import db_sync
from pyzill.cli.query import coerce_value
from pyzill.cli.search import extract_listings, search_rent, search_sale, search_sold
from pyzill.db import begin_sync_run, connect, finish_sync_run, upsert_listings
from pyzill.geocode import GeocodeError, GeoResult
from pyzill.query import query_database
from pyzill.workflows import SearchFilters

runner = CliRunner()


# --- builders --------------------------------------------------------------


def search_body(*listings: dict[str, Any], **extra: Any) -> dict[str, Any]:
    """Wrap listing rows in the envelope a Zillow search response uses.

    ``extra`` adds the sibling result lists a response may carry, such as
    ``listResults``.
    """
    results: dict[str, Any] = {"mapResults": list(listings)}
    results.update(extra)
    return {"cat1": {"searchResults": results}}


def empty_body() -> dict[str, Any]:
    """A search response that holds no listings."""
    return search_body()


def capped_body(count: int = 500) -> dict[str, Any]:
    """A search response with enough rows to hit Zillow's 500-row request cap."""
    return search_body(*({"zpid": str(index)} for index in range(count)))


def write_json(path: Path, data: Any) -> Path:
    """Write data as JSON and return the path."""
    path.write_text(json.dumps(data))
    return path


def run_module(*args: str) -> subprocess.CompletedProcess[str]:
    """Run the CLI as a module, the way a user would from a shell."""
    return subprocess.run(  # noqa: S603 - the command is this test's own argv
        [sys.executable, "-m", "pyzill", *args],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
    )


def run_query(path: Path | str, *args: str) -> list[dict[str, Any]]:
    """Run the offline query command and return the rows it selected."""
    result = runner.invoke(app, ["query", str(path), *args])
    assert result.exit_code == 0, result.stderr
    return json.loads(result.stdout)


# --- stubs and fixtures ---------------------------------------------------


class _GeoStub:
    """Recording stand-in for pyzill.cli.search.geocode (installed autouse)."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.kwargs: list[dict[str, object]] = []
        self.result = GeoResult(
            display_name="Testville",
            bbox=BBox(north=42.9, south=42.7, east=-73.8, west=-74.0),
        )
        self.error: Exception | None = None

    def __call__(self, place: str, **kwargs: object) -> GeoResult:
        self.calls.append(place)
        self.kwargs.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.result

    def reset(self) -> None:
        self.calls.clear()
        self.kwargs.clear()
        self.error = None


_GEO_STUB = _GeoStub()


class _FakeConsoleContext:
    """Minimal stand-in for ``pyzill.cli.console.Context`` with a capture console."""

    def __init__(self, stdout_console) -> None:
        self.stdout_console = stdout_console


@pytest.fixture(autouse=True)
def _disable_rate_limit():
    """Keep tests fast: no throttling between mocked requests."""
    original = ratelimit.get_min_interval()
    ratelimit.set_min_interval(0)
    yield
    ratelimit.set_min_interval(original)


@pytest.fixture(autouse=True)
def _stub_geocode(monkeypatch):
    """Keep search tests offline: no bbox means the stubbed geocode result."""
    _GEO_STUB.reset()
    import importlib

    search_module = importlib.import_module("pyzill.cli.search")
    monkeypatch.setattr(search_module, "geocode", _GEO_STUB)


@pytest.fixture
def run_search(fake_http):
    """Run the sale search against a canned Zillow response."""

    def _run(body: dict[str, Any], *args: str) -> Result:
        fake_http.install("PUT", json_data=body)
        return runner.invoke(app, ["search", "sale", "12305", *args])

    return _run


@pytest.fixture
def run_sync(fake_http):
    """Run a database sync against a canned Zillow response."""

    def _run(body: dict[str, Any], *args: str, db_path: Path | None = None, kind: str = "sale") -> Result:
        fake_http.install("PUT", json_data=body)
        argv = ["db", "sync", kind, "12305"]
        if db_path is not None:
            argv += ["--db", str(db_path)]
        return runner.invoke(app, [*argv, *args])

    return _run


@pytest.fixture
def import_rows(tmp_path):
    """Import rows into a database file and return the database path."""

    def _import(
        rows: Any,
        *,
        db_path: Path | None = None,
        fetched_at: str | None = None,
        name: str = "rows.json",
    ) -> Path:
        target = db_path or tmp_path / "test.db"
        source = write_json(tmp_path / name, rows)
        argv = ["db", "import", str(source), "--db", str(target)]
        if fetched_at is not None:
            argv += ["--fetched-at", fetched_at]
        result = runner.invoke(app, argv)
        assert result.exit_code == 0, result.stderr
        return target

    return _import


@pytest.fixture
def db_rows():
    """Run a database query and return the rows it selected."""

    def _query(db_path: Path, *args: str) -> list[dict[str, Any]]:
        result = runner.invoke(app, ["db", "query", "--db", str(db_path), *args])
        assert result.exit_code == 0, result.stderr
        return json.loads(result.stdout)

    return _query


@pytest.fixture
def normalize_file(tmp_path):
    """Write data to a JSON file and normalize it through the CLI."""

    def _normalize(data: Any, *, name: str = "input.json") -> Result:
        source = write_json(tmp_path / name, data)
        return runner.invoke(app, ["normalize", str(source)])

    return _normalize


@pytest.fixture
def patch_geocode(monkeypatch):
    """Replace the geocode service the geocode command calls.

    Returns the list the patched service records the places it was asked
    for.
    """
    calls: list[str] = []

    def _patch(result: GeoResult | None = None, error: Exception | None = None) -> list[str]:
        def fake_geocode(place: str, **kwargs: object) -> GeoResult:
            calls.append(place)
            if error is not None:
                raise error
            assert result is not None
            return result

        monkeypatch.setattr(geocode_module, "geocode_place", fake_geocode)
        return calls

    return _patch


class TestRootAndHelp:
    """Test root help and version."""

    def test_root_help_displays_name(self):
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "pyzill" in result.stdout

    def test_version_shows_version(self):
        result = runner.invoke(app, ["--version"])
        assert result.exit_code == 0
        assert "pyzill" in result.stdout

    def test_listing_schema_command_is_versioned_json(self):
        result = runner.invoke(app, ["schema", "listing"])

        assert result.exit_code == 0
        schema = json.loads(result.stdout)
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert schema["$id"].endswith("/listing/1.0.0")
        assert schema["properties"]["zipcode"]["anyOf"][-1] == {"type": "null"}

    def test_db_listing_schema_command_is_versioned_json(self):
        result = runner.invoke(app, ["schema", "db-listing"])

        assert result.exit_code == 0
        schema = json.loads(result.stdout)
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert schema["$id"].endswith("/db-listing/1.0.0")
        assert schema["additionalProperties"] is False
        # The kind and the two timestamps the database adds are described too.
        assert schema["properties"]["kind"] == {"type": "string"}
        assert schema["properties"]["first_seen_utc"] == {"type": "string"}
        assert schema["properties"]["price"]["anyOf"][-1] == {"type": "null"}
        # The computed price per square foot is projectable, so it is described.
        assert schema["properties"]["ppsf"]["anyOf"][0] == {"type": "number"}

    def test_db_listing_schema_covers_every_column_db_query_returns(self, tmp_path):
        """The published schema must describe what the query command emits."""
        db_path = tmp_path / "schema.db"
        conn = connect(db_path)
        upsert_listings(conn, [{"zpid": 1, "price": 250000, "city": "Albany"}], seen_utc="2026-10-01T00:00:00Z")
        rows, _ = query_database(conn)
        conn.close()

        result = runner.invoke(app, ["schema", "db-listing"])
        schema = json.loads(result.stdout)

        assert set(rows[0]) <= set(schema["properties"])
        assert set(schema["properties"]) - set(rows[0]) == {"ppsf"}

    def test_detail_schema_command_describes_the_normalized_detail(self):
        result = runner.invoke(app, ["schema", "detail"])

        assert result.exit_code == 0
        schema = json.loads(result.stdout)
        assert schema["$id"].endswith("/detail/1.0.0")
        # A list-typed field is an array, not an empty (accept-anything) schema.
        assert schema["properties"]["schools"]["anyOf"][0] == {
            "type": "array",
            "items": {"type": "object"},
        }
        assert schema["properties"]["description"]["anyOf"][-1] == {"type": "null"}

    def test_no_args_shows_help(self):
        result = runner.invoke(app, [])
        assert result.exit_code == 2  # Typer's no_args_is_help returns 2
        assert "help" in result.stdout.lower()


class TestSearchCommands:
    """Test search command routing and options."""

    @pytest.mark.parametrize("kind", ["sale", "rent", "sold"])
    def test_search_command_exists(self, fake_http, search_success_body, kind):
        fake_http.install("PUT", json_data=search_success_body)
        result = runner.invoke(app, ["search", kind, "12305"])
        assert result.exit_code == 0

    def test_search_zip_only_returns_listings_in_requested_zip(self, fake_http):
        fake_http.install(
            "PUT",
            json_data=search_body(
                {"zpid": 1, "addressZipcode": "12305"},
                {"zpid": 2, "addressZipcode": "12309"},
                {"zpid": 3},
            ),
        )

        result = runner.invoke(app, ["search", "sale", "12305"])

        assert result.exit_code == 0
        assert [row["zpid"] for row in json.loads(result.stdout)] == [1]

    def test_search_zip_selects_results_containing_requested_zip(self, fake_http):
        fake_http.install(
            "PUT",
            json_data=search_body(
                {"zpid": 1, "addressZipcode": "12305"},
                listResults=[
                    {"zpid": 2, "addressZipcode": "12309"},
                    {"zpid": 3, "addressZipcode": "12309"},
                ],
            ),
        )

        result = runner.invoke(app, ["search", "sale", "12305"])

        assert result.exit_code == 0
        assert [row["zpid"] for row in json.loads(result.stdout)] == [1]

    def test_search_requires_query(self):
        result = runner.invoke(app, ["search", "sale"])
        assert result.exit_code == 2

    def test_blocked_request_emits_json_error_and_exit_code(self, fake_http):
        fake_http.install("PUT", status_code=403, text="blocked")

        result = runner.invoke(app, ["--json-errors", "search", "sale", "12305"])

        assert result.exit_code == 3
        payload = json.loads(result.stderr)["error"]
        assert payload == {
            "code": "blocked",
            "message": "Zillow blocked the request: Zillow returned HTTP 403 for "
            "https://www.zillow.com/async-create-search-page-state. Body starts with: 'blocked'",
            "retryable": False,
            "status_code": 403,
        }

    def test_parse_failure_uses_schema_exit_code_by_default(self, fake_http):
        fake_http.install("PUT", text="not json")

        result = runner.invoke(app, ["search", "sale", "12305"])

        assert result.exit_code == 5
        assert json.loads(result.stderr)["error"]["code"] == "schema_drift"

    def test_server_error_uses_retryable_transient_exit_code(self, fake_http, monkeypatch):
        import time

        fake_http.install("PUT", status_code=503, text="Unavailable")
        monkeypatch.setattr(time, "sleep", lambda *args, **kwargs: None)

        result = runner.invoke(app, ["--json-errors", "search", "sale", "12305"])

        assert result.exit_code == 6
        payload = json.loads(result.stderr)["error"]
        assert payload["code"] == "server_error"
        assert payload["retryable"] is True
        assert payload["status_code"] == 503

    def test_not_found_uses_dedicated_exit_code(self, fake_http):
        fake_http.install("GET", status_code=404, text="Not Found")

        result = runner.invoke(app, ["--json-errors", "detail", "home", "123"])

        assert result.exit_code == 4
        payload = json.loads(result.stderr)["error"]
        assert payload["code"] == "not_found"
        assert payload["retryable"] is False

    def test_network_error_uses_transient_exit_code(self, monkeypatch, no_sleep):
        from curl_cffi import requests as cffi_requests
        from curl_cffi.requests.exceptions import RequestException

        def fail_network(**kwargs: Any) -> None:
            raise RequestException("offline")

        monkeypatch.setattr(cffi_requests.Session, "request", lambda self, **kwargs: fail_network(**kwargs))

        result = runner.invoke(app, ["--json-errors", "search", "sale", "12305"])

        assert result.exit_code == 6
        payload = json.loads(result.stderr)["error"]
        assert payload["code"] == "network"
        assert payload["retryable"] is True
        # The failure was retried before it was reported.
        assert no_sleep, "the request was not retried"

    def test_json_errors_can_be_disabled(self, fake_http):
        fake_http.install("PUT", status_code=403, text="blocked")

        result = runner.invoke(app, ["--no-json-errors", "search", "sale", "12305"])

        assert result.exit_code == 3
        assert result.stderr.startswith("Error:")

    @pytest.mark.parametrize(
        ("kind", "args"),
        [
            ("sale", ["--north", "42.85", "--south", "42.78", "--east", "-73.90", "--west", "-74.00"]),
            ("sale", ["--min-price", "100000", "--max-price", "500000"]),
            ("sale", ["--min-beds", "2", "--max-beds", "4", "--min-bathrooms", "1"]),
            ("rent", ["--room"]),
            ("rent", ["--entire-place"]),
        ],
    )
    def test_search_accepts_the_documented_options(self, fake_http, search_success_body, kind, args):
        fake_http.install("PUT", json_data=search_success_body)
        result = runner.invoke(app, ["search", kind, "12305", *args])
        assert result.exit_code == 0

    @pytest.mark.parametrize("args", [["--max-requests", "0"], ["--max-depth", "7"]])
    def test_search_rejects_out_of_range_limits(self, args):
        result = runner.invoke(app, ["search", "sale", "12305", *args])
        assert result.exit_code == 2

    def test_search_warns_when_result_cap_is_hit(self, fake_http):
        fake_http.install("PUT", json_data=capped_body())

        result = runner.invoke(app, ["search", "sale", "12305"])

        assert result.exit_code == 0
        assert "500 listings" in result.stderr

    def test_search_tile_splits_capped_results(self, fake_http):
        fake_http.install("PUT", json_data=capped_body())

        result = runner.invoke(app, ["search", "sale", "12305", "--tile", "--max-depth", "1"])

        assert result.exit_code == 0
        assert len(fake_http.calls) == 5
        assert "still returned 500" in result.stderr

    def test_search_tile_stops_at_max_requests_budget(self, fake_http):
        fake_http.install("PUT", json_data=capped_body())

        result = runner.invoke(
            app,
            ["search", "sale", "12305", "--tile", "--max-depth", "1", "--max-requests", "2"],
        )

        assert result.exit_code == 0
        assert len(fake_http.calls) == 2
        assert "budget of 2 exhausted" in result.stderr

    def test_search_tile_reports_progress_on_stderr(self, fake_http):
        fake_http.install("PUT", json_data=capped_body())

        result = runner.invoke(app, ["search", "sale", "12305", "--tile", "--max-depth", "0"])

        assert result.exit_code == 0
        assert "listings so far" in result.stderr

    def test_search_tile_progress_suppressed_by_quiet(self, fake_http):
        fake_http.install("PUT", json_data=capped_body())

        result = runner.invoke(app, ["--quiet", "search", "sale", "12305", "--tile", "--max-depth", "0"])

        assert result.exit_code == 0
        assert "listings so far" not in result.stderr

    def test_search_passes_filters_and_rent_switches_to_the_request(self, fake_http, search_success_body):
        """The declared filters and rental switches must reach the request."""
        fake_http.install("PUT", json_data=search_success_body)

        result = runner.invoke(
            app,
            ["search", "rent", "12305", "--min-price", "100000", "--max-beds", "4", "--room"],
        )

        assert result.exit_code == 0
        filter_state = fake_http.calls[0]["json"]["searchQueryState"]["filterState"]
        assert filter_state["price"]["min"] == 100000
        assert filter_state["beds"]["max"] == 4
        assert filter_state["isRoomForRent"]["value"] is True
        assert filter_state["isEntirePlaceForRent"]["value"] is False

    def test_search_sale_leaves_rent_switches_off(self, fake_http, search_success_body):
        """The sale command has no rental switches, so they stay off."""
        fake_http.install("PUT", json_data=search_success_body)

        result = runner.invoke(app, ["search", "sale", "12305"])

        assert result.exit_code == 0
        filter_state = fake_http.calls[0]["json"]["searchQueryState"]["filterState"]
        assert "isRoomForRent" not in filter_state
        assert "isEntirePlaceForRent" not in filter_state

    def test_search_passes_the_zoom_filter_to_the_request(self, fake_http, search_success_body):
        """The map zoom is a filter like any other and must reach the request."""
        fake_http.install("PUT", json_data=search_success_body)

        result = runner.invoke(app, ["search", "sale", "12305", "--zoom", "12"])

        assert result.exit_code == 0
        assert fake_http.calls[0]["json"]["searchQueryState"]["mapZoom"] == 12

    def test_raw_format_rejects_csv(self, fake_http, search_success_body):
        fake_http.install("PUT", json_data=search_success_body)
        result = runner.invoke(app, ["search", "sale", "12305", "--raw", "--format", "csv"])
        assert result.exit_code == 2
        assert "--raw only supports JSON" in result.stderr

    def test_empty_search_results_success(self, fake_http):
        fake_http.install("PUT", json_data=empty_body())
        result = runner.invoke(app, ["search", "sale", "12305"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data == []


def test_every_search_command_declares_every_filter():
    """The CLI reads the filters out of a command's own locals(), by name.

    The names come from :class:`~pyzill.workflows.SearchFilters`, so a filter
    the model has but a command does not declare would be a KeyError in the
    middle of a search. The two are checked here instead.
    """
    filter_names = {field.name for field in fields(SearchFilters)}
    for command in (search_sale, search_rent, search_sold, db_sync):
        declared = set(signature(command).parameters)
        missing = filter_names - declared
        assert not missing, f"{command.__name__} does not declare {sorted(missing)}"


class TestExtractListings:
    """extract_listings must prefer the result list holding real listings."""

    building: ClassVar[dict[str, Any]] = {
        "isBuilding": True,
        "zpid": "47.61503--122.34274",
        "address": "2208 4th Ave",
    }
    unit: ClassVar[dict[str, Any]] = {"zpid": "12345", "address": "1 Main St", "price": "$2,000"}

    def test_prefers_list_results_when_map_results_are_building_cards(self):
        body = {"mapResults": [self.building, self.building], "listResults": [self.unit]}
        assert extract_listings(body) == [self.unit]

    def test_prefers_map_results_when_both_have_real_listings(self):
        # max() is stable, so a tie keeps mapResults (the historical first key).
        body = {"mapResults": [self.unit], "listResults": [self.unit]}
        assert extract_listings(body) == [self.unit]

    def test_falls_back_to_map_results_when_list_results_are_empty(self):
        body = {"mapResults": [self.unit], "listResults": []}
        assert extract_listings(body) == [self.unit]

    def test_counts_building_id_zpid_cards_as_unusable(self):
        # A building card stamped with a non-numeric zpid still has no usable zpid.
        body = {"mapResults": [self.building], "listResults": [self.building]}
        assert extract_listings(body) == [self.building]


class TestDetailCommands:
    """Test detail command routing, options, and exploration flags."""

    @pytest.mark.parametrize(
        "target",
        [
            "348914888",
            "https://www.zillow.com/homedetails/27-Washington-Ave/348914888_zpid/",
        ],
    )
    def test_detail_home_accepts_an_id_or_a_url(self, fake_http, home_detail_body, target):
        fake_http.install("GET", content=home_detail_body)
        result = runner.invoke(app, ["detail", "home", target])
        assert result.exit_code == 0

    @pytest.mark.parametrize(
        "target",
        [
            "CgKZT4",
            "https://www.zillow.com/apartments/kissimmee-fl/the-nexus-at-overbrook/9DSWrh/",
        ],
    )
    def test_detail_apartment_accepts_an_id_or_a_url(self, fake_http, department_detail_body, target):
        fake_http.install("GET", content=department_detail_body)
        result = runner.invoke(app, ["detail", "apartment", target])
        assert result.exit_code == 0

    def test_detail_apartment_rejects_non_zillow_url_as_usage_error(self, fake_http):
        fake_http.install("GET")
        result = runner.invoke(app, ["detail", "apartment", "https://example.com/listing"])
        assert result.exit_code == 2
        assert fake_http.calls == []

    def test_detail_apartment_encodes_id_in_url(self, fake_http, department_detail_body):
        fake_http.install("GET", content=department_detail_body)
        result = runner.invoke(app, ["detail", "apartment", "Cg KZT4"])
        assert result.exit_code == 0
        assert fake_http.calls[-1]["url"] == "https://www.zillow.com/apartments/Cg%20KZT4/"

    def test_detail_requires_url_or_id(self):
        result = runner.invoke(app, ["detail", "home"])
        assert result.exit_code == 2

    @pytest.mark.parametrize("value", ["garbage", "https://example.com/listing"])
    def test_detail_home_rejects_invalid_input(self, value, fake_http):
        fake_http.install("GET")
        result = runner.invoke(app, ["detail", "home", value])
        assert result.exit_code == 2
        assert fake_http.calls == []

    def test_detail_parse_failure_has_schema_guidance(self, fake_http):
        fake_http.install("GET", content=b"<html>unexpected page</html>")

        result = runner.invoke(app, ["detail", "home", "123"])

        assert result.exit_code == 5
        assert "schema may have changed" in result.stderr
        assert "blocked the request" not in result.stderr

    @pytest.mark.parametrize(("option", "value"), [("--max-retries", "-1"), ("--timeout", "0")])
    def test_global_request_limits_reject_invalid_values(self, option, value):
        result = runner.invoke(app, [option, value, "detail", "home", "123"])
        assert result.exit_code == 2

    def test_timeout_zero_honors_json_errors(self):
        # stdout is captured (not a TTY), so errors default to JSON.
        result = runner.invoke(app, ["--timeout", "0", "detail", "home", "123"])
        assert result.exit_code == 2
        payload = json.loads(result.stderr)
        assert payload["error"]["code"] == "usage"
        assert "--timeout" in payload["error"]["message"]

    def test_detail_rejects_non_json_format(self, fake_http, home_detail_body):
        fake_http.install("GET", content=home_detail_body)
        result = runner.invoke(app, ["detail", "home", "348914888", "--format", "csv"])
        assert result.exit_code == 2
        assert "only support JSON" in result.stderr

    def test_keys_lists_fields(self, fake_http, home_detail_body):
        fake_http.install("GET", content=home_detail_body)
        result = runner.invoke(app, ["detail", "home", "348914888", "--keys"])
        assert result.exit_code == 0
        assert "zpid" in result.stdout
        assert "address" in result.stdout

    def test_path_extracts_nested_value(self, fake_http, home_detail_body):
        fake_http.install("GET", content=home_detail_body)
        result = runner.invoke(app, ["detail", "home", "348914888", "--path", "address.city"])
        assert result.exit_code == 0
        assert json.loads(result.stdout) == "Schenectady"

    def test_path_list_index(self, fake_http):
        body = {
            "props": {
                "pageProps": {
                    "componentProps": {
                        "gdpClientCache": json.dumps(
                            {"cache": {"property": {"zpid": 1, "tags": [{"name": "first"}, {"name": "second"}]}}}
                        )
                    }
                }
            }
        }
        html = (
            "<html><head></head><body>"
            f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(body)}</script>'
            "</body></html>"
        )
        fake_http.install("GET", content=html.encode())
        result = runner.invoke(app, ["detail", "home", "1", "--path", "tags[1].name"])
        assert result.exit_code == 0
        assert json.loads(result.stdout) == "second"

    def test_path_not_found_is_clean_error(self, fake_http, home_detail_body):
        fake_http.install("GET", content=home_detail_body)
        result = runner.invoke(app, ["detail", "home", "348914888", "--path", "nope.deep"])
        assert result.exit_code == 1
        assert "not found" in result.stderr

    def test_keys_and_path_conflict(self, fake_http, home_detail_body):
        fake_http.install("GET", content=home_detail_body)
        result = runner.invoke(app, ["detail", "home", "348914888", "--keys", "--path", "address"])
        assert result.exit_code == 2

    def test_keys_respects_output_file(self, fake_http, home_detail_body, tmp_path):
        fake_http.install("GET", content=home_detail_body)
        out = tmp_path / "keys.txt"
        result = runner.invoke(app, ["detail", "home", "348914888", "--keys", "-o", str(out)])
        assert result.exit_code == 0
        assert "zpid" in out.read_text()


class TestNormalizeCommand:
    """Test normalize command."""

    def test_normalize_from_file(self, normalize_file, search_success_body):
        result = normalize_file(search_success_body)
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert isinstance(data, list)

    def test_normalize_from_stdin(self, search_success_body):
        result = runner.invoke(app, ["normalize", "-"], input=json.dumps(search_success_body))
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert isinstance(data, list)

    @pytest.mark.parametrize(
        "payload",
        [
            {"mapResults": [{"zpid": "1", "address": "test"}]},
            {"listResults": [{"zpid": "1", "address": "test"}]},
            [{"zpid": "1", "address": "test"}],
            {"zpid": "1", "address": "test"},
        ],
    )
    def test_normalize_accepts_every_listing_shape(self, normalize_file, payload):
        result = normalize_file(payload)
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert isinstance(data, list)
        assert len(data) == 1

    def test_normalize_rejects_invalid_json(self, tmp_path):
        input_file = tmp_path / "invalid.json"
        input_file.write_text("not json")

        result = runner.invoke(app, ["normalize", str(input_file)])
        assert result.exit_code == 2
        assert "Invalid JSON" in result.stderr

    def test_normalize_rejects_a_payload_that_is_neither_object_nor_array(self, tmp_path):
        input_file = write_json(tmp_path / "input.json", "string")

        result = runner.invoke(app, ["normalize", str(input_file)])
        assert result.exit_code == 2
        assert "must be a JSON object or array" in result.stderr

    def test_normalize_rejects_missing_file(self):
        result = runner.invoke(app, ["normalize", "/nonexistent/file.json"])
        assert result.exit_code == 2
        assert "not found" in result.stderr


class TestOutputFormats:
    """Test output format options."""

    def test_json_output(self, run_search, search_success_body):
        result = run_search(search_success_body, "--format", "json")
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert isinstance(data, list)

    def test_jsonl_output(self, run_search, search_success_body):
        result = run_search(search_success_body, "--format", "jsonl")
        assert result.exit_code == 0
        lines = result.stdout.strip().split("\n")
        assert len(lines) == 2
        for line in lines:
            json.loads(line)

    def test_csv_output(self, run_search, search_success_body):
        result = run_search(search_success_body, "--format", "csv")
        assert result.exit_code == 0
        lines = result.stdout.strip().split("\n")
        assert len(lines) == 3  # header + 2 rows

    def test_csv_empty_list_is_success(self, run_search):
        result = run_search(empty_body(), "--format", "csv")
        assert result.exit_code == 0
        assert result.stdout.strip() == ""  # empty file, not an error

    def test_table_output(self, run_search, search_success_body):
        result = run_search(search_success_body, "--format", "table")
        assert result.exit_code == 0

    def test_table_uses_concise_columns(self, run_search, search_success_body, monkeypatch):
        monkeypatch.setenv("COLUMNS", "200")
        result = run_search(search_success_body, "--format", "table")
        assert result.exit_code == 0
        assert "address" in result.stdout
        assert "zpid" not in result.stdout  # concise display set, not all 21 fields

    def test_output_to_file(self, run_search, search_success_body, tmp_path):
        output_file = tmp_path / "output.json"
        result = run_search(search_success_body, "--output", str(output_file))
        assert result.exit_code == 0
        assert output_file.exists()
        data = json.loads(output_file.read_text())
        assert isinstance(data, list)

    def test_output_dash_means_stdout(self, run_search, search_success_body):
        result = run_search(search_success_body, "--output", "-")
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert isinstance(data, list)


class TestErrorHandling:
    """Test error handling and exit codes."""

    def test_zillow_blocked_error(self, fake_http):
        fake_http.install("PUT", status_code=403, text="blocked")
        result = runner.invoke(app, ["search", "sale", "12305"])
        assert result.exit_code == 3
        assert "blocked" in result.stderr.lower()

    def test_invalid_bbox(self, fake_http, search_success_body):
        fake_http.install("PUT", json_data=search_success_body)
        result = runner.invoke(
            app,
            ["search", "sale", "12305", "--north", "0", "--south", "10", "--east", "0", "--west", "10"],
        )
        assert result.exit_code == 2
        assert "Invalid bounding box" in result.stderr

    def test_quiet_mode_hides_warnings(self, fake_http, search_success_body):
        fake_http.install("PUT", json_data=search_success_body)
        result = runner.invoke(app, ["--quiet", "search", "sale", "12305"])
        assert result.exit_code == 0

    def test_context_does_not_leak_between_invocations(self, fake_http):
        fake_http.install("PUT", json_data=capped_body())
        quiet_result = runner.invoke(app, ["--quiet", "search", "sale", "12305"])
        assert quiet_result.exit_code == 0
        assert "500 listings" not in quiet_result.stderr

        fake_http.install("PUT", json_data=capped_body())
        regular_result = runner.invoke(app, ["search", "sale", "12305"])
        assert regular_result.exit_code == 0
        assert "500 listings" in regular_result.stderr

    def test_verbose_mode(self, fake_http, search_success_body):
        fake_http.install("PUT", json_data=search_success_body)
        result = runner.invoke(app, ["--verbose", "search", "sale", "12305"])
        assert result.exit_code == 0


class TestGlobalOptions:
    """Test global options and the environment they fall back to."""

    @pytest.mark.parametrize(
        "args",
        [
            ["--proxy", "http://user:pass@proxy:8080"],
            ["--impersonate", "chrome120"],
            ["--timeout", "60"],
            ["--max-retries", "3"],
            ["--no-color"],
        ],
    )
    def test_global_options_are_accepted(self, fake_http, search_success_body, args):
        fake_http.install("PUT", json_data=search_success_body)
        result = runner.invoke(app, [*args, "search", "sale", "12305"])
        assert result.exit_code == 0

    def test_geocode_receives_configured_proxy(self, fake_http, search_success_body):
        fake_http.install("PUT", json_data=search_success_body)
        result = runner.invoke(app, ["--proxy", "http://user:pass@proxy:8080", "search", "sale", "12305"])
        assert result.exit_code == 0
        assert _GEO_STUB.kwargs[-1]["proxy_url"] == "http://user:pass@proxy:8080"

    def test_cli_overrides_env(self, fake_http, search_success_body, monkeypatch):
        fake_http.install("PUT", json_data=search_success_body)
        monkeypatch.setenv("PYZILL_PROXY", "http://env:pass@proxy:8080")
        result = runner.invoke(app, ["--proxy", "http://cli:pass@proxy:9090", "search", "sale", "12305"])
        assert result.exit_code == 0

    def test_rate_limit_option_applied(self, fake_http, search_success_body, no_sleep):
        fake_http.install("PUT", json_data=search_success_body)
        result = runner.invoke(app, ["--rate-limit", "2.5", "search", "sale", "12305"])
        assert result.exit_code == 0
        assert ratelimit.get_min_interval() == 2.5
        # The interval is not only stored: the request waited for it.
        assert no_sleep, "the search did not wait for the rate limit"

    def test_rate_limit_env_var(self, fake_http, search_success_body, monkeypatch, no_sleep):
        fake_http.install("PUT", json_data=search_success_body)
        monkeypatch.setenv("PYZILL_RATE_LIMIT", "3.5")
        result = runner.invoke(app, ["search", "sale", "12305"])
        assert result.exit_code == 0
        assert ratelimit.get_min_interval() == 3.5
        assert no_sleep, "the search did not wait for the rate limit"


class TestProxyValidation:
    """The root callback rejects bad proxies before any request."""

    def test_invalid_scheme_is_usage_error_and_redacted(self):
        result = runner.invoke(app, ["--proxy", "ftp://user:SECRETPW@host:1", "search", "sale", "12305"])
        assert result.exit_code == 2
        assert "Invalid proxy" in result.stderr
        assert "SECRETPW" not in result.stderr

    def test_json_error_redacts_proxy_credentials(self):
        result = runner.invoke(
            app,
            ["--json-errors", "--proxy", "ftp://user:SECRETPW@host:1", "search", "sale", "12305"],
        )

        assert result.exit_code == 2
        payload = json.loads(result.stderr)["error"]
        assert payload["code"] == "usage"
        assert "SECRETPW" not in payload["message"]

    @pytest.mark.parametrize("value", ["ftp://user:SECRETPW@host:1", "user:SECRETPW@host:8080"])
    def test_invalid_zillow_proxy_env_is_redacted(self, monkeypatch, value):
        monkeypatch.setenv("ZILLOW_PROXY", value)
        result = runner.invoke(app, ["search", "sale", "12305"])
        assert result.exit_code == 2
        assert "Invalid proxy" in result.stderr
        assert "SECRETPW" not in result.stderr

    def test_redacts_password_containing_at_sign(self):
        assert _redact("http://user:p@ssw0rd@host") == "http://***@host"
        assert _redact("socks5://u:p@ss@host:1080") == "socks5://***@host:1080"
        assert _redact("keep foo@bar.com visible") == "keep foo@bar.com visible"

    @pytest.mark.parametrize("value", ["", "1"])
    def test_no_color_value_decides_whether_color_is_disabled(self, fake_http, search_success_body, monkeypatch, value):
        # The NO_COLOR spec: only a non-empty value disables color.
        fake_http.install("PUT", json_data=search_success_body)
        monkeypatch.setenv("NO_COLOR", value)
        result = runner.invoke(app, ["search", "sale", "12305"])
        assert result.exit_code == 0

    def test_stderr_line_is_not_width_wrapped(self, capsys, monkeypatch):
        monkeypatch.setenv("COLUMNS", "30")
        long_message = "y" * 200
        stderr_line("red", "Error:", long_message)
        err = capsys.readouterr().err
        assert long_message in err
        assert "\n" not in err.strip()

    def test_empty_pyzill_proxy_env_does_not_crash(self, fake_http, search_success_body, monkeypatch):
        fake_http.install("PUT", json_data=search_success_body)
        monkeypatch.setenv("PYZILL_PROXY", "")
        result = runner.invoke(app, ["search", "sale", "12305"])
        assert result.exit_code == 0

    def test_empty_pyzill_db_env_uses_default(self, fake_http, search_success_body, monkeypatch, tmp_path):
        fake_http.install("PUT", json_data=search_success_body)
        monkeypatch.setenv("PYZILL_DB", "")
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
        result = runner.invoke(app, ["db", "sync", "sale", "12305"])
        assert result.exit_code == 0
        assert (tmp_path / "xdg" / "pyzill" / "pyzill.db").exists()


class TestBrokenPipe:
    """Test broken pipe handling."""

    def test_broken_pipe_no_traceback(self, fake_http, search_success_body):
        fake_http.install("PUT", json_data=search_success_body)
        result = runner.invoke(app, ["search", "sale", "12305"])
        assert result.exit_code == 0

    def test_emit_converts_broken_pipe_to_clean_exit(self, monkeypatch):
        """`pyzill ... | head` must exit 0, not dump a traceback."""

        class _BrokenStdout:
            def flush(self) -> None:
                raise BrokenPipeError

            def fileno(self) -> int:
                return 1

        monkeypatch.setattr(typer, "echo", lambda *a, **k: None)
        monkeypatch.setattr(sys, "stdout", _BrokenStdout())
        monkeypatch.setattr(os, "open", lambda *a, **k: 0)
        monkeypatch.setattr(os, "dup2", lambda *a, **k: None)
        monkeypatch.setattr(os, "close", lambda *a, **k: None)

        with pytest.raises(typer.Exit) as exc_info:
            console.emit("payload", None)

        assert exc_info.value.exit_code == 0


class TestModuleInvocation:
    """Test python -m pyzill invocation."""

    def test_module_invocation_works(self):
        result = run_module("--help")
        assert result.returncode == 0
        assert "pyzill" in result.stdout

    def test_parser_errors_are_json_when_requested(self):
        result = run_module("--json-errors", "search", "sale", "12305", "--max-requests", "0")

        assert result.returncode == 2
        assert json.loads(result.stderr)["error"] == {
            "code": "usage",
            "message": "Invalid value for '--max-requests': 0 is not in the range x>=1.",
            "retryable": False,
        }

    @pytest.mark.parametrize(
        ("args", "as_json"),
        [
            (["search", "sale", "12305", "--max-requests", "0"], True),
            (["--no-json-errors", "search", "sale", "12305", "--max-requests", "0"], False),
        ],
    )
    def test_parser_error_format_follows_the_flag(self, args, as_json):
        result = run_module(*args)

        assert result.returncode == 2
        if as_json:
            assert json.loads(result.stderr)["error"]["code"] == "usage"
        else:
            assert result.stderr.startswith("Error: Invalid value for '--max-requests'")


class TestMainEntry:
    """The console-script entry point: exit codes and the error format.

    The subprocess tests cover the parser errors; these cover the entry
    point in-process, including the errors a command raises itself.
    """

    @pytest.fixture
    def boom_command(self):
        """A command that raises a plain ClickException, not a UsageError."""

        @app.command("boom")
        def boom() -> None:
            raise ClickException("something went wrong")

        try:
            yield
        finally:
            for command in list(app.registered_commands):
                if command.name == "boom":
                    app.registered_commands.remove(command)

    def _run(self, monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> int:
        """Run the entry point with the given arguments and return its exit code."""
        monkeypatch.setattr(sys, "argv", ["pyzill", *argv])
        with pytest.raises(SystemExit) as exit_info:
            main_entry()
        code = exit_info.value.code
        assert isinstance(code, int)
        return code

    def test_a_usage_error_exits_with_code_2(self, monkeypatch, capsys):
        code = self._run(monkeypatch, ["--json-errors", "search", "sale"])

        assert code == 2
        assert json.loads(capsys.readouterr().err)["error"]["code"] == "usage"

    def test_no_json_errors_emits_a_plain_message(self, monkeypatch, capsys):
        code = self._run(monkeypatch, ["--no-json-errors", "search", "sale"])

        assert code == 2
        captured = capsys.readouterr().err
        assert captured.startswith("Error:")
        assert "{" not in captured

    @pytest.mark.usefixtures("boom_command")
    def test_a_command_error_honors_no_json_errors(self, monkeypatch, capsys):
        """An error a command raises itself must honor the flag too.

        Only a UsageError carries a context, so the error format cannot be
        derived from the exception here; the context the root callback
        installed is what says which format the user asked for.
        """
        code = self._run(monkeypatch, ["--no-json-errors", "boom"])

        assert code == 1
        captured = capsys.readouterr().err
        assert captured.startswith("Error:")
        assert "{" not in captured

    @pytest.mark.usefixtures("boom_command")
    def test_a_command_error_is_json_when_requested(self, monkeypatch, capsys):
        code = self._run(monkeypatch, ["--json-errors", "boom"])

        assert code == 1
        assert json.loads(capsys.readouterr().err)["error"] == {
            "code": "other",
            "message": "something went wrong",
            "retryable": False,
        }


class TestGeocode:
    """The geocode command and the viewport a search resolves."""

    def test_no_bbox_geocodes_query(self, fake_http, search_success_body):
        fake_http.install("PUT", json_data=search_success_body)
        result = runner.invoke(app, ["search", "sale", "Schenectady NY"])
        assert result.exit_code == 0
        assert _GEO_STUB.calls == ["Schenectady NY"]

    def test_explicit_bbox_skips_geocode(self, fake_http, search_success_body):
        fake_http.install("PUT", json_data=search_success_body)
        result = runner.invoke(
            app,
            ["search", "sale", "12305", "--north", "42.85", "--south", "42.78", "--east", "-73.9", "--west", "-74.0"],
        )
        assert result.exit_code == 0
        assert _GEO_STUB.calls == []

    def test_geocode_failure_is_clean_error(self):
        _GEO_STUB.error = GeocodeError("Geocoding 'nowhere' failed: DNS error. Pass --north/--south/--east/--west.")
        result = runner.invoke(app, ["search", "sale", "nowhere"])
        assert result.exit_code == 1
        assert "Geocoding 'nowhere' failed" in result.stderr
        assert "--east/--west" in result.stderr.replace("\n", "")

    def test_partial_bbox_still_usage_error(self):
        result = runner.invoke(app, ["search", "sale", "12305", "--north", "42.85"])
        assert result.exit_code == 2

    def test_geocode_prints_the_resolved_viewport(self, patch_geocode):
        calls = patch_geocode(
            GeoResult(
                display_name="Schenectady, NY, USA",
                bbox=BBox(north=42.9, south=42.6, east=-73.8, west=-74.1),
            )
        )

        result = runner.invoke(app, ["geocode", "Schenectady NY"])

        assert result.exit_code == 0
        assert calls == ["Schenectady NY"]
        payload = json.loads(result.stdout)
        assert payload["display_name"] == "Schenectady, NY, USA"
        assert payload["north"] == 42.9
        assert payload["south"] == 42.6
        assert payload["east"] == -73.8
        assert payload["west"] == -74.1

    def test_geocode_rejects_an_empty_place(self):
        result = runner.invoke(app, ["geocode", "  "])

        assert result.exit_code == 2
        assert "Place name must not be empty" in result.stderr

    def test_geocode_rejects_a_format_it_cannot_render(self, patch_geocode):
        patch_geocode(GeoResult(display_name="x", bbox=BBox(north=1, south=0, east=1, west=0)))

        result = runner.invoke(app, ["geocode", "Schenectady NY", "--format", "table"])

        assert result.exit_code == 2
        assert "only supports JSON format" in result.stderr

    def test_geocode_reports_an_unreachable_service_as_retryable(self, patch_geocode):
        from pyzill.geocode import GeocodeUnavailableError

        patch_geocode(error=GeocodeUnavailableError("the service is down"))

        result = runner.invoke(app, ["--json-errors", "geocode", "Schenectady NY"])

        assert result.exit_code == 6
        error_payload = json.loads(result.stderr)["error"]
        assert error_payload["retryable"] is True

    def test_geocode_reports_an_unresolved_place_as_an_error(self, patch_geocode):
        patch_geocode(error=GeocodeError("no such place"))

        result = runner.invoke(app, ["--json-errors", "geocode", "Nowhere"])

        assert result.exit_code == 1
        error_payload = json.loads(result.stderr)["error"]
        assert error_payload["retryable"] is False


class TestQueryCommand:
    """Offline re-slicing of saved search results."""

    @pytest.fixture
    def saved(self, tmp_path):
        rows = [
            {
                "zpid": 1,
                "price": 150000,
                "beds": 2,
                "baths": 1,
                "area": 1000,
                "city": "Schenectady",
                "zipcode": "12304",
            },
            {
                "zpid": 2,
                "price": 250000,
                "beds": 3,
                "baths": 2,
                "area": 1500,
                "city": "Schenectady",
                "zipcode": "12308",
            },
            {"zpid": 3, "price": 350000, "beds": 4, "baths": 2, "area": 2000, "city": "Scotia", "zipcode": "12302"},
            {
                "zpid": 4,
                "price": 200000,
                "beds": 3,
                "baths": 1,
                "area": None,
                "city": "Schenectady",
                "zipcode": "12303",
            },
        ]
        return write_json(tmp_path / "saved.json", rows)

    def test_where_filter(self, saved):
        assert {r["zpid"] for r in run_query(saved, "--where", "price<=250000")} == {1, 2, 4}

    def test_multiple_where_anded(self, saved):
        rows = run_query(saved, "--where", "price<=250000", "--where", "beds>=3")
        assert {r["zpid"] for r in rows} == {2, 4}

    def test_where_string_equality_with_numeric_literal(self, saved):
        assert [r["zpid"] for r in run_query(saved, "--where", "zipcode==12304")] == [1]

    @pytest.mark.parametrize("expression", ["zipcode==02134", 'zipcode=="02134"'])
    def test_where_preserves_leading_zero_zipcodes(self, tmp_path, expression):
        path = write_json(tmp_path / "zipcodes.json", [{"zpid": 1, "zipcode": "02134"}])

        assert [row["zpid"] for row in run_query(path, "--where", expression)] == [1]

    def test_where_json_filter(self, saved):
        rows = run_query(saved, "--where-json", '[{"field":"price","op":"<=","value":250000}]')
        assert {row["zpid"] for row in rows} == {1, 2, 4}

    def test_where_json_rejects_unknown_operators(self, saved):
        result = runner.invoke(
            app,
            ["query", str(saved), "--where-json", '[{"field":"price","op":"like","value":1}]'],
        )

        assert result.exit_code == 2
        assert "Unknown operator" in result.stderr

    def test_db_query_preserves_leading_zero_zipcodes_during_write(self, tmp_path):
        db_path = tmp_path / "zipcodes.db"
        writer = connect(db_path)
        upsert_listings(writer, [{"zpid": 1, "zipcode": "02134"}], seen_utc="2026-10-01T00:00:00Z")
        writer.execute("BEGIN IMMEDIATE")
        try:
            result = runner.invoke(
                app,
                ["db", "query", "--where", "zipcode==02134", "--db", str(db_path)],
            )
            assert result.exit_code == 0
            assert [row["zpid"] for row in json.loads(result.stdout)] == [1]
        finally:
            writer.rollback()
            writer.close()

    def test_where_contains(self, saved):
        assert len(run_query(saved, "--where", "city~schen")) == 3

    def test_where_money_string_value(self, saved):
        assert {r["zpid"] for r in run_query(saved, "--where", "price>=$200,000")} == {2, 3, 4}

    def test_sort_asc_and_desc(self, saved):
        rows = run_query(saved, "--sort", "price:desc")
        assert [r["price"] for r in rows] == [350000, 250000, 200000, 150000]

    def test_sort_missing_values_last(self, saved):
        rows = run_query(saved, "--sort", "area")
        assert rows[-1]["zpid"] == 4  # area=None goes last

    def test_ppsf_computed_field(self, saved):
        assert run_query(saved, "--where", "ppsf==150", "--cols", "zpid,ppsf") == [{"zpid": 1, "ppsf": 150.0}]

    def test_ppsf_missing_area_excluded(self, saved):
        rows = run_query(saved, "--where", "ppsf>=50")
        assert 4 not in {r["zpid"] for r in rows}

    def test_cols_selection(self, saved):
        rows = run_query(saved, "--cols", "zpid,price")
        assert list(rows[0]) == ["zpid", "price"]

    def test_limit(self, saved):
        rows = run_query(saved, "--sort", "price:asc", "--limit", "2")
        assert [r["zpid"] for r in rows] == [1, 4]

    def test_csv_format(self, saved):
        result = runner.invoke(app, ["query", str(saved), "--format", "csv", "--cols", "zpid,price"])
        assert result.exit_code == 0
        assert result.stdout.splitlines()[0] == "zpid,price"

    def test_table_honors_cols(self, saved):
        result = runner.invoke(app, ["query", str(saved), "--format", "table", "--cols", "zpid,price"])
        assert result.exit_code == 0
        assert "zpid" in result.stdout
        assert "price" in result.stdout
        assert "city" not in result.stdout  # in the data and concise set, but not in --cols

    def test_stdin(self, saved):
        result = runner.invoke(app, ["query", "-"], input=saved.read_text())
        assert result.exit_code == 0
        assert len(json.loads(result.stdout)) == 4

    @pytest.mark.parametrize(
        "args",
        [
            ["--where", "just a word"],
            ["--sort", "nope"],
            ["--cols", "zpid,nope"],
            ["--limit", "0"],
        ],
    )
    def test_query_rejects_unknown_or_invalid_arguments(self, saved, args):
        result = runner.invoke(app, ["query", str(saved), *args])
        assert result.exit_code == 2

    def test_unknown_where_field_is_named_in_the_error(self, saved):
        result = runner.invoke(app, ["query", str(saved), "--where", "sqft>=1000"])
        assert result.exit_code == 2
        assert "Unknown field" in result.stderr

    def test_non_list_input(self, tmp_path):
        path = write_json(tmp_path / "obj.json", {"zpid": 1})
        result = runner.invoke(app, ["query", str(path)])
        assert result.exit_code == 2
        assert "JSON array" in result.stderr

    def test_missing_file(self):
        result = runner.invoke(app, ["query", "/nonexistent/file.json"])
        assert result.exit_code == 2

    def test_empty_result_is_success(self, saved):
        assert run_query(saved, "--where", "price>999999") == []

    def test_where_rejects_null_with_ordering_operator(self, saved):
        result = runner.invoke(app, ["query", str(saved), "--where", "price>null"])
        assert result.exit_code == 2
        assert "cannot be compared with null" in result.stderr


def test_where_coercion_only_accepts_strict_numeric_literals():
    assert coerce_value("-$1,234.50") == -1234.5
    assert coerce_value("nan") == "nan"
    assert coerce_value("inf") == "inf"
    assert coerce_value("1e3") == "1e3"


class TestDatabaseCommands:
    """Test db sub-app commands."""

    def test_db_query_empty_result_emits_json_array(self, tmp_path):
        """An empty match must still produce valid JSON on stdout."""
        db_path = tmp_path / "empty.db"
        input_file = write_json(tmp_path / "input.json", [{"zpid": 1, "price": 100000}])

        assert runner.invoke(app, ["db", "import", str(input_file), "--db", str(db_path)]).exit_code == 0

        result = runner.invoke(app, ["db", "query", "--db", str(db_path), "--where", "price>999999999"])

        assert result.exit_code == 0
        assert json.loads(result.stdout) == []

    def test_db_query_empty_result_warns_for_human_output(self, tmp_path):
        db_path = tmp_path / "empty.db"
        input_file = write_json(tmp_path / "input.json", [{"zpid": 1, "price": 100000}])

        assert runner.invoke(app, ["db", "import", str(input_file), "--db", str(db_path)]).exit_code == 0

        result = runner.invoke(app, ["db", "query", "--db", str(db_path), "--where", "price>999999999", "-f", "table"])

        assert result.exit_code == 0
        assert "No listings matched the query" in result.stderr

    def test_db_sync_rejects_empty_query(self, tmp_path):
        """An empty query would make a search run look like a file import."""
        result = runner.invoke(app, ["db", "sync", "sale", "", "--db", str(tmp_path / "q.db")])

        assert result.exit_code == 2
        assert "Query must not be empty" in result.stderr

    def test_db_runs_lists_the_recorded_runs(self, tmp_path):
        """A run id handed out by a sync must be resolvable afterwards."""
        db_path = tmp_path / "runs.db"
        conn = connect(db_path)
        run_id = begin_sync_run(
            conn,
            kind="sale",
            query="Albany",
            bbox={"north": 43.0, "south": 42.0},
            filters={"max_price": 500000},
            started_utc="2026-10-01T00:00:00Z",
        )
        finish_sync_run(conn, run_id, result_count=7, finished_utc="2026-10-01T00:01:00Z")
        conn.commit()
        conn.close()

        result = runner.invoke(app, ["db", "runs", "--db", str(db_path)])

        assert result.exit_code == 0
        runs = json.loads(result.stdout)
        assert len(runs) == 1
        assert runs[0]["id"] == run_id
        assert runs[0]["kind"] == "sale"
        assert runs[0]["query"] == "Albany"
        assert runs[0]["result_count"] == 7
        assert runs[0]["truncated"] is False
        assert runs[0]["bbox"] == {"north": 43.0, "south": 42.0}
        assert runs[0]["filters"] == {"max_price": 500000}

    def test_db_runs_filters_by_kind(self, tmp_path):
        db_path = tmp_path / "runs.db"
        conn = connect(db_path)
        begin_sync_run(conn, kind="sale", query="Albany", started_utc="2026-10-01T00:00:00Z")
        begin_sync_run(conn, kind="rent", query="Albany", started_utc="2026-10-02T00:00:00Z")
        conn.commit()
        conn.close()

        result = runner.invoke(app, ["db", "runs", "--db", str(db_path), "--kind", "rent"])

        assert result.exit_code == 0
        runs = json.loads(result.stdout)
        assert [run["kind"] for run in runs] == ["rent"]

    def test_db_sync_zip_only_stores_listings_in_requested_zip(self, run_sync, tmp_path):
        db_path = tmp_path / "zip.db"

        result = run_sync(
            search_body(
                {"zpid": 1, "addressZipcode": "12305", "price": 100000},
                {"zpid": 2, "addressZipcode": "12309", "price": 200000},
                {"zpid": 3, "price": 300000},
            ),
            db_path=db_path,
        )

        assert result.exit_code == 0
        conn = connect(db_path)
        assert conn.execute("SELECT zpid FROM listings ORDER BY zpid").fetchall() == [(1,)]
        assert conn.execute("SELECT result_count FROM sync_runs").fetchone() == (1,)
        conn.close()

    def test_db_sync_zip_selects_results_containing_requested_zip(self, run_sync, tmp_path):
        db_path = tmp_path / "zip.db"

        result = run_sync(
            search_body(
                {"zpid": 1, "addressZipcode": "12305"},
                listResults=[
                    {"zpid": 2, "addressZipcode": "12309"},
                    {"zpid": 3, "addressZipcode": "12309"},
                ],
            ),
            db_path=db_path,
        )

        assert result.exit_code == 0
        conn = connect(db_path)
        assert conn.execute("SELECT zpid FROM listings ORDER BY zpid").fetchall() == [(1,)]
        assert conn.execute("SELECT result_count FROM sync_runs").fetchone() == (1,)
        conn.close()

    def test_db_sync_creates_database(self, run_sync, search_success_body, tmp_path):
        db_path = tmp_path / "test.db"
        result = run_sync(search_success_body, db_path=db_path)
        assert result.exit_code == 0
        assert db_path.exists()
        assert "Synced 2 listings: 2 new, 0 updated (0 price changes), 0 skipped" in result.stderr

    def test_db_sync_json_summary(self, run_sync, search_success_body, tmp_path):
        result = run_sync(search_success_body, "--format", "json", db_path=tmp_path / "summary.db")

        assert result.exit_code == 0
        assert json.loads(result.stdout) == {
            "action": "synced",
            "total": 2,
            "added": 2,
            "updated": 0,
            "price_changes": 0,
            "skipped": 0,
            "run_id": 1,
            "truncated": False,
        }

    def test_db_import_json_summary(self, tmp_path):
        db_path = tmp_path / "summary.db"
        source = write_json(tmp_path / "rows.json", [{"zpid": 1, "price": 100}])

        result = runner.invoke(app, ["db", "import", str(source), "--format", "json", "--db", str(db_path)])

        assert result.exit_code == 0
        assert json.loads(result.stdout) == {
            "action": "imported",
            "total": 1,
            "added": 1,
            "updated": 0,
            "price_changes": 0,
            "skipped": 0,
            "run_id": 1,
            "truncated": False,
        }
        assert db_path.exists()

    def test_db_import_accepts_kind(self, tmp_path):
        rows_file = write_json(tmp_path / "rent.json", [{"zpid": 1, "price": 2200}])
        db_path = tmp_path / "rent.db"

        result = runner.invoke(app, ["db", "import", str(rows_file), "--kind", "rent", "--db", str(db_path)])

        assert result.exit_code == 0
        conn = connect(db_path)
        assert conn.execute("SELECT kind FROM listings WHERE zpid = 1").fetchone()[0] == "rent"
        assert conn.execute("SELECT kind FROM price_history WHERE zpid = 1").fetchone()[0] == "rent"
        conn.close()

    def test_db_import_rejects_a_payload_that_is_not_a_listing_array(self, tmp_path):
        """A payload that is not an object or an array is a usage error."""
        input_file = write_json(tmp_path / "scalar.json", "hello")

        result = runner.invoke(
            app,
            ["db", "import", str(input_file), "--db", str(tmp_path / "test.db")],
        )

        assert result.exit_code == 2
        assert "Traceback" not in result.stderr

    def test_db_sync_blocked_request_reports_the_error_not_a_traceback(self, fake_http, tmp_path):
        """A blocked request must be a stable exit code, not an unhandled error.

        A script has to be able to tell "Zillow blocked us" from "the tool
        has a bug", so the sync command reports the library failures the
        same way the search command does.
        """
        fake_http.install("PUT", status_code=403, text="blocked")

        result = runner.invoke(
            app,
            ["--json-errors", "db", "sync", "sale", "12305", "--db", str(tmp_path / "blocked.db")],
        )

        assert result.exit_code == 3
        payload = json.loads(result.stderr)
        assert payload["error"]["code"] == "blocked"
        assert payload["error"]["status_code"] == 403

    def test_db_sync_rejects_a_format_it_cannot_render_before_it_searches(self, fake_http, tmp_path):
        """The format check must not depend on whether the search found rows."""
        fake_http.install("PUT", status_code=403, text="blocked")

        result = runner.invoke(
            app,
            ["db", "sync", "sale", "12305", "--format", "csv", "--db", str(tmp_path / "test.db")],
        )

        assert result.exit_code == 2

    def test_db_import_rejects_a_format_it_cannot_render_on_an_empty_file(self, tmp_path):
        """An empty file must not skip the format check a full file gets."""
        rows_file = write_json(tmp_path / "empty.json", [])

        result = runner.invoke(
            app,
            ["db", "import", str(rows_file), "--format", "csv", "--db", str(tmp_path / "test.db")],
        )

        assert result.exit_code == 2

    def test_db_sync_json_summary_reports_a_truncated_run(self, run_sync, search_success_body, tmp_path):
        """A script must be able to tell a complete run from a partial one."""
        db_path = tmp_path / "truncated.db"
        first = run_sync(search_success_body, db_path=db_path)
        assert first.exit_code == 0

        # Same query/bbox/filters, now empty: a soft block, not a real clear-out.
        result = run_sync(empty_body(), "--format", "json", db_path=db_path)

        assert result.exit_code == 0
        summary = json.loads(result.stdout)
        assert summary["truncated"] is True
        assert summary["run_id"] > 0

    def test_db_sync_empty_results_warns(self, run_sync, tmp_path):
        db_path = tmp_path / "test.db"
        result = run_sync(empty_body(), db_path=db_path)
        assert result.exit_code == 0
        assert "No listings found" in result.stderr
        conn = connect(db_path)
        assert conn.execute("SELECT result_count, truncated FROM sync_runs").fetchone() == (0, 0)
        conn.close()

    def test_db_sync_empty_after_prior_results_is_truncated(self, run_sync, search_success_body, tmp_path):
        db_path = tmp_path / "test.db"
        first = run_sync(search_success_body, db_path=db_path)
        assert first.exit_code == 0

        # Same query/bbox/filters, now empty: a soft block, not a real clear-out.
        second = run_sync(empty_body(), db_path=db_path)
        assert second.exit_code == 0
        assert "truncated" in second.stderr

        conn = connect(db_path)
        runs = conn.execute("SELECT result_count, truncated FROM sync_runs ORDER BY id").fetchall()
        conn.close()
        assert runs[0] == (2, 0)
        assert runs[-1] == (0, 1)

    def test_db_sync_confirm_empty_reports_delistings(self, run_sync, search_success_body, tmp_path):
        from pyzill.db import get_changes

        db_path = tmp_path / "confirmed_empty.db"
        assert run_sync(search_success_body, db_path=db_path).exit_code == 0

        assert run_sync(empty_body(), db_path=db_path).exit_code == 0
        confirmed = run_sync(empty_body(), "--confirm-empty", db_path=db_path)

        assert confirmed.exit_code == 0
        assert "truncated" not in confirmed.stderr
        conn = connect(db_path)
        assert conn.execute("SELECT result_count, truncated FROM sync_runs ORDER BY id").fetchall() == [
            (2, 0),
            (0, 1),
            (0, 0),
        ]
        changes = get_changes(conn, "2000-01-01T00:00:00Z")
        assert {event["zpid"] for event in changes if event["event"] == "delisted"} == {348914888, 348914889}
        conn.close()

    def test_db_sync_re_sync_updates_last_seen(self, run_sync, search_success_body, tmp_path):
        db_path = tmp_path / "test.db"

        # First sync
        result1 = run_sync(search_success_body, db_path=db_path)
        assert result1.exit_code == 0
        assert "new" in result1.stderr

        # Re-sync (same data)
        result2 = run_sync(search_success_body, db_path=db_path)
        assert result2.exit_code == 0
        assert "updated" in result2.stderr

    def test_db_sync_price_change_detection(self, fake_http, tmp_path, monkeypatch):
        # Pin the sync clock so the later --fetched-at import is always newer.
        # The sync run's timestamp is taken by the workflow layer, which owns
        # the run lifecycle.
        monkeypatch.setattr("pyzill.workflows.sync.utc_now", lambda: "2026-10-19T10:00:00Z")

        # First sync with price 250000
        body1 = search_body({"zpid": "123", "price": 250000, "addressZipcode": "12305"})
        fake_http.install("PUT", json_data=body1)
        db_path = tmp_path / "test.db"

        result1 = runner.invoke(app, ["db", "sync", "sale", "12305", "--db", str(db_path)])
        assert result1.exit_code == 0

        # Import instead of sync to control timestamp
        rows = [{"zpid": "123", "price": 260000, "address": "test"}]
        input_file = write_json(tmp_path / "input.json", rows)
        result2 = runner.invoke(
            app, ["db", "import", str(input_file), "--fetched-at", "2026-10-19T12:00:00Z", "--db", str(db_path)]
        )
        assert result2.exit_code == 0
        assert "1 price changes" in result2.stderr

    def test_db_sync_invalid_kind(self, fake_http, search_success_body):
        fake_http.install("PUT", json_data=search_success_body)
        result = runner.invoke(app, ["db", "sync", "invalid", "12305"])
        assert result.exit_code == 2
        assert "sale" in result.stderr
        assert "rent" in result.stderr
        assert "sold" in result.stderr

    def test_db_sync_room_with_non_rent(self, fake_http, search_success_body):
        fake_http.install("PUT", json_data=search_success_body)
        result = runner.invoke(app, ["db", "sync", "sale", "12305", "--room"])
        assert result.exit_code == 2
        assert "only work with 'rent' kind" in result.stderr

    def test_db_import_raw_response(self, db_rows, tmp_path):
        body = search_body({"zpid": "123", "price": 250000, "address": "test"})
        input_file = write_json(tmp_path / "input.json", body)
        db_path = tmp_path / "test.db"

        result = runner.invoke(app, ["db", "import", str(input_file), "--db", str(db_path)])
        assert result.exit_code == 0
        assert "Imported 1 listings: 1 new, 0 updated (0 price changes), 0 skipped" in result.stderr
        assert db_path.exists()

        # The nested response envelope must yield its mapResults rows, not be
        # treated as a single bogus listing.
        data = db_rows(db_path)
        assert len(data) == 1
        assert data[0]["zpid"] == 123
        assert data[0]["price"] == 250000

    def test_db_import_normalizes_raw_rows(self, db_rows, tmp_path):
        body = search_body(
            {
                "zpid": "32463997",
                "price": "$459,000",
                "address": "1230 State St, Schenectady, NY 12305",
                "detailUrl": "/homes/1230-State-St.html",
                "imgSrc": "https://img.example/x.jpg",
                "statusText": "House for sale",
            }
        )
        input_file = write_json(tmp_path / "input.json", body)
        db_path = tmp_path / "test.db"

        result = runner.invoke(app, ["db", "import", str(input_file), "--db", str(db_path)])
        assert result.exit_code == 0

        data = db_rows(db_path)
        assert len(data) == 1
        row = data[0]
        assert row["price"] == 459000
        assert row["detail_url"] == "https://www.zillow.com/homes/1230-State-St.html"
        assert row["image_url"] == "https://img.example/x.jpg"
        assert row["status"] == "House for sale"

    def test_db_import_preserves_normalized_fields(self, db_rows, tmp_path):
        # Rows exported by `db query` carry the timestamp columns; they must be
        # imported as-is rather than re-normalized (which would null out city).
        rows = [
            {
                "zpid": 123,
                "price": 250000,
                "address": "123 Main St",
                "city": "Schenectady",
                "first_seen_utc": "2026-10-01T12:00:00Z",
                "last_seen_utc": "2026-10-01T12:00:00Z",
            }
        ]
        input_file = write_json(tmp_path / "input.json", rows)
        db_path = tmp_path / "test.db"

        result = runner.invoke(app, ["db", "import", str(input_file), "--db", str(db_path)])
        assert result.exit_code == 0
        assert "Imported 1 listings: 1 new" in result.stderr

        assert db_rows(db_path)[0]["city"] == "Schenectady"

    def test_db_import_same_timestamp_price_change(self, tmp_path):
        db_path = tmp_path / "test.db"
        first = write_json(tmp_path / "first.json", [{"zpid": 123, "price": 250000}])
        second = write_json(tmp_path / "second.json", [{"zpid": 123, "price": 260000}])

        # Two imports stamped at the same second must not collide on the
        # (zpid, seen_utc) primary key of price_history.
        for path in (first, second):
            result = runner.invoke(
                app,
                ["db", "import", str(path), "--fetched-at", "2026-10-02T12:00:00Z", "--db", str(db_path)],
            )
            assert result.exit_code == 0
        assert "1 price changes" in result.stderr

        history = runner.invoke(app, ["db", "history", "123", "--db", str(db_path)])
        assert history.exit_code == 0
        data = json.loads(history.stdout)
        assert len(data) == 1
        assert data[0]["price"] == 260000

    def test_db_import_normalized_array(self, tmp_path):
        rows = [{"zpid": "123", "price": 250000, "address": "test", "city": "Schenectady"}]
        input_file = write_json(tmp_path / "input.json", rows)
        db_path = tmp_path / "test.db"

        result = runner.invoke(app, ["db", "import", str(input_file), "--db", str(db_path)])
        assert result.exit_code == 0
        assert "Imported 1 listings: 1 new, 0 updated (0 price changes), 0 skipped" in result.stderr

    def test_db_import_stdin(self, tmp_path):
        rows = [{"zpid": "123", "price": 250000, "address": "test"}]
        db_path = tmp_path / "test.db"

        result = runner.invoke(app, ["db", "import", "-", "--db", str(db_path)], input=json.dumps(rows))
        assert result.exit_code == 0
        assert db_path.exists()

    def test_db_import_custom_timestamp(self, tmp_path):
        rows = [{"zpid": "123", "price": 250000, "address": "test"}]
        input_file = write_json(tmp_path / "input.json", rows)
        db_path = tmp_path / "test.db"

        result = runner.invoke(
            app, ["db", "import", str(input_file), "--fetched-at", "2026-10-01T12:00:00Z", "--db", str(db_path)]
        )
        assert result.exit_code == 0

    def test_db_import_invalid_timestamp(self, tmp_path):
        rows = [{"zpid": "123", "price": 250000, "address": "test"}]
        input_file = write_json(tmp_path / "input.json", rows)
        db_path = tmp_path / "test.db"

        result = runner.invoke(app, ["db", "import", str(input_file), "--fetched-at", "invalid", "--db", str(db_path)])
        assert result.exit_code == 2
        assert "Invalid --fetched-at timestamp" in result.stderr

    @pytest.mark.parametrize("command", [["query"], ["history", "123"], ["stats"], ["runs"]])
    def test_db_commands_report_a_missing_database(self, tmp_path, command):
        result = runner.invoke(app, ["db", *command, "--db", str(tmp_path / "nonexistent.db")])

        assert result.exit_code == 1
        assert "Database not found" in result.stderr

    @pytest.mark.parametrize("command", [["query"], ["history", "123"], ["stats"]])
    def test_db_commands_report_corrupt_database_without_traceback(self, tmp_path, command):
        db_path = tmp_path / "corrupt.db"
        db_path.write_text("not a SQLite database")

        result = runner.invoke(app, ["db", *command, "--db", str(db_path)])

        assert result.exit_code == 1
        assert "database" in result.stderr.lower()
        assert "Traceback" not in result.stderr

    def test_db_query_binds_filter_values(self, db_rows, import_rows):
        records = [{"zpid": 123, "city": "Albany' OR 1=1 --"}, {"zpid": 456, "city": "Troy"}]
        db_path = import_rows(records)

        rows = db_rows(db_path, "--where", "city==Albany' OR 1=1 --")

        assert [row["zpid"] for row in rows] == [123]

    def test_db_query_with_timestamp_filter(self, db_rows, import_rows):
        # Import data with specific timestamp
        rows = [
            {"zpid": "123", "price": 250000, "address": "test", "city": "Schenectady"},
            {"zpid": "456", "price": 300000, "address": "test2", "city": "Albany"},
        ]
        db_path = import_rows(rows, fetched_at="2026-10-01T12:00:00Z")

        # Query with timestamp filter
        assert len(db_rows(db_path, "--where", "last_seen_utc>=2026-10-01")) == 2

    def test_db_query_reuses_where_sort_cols_limit(self, db_rows, import_rows):
        rows = [
            {"zpid": "123", "price": 250000, "address": "123 Main St", "city": "Schenectady"},
            {"zpid": "456", "price": 300000, "address": "456 Oak Ave", "city": "Albany"},
        ]
        db_path = import_rows(rows)

        # Test --where
        assert len(db_rows(db_path, "--where", "city==Schenectady")) == 1

        # Test --sort
        assert db_rows(db_path, "--sort", "price:desc")[0]["price"] == 300000

        # Test --cols
        assert list(db_rows(db_path, "--cols", "zpid,price")[0].keys()) == ["zpid", "price"]

        # Test --limit
        assert len(db_rows(db_path, "--limit", "1")) == 1

    def test_db_history_shows_price_timeline(self, import_rows):
        rows = [{"zpid": "123", "price": 250000, "address": "test"}]
        db_path = import_rows(rows, fetched_at="2026-10-01T12:00:00Z")

        # Update with price 260000
        rows[0]["price"] = 260000
        import_rows(rows, db_path=db_path, fetched_at="2026-10-02T12:00:00Z")

        # Check history
        result = runner.invoke(app, ["db", "history", "123", "--db", str(db_path)])
        assert result.exit_code == 0
        history = json.loads(result.stdout)
        assert len(history) == 2
        assert history[0]["price"] == 250000
        assert history[1]["price"] == 260000

    def test_db_history_empty_for_unknown_zpid(self, import_rows):
        db_path = import_rows([{"zpid": "123", "price": 250000, "address": "test"}])

        result = runner.invoke(app, ["db", "history", "999", "--db", str(db_path)])
        assert result.exit_code == 0
        assert json.loads(result.stdout) == []

    def test_db_changes_reports_price_drop_from_imports(self, import_rows):
        """Imports still report price changes, but never a delisting."""
        db_path = import_rows(
            [{"zpid": 1, "price": 300000}, {"zpid": 2, "price": 200000}],
            fetched_at="2026-10-01T00:00:00Z",
        )
        import_rows([{"zpid": 1, "price": 250000}], db_path=db_path, fetched_at="2026-10-02T00:00:00Z")

        result = runner.invoke(app, ["db", "changes", "2026-10-02T00:00:00Z", "--db", str(db_path)])

        assert result.exit_code == 0
        event_types = {(event["event"], event["zpid"]) for event in json.loads(result.stdout)}
        assert ("price_drop", 1) in event_types
        # The second import simply did not mention zpid 2; that is not a
        # delisting, so the two imports must not be compared with each other.
        assert ("delisted", 2) not in event_types

    def test_db_changes_table_shows_the_columns_of_the_events(self, import_rows):
        """A change is not a listing: the table shows what an event carries."""
        db_path = import_rows([{"zpid": 1, "price": 250000}], fetched_at="2026-10-01T00:00:00Z")
        import_rows([{"zpid": 1, "price": 240000}], db_path=db_path, fetched_at="2026-10-02T00:00:00Z")

        result = runner.invoke(app, ["db", "changes", "2026-10-01T00:00:00Z", "--db", str(db_path), "-f", "table"])

        assert result.exit_code == 0
        assert "event" in result.stdout
        assert "zpid" in result.stdout
        assert "price_drop" in result.stdout

    def test_db_changes_with_no_events_says_so(self, import_rows):
        db_path = import_rows([{"zpid": 1, "price": 250000}], fetched_at="2026-10-01T00:00:00Z")

        result = runner.invoke(app, ["db", "changes", "2030-01-01T00:00:00Z", "--db", str(db_path), "-f", "table"])

        assert result.exit_code == 0
        assert "No changes found" in result.stderr
        assert "No listings found" not in result.stderr

    def test_db_history_table_shows_the_columns_of_the_history(self, import_rows):
        db_path = import_rows([{"zpid": 1, "price": 250000}], fetched_at="2026-10-01T00:00:00Z")

        result = runner.invoke(app, ["db", "history", "1", "--db", str(db_path), "-f", "table"])

        assert result.exit_code == 0
        assert "seen_utc" in result.stdout
        assert "kind" in result.stdout

    def test_db_changes_reports_delisted_from_searches(self, run_sync, tmp_path):
        """Two searches of the same query detect a listing that disappeared."""
        db_path = tmp_path / "searches.db"

        def body(*zpids: int) -> dict[str, Any]:
            return search_body(*({"zpid": zpid, "addressZipcode": "12305", "price": 300000} for zpid in zpids))

        first = run_sync(body(1, 2), db_path=db_path)
        assert first.exit_code == 0

        second = run_sync(body(1), db_path=db_path)
        assert second.exit_code == 0

        result = runner.invoke(app, ["db", "changes", "2020-01-01T00:00:00Z", "--db", str(db_path)])

        assert result.exit_code == 0
        event_types = {(event["event"], event["zpid"]) for event in json.loads(result.stdout)}
        assert ("delisted", 2) in event_types

    def test_db_changes_normalizes_since_with_offset(self, import_rows):
        db_path = import_rows([{"zpid": 1, "price": 250000}], fetched_at="2026-10-01T12:00:00Z")

        # An offset timestamp must compare the same as its canonical UTC form.
        result = runner.invoke(app, ["db", "changes", "2026-10-01T14:00:00+02:00", "--db", str(db_path)])
        assert result.exit_code == 0

    def test_db_changes_rejects_invalid_since(self, import_rows):
        db_path = import_rows([{"zpid": 1, "price": 250000}])
        result = runner.invoke(app, ["db", "changes", "not-a-date", "--db", str(db_path)])
        assert result.exit_code == 2

    def test_db_changes_normalizes_since_with_milliseconds(self, import_rows):
        # A sub-second suffix must compare the same as its canonical UTC form;
        # otherwise a "2026-10-01T12:00:00.500Z" SINCE would sort after the
        # fixed-width stored timestamps and silently drop the listing.
        db_path = import_rows([{"zpid": 1, "price": 250000}], fetched_at="2026-10-01T12:00:00Z")

        result = runner.invoke(app, ["db", "changes", "2026-10-01T12:00:00.500Z", "--db", str(db_path)])
        assert result.exit_code == 0
        events = json.loads(result.stdout)
        assert any(event["event"] == "new" and event["zpid"] == 1 for event in events)

    def test_db_stats_shows_statistics(self, import_rows):
        rows = [
            {"zpid": "123", "price": 250000, "address": "test"},
            {"zpid": "456", "price": 300000, "address": "test2"},
        ]
        db_path = import_rows(rows, fetched_at="2026-10-01T12:00:00Z")

        result = runner.invoke(app, ["db", "stats", "--db", str(db_path)])
        assert result.exit_code == 0
        stats = json.loads(result.stdout)
        assert stats["total_listings"] == 2
        assert stats["db_path"] == str(db_path)

    def test_db_history_honors_csv_format(self, import_rows):
        db_path = import_rows([{"zpid": 1, "price": 100}])

        result = runner.invoke(app, ["db", "history", "1", "--format", "csv", "--db", str(db_path)])

        assert result.exit_code == 0
        assert result.stdout.splitlines()[0] == "price,seen_utc,kind"
        assert result.stdout.endswith(",sale\n")

    def test_db_prune_requires_a_selector(self, tmp_path):
        db_path = tmp_path / "prune.db"
        runner.invoke(app, ["db", "import", "-", "--db", str(db_path)], input='[{"zpid": 1}]')

        result = runner.invoke(app, ["db", "prune", "--db", str(db_path)])

        assert result.exit_code == 2
        assert "Nothing to prune" in result.stderr

    def test_db_prune_keep_runs_reports_json_and_keeps_listings(self, import_rows):
        db_path = import_rows([{"zpid": 1, "price": 100}], fetched_at="2026-10-01T00:00:00Z")
        import_rows([{"zpid": 2, "price": 200}], db_path=db_path, fetched_at="2026-10-02T00:00:00Z")

        result = runner.invoke(app, ["db", "prune", "--keep-runs", "1", "--format", "json", "--db", str(db_path)])

        assert result.exit_code == 0
        assert json.loads(result.stdout) == {
            "runs_deleted": 1,
            "sightings_deleted": 1,
            "vacuumed": False,
        }
        stats = runner.invoke(app, ["db", "stats", "--db", str(db_path)])
        assert json.loads(stats.stdout)["total_listings"] == 2

    def test_db_prune_rejects_negative_limits(self, import_rows):
        db_path = import_rows([{"zpid": 1, "price": 100}])

        result = runner.invoke(app, ["db", "prune", "--older-than", "-1", "--db", str(db_path)])

        assert result.exit_code == 2
        assert "must be non-negative" in result.stderr

    def test_db_stats_can_force_human_format(self, tmp_path):
        db_path = tmp_path / "stats.db"
        runner.invoke(app, ["db", "import", "-", "--db", str(db_path)], input='[{"zpid": 1}]')

        result = runner.invoke(app, ["--no-json-errors", "db", "stats", "--format", "table", "--db", str(db_path)])

        assert result.exit_code == 2
        assert "only supports --format json" in result.stderr

    def test_db_stats_human_output_on_tty(self, tmp_path, monkeypatch):
        import io

        from rich.console import Console

        from pyzill.cli import db as db_module

        db_path = tmp_path / "stats_human.db"
        runner.invoke(
            app,
            ["db", "import", "-", "--fetched-at", "2026-10-01T12:00:00Z", "--db", str(db_path)],
            input='[{"zpid": 1, "price": 100000}, {"zpid": 2, "price": 200000}]',
        )

        class _TtyStdout(io.StringIO):
            def isatty(self) -> bool:
                return True

        buffer = _TtyStdout()
        monkeypatch.setattr(sys, "stdout", buffer)
        monkeypatch.setattr(
            db_module,
            "context",
            lambda: _FakeConsoleContext(Console(file=buffer, force_terminal=False, no_color=True, width=200)),
        )

        db_module.db_stats_cmd(db_path=str(db_path), output_format=None)

        assert "Total listings: 2" in buffer.getvalue()
        assert str(db_path) in buffer.getvalue()

    def test_db_env_var_path(self, tmp_path, monkeypatch):
        rows = [{"zpid": "123", "price": 250000, "address": "test"}]
        input_file = write_json(tmp_path / "input.json", rows)
        db_path = tmp_path / "test.db"

        monkeypatch.setenv("PYZILL_DB", str(db_path))
        result = runner.invoke(app, ["db", "import", str(input_file)])
        assert result.exit_code == 0
        assert db_path.exists()

    def test_db_default_path(self, tmp_path, monkeypatch):
        rows = [{"zpid": "123", "price": 250000, "address": "test"}]
        input_file = write_json(tmp_path / "input.json", rows)
        xdg_home = tmp_path / "xdg"
        monkeypatch.setenv("XDG_DATA_HOME", str(xdg_home))

        result = runner.invoke(app, ["db", "import", str(input_file)])
        assert result.exit_code == 0
        assert (xdg_home / "pyzill" / "pyzill.db").exists()

    def test_db_legacy_warning_when_local_pyzill_db_exists(self, tmp_path, monkeypatch):
        import pyzill.cli.db as cli_db

        monkeypatch.setattr(cli_db._warning_state, "legacy_db_shown", False)
        legacy_db = tmp_path / "pyzill.db"

        legacy_db.touch()
        xdg_home = tmp_path / "xdg"
        monkeypatch.setenv("XDG_DATA_HOME", str(xdg_home))
        monkeypatch.chdir(tmp_path)

        rows = [{"zpid": "123", "price": 250000, "address": "test"}]
        input_file = write_json(tmp_path / "input.json", rows)

        result = runner.invoke(app, ["db", "import", str(input_file)])
        assert result.exit_code == 0
        assert "default database is now" in result.stderr
