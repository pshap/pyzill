"""Where the local database lives.

The default path follows the XDG data directory; an explicit path (the
``--db`` flag) wins over the ``PYZILL_DB`` environment variable, which wins
over the default.
"""

import os
from pathlib import Path

__all__ = ["default_db_path", "resolve_db_path"]


def default_db_path() -> Path:
    """Return the XDG data directory's pyzill database path."""
    data_home = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return data_home / "pyzill" / "pyzill.db"


def resolve_db_path(
    explicit_path: str | None = None,
    environment_path: str | None = None,
    default_path: Path | None = None,
) -> Path:
    """Resolve the database path: explicit, then environment, then default."""
    if explicit_path:
        return Path(explicit_path)
    if environment_path:
        return Path(environment_path)
    return default_path if default_path is not None else default_db_path()
