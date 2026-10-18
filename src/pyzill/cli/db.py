"""Shared database command types and path resolution."""

from enum import StrEnum
from pathlib import Path


class ListingKind(StrEnum):
    sale = "sale"
    rent = "rent"
    sold = "sold"


def resolve_db_path(explicit_path: str | None, environment_path: str | None, default_path: Path) -> Path:
    if explicit_path:
        return Path(explicit_path)
    if environment_path:
        return Path(environment_path)
    return default_path
