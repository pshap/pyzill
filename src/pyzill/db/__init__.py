"""SQLite database for tracking Zillow listings with price history.

Uses stdlib sqlite3 for zero new dependencies. The database lives at
$XDG_DATA_HOME/pyzill/pyzill.db (~/.local/share/pyzill/pyzill.db) by default,
configurable via --db or PYZILL_DB.

The implementation is split across submodules: :mod:`pyzill.db.runs` for sync-run
bookkeeping, :mod:`pyzill.db.schema` for the DDL and migrations,
:mod:`pyzill.db.connection` for opening the database, :mod:`pyzill.db.upsert`
for writes, :mod:`pyzill.db.queries` for reads, :mod:`pyzill.db.changes` for
the change events, :mod:`pyzill.db.paths` for the database location,
:mod:`pyzill.db.timestamps` for the stored timestamps, and
:mod:`pyzill.db.stats` for summary figures. This package re-exports the
public names.
"""

from pyzill.db.changes import get_changes
from pyzill.db.connection import connect, connect_readonly
from pyzill.db.paths import default_db_path, resolve_db_path
from pyzill.db.prune import PruneStats, prune_database
from pyzill.db.queries import get_price_history, load_listings
from pyzill.db.runs import (
    SyncRun,
    begin_sync_run,
    comparable_run_had_results,
    finish_sync_run,
    list_runs,
    utc_now,
)
from pyzill.db.stats import DBStats, db_stats
from pyzill.db.timestamps import normalize_utc_timestamp
from pyzill.db.upsert import SyncStats, upsert_listings

__all__ = [
    "DBStats",
    "PruneStats",
    "SyncRun",
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
    "list_runs",
    "load_listings",
    "normalize_utc_timestamp",
    "prune_database",
    "resolve_db_path",
    "upsert_listings",
    "utc_now",
]
