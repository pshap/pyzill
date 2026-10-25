"""Shared CLI context, diagnostics, and output plumbing."""

import json
import os
import sys
from collections.abc import Callable, Iterable, Iterator, Sequence
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

from pyzill.cli.output import (
    LISTING_TABLE_COLUMNS,
    OutputError,
    OutputFormat,
    render_csv,
    render_json,
    render_jsonl,
    render_table,
    render_table_text,
    require_list,
)
from pyzill.cli.safety import (
    clear_block_state,
    cooldown_until,
    is_in_cooldown,
    record_block,
    seconds_until_unblock,
)
from pyzill.config import DEFAULT_IMPERSONATE, DEFAULT_MAX_RETRIES, DEFAULT_TIMEOUT
from pyzill.exceptions import (
    ZillowError,
)
from pyzill.proxy import redact_credentials, resolve_proxy
from pyzill.workflows import Notice


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
    rate_limit: float | None = None

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


def context_installed() -> bool:
    """True when a CLI command has installed its context.

    The root callback installs it, so this is False only when the error
    happened before the callback ran -- while the root's own parameters
    were being parsed, for instance.
    """
    return _active_context.get() is not None


def context() -> Context:
    """Return the active context, or a default one outside a command."""
    return _active_context.get() or Context()


_USAGE_EXIT_CODE = 2


def _redact(message: str) -> str:
    """Strip credentials from any URLs embedded in a message.

    Delegates to the package-wide redaction helper so the CLI and the
    library mask credentials with exactly the same rules.
    """
    return redact_credentials(message)


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
    retry_after: str | None = None,
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
        if retry_after is not None:
            payload["error"]["retry_after"] = retry_after
        typer.echo(json.dumps(payload, ensure_ascii=False), err=True)
    else:
        stderr_line("red", "Error:", message)
    raise typer.Exit(code)


def enforce_cooldown() -> None:
    """Refuse a network command while a recorded Zillow block is cooling down.

    Offline commands never call this, and ``--plan`` checks it only after it
    has decided not to make a request, so neither is blocked by a stale
    cooldown.
    """
    if not is_in_cooldown():
        return
    remaining = seconds_until_unblock()
    retry_after = cooldown_until()
    error(
        f"Zillow blocked us recently; retry in {remaining} second(s)"
        + (f" (after {retry_after})" if retry_after else "")
        + " (set PYZILL_BLOCK_STATE=off to disable block memory)",
        code=3,
        error_code="cooling_down",
        retryable=True,
        retry_after=retry_after,
    )


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


def report_notices(notices: Iterable[Notice]) -> None:
    """Print the workflow notices as warnings.

    The workflow layer reports conditions as structured notices; the wording
    belongs to the CLI, so the messages are rendered here.
    """
    for notice in notices:
        values = notice.values
        if notice.code == "synthetic_viewport":
            warn(
                "Nominatim returned a point-like viewport; pyzill expanded it synthetically, "
                "which may not cover the full area"
            )
        elif notice.code == "tile_capped":
            warn(
                f"A tile still returned {values['cap']} listings at --max-depth {values['max_depth']}; "
                "results may be truncated"
            )
        elif notice.code == "result_cap":
            warn(
                f"Zillow returned {values['cap']} listings, its per-search cap; "
                "use --tile to subdivide the bounding box"
            )
        elif notice.code == "budget_exhausted":
            warn(
                f"request budget of {values['max_requests']} exhausted; returning partial results "
                "(raise --max-requests or lower --max-depth for more)"
            )
        elif notice.code == "suspect_empty":
            warn(
                "Zillow returned no listings for a search that previously matched; "
                "recording this run as truncated (use --confirm-empty to accept a verified empty result)"
            )


@contextmanager
def network_errors(failure: str) -> Iterator[None]:
    """Translate known library failures to stable CLI errors.

    A successful request clears any remembered block: the block cooldown has
    done its job, and the next command should be allowed to run.
    """
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
        if e.code == "blocked":
            # Remember the block so an agent looping on exit code 3 does not
            # keep hitting Zillow. The state file is opt-in via
            # PYZILL_BLOCK_STATE; when unset this is a no-op.
            record_block(e.code, retryable=e.retryable, status_code=getattr(e, "status_code", None))
        error(
            messages.get(e.code, f"{failure}: {e}"),
            code=exit_codes.get(e.code, 1),
            error_code=e.code,
            retryable=e.retryable,
            status_code=getattr(e, "status_code", None),
        )
    except typer.Exit:
        # The CLI raises this to end a command with its own exit code. It is
        # an Exception, so the catch-all below would report it as an
        # unexpected failure and replace the code the CLI chose.
        raise
    except Exception as e:
        if context().verbose:
            raise
        error(f"Unexpected error during {failure}: {e}", code=1)
    else:
        # Every request the block guarded has succeeded; the remembered block
        # is stale now.
        clear_block_state()


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
    """Read JSON from a file path or stdin ('-'), failing cleanly on bad input.

    A file that is not valid JSON is a usage error, the same as a file that
    is not there: the input is the caller's, not the tool's.
    """
    text = read_input(source)
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        error(f"Invalid JSON input: {e}", code=_USAGE_EXIT_CODE)


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
    default_columns: Sequence[str] | None = LISTING_TABLE_COLUMNS,
    title: str = "Listings",
    empty_message: str = "No listings found",
) -> None:
    """Render the data and write it out, deciding what to report on the way.

    The renderers in :mod:`pyzill.cli.output` are pure; this is where the
    CLI decides to emit, warn, or fail.

    ``columns`` is the caller's own choice of columns. ``default_columns``
    is what a table shows when the caller chose none: the listing columns
    for a listing, and None for rows that are not listings, whose own
    columns are shown instead.
    """
    if output == "-":
        output = None
    if output_format is OutputFormat.json:
        emit(_render(render_json, data), output)
        return
    if output_format is OutputFormat.jsonl:
        emit(_render(render_jsonl, data), output)
        return
    rows = _render(require_list, data, output_format)
    if not rows:
        warn(empty_message)
    if output_format is OutputFormat.csv:
        emit(render_csv(rows, columns), output)
        return
    if not rows:
        return
    table = render_table(rows, columns, default_columns, title)
    if output:
        emit(render_table_text(table), output)
        return
    context().stdout_console.print(table)


def _render[T](renderer: Callable[..., T], *args: Any) -> T:
    """Run a renderer, turning its failure into a CLI error."""
    try:
        return renderer(*args)
    except OutputError as exc:
        error(str(exc), code=exc.exit_code)
