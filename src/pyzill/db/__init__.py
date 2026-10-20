"""SQLite database for tracking Zillow listings with price history.

Uses stdlib sqlite3 for zero new dependencies. The database lives at
$XDG_DATA_HOME/pyzill/pyzill.db (~/.local/share/pyzill/pyzill.db) by default,
configurable via --db or PYZILL_DB.

The implementation is split across submodules: :mod:`pyzill.db.runs` for sync-run
bookkeeping, :mod:`pyzill.db.schema` for the DDL and migrations,
:mod:`pyzill.db.connection` for opening the database, :mod:`pyzill.db.upsert`
for writes, :mod:`pyzill.db.queries` for reads, and :mod:`pyzill.db.stats` for
summary figures. This package re-exports the public names.
"""

from pyzill.db.connection import connect, connect_readonly
from pyzill.db.queries import get_changes, get_price_history, load_listings
from pyzill.db.runs import (
    begin_sync_run,
    comparable_run_had_results,
    default_db_path,
    finish_sync_run,
    utc_now,
)
from pyzill.db.stats import DBStats, db_stats
from pyzill.db.upsert import SyncStats, upsert_listings

__all__ = [
    "DBStats",
    "SyncStats",
    "begin_sync_run",
    "comparable_run_had_results",
    "connect",
    "connect_readonly",
    "db_stats",
    "default_db_path",
    "finish_sync_run",
    "get_changes",
    "get_price_history",
    "load_listings",
    "upsert_listings",
    "utc_now",
]
