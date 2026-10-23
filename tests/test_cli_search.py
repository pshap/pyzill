"""CLI tests: search."""

import json
from dataclasses import fields
from inspect import signature
from typing import Any, ClassVar

import pytest

from pyzill.cli import app
from pyzill.cli.db import db_sync
from pyzill.cli.search import extract_listings, search_rent, search_sale, search_sold
from pyzill.workflows import SearchFilters
from tests.cli_helpers import capped_body, empty_body, runner, search_body

pytestmark = pytest.mark.usefixtures("_stub_geocode")


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
