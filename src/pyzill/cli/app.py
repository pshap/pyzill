"""CLI for pyzill Zillow scraper.

Uses Typer for command-line interface and Rich for terminal output.
"""

import os
import sys
from typing import Annotated

import typer
from typer._click.exceptions import ClickException

from pyzill.cli.console import (
    Context,
    context_installed,
    error,
    set_context,
    version_callback,
)
from pyzill.cli.db import db_app
from pyzill.cli.detail import detail_app
from pyzill.cli.geocode import register as register_geocode_command
from pyzill.cli.query import register as register_offline_commands
from pyzill.cli.schema import schema_app
from pyzill.cli.search import search_app
from pyzill.config import DEFAULT_IMPERSONATE, DEFAULT_MAX_RETRIES, DEFAULT_TIMEOUT
from pyzill.proxy import resolve_proxy
from pyzill.ratelimit import set_min_interval

# Floors that keep an automated agent from hammering Zillow. They are
# overridable with --force, but the override is explicit: an agent that
# disables throttling must say so on every invocation.
_MIN_RATE_LIMIT = 1.0
_MAX_REQUESTS_CAP = 50

app = typer.Typer(
    name="pyzill",
    pretty_exceptions_show_locals=False,
    add_completion=False,
    no_args_is_help=True,
)

app.add_typer(search_app)
app.add_typer(detail_app)
app.add_typer(db_app)
app.add_typer(schema_app)
register_offline_commands(app)
register_geocode_command(app)


def _no_color_requested() -> bool:
    """Honor the NO_COLOR spec: only a non-empty value disables color."""
    return any(os.environ.get(name, "") != "" for name in ("NO_COLOR", "PYZILL_NO_COLOR"))


@app.callback()
def main(
    ctx: typer.Context,
    quiet: Annotated[
        bool,
        typer.Option("--quiet", "-q", help="Suppress progress and nonessential diagnostics"),
    ] = False,
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v", help="Additional stderr diagnostics"),
    ] = False,
    no_color: Annotated[
        bool,
        typer.Option("--no-color", help="Disable colors and styling"),
    ] = False,
    proxy: Annotated[
        str | None,
        typer.Option(
            "--proxy",
            help="Proxy URL (overrides PYZILL_PROXY and ZILLOW_PROXY)",
            envvar="PYZILL_PROXY",
        ),
    ] = None,
    impersonate: Annotated[
        str,
        typer.Option(
            "--impersonate",
            help=f"TLS impersonation profile (default: {DEFAULT_IMPERSONATE})",
            envvar="PYZILL_IMPERSONATE",
        ),
    ] = DEFAULT_IMPERSONATE,
    timeout: Annotated[
        float,
        typer.Option("--timeout", help="Request timeout in seconds (must be greater than 0; default: 30)"),
    ] = DEFAULT_TIMEOUT,
    max_retries: Annotated[
        int,
        typer.Option("--max-retries", min=0, help="Retries on transient 5xx, 429, and network errors (default: 1)"),
    ] = DEFAULT_MAX_RETRIES,
    json_errors: Annotated[
        bool | None,
        typer.Option("--json-errors/--no-json-errors", help="Emit structured error objects to stderr"),
    ] = None,
    rate_limit: Annotated[
        float | None,
        typer.Option(
            "--rate-limit",
            help=(
                "Minimum seconds between requests (default: PYZILL_RATE_LIMIT "
                "or 1.5; 0 disables; floor 1.0 without --force)"
            ),
            envvar="PYZILL_RATE_LIMIT",
        ),
    ] = None,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Override the safety floors on --rate-limit and --max-requests",
        ),
    ] = False,
    _version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=version_callback,
            is_eager=True,
            help="Show version and exit",
        ),
    ] = False,
) -> None:
    """pyzill — Zillow scraper CLI.

    Search for-sale/for-rent/sold listings and fetch property details.
    """
    # Typer already applies the PYZILL_PROXY / PYZILL_IMPERSONATE env vars (see
    # `envvar=` above), so there is no need to read them again by hand.
    cli_context = Context(
        quiet=quiet,
        verbose=verbose,
        no_color=no_color or _no_color_requested(),
        proxy=proxy,
        impersonate=impersonate or DEFAULT_IMPERSONATE,
        timeout=timeout,
        max_retries=max_retries,
        json_errors=json_errors if json_errors is not None else not sys.stdout.isatty(),
        rate_limit=rate_limit,
        force=force,
    )
    ctx.obj = cli_context
    set_context(cli_context)

    # Validate --timeout after the context is installed so a bad value honors
    # --json-errors rather than falling back to a bare context.
    if timeout <= 0:
        error("--timeout must be greater than 0", code=2)

    # Validate the proxy (explicit, PYZILL_PROXY, or ZILLOW_PROXY) up front so
    # a bad URL is a clean usage error, not a traceback mid-command. The
    # ValueError message quotes the full URL, so route it through error()'s
    # credential redaction.
    try:
        resolve_proxy(proxy)
    except ValueError as e:
        error(f"Invalid proxy: {e}", code=2)

    if rate_limit is not None:
        if not force and rate_limit < _MIN_RATE_LIMIT:
            error(
                f"--rate-limit must be at least {_MIN_RATE_LIMIT} second(s) (use --force to override)",
                code=2,
            )
        set_min_interval(rate_limit)


def main_entry() -> None:
    """Entry point for the CLI."""
    try:
        exit_code = app(standalone_mode=False)
    except ClickException as exc:
        if not context_installed():
            # The root callback never ran, so its --json-errors choice is
            # unknown: take it from the context the exception carries.
            # When the callback did run, the installed context already holds
            # the user's choice, and overwriting it here would discard it.
            # Only a UsageError carries a context; a plain ClickException
            # has no `ctx` attribute at all.
            root = getattr(exc, "ctx", None)
            while root is not None and root.parent is not None:
                root = root.parent
            json_errors = root.params.get("json_errors") if root is not None else None
            set_context(Context(json_errors=json_errors if json_errors is not None else not sys.stdout.isatty()))
        try:
            error(exc.format_message(), code=exc.exit_code)
        except typer.Exit as usage_exit:
            raise SystemExit(usage_exit.exit_code) from None
    if isinstance(exit_code, int):
        raise SystemExit(exit_code)


if __name__ == "__main__":
    main_entry()
