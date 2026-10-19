"""Offline query/normalize commands plus the pure filtering engines they use."""

import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
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
    WhereJSONOpt,
    WhereOpt,
)
from pyzill.cli.output import OutputFormat
from pyzill.cli.search import extract_listings
from pyzill.normalize import Listing

_QUERY_FIELDS = frozenset(Listing.__annotations__) | {"ppsf"}
# "kind" is part of the listings primary key but not a normalized Listing field.
_DB_QUERY_FIELDS = _QUERY_FIELDS | {"first_seen_utc", "last_seen_utc", "kind"}
_NUMERIC_FIELDS = frozenset(
    {
        "zpid",
        "price",
        "beds",
        "baths",
        "area",
        "ppsf",
        "zestimate",
        "rent_zestimate",
        "latitude",
        "longitude",
        "days_on_zillow",
        "price_change",
        "lot_area_value",
        "year_built",
        "tax_assessed_value",
    }
)

_QUOTED_LITERAL_LENGTH = 2
_STRING_OPS = frozenset({"=", "==", "!=", "~"})
_WHERE_RE = re.compile(r"^\s*([A-Za-z_]\w*)\s*(>=|<=|!=|==|=|>|<|~)\s*(.+?)\s*$")
_SORT_RE = re.compile(r"^\s*([A-Za-z_]\w*)\s*(?::\s*(asc|desc))?\s*$", re.IGNORECASE)
# Optional k/m suffix, e.g. "500k" or "1.2m".
_NUMBER_RE = re.compile(r"[+-]?\$?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?([kKmM])?")
_NUMBER_SUFFIXES = {"k": 1_000, "m": 1_000_000}


class QueryValidationError(ValueError):
    """Invalid query expression or unrecognized listing field."""


@dataclass(frozen=True)
class Condition:
    field: str
    operator: str
    value: Any


def coerce_value(text: str, field: str | None = None) -> Any:
    """Convert a query literal to a finite-format number, null, or string.

    A bare literal compared against a numeric field must itself be numeric;
    anything else raises :class:`QueryValidationError` rather than silently
    comparing text against a REAL column (which would match everything or
    nothing). ``k`` and ``m`` suffixes are accepted as thousands/millions.
    """
    literal = text.strip()
    quoted = len(literal) >= _QUOTED_LITERAL_LENGTH and literal[0] in "'\"" and literal[-1] == literal[0]
    value = literal[1:-1] if quoted else literal
    if not quoted and value.lower() in ("null", "none"):
        return None
    if field is not None and field not in _NUMERIC_FIELDS:
        return value
    if quoted:
        return value
    match = _NUMBER_RE.fullmatch(value)
    if match is None:
        if field is None:
            return value
        raise QueryValidationError(f"Field {field!r} is numeric; {text!r} is not a number")
    suffix = (match.group(1) or "").lower()
    number = float(value.replace("$", "").replace(",", "").removesuffix(match.group(1) or ""))
    number *= _NUMBER_SUFFIXES.get(suffix, 1)
    return int(number) if number.is_integer() else number


def _parse_where(expression: str, valid_fields: frozenset[str]) -> Condition:
    match = _WHERE_RE.match(expression)
    if not match:
        raise QueryValidationError(f"Bad --where {expression!r}. Use: FIELD OP VALUE with OP in >= <= > < == != ~")
    field, operator, raw_value = match.groups()
    if field not in valid_fields:
        raise QueryValidationError(f"Unknown field {field!r} in --where. Fields: {', '.join(sorted(valid_fields))}")
    return Condition(field, operator, coerce_value(raw_value, field))


def _parse_conditions(
    where: list[str] | None,
    where_json: str | None,
    valid_fields: frozenset[str],
) -> list[Condition]:
    conditions = [_parse_where(expression, valid_fields) for expression in (where or [])]
    if where_json is None:
        return conditions
    try:
        raw_conditions = json.loads(where_json)
    except json.JSONDecodeError as exc:
        raise QueryValidationError(f"--where-json must be a JSON array: {exc.msg}") from exc
    if not isinstance(raw_conditions, list):
        raise QueryValidationError("--where-json must be a JSON array of condition objects")
    for item in raw_conditions:
        if not isinstance(item, dict) or not {"field", "op", "value"} <= item.keys():
            raise QueryValidationError("Each --where-json condition needs field, op, and value")
        field, operator, value = item["field"], item["op"], item["value"]
        if not isinstance(field, str) or field not in valid_fields:
            raise QueryValidationError(f"Unknown field {field!r} in --where-json")
        if not isinstance(operator, str) or operator not in {">", "<", ">=", "<=", "==", "=", "!=", "~"}:
            raise QueryValidationError(f"Unknown operator {operator!r} in --where-json")
        if not isinstance(value, (str, int, float, bool, type(None))):
            raise QueryValidationError("--where-json values must be strings, numbers, booleans, or null")
        conditions.append(Condition(field, operator, value))
    return conditions


def _field_value(row: dict[str, Any], field: str) -> Any:
    if field == "ppsf":
        price, area = row.get("price"), row.get("area")
        return round(price / area, 2) if price and area else None
    return row.get(field)


def _selected_columns(cols: str | None, valid_fields: frozenset[str]) -> list[str] | None:
    selected = [column.strip() for column in cols.split(",") if column.strip()] if cols else None
    if selected:
        unknown = [column for column in selected if column not in valid_fields]
        if unknown:
            raise QueryValidationError(
                f"Unknown column(s): {', '.join(unknown)}. Fields: {', '.join(sorted(valid_fields))}"
            )
    return selected


