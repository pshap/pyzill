"""Tests for pyzill CLI."""

import json
import sys
from typing import Any, ClassVar

import pytest
from typer.testing import CliRunner

from pyzill.bbox import BBox
from pyzill.cli import app
from pyzill.cli.search import extract_listings
from pyzill.geocode import GeoResult

runner = CliRunner()


class _GeoStub:
    """Recording stand-in for pyzill.cli.search.geocode (installed autouse)."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.result = GeoResult(
            display_name="Testville",
            bbox=BBox(north=42.9, south=42.7, east=-73.8, west=-74.0),
        )
        self.error: Exception | None = None

    def __call__(self, place: str, **_kwargs: object) -> GeoResult:
        self.calls.append(place)
        if self.error is not None:
            raise self.error
        return self.result

    def reset(self) -> None:
        self.calls.clear()
        self.error = None


_GEO_STUB = _GeoStub()


@pytest.fixture(autouse=True)
def _disable_rate_limit(monkeypatch):
    """Keep tests fast: no throttling between mocked requests."""
    from pyzill import ratelimit

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

    def test_no_args_shows_help(self):
        result = runner.invoke(app, [])
        assert result.exit_code == 2  # Typer's no_args_is_help returns 2
        assert "help" in result.stdout.lower()


class TestSearchCommands:
    """Test search command routing and options."""

    def test_search_sale_command_exists(self, fake_http, search_success_body):
        fake_http.install("PUT", json_data=search_success_body)
        result = runner.invoke(app, ["search", "sale", "12305"])
        assert result.exit_code == 0

    def test_search_rent_command_exists(self, fake_http, search_success_body):
        fake_http.install("PUT", json_data=search_success_body)
        result = runner.invoke(app, ["search", "rent", "12305"])
        assert result.exit_code == 0

    def test_search_sold_command_exists(self, fake_http, search_success_body):
        fake_http.install("PUT", json_data=search_success_body)
        result = runner.invoke(app, ["search", "sold", "12305"])
        assert result.exit_code == 0

    def test_search_requires_query(self):
        result = runner.invoke(app, ["search", "sale"])
        assert result.exit_code == 2

    def test_search_with_bbox_options(self, fake_http, search_success_body):
        fake_http.install("PUT", json_data=search_success_body)
        result = runner.invoke(
            app,
            [
                "search",
                "sale",
                "12305",
                "--north",
                "42.85",
                "--south",
                "42.78",
                "--east",
                "-73.90",
                "--west",
                "-74.00",
            ],
        )
        assert result.exit_code == 0

    def test_search_warns_when_result_cap_is_hit(self, fake_http):
        body = {"cat1": {"searchResults": {"mapResults": [{"zpid": str(index)} for index in range(500)]}}}
        fake_http.install("PUT", json_data=body)

        result = runner.invoke(app, ["search", "sale", "12305"])

        assert result.exit_code == 0
        assert "500 listings" in result.stderr

    def test_search_tile_splits_capped_results(self, fake_http):
        body = {"cat1": {"searchResults": {"mapResults": [{"zpid": str(index)} for index in range(500)]}}}
        fake_http.install("PUT", json_data=body)

        result = runner.invoke(app, ["search", "sale", "12305", "--tile", "--max-depth", "1"])

        assert result.exit_code == 0
        assert len(fake_http.calls) == 5
        assert "still returned 500" in result.stderr

    def test_search_tile_stops_at_max_requests_budget(self, fake_http):
        body = {"cat1": {"searchResults": {"mapResults": [{"zpid": str(index)} for index in range(500)]}}}
        fake_http.install("PUT", json_data=body)

        result = runner.invoke(
            app,
            ["search", "sale", "12305", "--tile", "--max-depth", "1", "--max-requests", "2"],
        )

        assert result.exit_code == 0
        assert len(fake_http.calls) == 2
        assert "budget of 2 exhausted" in result.stderr

    def test_search_max_requests_rejects_zero(self):
        result = runner.invoke(app, ["search", "sale", "12305", "--max-requests", "0"])

        assert result.exit_code == 2

    def test_search_max_depth_rejects_values_above_six(self):
        result = runner.invoke(app, ["search", "sale", "12305", "--max-depth", "7"])

        assert result.exit_code == 2

    def test_search_tile_reports_progress_on_stderr(self, fake_http):
        body = {"cat1": {"searchResults": {"mapResults": [{"zpid": str(index)} for index in range(500)]}}}
        fake_http.install("PUT", json_data=body)

        result = runner.invoke(app, ["search", "sale", "12305", "--tile", "--max-depth", "0"])

        assert result.exit_code == 0
        assert "listings so far" in result.stderr

    def test_search_tile_progress_suppressed_by_quiet(self, fake_http):
        body = {"cat1": {"searchResults": {"mapResults": [{"zpid": str(index)} for index in range(500)]}}}
        fake_http.install("PUT", json_data=body)

        result = runner.invoke(app, ["--quiet", "search", "sale", "12305", "--tile", "--max-depth", "0"])

        assert result.exit_code == 0
        assert "listings so far" not in result.stderr

    def test_search_with_price_filters(self, fake_http, search_success_body):
        fake_http.install("PUT", json_data=search_success_body)
        result = runner.invoke(
            app,
            ["search", "sale", "12305", "--min-price", "100000", "--max-price", "500000"],
        )
        assert result.exit_code == 0

    def test_search_with_bed_bath_filters(self, fake_http, search_success_body):
        fake_http.install("PUT", json_data=search_success_body)
        result = runner.invoke(
            app,
            ["search", "sale", "12305", "--min-beds", "2", "--max-beds", "4", "--min-bathrooms", "1"],
        )
        assert result.exit_code == 0

    def test_search_rent_with_room_option(self, fake_http, search_success_body):
        fake_http.install("PUT", json_data=search_success_body)
        result = runner.invoke(app, ["search", "rent", "12305", "--room"])
        assert result.exit_code == 0

    def test_search_rent_with_entire_place_option(self, fake_http, search_success_body):
        fake_http.install("PUT", json_data=search_success_body)
        result = runner.invoke(app, ["search", "rent", "12305", "--entire-place"])
        assert result.exit_code == 0

    def test_raw_format_rejects_csv(self, fake_http, search_success_body):
        fake_http.install("PUT", json_data=search_success_body)
        result = runner.invoke(app, ["search", "sale", "12305", "--raw", "--format", "csv"])
        assert result.exit_code == 2
        assert "--raw only supports JSON" in result.stderr


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
    """Test detail command routing and options."""

    def test_detail_home_by_id(self, fake_http, home_detail_body):
        fake_http.install("GET", content=home_detail_body)
        result = runner.invoke(app, ["detail", "home", "348914888"])
        assert result.exit_code == 0

    def test_detail_home_by_url(self, fake_http, home_detail_body):
        fake_http.install("GET", content=home_detail_body)
        result = runner.invoke(
            app,
            [
                "detail",
                "home",
                "https://www.zillow.com/homedetails/27-Washington-Ave/348914888_zpid/",
            ],
        )
        assert result.exit_code == 0

    def test_detail_apartment_by_id(self, fake_http, department_detail_body):
        fake_http.install("GET", content=department_detail_body)
        result = runner.invoke(app, ["detail", "apartment", "CgKZT4"])
        assert result.exit_code == 0

    def test_detail_apartment_by_url(self, fake_http, department_detail_body):
        fake_http.install("GET", content=department_detail_body)
        result = runner.invoke(
            app,
            [
                "detail",
                "apartment",
                "https://www.zillow.com/apartments/kissimmee-fl/the-nexus-at-overbrook/9DSWrh/",
            ],
        )
        assert result.exit_code == 0

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

        assert result.exit_code == 1
        assert "schema may have changed" in result.stderr
        assert "blocked the request" not in result.stderr

    @pytest.mark.parametrize(("option", "value"), [("--max-retries", "-1"), ("--timeout", "0")])
    def test_global_request_limits_reject_invalid_values(self, option, value):
        result = runner.invoke(app, [option, value, "detail", "home", "123"])
        assert result.exit_code == 2

    def test_detail_rejects_non_json_format(self, fake_http, home_detail_body):
        fake_http.install("GET", content=home_detail_body)
        result = runner.invoke(app, ["detail", "home", "348914888", "--format", "csv"])
        assert result.exit_code == 2
        assert "only support JSON" in result.stderr


class TestNormalizeCommand:
    """Test normalize command."""

    def test_normalize_from_file(self, tmp_path, fake_http, search_success_body):
        fake_http.install("PUT", json_data=search_success_body)
        input_file = tmp_path / "input.json"
        input_file.write_text(json.dumps(search_success_body))

        result = runner.invoke(app, ["normalize", str(input_file)])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert isinstance(data, list)

    def test_normalize_from_stdin(self, fake_http, search_success_body):
        fake_http.install("PUT", json_data=search_success_body)
        input_data = json.dumps(search_success_body)

        result = runner.invoke(app, ["normalize", "-"], input=input_data)
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert isinstance(data, list)

    def test_normalize_handles_mapresults(self, tmp_path):
        input_data = {"mapResults": [{"zpid": "1", "address": "test"}]}
        input_file = tmp_path / "input.json"
        input_file.write_text(json.dumps(input_data))

        result = runner.invoke(app, ["normalize", str(input_file)])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert len(data) == 1

    def test_normalize_handles_listresults(self, tmp_path):
        input_data = {"listResults": [{"zpid": "1", "address": "test"}]}
        input_file = tmp_path / "input.json"
        input_file.write_text(json.dumps(input_data))

        result = runner.invoke(app, ["normalize", str(input_file)])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert len(data) == 1

    def test_normalize_handles_array(self, tmp_path):
        input_data = [{"zpid": "1", "address": "test"}]
        input_file = tmp_path / "input.json"
        input_file.write_text(json.dumps(input_data))

        result = runner.invoke(app, ["normalize", str(input_file)])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert len(data) == 1

    def test_normalize_rejects_invalid_json(self, tmp_path):
        input_file = tmp_path / "invalid.json"
        input_file.write_text("not json")

        result = runner.invoke(app, ["normalize", str(input_file)])
        assert result.exit_code == 1
        assert "Invalid JSON" in result.stderr

    def test_normalize_rejects_missing_file(self):
        result = runner.invoke(app, ["normalize", "/nonexistent/file.json"])
        assert result.exit_code == 2
        assert "not found" in result.stderr


class TestOutputFormats:
    """Test output format options."""

    def test_json_output(self, fake_http, search_success_body):
        fake_http.install("PUT", json_data=search_success_body)
        result = runner.invoke(app, ["search", "sale", "12305", "--format", "json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert isinstance(data, list)

    def test_jsonl_output(self, fake_http, search_success_body):
        fake_http.install("PUT", json_data=search_success_body)
        result = runner.invoke(app, ["search", "sale", "12305", "--format", "jsonl"])
        assert result.exit_code == 0
        lines = result.stdout.strip().split("\n")
        assert len(lines) == 2
        for line in lines:
            json.loads(line)

    def test_csv_output(self, fake_http, search_success_body):
        fake_http.install("PUT", json_data=search_success_body)
        result = runner.invoke(app, ["search", "sale", "12305", "--format", "csv"])
        assert result.exit_code == 0
        lines = result.stdout.strip().split("\n")
        assert len(lines) == 3  # header + 2 rows

    def test_table_output(self, fake_http, search_success_body):
        fake_http.install("PUT", json_data=search_success_body)
        result = runner.invoke(app, ["search", "sale", "12305", "--format", "table"])
        assert result.exit_code == 0

    def test_table_uses_concise_columns(self, fake_http, search_success_body):
        fake_http.install("PUT", json_data=search_success_body)
        result = runner.invoke(app, ["search", "sale", "12305", "--format", "table"])
        assert result.exit_code == 0
        assert "address" in result.stdout
        assert "zpid" not in result.stdout  # concise display set, not all 21 fields

    def test_output_to_file(self, fake_http, search_success_body, tmp_path):
        fake_http.install("PUT", json_data=search_success_body)
        output_file = tmp_path / "output.json"
        result = runner.invoke(app, ["search", "sale", "12305", "--output", str(output_file)])
        assert result.exit_code == 0
        assert output_file.exists()
        data = json.loads(output_file.read_text())
        assert isinstance(data, list)

    def test_output_dash_means_stdout(self, fake_http, search_success_body):
        fake_http.install("PUT", json_data=search_success_body)
        result = runner.invoke(app, ["search", "sale", "12305", "--output", "-"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert isinstance(data, list)


class TestErrorHandling:
    """Test error handling and exit codes."""

    def test_zillow_blocked_error(self, fake_http):
        fake_http.install("PUT", status_code=403, text="blocked")
        result = runner.invoke(app, ["search", "sale", "12305"])
        assert result.exit_code == 1
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
        capped_response = {"cat1": {"searchResults": {"mapResults": [{"zpid": str(index)} for index in range(500)]}}}
        fake_http.install("PUT", json_data=capped_response)
        quiet_result = runner.invoke(app, ["--quiet", "search", "sale", "12305"])
        assert quiet_result.exit_code == 0
        assert "500 listings" not in quiet_result.stderr

        fake_http.install("PUT", json_data=capped_response)
        regular_result = runner.invoke(app, ["search", "sale", "12305"])
        assert regular_result.exit_code == 0
        assert "500 listings" in regular_result.stderr

    def test_verbose_mode(self, fake_http, search_success_body):
        fake_http.install("PUT", json_data=search_success_body)
        result = runner.invoke(app, ["--verbose", "search", "sale", "12305"])
        assert result.exit_code == 0


class TestGlobalOptions:
    """Test global options."""

    def test_proxy_option(self, fake_http, search_success_body):
        fake_http.install("PUT", json_data=search_success_body)
        result = runner.invoke(app, ["--proxy", "http://user:pass@proxy:8080", "search", "sale", "12305"])
        assert result.exit_code == 0

    def test_impersonate_option(self, fake_http, search_success_body):
        fake_http.install("PUT", json_data=search_success_body)
        result = runner.invoke(app, ["--impersonate", "chrome120", "search", "sale", "12305"])
        assert result.exit_code == 0

    def test_timeout_option(self, fake_http, search_success_body):
        fake_http.install("PUT", json_data=search_success_body)
        result = runner.invoke(app, ["--timeout", "60", "search", "sale", "12305"])
        assert result.exit_code == 0

    def test_max_retries_option(self, fake_http, search_success_body):
        fake_http.install("PUT", json_data=search_success_body)
        result = runner.invoke(app, ["--max-retries", "3", "search", "sale", "12305"])
        assert result.exit_code == 0

    def test_no_color_option(self, fake_http, search_success_body):
        fake_http.install("PUT", json_data=search_success_body)
        result = runner.invoke(app, ["--no-color", "search", "sale", "12305"])
        assert result.exit_code == 0


class TestEmptyResults:
    """Test handling of empty results."""

    def test_empty_search_results_success(self, fake_http):
        fake_http.install("PUT", json_data={"cat1": {"searchResults": {"mapResults": []}}})
        result = runner.invoke(app, ["search", "sale", "12305"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data == []


class TestEnvVarPrecedence:
    """Test CLI > environment > defaults precedence."""

    def test_cli_overrides_env(self, fake_http, search_success_body, monkeypatch):
        fake_http.install("PUT", json_data=search_success_body)
        monkeypatch.setenv("PYZILL_PROXY", "http://env:pass@proxy:8080")
        result = runner.invoke(app, ["--proxy", "http://cli:pass@proxy:9090", "search", "sale", "12305"])
        assert result.exit_code == 0


class TestProxyValidation:
    """The root callback rejects bad proxies before any request."""

    def test_invalid_scheme_is_usage_error_and_redacted(self):
        result = runner.invoke(app, ["--proxy", "ftp://user:SECRETPW@host:1", "search", "sale", "12305"])
        assert result.exit_code == 2
        assert "Invalid proxy" in result.stderr
        assert "SECRETPW" not in result.stderr

    def test_invalid_zillow_proxy_env_is_redacted(self, monkeypatch):
        monkeypatch.setenv("ZILLOW_PROXY", "ftp://user:SECRETPW@host:1")
        result = runner.invoke(app, ["search", "sale", "12305"])
        assert result.exit_code == 2
        assert "Invalid proxy" in result.stderr
        assert "SECRETPW" not in result.stderr


class TestRateLimit:
    """--rate-limit and PYZILL_RATE_LIMIT configure the shared limiter."""

    def test_rate_limit_option_applied(self, fake_http, search_success_body):
        from pyzill import get_min_interval

        fake_http.install("PUT", json_data=search_success_body)
        result = runner.invoke(app, ["--rate-limit", "2.5", "search", "sale", "12305"])
        assert result.exit_code == 0
        assert get_min_interval() == 2.5

    def test_rate_limit_env_var(self, fake_http, search_success_body, monkeypatch):
        from pyzill import get_min_interval

        fake_http.install("PUT", json_data=search_success_body)
        monkeypatch.setenv("PYZILL_RATE_LIMIT", "3.5")
        result = runner.invoke(app, ["search", "sale", "12305"])
        assert result.exit_code == 0
        assert get_min_interval() == 3.5


class TestNormalizeInputShapes:
    """Test normalize command input shape handling."""

    def test_normalize_single_dict(self, tmp_path):
        input_data = {"zpid": "1", "address": "test"}
        input_file = tmp_path / "input.json"
        input_file.write_text(json.dumps(input_data))

        result = runner.invoke(app, ["normalize", str(input_file)])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert isinstance(data, list)
        assert len(data) == 1

    def test_normalize_rejects_non_dict_non_list(self, tmp_path):
        input_file = tmp_path / "input.json"
        input_file.write_text(json.dumps("string"))

        result = runner.invoke(app, ["normalize", str(input_file)])
        assert result.exit_code == 2
        assert "must be a JSON object or array" in result.stderr


class TestCsvOutput:
    """Test CSV output specifics."""

    def test_csv_empty_list_is_success(self, fake_http):
        fake_http.install("PUT", json_data={"cat1": {"searchResults": {"mapResults": []}}})
        result = runner.invoke(app, ["search", "sale", "12305", "--format", "csv"])
        assert result.exit_code == 0
        assert result.stdout.strip() == ""  # empty file, not an error


class TestBrokenPipe:
    """Test broken pipe handling."""

    def test_broken_pipe_no_traceback(self, fake_http, search_success_body):
        fake_http.install("PUT", json_data=search_success_body)
        # Simulate broken pipe by redirecting to a process that exits immediately
        # This is hard to test in CliRunner, so we just ensure no crash
        result = runner.invoke(app, ["search", "sale", "12305"])
        assert result.exit_code == 0


class TestModuleInvocation:
    """Test python -m pyzill invocation."""

    def test_module_invocation_works(self):
        import subprocess
        from pathlib import Path

        repo_root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [sys.executable, "-m", "pyzill", "--help"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0
        assert "pyzill" in result.stdout


class TestGeocodeIntegration:
    """Searches without an explicit bbox geocode the query text."""

    def test_no_bbox_geocodes_query(self, fake_http, search_success_body):
        fake_http.install("PUT", json_data=search_success_body)
        result = runner.invoke(app, ["search", "sale", "Schenectady NY"])
        assert result.exit_code == 0
        assert _GEO_STUB.calls == ["Schenectady NY"]


def test_where_coercion_only_accepts_strict_numeric_literals():
    from pyzill.cli.query import coerce_value as _coerce_value

    assert _coerce_value("-$1,234.50") == -1234.5
    assert _coerce_value("nan") == "nan"
    assert _coerce_value("inf") == "inf"
    assert _coerce_value("1e3") == "1e3"


def test_db_import_accepts_kind(tmp_path):
    rows_file = tmp_path / "rent.json"
    rows_file.write_text(json.dumps([{"zpid": 1, "price": 2200}]))
    db_path = tmp_path / "rent.db"

    result = runner.invoke(app, ["db", "import", str(rows_file), "--kind", "rent", "--db", str(db_path)])

    assert result.exit_code == 0
    import sqlite3

    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT kind FROM listings WHERE zpid = 1").fetchone()[0] == "rent"
    assert conn.execute("SELECT kind FROM price_history WHERE zpid = 1").fetchone()[0] == "rent"
    conn.close()

    def test_explicit_bbox_skips_geocode(self, fake_http, search_success_body):
        fake_http.install("PUT", json_data=search_success_body)
        result = runner.invoke(
            app,
            ["search", "sale", "12305", "--north", "42.85", "--south", "42.78", "--east", "-73.9", "--west", "-74.0"],
        )
        assert result.exit_code == 0
        assert _GEO_STUB.calls == []

    def test_geocode_failure_is_clean_error(self):
        _GEO_STUB.error = ValueError("Geocoding 'nowhere' failed: DNS error. Pass --north/--south/--east/--west.")
        result = runner.invoke(app, ["search", "sale", "nowhere"])
        assert result.exit_code == 1
        assert "Geocoding 'nowhere' failed" in result.stderr
        assert "--east/--west" in result.stderr.replace("\n", "")

    def test_partial_bbox_still_usage_error(self):
        result = runner.invoke(app, ["search", "sale", "12305", "--north", "42.85"])
        assert result.exit_code == 2


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
        path = tmp_path / "saved.json"
        path.write_text(json.dumps(rows))
        return path

    def test_where_filter(self, saved):
        result = runner.invoke(app, ["query", str(saved), "--where", "price<=250000"])
        assert result.exit_code == 0
        rows = json.loads(result.stdout)
        assert {r["zpid"] for r in rows} == {1, 2, 4}

    def test_multiple_where_anded(self, saved):
        result = runner.invoke(app, ["query", str(saved), "--where", "price<=250000", "--where", "beds>=3"])
        assert result.exit_code == 0
        rows = json.loads(result.stdout)
        assert {r["zpid"] for r in rows} == {2, 4}

    def test_where_string_equality_with_numeric_literal(self, saved):
        result = runner.invoke(app, ["query", str(saved), "--where", "zipcode==12304"])
        assert result.exit_code == 0
        rows = json.loads(result.stdout)
        assert [r["zpid"] for r in rows] == [1]

    def test_where_preserves_leading_zero_zipcodes(self, tmp_path):
        path = tmp_path / "zipcodes.json"
        path.write_text(json.dumps([{"zpid": 1, "zipcode": "02134"}]))

        for expression in ("zipcode==02134", 'zipcode=="02134"'):
            result = runner.invoke(app, ["query", str(path), "--where", expression])
            assert result.exit_code == 0
            assert [row["zpid"] for row in json.loads(result.stdout)] == [1]

    def test_db_query_preserves_leading_zero_zipcodes_during_write(self, tmp_path):
        from pyzill.db import connect, upsert_listings

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
        result = runner.invoke(app, ["query", str(saved), "--where", "city~schen"])
        assert result.exit_code == 0
        rows = json.loads(result.stdout)
        assert len(rows) == 3

    def test_where_money_string_value(self, saved):
        result = runner.invoke(app, ["query", str(saved), "--where", "price>=$200,000"])
        assert result.exit_code == 0
        rows = json.loads(result.stdout)
        assert {r["zpid"] for r in rows} == {2, 3, 4}

    def test_sort_asc_and_desc(self, saved):
        result = runner.invoke(app, ["query", str(saved), "--sort", "price:desc"])
        assert result.exit_code == 0
        rows = json.loads(result.stdout)
        assert [r["price"] for r in rows] == [350000, 250000, 200000, 150000]

    def test_sort_missing_values_last(self, saved):
        result = runner.invoke(app, ["query", str(saved), "--sort", "area"])
        assert result.exit_code == 0
        rows = json.loads(result.stdout)
        assert rows[-1]["zpid"] == 4  # area=None goes last

    def test_ppsf_computed_field(self, saved):
        result = runner.invoke(app, ["query", str(saved), "--where", "ppsf==150", "--cols", "zpid,ppsf"])
        assert result.exit_code == 0
        rows = json.loads(result.stdout)
        assert rows == [{"zpid": 1, "ppsf": 150.0}]

    def test_ppsf_missing_area_excluded(self, saved):
        result = runner.invoke(app, ["query", str(saved), "--where", "ppsf>=50"])
        assert result.exit_code == 0
        rows = json.loads(result.stdout)
        assert 4 not in {r["zpid"] for r in rows}

    def test_cols_selection(self, saved):
        result = runner.invoke(app, ["query", str(saved), "--cols", "zpid,price"])
        assert result.exit_code == 0
        rows = json.loads(result.stdout)
        assert list(rows[0]) == ["zpid", "price"]

    def test_limit(self, saved):
        result = runner.invoke(app, ["query", str(saved), "--sort", "price:asc", "--limit", "2"])
        assert result.exit_code == 0
        rows = json.loads(result.stdout)
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
        data = saved.read_text()
        result = runner.invoke(app, ["query", "-"], input=data)
        assert result.exit_code == 0
        assert len(json.loads(result.stdout)) == 4

    def test_unknown_where_field(self, saved):
        result = runner.invoke(app, ["query", str(saved), "--where", "sqft>=1000"])
        assert result.exit_code == 2
        assert "Unknown field" in result.stderr

    def test_bad_where_syntax(self, saved):
        result = runner.invoke(app, ["query", str(saved), "--where", "just a word"])
        assert result.exit_code == 2

    def test_unknown_sort_field(self, saved):
        result = runner.invoke(app, ["query", str(saved), "--sort", "nope"])
        assert result.exit_code == 2

    def test_unknown_cols(self, saved):
        result = runner.invoke(app, ["query", str(saved), "--cols", "zpid,nope"])
        assert result.exit_code == 2

    def test_limit_zero_is_usage_error(self, saved):
        result = runner.invoke(app, ["query", str(saved), "--limit", "0"])
        assert result.exit_code == 2

    def test_non_list_input(self, tmp_path):
        path = tmp_path / "obj.json"
        path.write_text(json.dumps({"zpid": 1}))
        result = runner.invoke(app, ["query", str(path)])
        assert result.exit_code == 2
        assert "JSON array" in result.stderr

    def test_missing_file(self):
        result = runner.invoke(app, ["query", "/nonexistent/file.json"])
        assert result.exit_code == 2

    def test_empty_result_is_success(self, saved):
        result = runner.invoke(app, ["query", str(saved), "--where", "price>999999"])
        assert result.exit_code == 0
        assert json.loads(result.stdout) == []


class TestDetailExploration:
    """--keys and --path on detail commands."""

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


class TestDatabaseCommands:
    """Test db sub-app commands."""

    def test_db_sync_creates_database(self, fake_http, search_success_body, tmp_path):
        fake_http.install("PUT", json_data=search_success_body)
        db_path = tmp_path / "test.db"
        result = runner.invoke(app, ["db", "sync", "sale", "12305", "--db", str(db_path)])
        assert result.exit_code == 0
        assert db_path.exists()
        assert "Synced 2 listings: 2 new, 0 updated (0 price changes), 0 skipped" in result.stderr

    def test_db_sync_empty_results_warns(self, fake_http, tmp_path):
        fake_http.install("PUT", json_data={"cat1": {"searchResults": {"mapResults": []}}})
        db_path = tmp_path / "test.db"
        result = runner.invoke(app, ["db", "sync", "sale", "12305", "--db", str(db_path)])
        assert result.exit_code == 0
        assert "No listings found" in result.stderr
        assert not db_path.exists()

    def test_db_sync_re_sync_updates_last_seen(self, fake_http, search_success_body, tmp_path):
        fake_http.install("PUT", json_data=search_success_body)
        db_path = tmp_path / "test.db"

        # First sync
        result1 = runner.invoke(app, ["db", "sync", "sale", "12305", "--db", str(db_path)])
        assert result1.exit_code == 0
        assert "new" in result1.stderr

        # Re-sync (same data)
        result2 = runner.invoke(app, ["db", "sync", "sale", "12305", "--db", str(db_path)])
        assert result2.exit_code == 0
        assert "updated" in result2.stderr

    def test_db_sync_price_change_detection(self, fake_http, tmp_path):
        # First sync with price 250000
        body1 = {"cat1": {"searchResults": {"mapResults": [{"zpid": "123", "price": 250000, "address": "test"}]}}}
        fake_http.install("PUT", json_data=body1)
        db_path = tmp_path / "test.db"

        result1 = runner.invoke(app, ["db", "sync", "sale", "12305", "--db", str(db_path)])
        assert result1.exit_code == 0

        # Re-sync with price 260000 - use --fetched-at to ensure different timestamp
        body2 = {"cat1": {"searchResults": {"mapResults": [{"zpid": "123", "price": 260000, "address": "test"}]}}}
        fake_http.install("PUT", json_data=body2)
        # Import instead of sync to control timestamp
        rows = [{"zpid": "123", "price": 260000, "address": "test"}]
        input_file = tmp_path / "input.json"
        input_file.write_text(json.dumps(rows))
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

    def test_db_import_raw_response(self, tmp_path):
        body = {"cat1": {"searchResults": {"mapResults": [{"zpid": "123", "price": 250000, "address": "test"}]}}}
        input_file = tmp_path / "input.json"
        input_file.write_text(json.dumps(body))
        db_path = tmp_path / "test.db"

        result = runner.invoke(app, ["db", "import", str(input_file), "--db", str(db_path)])
        assert result.exit_code == 0
        assert "Imported 1 listings: 1 new, 0 updated (0 price changes), 0 skipped" in result.stderr
        assert db_path.exists()

        # The nested response envelope must yield its mapResults rows, not be
        # treated as a single bogus listing.
        query = runner.invoke(app, ["db", "query", "--db", str(db_path)])
        assert query.exit_code == 0
        data = json.loads(query.stdout)
        assert len(data) == 1
        assert data[0]["zpid"] == 123
        assert data[0]["price"] == 250000

    def test_db_import_normalizes_raw_rows(self, tmp_path):
        body = {
            "cat1": {
                "searchResults": {
                    "mapResults": [
                        {
                            "zpid": "32463997",
                            "price": "$459,000",
                            "address": "1230 State St, Schenectady, NY 12305",
                            "detailUrl": "/homes/1230-State-St.html",
                            "imgSrc": "https://img.example/x.jpg",
                            "statusText": "House for sale",
                        }
                    ]
                }
            }
        }
        input_file = tmp_path / "input.json"
        input_file.write_text(json.dumps(body))
        db_path = tmp_path / "test.db"

        result = runner.invoke(app, ["db", "import", str(input_file), "--db", str(db_path)])
        assert result.exit_code == 0

        query = runner.invoke(app, ["db", "query", "--db", str(db_path)])
        assert query.exit_code == 0
        data = json.loads(query.stdout)
        assert len(data) == 1
        row = data[0]
        assert row["price"] == 459000
        assert row["detail_url"] == "https://www.zillow.com/homes/1230-State-St.html"
        assert row["image_url"] == "https://img.example/x.jpg"
        assert row["status"] == "House for sale"

    def test_db_import_preserves_normalized_fields(self, tmp_path):
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
        input_file = tmp_path / "input.json"
        input_file.write_text(json.dumps(rows))
        db_path = tmp_path / "test.db"

        result = runner.invoke(app, ["db", "import", str(input_file), "--db", str(db_path)])
        assert result.exit_code == 0
        assert "Imported 1 listings: 1 new" in result.stderr

        query = runner.invoke(app, ["db", "query", "--db", str(db_path)])
        data = json.loads(query.stdout)
        assert data[0]["city"] == "Schenectady"

    def test_db_import_same_timestamp_price_change(self, tmp_path):
        db_path = tmp_path / "test.db"
        first = tmp_path / "first.json"
        second = tmp_path / "second.json"
        first.write_text(json.dumps([{"zpid": 123, "price": 250000}]))
        second.write_text(json.dumps([{"zpid": 123, "price": 260000}]))

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
        input_file = tmp_path / "input.json"
        input_file.write_text(json.dumps(rows))
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
        input_file = tmp_path / "input.json"
        input_file.write_text(json.dumps(rows))
        db_path = tmp_path / "test.db"

        result = runner.invoke(
            app, ["db", "import", str(input_file), "--fetched-at", "2026-10-01T12:00:00Z", "--db", str(db_path)]
        )
        assert result.exit_code == 0

    def test_db_import_invalid_timestamp(self, tmp_path):
        rows = [{"zpid": "123", "price": 250000, "address": "test"}]
        input_file = tmp_path / "input.json"
        input_file.write_text(json.dumps(rows))
        db_path = tmp_path / "test.db"

        result = runner.invoke(app, ["db", "import", str(input_file), "--fetched-at", "invalid", "--db", str(db_path)])
        assert result.exit_code == 2
        assert "Invalid --fetched-at timestamp" in result.stderr

    def test_db_query_missing_database(self, tmp_path):
        db_path = tmp_path / "nonexistent.db"
        result = runner.invoke(app, ["db", "query", "--db", str(db_path)])
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

    def test_db_query_binds_filter_values(self, tmp_path):
        records = [{"zpid": 123, "city": "Albany' OR 1=1 --"}, {"zpid": 456, "city": "Troy"}]
        input_file = tmp_path / "rows.json"
        input_file.write_text(json.dumps(records))
        db_path = tmp_path / "query.db"
        imported = runner.invoke(app, ["db", "import", str(input_file), "--db", str(db_path)])
        assert imported.exit_code == 0

        result = runner.invoke(app, ["db", "query", "--where", "city==Albany' OR 1=1 --", "--db", str(db_path)])

        assert result.exit_code == 0
        assert [row["zpid"] for row in json.loads(result.stdout)] == [123]

    def test_db_query_with_timestamp_filter(self, tmp_path):
        # Import data with specific timestamp
        rows = [
            {"zpid": "123", "price": 250000, "address": "test", "city": "Schenectady"},
            {"zpid": "456", "price": 300000, "address": "test2", "city": "Albany"},
        ]
        input_file = tmp_path / "input.json"
        input_file.write_text(json.dumps(rows))
        db_path = tmp_path / "test.db"

        runner.invoke(
            app, ["db", "import", str(input_file), "--fetched-at", "2026-10-01T12:00:00Z", "--db", str(db_path)]
        )

        # Query with timestamp filter
        result = runner.invoke(app, ["db", "query", "--where", "last_seen_utc>=2026-10-01", "--db", str(db_path)])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert len(data) == 2

    def test_db_query_reuses_where_sort_cols_limit(self, tmp_path):
        rows = [
            {"zpid": "123", "price": 250000, "address": "123 Main St", "city": "Schenectady"},
            {"zpid": "456", "price": 300000, "address": "456 Oak Ave", "city": "Albany"},
        ]
        input_file = tmp_path / "input.json"
        input_file.write_text(json.dumps(rows))
        db_path = tmp_path / "test.db"

        runner.invoke(app, ["db", "import", str(input_file), "--db", str(db_path)])

        # Test --where
        result = runner.invoke(app, ["db", "query", "--where", "city==Schenectady", "--db", str(db_path)])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert len(data) == 1

        # Test --sort
        result = runner.invoke(app, ["db", "query", "--sort", "price:desc", "--db", str(db_path)])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data[0]["price"] == 300000

        # Test --cols
        result = runner.invoke(app, ["db", "query", "--cols", "zpid,price", "--db", str(db_path)])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert list(data[0].keys()) == ["zpid", "price"]

        # Test --limit
        result = runner.invoke(app, ["db", "query", "--limit", "1", "--db", str(db_path)])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert len(data) == 1

    def test_db_history_missing_database(self, tmp_path):
        db_path = tmp_path / "nonexistent.db"
        result = runner.invoke(app, ["db", "history", "123", "--db", str(db_path)])
        assert result.exit_code == 1
        assert "Database not found" in result.stderr

    def test_db_history_shows_price_timeline(self, tmp_path):
        rows = [{"zpid": "123", "price": 250000, "address": "test"}]
        input_file = tmp_path / "input.json"
        input_file.write_text(json.dumps(rows))
        db_path = tmp_path / "test.db"

        # Import with price 250000
        runner.invoke(
            app, ["db", "import", str(input_file), "--fetched-at", "2026-10-01T12:00:00Z", "--db", str(db_path)]
        )

        # Update with price 260000
        rows[0]["price"] = 260000
        input_file.write_text(json.dumps(rows))
        runner.invoke(
            app, ["db", "import", str(input_file), "--fetched-at", "2026-10-02T12:00:00Z", "--db", str(db_path)]
        )

        # Check history
        result = runner.invoke(app, ["db", "history", "123", "--db", str(db_path)])
        assert result.exit_code == 0
        history = json.loads(result.stdout)
        assert len(history) == 2
        assert history[0]["price"] == 250000
        assert history[1]["price"] == 260000

    def test_db_history_empty_for_unknown_zpid(self, tmp_path):
        rows = [{"zpid": "123", "price": 250000, "address": "test"}]
        input_file = tmp_path / "input.json"
        input_file.write_text(json.dumps(rows))
        db_path = tmp_path / "test.db"

        runner.invoke(app, ["db", "import", str(input_file), "--db", str(db_path)])

        result = runner.invoke(app, ["db", "history", "999", "--db", str(db_path)])
        assert result.exit_code == 0
        assert "No price history found" in result.stderr

    def test_db_stats_missing_database(self, tmp_path):
        db_path = tmp_path / "nonexistent.db"
        result = runner.invoke(app, ["db", "stats", "--db", str(db_path)])
        assert result.exit_code == 1
        assert "Database not found" in result.stderr

    def test_db_stats_shows_statistics(self, tmp_path):
        rows = [
            {"zpid": "123", "price": 250000, "address": "test"},
            {"zpid": "456", "price": 300000, "address": "test2"},
        ]
        input_file = tmp_path / "input.json"
        input_file.write_text(json.dumps(rows))
        db_path = tmp_path / "test.db"

        runner.invoke(
            app, ["db", "import", str(input_file), "--fetched-at", "2026-10-01T12:00:00Z", "--db", str(db_path)]
        )

        result = runner.invoke(app, ["db", "stats", "--db", str(db_path)])
        assert result.exit_code == 0
        assert "Total listings: 2" in result.stdout
        assert str(db_path) in result.stdout

    def test_db_env_var_path(self, tmp_path, monkeypatch):
        rows = [{"zpid": "123", "price": 250000, "address": "test"}]
        input_file = tmp_path / "input.json"
        input_file.write_text(json.dumps(rows))
        db_path = tmp_path / "test.db"

        monkeypatch.setenv("PYZILL_DB", str(db_path))
        result = runner.invoke(app, ["db", "import", str(input_file)])
        assert result.exit_code == 0
        assert db_path.exists()

    def test_db_default_path(self, tmp_path, monkeypatch):
        rows = [{"zpid": "123", "price": 250000, "address": "test"}]
        input_file = tmp_path / "input.json"
        input_file.write_text(json.dumps(rows))

        # Change to tmp_path so default db is created there
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["db", "import", str(input_file)])
        assert result.exit_code == 0
        assert (tmp_path / "pyzill.db").exists()
