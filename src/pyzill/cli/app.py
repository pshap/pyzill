"""CLI for pyzill Zillow scraper.

Uses Typer for command-line interface and Rich for terminal output.
"""

import os
import sys
from typing import Annotated

import typer

from pyzill import resolve_proxy, set_min_interval
from pyzill.cli.console import (
    Context,
    error,
    set_context,
    version_callback,
)
from pyzill.cli.db import db_app
from pyzill.cli.detail import detail_app
from pyzill.cli.query import register as register_offline_commands
from pyzill.cli.schema import schema_app
from pyzill.cli.search import search_app
from pyzill.config import DEFAULT_IMPERSONATE, DEFAULT_MAX_RETRIES, DEFAULT_TIMEOUT

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


@app.callback()
def main(  # noqa: PLR0913, PLR0917
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
            help="Minimum seconds between requests (default: PYZILL_RATE_LIMIT or 1.5; 0 disables)",
            envvar="PYZILL_RATE_LIMIT",
        ),
    ] = None,
    version: Annotated[  # noqa: ARG001
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
    if timeout <= 0:
        error("--timeout must be greater than 0", code=2)

    # Typer already applies the PYZILL_PROXY / PYZILL_IMPERSONATE env vars (see
    # `envvar=` above), so there is no need to read them again by hand.
    cli_context = Context(
        quiet=quiet,
        verbose=verbose,
        no_color=no_color or any(name in os.environ for name in ("NO_COLOR", "PYZILL_NO_COLOR")),
        proxy=proxy,
        impersonate=impersonate or DEFAULT_IMPERSONATE,
        timeout=timeout,
        max_retries=max_retries,
        json_errors=json_errors if json_errors is not None else not sys.stdout.isatty(),
    )
    ctx.obj = cli_context
    set_context(cli_context)

    # Validate the proxy (explicit, PYZILL_PROXY, or ZILLOW_PROXY) up front so
    # a bad URL is a clean usage error, not a traceback mid-command. The
    # ValueError message quotes the full URL, so route it through error()'s
    # credential redaction.
    try:
        resolve_proxy(proxy)
    except ValueError as e:
        error(f"Invalid proxy: {e}", code=2)

    if rate_limit is not None:
        set_min_interval(rate_limit)


def main_entry() -> None:
    """Entry point for the CLI."""
    app()


if __name__ == "__main__":
    main_entry()
