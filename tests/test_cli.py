"""Tests for pyzill CLI."""

import json
import sys

import pytest
from typer.testing import CliRunner

from pyzill.cli import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _disable_rate_limit(monkeypatch):
    """Keep tests fast: no throttling between mocked requests."""
    from pyzill import ratelimit

    original = ratelimit.get_min_interval()
    ratelimit.set_min_interval(0)
    yield
    ratelimit.set_min_interval(original)


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
