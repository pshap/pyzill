"""The offline query engine: filter and sort listings, from a file or the database.

This is pure code: it compiles the ``--where``/``--where-json`` conditions
into SQL and runs them against either an in-memory table built from a saved
response or the local database. It never prints and never exits; invalid
queries raise :class:`QueryValidationError` and the caller decides what to
do about it.
"""

import json
import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from pyzill.normalize import LISTING_FIELDS, NUMERIC_LISTING_FIELDS

__all__ = [
    "Condition",
    "QueryValidationError",
    "coerce_value",
    "query_database",
    "query_rows",
]

_QUERY_FIELDS = frozenset(LISTING_FIELDS) | {"ppsf"}
# "kind" is part of the listings primary key but not a normalized Listing field.
_DB_QUERY_FIELDS = _QUERY_FIELDS | {"first_seen_utc", "last_seen_utc", "kind"}
# The numeric listing fields, plus the computed price-per-square-foot.
_NUMERIC_FIELDS = NUMERIC_LISTING_FIELDS | {"ppsf"}

_QUOTED_LITERAL_LENGTH = 2
_STRING_OPS = frozenset({"=", "==", "!=", "~"})
# The operators the engine knows how to compile. An operator is written into
# the SQL, so only these may be used.
_OPERATORS = frozenset({">", "<", ">=", "<=", "==", "=", "!=", "~"})
# Operators a null value cannot be compared with: "=" and "!=" test for null
# (they compile to IS NULL / IS NOT NULL), but ordering and substring
# comparisons against null are meaningless and would silently match nothing.
_NULL_REJECTING_OPS = frozenset({">", "<", ">=", "<=", "~"})
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


def _unquote(text: str) -> str:
    """Return the literal inside a quoted value, or the value itself."""
    literal = text.strip()
    if len(literal) >= _QUOTED_LITERAL_LENGTH and literal[0] in "'\"" and literal[-1] == literal[0]:
        return literal[1:-1]
    return literal


def _parse_number(literal: str) -> int | float | None:
    """Parse a numeric literal, honoring the ``k`` and ``m`` suffixes."""
    match = _NUMBER_RE.fullmatch(literal)
    if match is None:
        return None
    suffix = (match.group(1) or "").lower()
    number = float(literal.replace("$", "").replace(",", "").removesuffix(match.group(1) or ""))
    number *= _NUMBER_SUFFIXES.get(suffix, 1)
    return int(number) if number.is_integer() else number


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
    number = _parse_number(value)
    if number is None:
        if field is None:
            return value
        raise QueryValidationError(f"Field {field!r} is numeric; {text!r} is not a number")
    return number


def _validate_condition(condition: Condition) -> None:
    """Reject a null value compared with an operator that cannot match it.

    Comparing a column with null using ``>``, ``<``, ``>=``, ``<=`` or ``~``
    can never be true, so it would silently return no rows. Use ``=`` or
    ``!=`` to test whether a value is null.
    """
    if condition.value is None and condition.operator in _NULL_REJECTING_OPS:
        raise QueryValidationError(
            f"Field {condition.field!r} cannot be compared with null using "
            f"{condition.operator!r}; use '=' or '!=' to test for a null value"
        )


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
    conditions: Sequence[Condition] | None = None,
) -> list[Condition]:
    parsed = [_parse_where(expression, valid_fields) for expression in (where or [])]
    parsed.extend(_validated_conditions(conditions or [], valid_fields))
    if where_json is None:
        return parsed
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
        if not isinstance(operator, str) or operator not in _OPERATORS:
            raise QueryValidationError(f"Unknown operator {operator!r} in --where-json")
        if not isinstance(value, (str, int, float, bool, type(None))):
            raise QueryValidationError("--where-json values must be strings, numbers, booleans, or null")
        parsed.append(Condition(field, operator, _condition_value(field, value)))
    return parsed


