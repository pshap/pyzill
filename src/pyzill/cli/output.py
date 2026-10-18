"""Pure renderers for CLI output formats."""

import csv
import io
import json
from collections.abc import Callable
from enum import StrEnum
from typing import Any, NoReturn

from rich.console import Console
from rich.table import Table
from rich.text import Text


class OutputFormat(StrEnum):
    """Supported CLI output formats."""

    json = "json"
    jsonl = "jsonl"
    csv = "csv"
    table = "table"


_TABLE_COLUMNS = ("address", "price", "beds", "baths", "area", "city", "state", "home_type", "status")
_DB_TABLE_COLUMNS = (*_TABLE_COLUMNS, "first_seen_utc", "last_seen_utc")


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


def _render_csv(rows: list[dict[str, Any]], columns: list[str] | None = None) -> str:
    if not rows and columns is None:
        return ""
    columns = columns or _columns(rows)
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({column: _cell(row.get(column)) for column in columns})
    return buffer.getvalue()


def _build_table(rows: list[dict[str, Any]], columns: list[str] | None, use_db_columns: bool) -> Table:
    if columns is None:
        defaults = _DB_TABLE_COLUMNS if use_db_columns else _TABLE_COLUMNS
        display = [column for column in defaults if any(column in row for row in rows)]
        columns = display or _columns(rows)
    table = Table(title="Listings")
    for column in columns:
        table.add_column(column)
    for row in rows:
        table.add_row(*(Text(_cell(row.get(column))) for column in columns))
    return table


def write_output(  # noqa: PLR0913
    data: Any,
    output_format: OutputFormat,
    output: str | None,
    columns: list[str] | None,
    use_db_columns: bool,
    *,
    emit: Callable[[str, str | None], None],
    error: Callable[[str, int], NoReturn],
    warn: Callable[[str], None],
    console: Console,
) -> None:
    if output == "-":
        output = None
    if output_format is OutputFormat.json:
        try:
            rendered = dumps(data)
        except ValueError as exc:
            error(f"Output is not valid JSON: {exc}", 1)
        emit(rendered, output)
        return
    if output_format is OutputFormat.jsonl:
        items = data if isinstance(data, list) else [data]
        try:
            rendered = "\n".join(dumps(item) for item in items)
        except ValueError as exc:
            error(f"Output is not valid JSON: {exc}", 1)
        emit(rendered, output)
        return
    if not isinstance(data, list):
        error(f"{output_format.value} output requires a list", 2)
    rows: list[dict[str, Any]] = data
    if not rows:
        warn("No listings found")
    if output_format is OutputFormat.csv:
        emit(_render_csv(rows, columns), output)
    elif rows:
        table = _build_table(rows, columns, use_db_columns)
        if output:
            buffer = io.StringIO()
            Console(file=buffer, no_color=True, force_terminal=False, width=200).print(table)
            emit(buffer.getvalue(), output)
        else:
            console.print(table)