def _sort_condition(spec: str, valid_fields: frozenset[str]) -> tuple[str, str]:
    match = _SORT_RE.match(spec)
    if not match:
        raise QueryValidationError(f"Bad --sort {spec!r}. Use FIELD or FIELD:asc / FIELD:desc")
    field, direction = match.groups()
    if field not in valid_fields:
        raise QueryValidationError(f"Unknown field {field!r} in --sort. Fields: {', '.join(sorted(valid_fields))}")
    return field, (direction or "asc").lower()


def _sql_column(field: str) -> str:
    if field == "ppsf":
        return "(CAST(price AS REAL) / NULLIF(area, 0))"
    column = f'"{field}"'
    return f"CAST({column} AS REAL)" if field in _NUMERIC_FIELDS else f"CAST({column} AS TEXT)"


def _sql_condition(condition: Condition) -> tuple[str, list[Any]]:
    field, operator, value = condition.field, condition.operator, condition.value
    column = _sql_column(field)
    if operator in ("=", "==", "!=") and value is None:
        return f"{column} IS {'NOT ' if operator == '!=' else ''}NULL", []
    if operator == "~":
        return f"instr(lower(CAST({column} AS TEXT)), lower(?)) > 0", [str(value)]
    if operator in _STRING_OPS and isinstance(value, str):
        string_column = f"lower(CAST({column} AS TEXT))"
        return f"{string_column} {'<>' if operator == '!=' else '='} lower(?)", [value]
    return f"{column} {'=' if operator == '==' else operator} ?", [value]


def _run_query(  # noqa: PLR0913
    conn: sqlite3.Connection,
    *,
    table: str,
    where: list[str] | None,
    where_json: str | None,
    sort: str | None,
    cols: str | None,
    limit: int | None,
    valid_fields: frozenset[str],
    default_order: str,
) -> tuple[list[dict[str, Any]], list[str] | None]:
    conditions = _parse_conditions(where, where_json, valid_fields)
    selected = _selected_columns(cols, valid_fields)
    clauses: list[str] = []
    parameters: list[Any] = []
    for condition in conditions:
        clause, values = _sql_condition(condition)
        clauses.append(clause)
        parameters.extend(values)

    order_clause = default_order
    if sort:
        field, direction = _sort_condition(sort, valid_fields)
        column = _sql_column(field)
        order_clause = f'({column}) IS NULL ASC, {column} {direction.upper()}, "zpid" ASC'

    sql = f'SELECT * FROM "{table}"'  # noqa: S608 - table names are internal constants
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY " + order_clause
    if limit is not None:
        sql += " LIMIT ?"
        parameters.append(limit)
    cursor = conn.execute(sql, parameters)
    names = [description[0] for description in cursor.description]
    rows: list[dict[str, Any]] = []
    for record in cursor.fetchall():
        row = dict(zip(names, record, strict=True))
        payload = json.loads(row.pop("__payload")) if "__payload" in row else row
        rows.append({column: _field_value(payload, column) for column in selected} if selected else payload)
    return rows, selected


def query_rows(  # noqa: PLR0913
    rows: list[dict[str, Any]],
    *,
    where: list[str] | None = None,
    where_json: str | None = None,
    sort: str | None = None,
    cols: str | None = None,
    limit: int | None = None,
    valid_fields: frozenset[str] = _QUERY_FIELDS,
) -> tuple[list[dict[str, Any]], list[str] | None]:
    fields = sorted(valid_fields)
    column_types = [f'"{field}" {"REAL" if field in _NUMERIC_FIELDS else "TEXT"}' for field in fields]
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute(f'CREATE TABLE "query_rows" ("__payload" TEXT, {", ".join(column_types)})')
        placeholders = ", ".join("?" for _ in range(len(fields) + 1))
        values: list[tuple[Any, ...]] = []
        for row in rows:
            row_values = tuple(
                json.dumps(row.get(field), ensure_ascii=False)
                if isinstance(row.get(field), dict | list)
                else row.get(field)
                for field in fields
            )
            values.append((json.dumps(row, ensure_ascii=False), *row_values))
        conn.executemany(f'INSERT INTO "query_rows" VALUES ({placeholders})', values)  # noqa: S608
        return _run_query(
            conn,
            table="query_rows",
            where=where,
            where_json=where_json,
            sort=sort,
            cols=cols,
            limit=limit,
            valid_fields=valid_fields,
            default_order="rowid ASC",
        )
    finally:
        conn.close()


def query_database(  # noqa: PLR0913
    conn: sqlite3.Connection,
    *,
    where: list[str] | None = None,
    where_json: str | None = None,
    sort: str | None = None,
    cols: str | None = None,
    limit: int | None = None,
) -> tuple[list[dict[str, Any]], list[str] | None]:
    return _run_query(
        conn,
        table="listings",
        where=where,
        where_json=where_json,
        sort=sort,
        cols=cols,
        limit=limit,
        valid_fields=_DB_QUERY_FIELDS,
        default_order='"last_seen_utc" DESC, "zpid" DESC',
    )


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


def query(  # noqa: PLR0913
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
    listings = extract_listings(data, wrap_single=True)
    write_output(normalize_listings(listings), output_format, output)


def register(app: typer.Typer) -> None:
    """Register the offline root commands on the root app."""
    app.command("query")(query)
    app.command("normalize")(normalize)
