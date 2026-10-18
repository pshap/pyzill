"""Versioned machine-readable schemas for normalized public data."""

import json
import types
from typing import Any, Union, get_args, get_origin, get_type_hints

import typer

from pyzill.normalize import Listing

schema_app = typer.Typer(name="schema", help="Print versioned JSON Schemas")

_SCHEMA_ID = "https://github.com/pshap/pyzill/schemas/listing/1.0.0"


def _json_schema(annotation: Any) -> dict[str, Any]:
    origin = get_origin(annotation)
    if origin in (Union, types.UnionType):
        return {"anyOf": [_json_schema(argument) for argument in get_args(annotation)]}
    json_types = {
        str: "string",
        int: "integer",
        float: "number",
        bool: "boolean",
        type(None): "null",
    }
    json_type = json_types.get(annotation)
    return {"type": json_type} if json_type is not None else {}


def listing_schema() -> dict[str, Any]:
    """Derive the versioned JSON Schema from the normalized Listing TypedDict."""
    hints = get_type_hints(Listing)
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": _SCHEMA_ID,
        "title": "Pyzill Listing",
        "type": "object",
        "properties": {name: _json_schema(annotation) for name, annotation in hints.items()},
        "required": sorted(Listing.__required_keys__),
        "additionalProperties": False,
    }


@schema_app.command("listing")
def listing() -> None:
    """Print the JSON Schema for normalized listings."""
    typer.echo(json.dumps(listing_schema(), indent=2, ensure_ascii=False))
