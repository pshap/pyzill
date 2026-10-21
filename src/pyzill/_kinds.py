"""Canonical listing-kind types shared by the client, DB, services, and CLI.

``Kind`` is the literal type used in library signatures; ``ListingKind`` is
the enum the CLI uses for argument choices. Both live here so there is one
place that knows the three listing kinds, and a test asserts the enum's
values and the literal's values never drift apart.
"""

from enum import StrEnum
from typing import Literal, get_args

Kind = Literal["sale", "rent", "sold"]
KINDS: tuple[Kind, ...] = get_args(Kind)


class ListingKind(StrEnum):
    """The listing kinds, as an enum (for CLI argument choices)."""

    sale = "sale"
    rent = "rent"
    sold = "sold"


def as_kind(value: ListingKind | Kind) -> Kind:
    """Return the literal kind for an enum member or an existing literal."""
    if isinstance(value, ListingKind):
        return value.value
    return value
