"""Pure renderers for the CLI output formats.

Nothing here prints, warns, or exits: a renderer turns data into text (or
a table) and raises :class:`OutputError` when the data cannot be rendered
in the requested format. The CLI layer decides what to do with the result.
"""

import csv
import io
import json
from collections.abc import Sequence
from enum import StrEnum
from typing import Any

from rich.console import Console
from rich.table import Table
from rich.text import Text


class OutputFormat(StrEnum):
    """Supported CLI output formats."""

    json = "json"
    jsonl = "jsonl"
    csv = "csv"
    table = "table"


class OutputError(ValueError):
    """The data cannot be rendered in the requested format."""

    exit_code = 1


class OutputUsageError(OutputError):
    """The data does not fit the requested format (a usage error)."""

    exit_code = 2


__all__ = [
    "DB_LISTING_TABLE_COLUMNS",
    "DETAILS_COLUMNS",
    "LISTING_TABLE_COLUMNS",
    "OutputError",
    "OutputFormat",
    "OutputUsageError",
    "dumps",
    "expand_cols_preset",
    "render_csv",
    "render_json",
    "render_jsonl",
    "render_table",
    "render_table_text",
    "require_list",
]

# The columns a listing table shows. The database adds the two timestamps it
# keeps, so a listing read back from the database carries those as well.
LISTING_TABLE_COLUMNS = ("address", "price", "beds", "baths", "area", "city", "state", "home_type", "status")
DB_LISTING_TABLE_COLUMNS = (*LISTING_TABLE_COLUMNS, "first_seen_utc", "last_seen_utc")

# Column preset: ``--cols details`` expands to these property-detail columns,
# plus the listing identifiers so rows can be matched back to a listing.
DETAILS_COLUMNS = (
    "zpid",
    "kind",
    "hoa_fee",
    "hoa_fee_unit",
    "hoa_monthly",
    "description",
    "photos_count",
    "year_built",
    "lot_size",
    "lot_size_unit",
    "tax_assessed_latest",
    "tax_year_latest",
    "tax_paid_latest",
    "tax_history",
    "schools",
)


def expand_cols_preset(cols: str | None) -> str | None:
    """Expand a column preset name (e.g. ``details``) to a comma-separated list."""
    if cols == "details":
        return ",".join(DETAILS_COLUMNS)
    return cols


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict | list):
        return json.dumps(value, ensure_ascii=False, allow_nan=False)
    return str(value)


def _columns(rows: list[dict[str, Any]]) -> list[str]:
    return list(dict.fromkeys(column for row in rows for column in row))


def dumps(value: Any) -> str:
    """Serialize output as strict JSON."""
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def render_csv(rows: list[dict[str, Any]], columns: list[str] | None = None) -> str:
    """Render rows as CSV text."""
    if not rows and columns is None:
        return ""
    columns = columns or _columns(rows)
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({column: _cell(row.get(column)) for column in columns})
    return buffer.getvalue()


def render_json(value: Any) -> str:
    """Render a value as strict JSON text."""
    try:
        return dumps(value)
    except ValueError as exc:
        raise OutputError(f"Output is not valid JSON: {exc}") from exc


def render_jsonl(value: Any) -> str:
    """Render a value (or a list of values) as newline-delimited JSON."""
    items = value if isinstance(value, list) else [value]
    try:
        return "\n".join(dumps(item) for item in items)
    except ValueError as exc:
        raise OutputError(f"Output is not valid JSON: {exc}") from exc


def render_table(
    rows: list[dict[str, Any]],
    columns: list[str] | None,
    default_columns: Sequence[str] | None,
    title: str = "Listings",
) -> Table:
    """Render rows as a table.

    ``columns`` is the caller's own choice of columns, as asked for with
    ``--cols``. ``default_columns`` is what a command shows when the caller
    chose none: the listing columns for a listing, or None for rows that
    are not listings, whose own columns are shown instead.
    """
    if columns is None:
        if default_columns is None:
            columns = _columns(rows)
        else:
            shown = [column for column in default_columns if any(column in row for row in rows)]
            columns = shown or _columns(rows)
    table = Table(title=title)
    for column in columns:
        table.add_column(column)
    for row in rows:
        table.add_row(*(Text(_cell(row.get(column))) for column in columns))
    return table


def render_table_text(table: Table) -> str:
    """Render a table as plain text (for writing to a file)."""
    buffer = io.StringIO()
    Console(file=buffer, no_color=True, force_terminal=False, width=200).print(table)
    return buffer.getvalue()


def require_list(data: Any, output_format: OutputFormat) -> list[dict[str, Any]]:
    """Return the data as a list of rows, or raise for a non-list payload."""
    if not isinstance(data, list):
        raise OutputUsageError(f"{output_format.value} output requires a list")
    return data
