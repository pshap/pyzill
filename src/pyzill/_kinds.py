"""Canonical listing-kind literal shared by the client, DB, and CLI."""

from typing import Literal, get_args

Kind = Literal["sale", "rent", "sold"]
KINDS: tuple[Kind, ...] = get_args(Kind)
