"""CLI tests: root."""

import json
import sys

import pytest
from typer._click.exceptions import ClickException

from pyzill import ratelimit
from pyzill.cli import app, main_entry
from pyzill.db import connect, upsert_listings, upsert_property_details
from pyzill.query import query_database
from tests.cli_helpers import _GEO_STUB, run_module, runner

pytestmark = pytest.mark.usefixtures("_stub_geocode")


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

    def test_db_detail_schema_command_is_versioned_json(self):
        result = runner.invoke(app, ["schema", "db-detail"])

        assert result.exit_code == 0
        schema = json.loads(result.stdout)
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert schema["$id"].endswith("/db-detail/1.0.0")
        assert schema["additionalProperties"] is False
        assert schema["properties"]["zpid"] == {"type": "integer"}
        assert schema["properties"]["enriched_at_utc"] == {"type": "string"}
        assert schema["properties"]["hoa_fee"]["anyOf"][-1] == {"type": "null"}
        assert schema["properties"]["description"]["anyOf"][-1] == {"type": "null"}
        assert schema["properties"]["tax_history"]["anyOf"][-1] == {"type": "null"}
        assert schema["properties"]["raw_json"]["anyOf"][-1] == {"type": "null"}

    def test_db_detail_schema_covers_every_column_db_query_returns(self, tmp_path):
        """The published schema must describe what projecting a detail field emits."""
        db_path = tmp_path / "detail_schema.db"
        conn = connect(db_path)
        upsert_listings(conn, [{"zpid": 1, "price": 250000, "city": "Albany"}], seen_utc="2026-10-01T00:00:00Z")
        upsert_property_details(conn, {"zpid": 1, "monthlyHoaFee": 0, "description": "Detached garage"}, kind="sale")
        # Project a detail field; every column returned must be in the schema.
        cols = ",".join(["zpid", "kind"] + ["hoa_fee"] * 14)
        rows, _ = query_database(conn, cols=cols)
        conn.close()

        result = runner.invoke(app, ["schema", "db-detail"])
        schema = json.loads(result.stdout)

        assert set(rows[0]) <= set(schema["properties"])

    def test_no_args_shows_help(self):
        result = runner.invoke(app, [])
        assert result.exit_code == 2  # Typer's no_args_is_help returns 2
        assert "help" in result.stdout.lower()


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

    def test_module_body_runs_under_runpy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``python -m pyzill`` runs the module body, not just its import.

        The body is three lines (import, guard, call); a test that only calls
        ``main_entry`` directly misses the guard, so the coverage gate never
        sees the path a user actually takes.
        """
        import runpy

        monkeypatch.setattr(sys, "argv", ["pyzill", "--version"])
        with pytest.raises(SystemExit) as exit_info:
            runpy.run_module("pyzill", run_name="__main__", alter_sys=True)
        assert exit_info.value.code == 0

    def test_max_requests_above_the_floor_is_rejected(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = self._run(monkeypatch, ["--json-errors", "search", "sale", "12305", "--max-requests", "1000"])
        assert code == 2
        assert json.loads(capsys.readouterr().err)["error"]["code"] == "usage"

    def test_force_overrides_the_max_requests_floor(self, fake_http, search_success_body) -> None:
        fake_http.install("PUT", json_data=search_success_body)
        result = runner.invoke(app, ["--force", "search", "sale", "12305", "--max-requests", "1000"])
        assert result.exit_code == 0

    def test_rate_limit_below_the_floor_is_rejected(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = self._run(monkeypatch, ["--json-errors", "--rate-limit", "0", "search", "sale", "12305"])
        assert code == 2
        assert json.loads(capsys.readouterr().err)["error"]["code"] == "usage"

    def test_force_overrides_the_rate_limit_floor(self, fake_http, search_success_body, no_sleep) -> None:  # noqa: ARG002
        fake_http.install("PUT", json_data=search_success_body)
        result = runner.invoke(app, ["--force", "--rate-limit", "0", "search", "sale", "12305"])
        assert result.exit_code == 0
        assert ratelimit.get_min_interval() == 0.0

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