def _condition_value(field: str, value: Any) -> Any:
    """Return the value a condition compares with.

    A numeric field is compared with a number.  SQLite orders every number
    before every string, so a string compared with a numeric column matches
    every row instead of failing, and a value that is not a number is a
    usage error rather than a comparison that means nothing.
    """
    if field in _NUMERIC_FIELDS and value is not None:
        return _numeric_value(field, value)
    return value


def _validated_conditions(
    conditions: Sequence[Condition],
    valid_fields: frozenset[str],
) -> list[Condition]:
    """Check structured conditions against the fields and operators the query can read.

    A condition that names a field the table does not have would compile to
    SQL against a column that is not there, and one that carries an operator
    the engine does not know would be written into the SQL as it stands.
    Both are rejected here, with the same errors the expression form raises.
    """
    validated: list[Condition] = []
    for condition in conditions:
        if condition.field not in valid_fields:
            raise QueryValidationError(f"Unknown field {condition.field!r}. Fields: {', '.join(sorted(valid_fields))}")
        if condition.operator not in _OPERATORS:
            raise QueryValidationError(
                f"Unknown operator {condition.operator!r}. Operators: {', '.join(sorted(_OPERATORS))}"
            )
        validated.append(
            Condition(condition.field, condition.operator, _condition_value(condition.field, condition.value))
        )
    return validated


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


def _numeric_value(field: str, value: Any) -> int | float:
    """Return the number a comparison against a numeric field compares with.

    A quoted number is compared as a number, so ``price=='250000'`` finds the
    rows that ``price==250000`` finds. A value that is not a number at all is
    a usage error rather than a comparison that silently matches nothing.
    """
    if isinstance(value, bool):
        raise QueryValidationError(f"Field {field!r} is numeric; {value!r} is not a number")
    if isinstance(value, int | float):
        return value
    if isinstance(value, str):
        number = _parse_number(_unquote(value))
        if number is not None:
            return number
    raise QueryValidationError(f"Field {field!r} is numeric; {value!r} is not a number")


def _sql_equality(field: str, column: str, operator: str, value: Any) -> tuple[str, list[Any]]:
    """Compile an equality comparison with the value's type and the column's in step.

    A number compared with a text column used to compile to a comparison
    that could never be true, because the column was cast to text and then
    compared with the number itself: ``zipcode==12305`` matched nothing even
    though the column holds ``'12305'``. A quoted number compared with a
    numeric column had the same problem in the other direction -- the column
    was rendered as ``'250000.0'`` and compared with the text ``'250000'``.
    """
    sql_operator = "<>" if operator == "!=" else "="
    if field in _NUMERIC_FIELDS:
        return f"{column} {sql_operator} ?", [_numeric_value(field, value)]
    text = value if isinstance(value, str) else str(value)
    return f"lower({column}) {sql_operator} lower(?)", [text]


def _sql_condition(condition: Condition) -> tuple[str, list[Any]]:
    field, operator, value = condition.field, condition.operator, condition.value
    column = _sql_column(field)
    if operator in ("=", "==", "!=") and value is None:
        return f"{column} IS {'NOT ' if operator == '!=' else ''}NULL", []
    if operator == "~":
        return f"instr(lower(CAST({column} AS TEXT)), lower(?)) > 0", [str(value)]
    if operator in _STRING_OPS:
        return _sql_equality(field, column, operator, value)
    return f"{column} {'=' if operator == '==' else operator} ?", [value]


def _projection_column(field: str) -> str:
    """The SQL expression that yields a field's projected value.

    ``ppsf`` is computed rather than stored, so the projection computes it in
    SQL, with the same rounding and the same "unknown" rule the in-memory
    projection uses.
    """
    if field == "ppsf":
        return (
            "(CASE WHEN price IS NOT NULL AND area IS NOT NULL AND price != 0 AND area != 0 "
            'THEN ROUND(CAST(price AS REAL) / area, 2) END) AS "ppsf"'
        )
    return f'"{field}"'


