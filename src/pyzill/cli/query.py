"""Offline query/normalize commands plus the pure filtering engines they use."""

import re
import sqlite3
from collections.abc import Callable
from functools import wraps
from typing import Annotated, Any

import typer

from pyzill import normalize_listings
from pyzill.cli.console import error, read_json, write_output
from pyzill.cli.options import (
    ColsOpt,
    FormatOpt,
    LimitOpt,
    OutputOpt,
    SortOpt,
    WhereOpt,
)
from pyzill.cli.output import OutputFormat
from pyzill.cli.search import extract_listings
from pyzill.normalize import Listing

_QUERY_FIELDS = frozenset(Listing.__annotations__) | {"ppsf"}
_DB_QUERY_FIELDS = _QUERY_FIELDS | {"first_seen_utc", "last_seen_utc"}
_NUMERIC_FIELDS = frozenset(
    {"price", "beds", "baths", "area", "ppsf", "zestimate", "rent_zestimate", "latitude", "longitude"}
)
_QUOTED_LITERAL_LENGTH = 2
_WHERE_RE = re.compile(r"^\s*([A-Za-z_]\w*)\s*(>=|<=|!=|==|=|>|<|~)\s*(.+?)\s*$")
_SORT_RE = re.compile(r"^\s*([A-Za-z_]\w*)\s*(?::\s*(asc|desc))?\s*$", re.IGNORECASE)


class QueryValidationError(ValueError):
    """Invalid query expression or unrecognized listing field."""


def coerce_value(text: str, field: str | None = None) -> Any:
    """Convert a query literal to a finite-format number, null, or string."""
    literal = text.strip()
    quoted = len(literal) >= _QUOTED_LITERAL_LENGTH and literal[0] in "'\"" and literal[-1] == literal[0]
    value = literal[1:-1] if quoted else literal
    if not quoted and value.lower() in ("null", "none"):
        return None
    if quoted or (field is not None and field not in _NUMERIC_FIELDS):
        return value
    if not re.fullmatch(r"[+-]?\$?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?", value):
        return value
    number = float(value.replace("$", "").replace(",", ""))
    return int(number) if number.is_integer() else number


def _parse_where(expression: str, valid_fields: frozenset[str]) -> tuple[str, str, Any]:
    match = _WHERE_RE.match(expression)
    if not match:
        raise QueryValidationError(f"Bad --where {expression!r}. Use: FIELD OP VALUE with OP in >= <= > < == != ~")
    field, operator, raw_value = match.groups()
    if field not in valid_fields:
        raise QueryValidationError(f"Unknown field {field!r} in --where. Fields: {', '.join(sorted(valid_fields))}")
    return field, operator, coerce_value(raw_value, field)


def _field_value(row: dict[str, Any], field: str) -> Any:
    if field == "ppsf":
        price, area = row.get("price"), row.get("area")
        return round(price / area, 2) if price and area else None
    return row.get(field)


def _order_match(have: Any, operator: str, want: Any) -> bool:
    try:
        return bool({">": have > want, "<": have < want, ">=": have >= want, "<=": have <= want}[operator])
    except TypeError:
        return False


def _where_match(row: dict[str, Any], condition: tuple[str, str, Any]) -> bool:
    field, operator, want = condition
    have = _field_value(row, field)
    if operator in ("==", "="):
        if isinstance(have, str) or isinstance(want, str):
            return str(have).lower() == str(want).lower() if have is not None and want is not None else have is want
        return bool(have == want)
    if operator == "!=":
        return not _where_match(row, (field, "==", want))
    if have is None or want is None:
        return False
    if operator == "~":
        return str(want).lower() in str(have).lower()
    return _order_match(have, operator, want)


def _selected_columns(cols: str | None, valid_fields: frozenset[str]) -> list[str] | None:
    selected = [column.strip() for column in cols.split(",") if column.strip()] if cols else None
    if selected:
        unknown = [column for column in selected if column not in valid_fields]
        if unknown:
            raise QueryValidationError(
                f"Unknown column(s): {', '.join(unknown)}. Fields: {', '.join(sorted(valid_fields))}"
            )
    return selected


