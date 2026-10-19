"""Shared CLI context, diagnostics, and output plumbing."""

import json
import os
import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import cached_property
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, NoReturn

import typer
from rich.console import Console
from rich.markup import escape

from pyzill import resolve_proxy
from pyzill.cli.output import OutputFormat
from pyzill.cli.output import write_output as _render_output
from pyzill.config import DEFAULT_IMPERSONATE, DEFAULT_MAX_RETRIES, DEFAULT_TIMEOUT
from pyzill.exceptions import (
    ZillowError,
)


@dataclass
class Context:
    """Shared context for CLI commands."""

    quiet: bool = False
    verbose: bool = False
    no_color: bool = False
    proxy: str | None = None
    impersonate: str = DEFAULT_IMPERSONATE
    timeout: float = DEFAULT_TIMEOUT
    max_retries: int = DEFAULT_MAX_RETRIES
    json_errors: bool = False

    @cached_property
    def stderr_console(self) -> Console:
        """Console for diagnostics and progress.

        ``soft_wrap`` keeps a single diagnostic from being hard-wrapped at the
        terminal width, so long URLs and error messages stay on one line and
        remain copy-pasteable.
        """
        return Console(stderr=True, no_color=self.no_color, force_terminal=False, soft_wrap=True)

    @cached_property
    def stdout_console(self) -> Console:
        """Console for human-readable output (not machine formats)."""
        return Console(stderr=False, no_color=self.no_color, force_terminal=False)

    @property
    def http_kwargs(self) -> dict[str, Any]:
        """Keyword arguments every pyzill network call accepts."""
        try:
            proxy_url = resolve_proxy(self.proxy)
        except ValueError as e:
            # Backstop; the root callback validates the proxy before any
            # command runs. Keeps a bad ZILLOW_PROXY from becoming a traceback.
            error(f"Invalid proxy: {e}", code=2)
        return {
            "proxy_url": proxy_url,
            "impersonate": self.impersonate,
            "timeout": self.timeout,
            "max_retries": self.max_retries,
        }


_active_context: ContextVar[Context | None] = ContextVar("pyzill_cli_context", default=None)


def set_context(context: Context) -> None:
    """Install the CLI context for the current invocation."""
    _active_context.set(context)


def context() -> Context:
    """Return the active context, or a default one outside a command."""
    return _active_context.get() or Context()


# user:password@ inside a URL (proxy credentials, etc.)
_USERINFO_RE = re.compile(
    r"(?<=://)[^/@\s]+@"  # userinfo immediately after a scheme separator
    r"|(?<![\w./])[^/@\s:]+:[^/@\s]*@"  # scheme-less user:pass@host (colon required)
)
_USAGE_EXIT_CODE = 2


def _redact(message: str) -> str:
    """Strip credentials from any URLs embedded in a message."""
    return _USERINFO_RE.sub("***@", message)


def stderr_line(style: str, label: str, message: str) -> None:
    """Print one diagnostic line to stderr, redacting credentials."""
    context().stderr_console.print(f"[{style}]{label}[/] {escape(_redact(message))}")


def error(
    message: str,
    code: int = 1,
    *,
    error_code: str | None = None,
    retryable: bool = False,
    status_code: int | None = None,
) -> NoReturn:
    """Print a redacted diagnostic to stderr and exit."""
    message = _redact(message)
    if context().json_errors:
        payload: dict[str, Any] = {
            "error": {
                "code": error_code or ("usage" if code == _USAGE_EXIT_CODE else "other"),
                "message": message,
                "retryable": retryable,
            }
        }
        if status_code is not None:
            payload["error"]["status_code"] = status_code
        typer.echo(json.dumps(payload, ensure_ascii=False), err=True)
    else:
        stderr_line("red", "Error:", message)
    raise typer.Exit(code)


def warn(message: str) -> None:
    """Print warning to stderr."""
    if not context().quiet:
        stderr_line("yellow", "Warning:", message)


def progress(label: str, message: str) -> None:
    """Print a progress line to stderr unless --quiet is active."""
    if not context().quiet:
        stderr_line("dim", label, message)


def debug(message: str) -> None:
    """Print debug message to stderr."""
    if context().verbose:
        stderr_line("dim", "Debug:", message)


@contextmanager
def network_errors(failure: str) -> Iterator[None]:
    """Translate known library failures to stable CLI errors."""
    try:
        yield
    except ZillowError as e:
        messages = {
            "server_error": f"Zillow server error after retries: {e}",
            "network": f"{failure}: {e}",
            "not_found": f"Zillow listing was not found: {e}",
            "schema_drift": f"Could not parse Zillow's response; its page schema may have changed: {e}",
            "blocked": f"Zillow blocked the request: {e}",
        }
        exit_codes = {"blocked": 3, "not_found": 4, "schema_drift": 5, "server_error": 6, "network": 6}
        error(
            messages.get(e.code, f"{failure}: {e}"),
            code=exit_codes.get(e.code, 1),
            error_code=e.code,
            retryable=e.retryable,
            status_code=getattr(e, "status_code", None),
        )
    except Exception as e:
        if context().verbose:
            raise
        error(f"Unexpected error during {failure}: {e}", code=1)


def version_callback(value: bool) -> None:
    """Show version and exit."""
    if value:
        try:
            ver = version("pyzill")
        except PackageNotFoundError:
            ver = "unknown"
        typer.echo(f"pyzill {ver}")
        raise typer.Exit


def read_input(source: str) -> str:
    """Read text from a file path, or from stdin when source is '-'."""
    if source == "-":
        try:
            return typer.get_text_stream("stdin").read()
        except (OSError, UnicodeDecodeError) as e:
            error(f"Failed to read stdin: {e}", code=1)

    path = Path(source)
    if not path.exists():
        error(f"Input file not found: {source}", code=2)
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        error(f"Failed to read {source}: {e}", code=1)


def read_json(source: str) -> Any:
    """Read JSON from a file path or stdin ('-'), failing cleanly on bad input."""
    text = read_input(source)
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        error(f"Invalid JSON input: {e}", code=1)


def emit(content: str, output: str | None) -> None:
    """Write text to a file, or to stdout when no file is given."""
    if content and not content.endswith("\n"):
        content += "\n"

    if output:
        try:
            with Path(output).open("w", encoding="utf-8", newline="") as fh:
                fh.write(content)
        except OSError as e:
            error(f"Failed to write to {output}: {e}", code=1)
        return

    try:
        typer.echo(content, nl=False)
        sys.stdout.flush()
    except BrokenPipeError:
        # e.g. `pyzill ... | head`. Point stdout at devnull so Python doesn't
        # complain again while shutting down.
        devnull = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(devnull, sys.stdout.fileno())
        finally:
            os.close(devnull)
        raise typer.Exit(0) from None


def write_output(
    data: Any,
    output_format: OutputFormat,
    output: str | None = None,
    columns: list[str] | None = None,
    use_db_columns: bool = False,
) -> None:
    """Write output using the standalone renderer and CLI callbacks."""
    _render_output(
        data,
        output_format,
        output,
        columns,
        use_db_columns,
        emit=emit,
        error=error,
        warn=warn,
        console=context().stdout_console,
    )