@dataclass(frozen=True)
class _QuerySpec:
    """One query: the table to read, the filters that narrow it, and the order.

    The two public entry points -- a saved response and the database -- run
    the same engine; they differ only in the spec they hand it.
    """

    table: str
    valid_fields: frozenset[str]
    default_order: str
    where: list[str] | None = None
    where_json: str | None = None
    sort: str | None = None
    cols: str | None = None
    limit: int | None = None
    payload_column: str | None = None
    conditions: Sequence[Condition] | None = None


def _run_query(conn: sqlite3.Connection, spec: _QuerySpec) -> tuple[list[dict[str, Any]], list[str] | None]:
    if spec.limit is not None and spec.limit < 0:
        raise QueryValidationError("limit must be non-negative")
    parsed = _parse_conditions(spec.where, spec.where_json, spec.valid_fields, spec.conditions)
    for condition in parsed:
        _validate_condition(condition)
    selected = _selected_columns(spec.cols, spec.valid_fields)
    clauses: list[str] = []
    parameters: list[Any] = []
    for condition in parsed:
        clause, values = _sql_condition(condition)
        clauses.append(clause)
        parameters.extend(values)

    order_clause = spec.default_order
    if spec.sort:
        field, direction = _sort_condition(spec.sort, spec.valid_fields)
        column = _sql_column(field)
        order_clause = f'({column}) IS NULL ASC, {column} {direction.upper()}, "zpid" ASC'

    if spec.payload_column is not None:
        # A saved row is its payload; the table columns exist to filter and
        # sort on, so they are not selected.
        select_clause = f'"{spec.payload_column}"'
    elif selected:
        # Projecting in SQL keeps the columns that were not asked for out of
        # the result, so a narrow query does not read the whole table.
        select_clause = ", ".join(_projection_column(column) for column in selected)
    else:
        select_clause = "*"
    sql = f'SELECT {select_clause} FROM "{spec.table}"'  # noqa: S608 - table names are internal constants
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY " + order_clause
    if spec.limit is not None:
        sql += " LIMIT ?"
        parameters.append(spec.limit)
    cursor = conn.execute(sql, parameters)
    names = [description[0] for description in cursor.description]
    rows: list[dict[str, Any]] = []
    for record in cursor.fetchall():
        row = dict(zip(names, record, strict=True))
        if spec.payload_column is not None:
            payload = json.loads(row[spec.payload_column])
            rows.append({column: _field_value(payload, column) for column in selected} if selected else payload)
        else:
            rows.append(row)
    return rows, selected


def query_rows(  # noqa: PLR0913 - the query's whole filter surface
    rows: list[dict[str, Any]],
    *,
    where: list[str] | None = None,
    where_json: str | None = None,
    sort: str | None = None,
    cols: str | None = None,
    limit: int | None = None,
    valid_fields: frozenset[str] = _QUERY_FIELDS,
) -> tuple[list[dict[str, Any]], list[str] | None]:
    """Filter and sort saved listings with the same engine the database uses."""
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
            _QuerySpec(
                table="query_rows",
                valid_fields=valid_fields,
                default_order="rowid ASC",
                where=where,
                where_json=where_json,
                sort=sort,
                cols=cols,
                limit=limit,
                payload_column="__payload",
            ),
        )
    finally:
        conn.close()


def query_database(  # noqa: PLR0913 - the query's whole filter surface
    conn: sqlite3.Connection,
    *,
    where: list[str] | None = None,
    where_json: str | None = None,
    sort: str | None = None,
    cols: str | None = None,
    limit: int | None = None,
    conditions: Sequence[Condition] | None = None,
) -> tuple[list[dict[str, Any]], list[str] | None]:
    """Filter and sort the listings stored in the local database.

    The conditions may be given as expressions (``--where``), as JSON
    (``--where-json``), or as structured :class:`Condition` objects. All
    three go through the same engine, so they mean the same thing.
    """
    return _run_query(
        conn,
        _QuerySpec(
            table="listings",
            valid_fields=_DB_QUERY_FIELDS,
            default_order='"last_seen_utc" DESC, "zpid" DESC, "kind" ASC',
            where=where,
            where_json=where_json,
            sort=sort,
            cols=cols,
            limit=limit,
            conditions=conditions,
        ),
    )