def _sort_rows(rows: list[dict[str, Any]], spec: str, valid_fields: frozenset[str]) -> list[dict[str, Any]]:
    match = _SORT_RE.match(spec)
    if not match:
        raise QueryValidationError(f"Bad --sort {spec!r}. Use FIELD or FIELD:asc / FIELD:desc")
    field, direction = match.groups()
    if field not in valid_fields:
        raise QueryValidationError(f"Unknown field {field!r} in --sort. Fields: {', '.join(sorted(valid_fields))}")
    reverse = (direction or "asc").lower() == "desc"
    present = [row for row in rows if _field_value(row, field) is not None]
    missing = [row for row in rows if _field_value(row, field) is None]
    present.sort(key=lambda row: _field_value(row, field), reverse=reverse)
    return present + missing


def query_rows(  # noqa: PLR0913
    rows: list[dict[str, Any]],
    *,
    where: list[str] | None = None,
    sort: str | None = None,
    cols: str | None = None,
    limit: int | None = None,
    valid_fields: frozenset[str] = _QUERY_FIELDS,
) -> tuple[list[dict[str, Any]], list[str] | None]:
    conditions = [_parse_where(expression, valid_fields) for expression in (where or [])]
    if conditions:
        rows = [row for row in rows if all(_where_match(row, condition) for condition in conditions)]
    if sort:
        rows = _sort_rows(rows, sort, valid_fields)
    selected = _selected_columns(cols, valid_fields)
    if selected:
        rows = [{column: _field_value(row, column) for column in selected} for row in rows]
    if limit is not None:
        rows = rows[:limit]
    return rows, selected


def _sql_column(field: str) -> str:
    return "(CAST(price AS REAL) / NULLIF(area, 0))" if field == "ppsf" else f'"{field}"'


def _sql_condition(field: str, operator: str, value: Any) -> tuple[str, list[Any]]:
    column = _sql_column(field)
    if operator in ("=", "==", "!=") and value is None:
        return f"{column} IS {'NOT ' if operator == '!=' else ''}NULL", []
    if operator == "~":
        return f"instr(lower(CAST({column} AS TEXT)), lower(?)) > 0", [str(value)]
    if operator in ("=", "==", "!=") and isinstance(value, str):
        sql_operator = "<>" if operator == "!=" else "="
        return f"lower(CAST({column} AS TEXT)) {sql_operator} lower(?)", [value]
    return f"{column} {'=' if operator == '==' else operator} ?", [value]


def query_database(
    conn: sqlite3.Connection,
    *,
    where: list[str] | None = None,
    sort: str | None = None,
    cols: str | None = None,
    limit: int | None = None,
) -> tuple[list[dict[str, Any]], list[str] | None]:
    conditions = [_parse_where(expression, _DB_QUERY_FIELDS) for expression in (where or [])]
    selected = _selected_columns(cols, _DB_QUERY_FIELDS)
    parameters: list[Any] = []
    clauses: list[str] = []
    for field, operator, value in conditions:
        clause, values = _sql_condition(field, operator, value)
        clauses.append(clause)
        parameters.extend(values)

    order_clause = '"last_seen_utc" DESC, "zpid" DESC'
    if sort:
        match = _SORT_RE.match(sort)
        if not match:
            raise QueryValidationError(f"Bad --sort {sort!r}. Use FIELD or FIELD:asc / FIELD:desc")
        field, direction = match.groups()
        if field not in _DB_QUERY_FIELDS:
            raise QueryValidationError(
                f"Unknown field {field!r} in --sort. Fields: {', '.join(sorted(_DB_QUERY_FIELDS))}"
            )
        column = _sql_column(field)
        order_clause = (
            f'({column}) IS NULL ASC, {column} {(direction or "asc").upper()}, "last_seen_utc" DESC, "zpid" DESC'
        )

    sql = "SELECT * FROM listings"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY " + order_clause
    if limit is not None:
        sql += " LIMIT ?"
        parameters.append(limit)
    cursor = conn.execute(sql, parameters)
    names = [description[0] for description in cursor.description]
    rows = [dict(zip(names, record, strict=True)) for record in cursor.fetchall()]
    if selected:
        rows = [{column: _field_value(row, column) for column in selected} for row in rows]
    return rows, selected


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


def query(  # noqa: PLR0913, PLR0917
    input_or_dash: Annotated[str, typer.Argument(help="Saved search JSON file, or '-' for stdin")],
    where: WhereOpt = None,
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

    rows, selected = cli_query_rows(rows, where=where, sort=sort, cols=cols, limit=limit)
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
    listings = extract_listings(data, wrap_single=True)
    write_output(normalize_listings(listings), output_format, output)


def register(app: typer.Typer) -> None:
    """Register the offline root commands on the root app."""
    app.command("query")(query)
    app.command("normalize")(normalize)
