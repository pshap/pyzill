"""Versioned machine-readable schemas for normalized public data.

Three shapes are published, one command each:

* ``listing`` -- the normalized search listing, the shape ``pyzill search``
  and ``pyzill normalize`` emit.
* ``db-listing`` -- a row of the local database's ``listings`` table, the
  shape ``pyzill db query`` emits.
* ``detail`` -- the normalized property detail, the shape
  :func:`pyzill.property_detail.normalize_property_detail` returns.

The listing and detail schemas are derived from their TypedDicts, and the
database schema from the DDL that declares the columns, so a schema cannot
drift away from the shape it describes.
"""

import json
import types
from typing import Any, Union, get_args, get_origin, get_type_hints

import typer

from pyzill.db.schema import LISTING_COLUMN_DEFINITIONS, LISTING_TABLE_COLUMNS, PROPERTY_DETAIL_COLUMN_DEFINITIONS
from pyzill.normalize import Listing
from pyzill.property_detail import PropertyDetail

schema_app = typer.Typer(name="schema", help="Print versioned JSON Schemas")

_LISTING_SCHEMA_ID = "https://github.com/pshap/pyzill/schemas/listing/1.0.0"
_DB_LISTING_SCHEMA_ID = "https://github.com/pshap/pyzill/schemas/db-listing/1.0.0"
_DETAIL_SCHEMA_ID = "https://github.com/pshap/pyzill/schemas/detail/1.0.0"
_DB_DETAIL_SCHEMA_ID = "https://github.com/pshap/pyzill/schemas/db-detail/1.0.0"
_JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"

# The JSON type each storage type in the DDL maps to.
_DDL_JSON_TYPES = {"INTEGER": "integer", "REAL": "number", "TEXT": "string"}


def _json_schema(annotation: Any) -> dict[str, Any]:
    origin = get_origin(annotation)
    if origin in (Union, types.UnionType):
        return {"anyOf": [_json_schema(argument) for argument in get_args(annotation)]}
    if origin in (list, tuple, set, frozenset):
        arguments = get_args(annotation)
        return {"type": "array", "items": _json_schema(arguments[0]) if arguments else {}}
    if origin is dict:
        return {"type": "object"}
    json_types = {
        str: "string",
        int: "integer",
        float: "number",
        bool: "boolean",
        type(None): "null",
    }
    json_type = json_types.get(annotation)
    return {"type": json_type} if json_type is not None else {}


def _ddl_column_schema(definition: str) -> dict[str, Any]:
    """Return the JSON Schema for one listings column, from the DDL that declares it.

    The DDL is the authority for a column's type, so the schema reads it
    rather than keeping a list of its own. A column the DDL declares NOT NULL
    cannot hold a null; every other column can.
    """
    column_type = definition.split()[0]
    json_type = _DDL_JSON_TYPES.get(column_type, "string")
    if "NOT NULL" in definition:
        return {"type": json_type}
    return {"anyOf": [{"type": json_type}, {"type": "null"}]}


def db_detail_schema() -> dict[str, Any]:
    """Derive the versioned JSON Schema for a row of the property_details table.

    This is the shape ``pyzill db query`` emits when it projects a detail
    field: the detail columns, keyed by ``(zpid, kind)`` with the foreign key
    that cascades deletes into listings. ``tax_assessed_value`` prefers the
    listings column and falls back to this one, but the stored detail column
    is still published here under its own name.
    """
    properties = {
        column: _ddl_column_schema(definition) for column, definition in PROPERTY_DETAIL_COLUMN_DEFINITIONS.items()
    }
    return {
        "$schema": _JSON_SCHEMA_DIALECT,
        "$id": _DB_DETAIL_SCHEMA_ID,
        "title": "Pyzill Database Property Detail",
        "description": (
            "A row of the local database's property_details table, as 'pyzill db query' emits "
            "when it projects a detail field. No property is required: '--cols' projects a subset "
            "of them; a property the table does not have is still rejected."
        ),
        "type": "object",
        "properties": properties,
        "required": [],
        "additionalProperties": False,
    }


def listing_schema() -> dict[str, Any]:
    """Derive the versioned JSON Schema from the normalized Listing TypedDict."""
    hints = get_type_hints(Listing)
    return {
        "$schema": _JSON_SCHEMA_DIALECT,
        "$id": _LISTING_SCHEMA_ID,
        "title": "Pyzill Listing",
        "type": "object",
        "properties": {name: _json_schema(annotation) for name, annotation in hints.items()},
        "required": sorted(Listing.__required_keys__),
        "additionalProperties": False,
    }


def db_listing_schema() -> dict[str, Any]:
    """Derive the versioned JSON Schema for a row of the listings table.

    This is the shape ``pyzill db query`` returns: the listing columns plus
    the kind and the two timestamps the database adds. The price per square
    foot is in the schema too, because ``--cols ppsf`` projects it.

    No property is required: ``--cols`` projects a subset of the columns, so
    a projection is a valid row as far as this schema is concerned. A
    property the table does not have is still rejected.
    """
    properties = {column: _ddl_column_schema(LISTING_COLUMN_DEFINITIONS[column]) for column in LISTING_TABLE_COLUMNS}
    properties["ppsf"] = {"anyOf": [{"type": "number"}, {"type": "null"}]}
    return {
        "$schema": _JSON_SCHEMA_DIALECT,
        "$id": _DB_LISTING_SCHEMA_ID,
        "title": "Pyzill Database Listing",
        "description": (
            "A row of the local database's listings table, as 'pyzill db query' returns it. "
            "'pyzill db query --cols' projects a subset of these properties, so none of them "
            "is required; a property the table does not have is still rejected."
        ),
        "type": "object",
        "properties": properties,
        "required": [],
        "additionalProperties": False,
    }


def detail_schema() -> dict[str, Any]:
    """Derive the versioned JSON Schema from the normalized PropertyDetail TypedDict."""
    hints = get_type_hints(PropertyDetail)
    return {
        "$schema": _JSON_SCHEMA_DIALECT,
        "$id": _DETAIL_SCHEMA_ID,
        "title": "Pyzill Property Detail",
        "type": "object",
        "properties": {name: _json_schema(annotation) for name, annotation in hints.items()},
        "required": sorted(PropertyDetail.__required_keys__),
        "additionalProperties": False,
    }


@schema_app.command("listing")
def listing() -> None:
    """Print the JSON Schema for normalized listings."""
    typer.echo(json.dumps(listing_schema(), indent=2, ensure_ascii=False))


@schema_app.command("db-listing")
def db_listing() -> None:
    """Print the JSON Schema for a row of the local database's listings table."""
    typer.echo(json.dumps(db_listing_schema(), indent=2, ensure_ascii=False))


@schema_app.command("detail")
def detail() -> None:
    """Print the JSON Schema for normalized property details."""
    typer.echo(json.dumps(detail_schema(), indent=2, ensure_ascii=False))


@schema_app.command("db-detail")
def db_detail() -> None:
    """Print the JSON Schema for a row of the property_details table."""
    typer.echo(json.dumps(db_detail_schema(), indent=2, ensure_ascii=False))
