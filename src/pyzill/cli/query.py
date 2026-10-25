"""Offline query/normalize commands.

The filtering engine itself lives in :mod:`pyzill.query`; this module wires
it into the CLI, turning a :class:`~pyzill.query.QueryValidationError` into
a usage error.
"""

from collections.abc import Callable
from functools import wraps
from typing import Annotated

import typer

from pyzill.cli.console import error, read_json, write_output
from pyzill.cli.options import (
    ColsOpt,
    FormatOpt,
    LimitOpt,
    OutputOpt,
    SortOpt,
    WhereJSONOpt,
    WhereOpt,
)
from pyzill.cli.output import OutputFormat, expand_cols_preset
from pyzill.normalize import normalize_listings
from pyzill.query import QueryValidationError, coerce_value, query_database, query_rows
from pyzill.workflows import extract_listings

__all__ = [
    "QueryValidationError",
    "cli_query_database",
    "cli_query_rows",
    "coerce_value",
    "query_database",
    "query_rows",
]


def _as_cli_error[**P, R](fn: Callable[P, R]) -> Callable[P, R]:
    """Adapt a pure query engine so QueryValidationError becomes a usage error."""

    @wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return fn(*args, **kwargs)
        except QueryValidationError as exc:
            error(str(exc), code=2)

    return wrapper


cli_query_rows = _as_cli_error(query_rows)
cli_query_database = _as_cli_error(query_database)


def query(
    input_or_dash: Annotated[str, typer.Argument(help="Saved search JSON file, or '-' for stdin")],
    where: WhereOpt = None,
    where_json: WhereJSONOpt = None,
    sort: SortOpt = None,
    cols: ColsOpt = None,
    limit: LimitOpt = None,
    output_format: FormatOpt = OutputFormat.json,
    output: OutputOpt = None,
) -> None:
    """Re-filter and sort saved search results without hitting Zillow."""
    data = read_json(input_or_dash)
    if not isinstance(data, list):
        error("query input must be a JSON array of listings (run 'pyzill normalize' first if it isn't)", code=2)
    rows = [row for row in data if isinstance(row, dict)]
    if data and not rows:
        error("query input contains no listing objects", code=2)

    cols = expand_cols_preset(cols)
    rows, selected = cli_query_rows(rows, where=where, where_json=where_json, sort=sort, cols=cols, limit=limit)
    write_output(rows, output_format, output, columns=selected)


def normalize(
    input_or_dash: Annotated[
        str,
        typer.Argument(help="JSON file path or '-' for stdin"),
    ],
    output_format: FormatOpt = OutputFormat.json,
    output: OutputOpt = None,
) -> None:
    """Normalize raw listing data from file or stdin."""
    data = read_json(input_or_dash)
    try:
        listings = extract_listings(data, wrap_single=True)
    except ValueError as exc:
        error(str(exc), code=2)
    write_output(normalize_listings(listings), output_format, output)


def register(app: typer.Typer) -> None:
    """Register the offline root commands on the root app."""
    app.command("query")(query)
    app.command("normalize")(normalize)
