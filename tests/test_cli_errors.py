"""CLI tests: errors."""

import json
import os
import sys

import pytest
import typer

from pyzill.cli import app, console
from pyzill.cli.console import _redact, stderr_line
from tests.cli_helpers import capped_body, runner

pytestmark = pytest.mark.usefixtures("_stub_geocode")


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
