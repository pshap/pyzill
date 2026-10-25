"""Agent-safety floors and persisted block memory.

An automated agent can point ``--rate-limit 0`` or ``--max-requests 100000`` at
the CLI and burn Zillow's PerimeterX. The floors here are the enforcement:
they live in the CLI layer, so the library stays permissive for callers who
know what they are doing, and the CLI refuses the dangerous values unless
overridden by environment variables.

A blocked response is also remembered between runs, so a loop that keeps
hitting exit code 3 does not keep hitting Zillow. Block memory is on by
default at ``$XDG_STATE_HOME/pyzill/block.json`` (falling back to
``~/.local/state/pyzill/block.json``); ``PYZILL_BLOCK_STATE`` overrides the
path, and ``PYZILL_BLOCK_STATE=off`` disables it.

Block memory escalates: the more blocks in the last 24 hours, the longer the
cooldown before the CLI will attempt another request -- 15 minutes, then 1
hour, then 4 hours, then 24 hours.
"""

import json
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

__all__ = [
    "DEFAULT_MAX_REQUESTS_CAP",
    "DEFAULT_MIN_RATE_LIMIT",
    "clear_block_state",
    "cooldown_for",
    "cooldown_until",
    "is_in_cooldown",
    "max_requests_cap",
    "min_rate_limit",
    "read_block_state",
    "record_block",
    "seconds_until_unblock",
    "write_block_state",
]

# The conservative floors. The environment variables raise the ceiling or
# lower the minimum; they are read on every call so a process that changes
# them (and a test) sees the new value without an import-time snapshot.
DEFAULT_MIN_RATE_LIMIT = 1.0
DEFAULT_MAX_REQUESTS_CAP = 50
_BLOCK_STATE_ENV = "PYZILL_BLOCK_STATE"
_MIN_RATE_LIMIT_ENV = "PYZILL_MIN_RATE_LIMIT_FLOOR"
_MAX_REQUESTS_ENV = "PYZILL_MAX_REQUESTS_CEILING"


def min_rate_limit() -> float:
    """Return the minimum seconds between requests the CLI accepts."""
    raw = os.environ.get(_MIN_RATE_LIMIT_ENV, "").strip()
    if not raw:
        return DEFAULT_MIN_RATE_LIMIT
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_MIN_RATE_LIMIT


def max_requests_cap() -> int:
    """Return the largest ``--max-requests`` value the CLI accepts."""
    raw = os.environ.get(_MAX_REQUESTS_ENV, "").strip()
    if not raw:
        return DEFAULT_MAX_REQUESTS_CAP
    try:
        return int(raw)
    except ValueError:
        return DEFAULT_MAX_REQUESTS_CAP


# Cooldown ladder (seconds), indexed by recent-block count (capped at 4).
_COOLDOWN_SECONDS = (0, 900, 3600, 14400, 86400)
_COOLDOWN_WINDOW = timedelta(hours=24)


def _default_state_path() -> Path:
    state_home = os.environ.get("XDG_STATE_HOME", "").strip()
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return base / "pyzill" / "block.json"


def _block_state_path() -> Path | None:
    """Return the state file path, or None when block memory is disabled."""
    raw = os.environ.get(_BLOCK_STATE_ENV)
    if raw is not None and raw.strip().lower() == "off":
        return None
    if raw is not None and raw.strip():
        return Path(raw).expanduser()
    return _default_state_path()


def read_block_state() -> dict[str, Any] | None:
    """Return the persisted block state, or None when blocking is disabled."""
    path = _block_state_path()
    if path is None or not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def write_block_state(state: dict[str, Any]) -> None:
    path = _block_state_path()
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2, sort_keys=True)
    tmp.replace(path)


def clear_block_state() -> None:
    path = _block_state_path()
    if path is not None and path.exists():
        path.unlink()


def _parse_recorded_at(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (ValueError, AttributeError):
        return None


def _recent_blocks(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Blocks recorded within the last 24 hours, most recent first."""
    cutoff = datetime.now(UTC) - _COOLDOWN_WINDOW
    blocks = state.get("blocks", [])
    if not isinstance(blocks, list):
        return []
    recent = []
    for entry in blocks:
        if not isinstance(entry, dict):
            continue
        recorded_at = _parse_recorded_at(str(entry.get("recorded_at", "")))
        if recorded_at is not None and recorded_at >= cutoff:
            recent.append(entry)
    recent.sort(
        key=lambda e: _parse_recorded_at(str(e.get("recorded_at", ""))) or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )
    return recent


def _cooldown_seconds(state: dict[str, Any]) -> int:
    """Return the ladder duration for the blocks in ``state``, or 0."""
    recent = _recent_blocks(state)
    if not recent:
        return 0
    level = min(len(recent), len(_COOLDOWN_SECONDS) - 1)
    return _COOLDOWN_SECONDS[level]


def cooldown_for() -> int:
    """Return the cooldown duration in seconds for the recorded blocks.

    The ladder escalates with the number of blocks in the last 24 hours:
    1 block -> 15 minutes, 2 -> 1 hour, 3 -> 4 hours, 4+ -> 24 hours.
    Returns 0 when block memory is disabled or there are no recent blocks.
    """
    if _block_state_path() is None:
        return 0
    state = read_block_state()
    if state is None:
        return 0
    return _cooldown_seconds(state)


def _cooldown_deadline() -> datetime | None:
    """Return when the current cooldown ends, or None when it is not active."""
    if _block_state_path() is None:
        return None
    state = read_block_state()
    if state is None:
        return None
    seconds = _cooldown_seconds(state)
    if seconds == 0:
        return None
    recent = _recent_blocks(state)
    last_recorded = _parse_recorded_at(str(recent[0].get("recorded_at", ""))) if recent else None
    if last_recorded is None:
        return None
    deadline = last_recorded + timedelta(seconds=seconds)
    return deadline if deadline > datetime.now(UTC) else None


def is_in_cooldown() -> bool:
    """True when a block cooldown is active and requests should be paused."""
    return _cooldown_deadline() is not None


def seconds_until_unblock() -> int:
    """Seconds remaining in the current cooldown, or 0 when not in cooldown."""
    deadline = _cooldown_deadline()
    if deadline is None:
        return 0
    return max(0, int((deadline - datetime.now(UTC)).total_seconds()))


def cooldown_until() -> str | None:
    """Return the UTC timestamp the cooldown ends, or None when not active."""
    deadline = _cooldown_deadline()
    if deadline is None:
        return None
    return deadline.strftime("%Y-%m-%dT%H:%M:%SZ")


def record_block(code: str, *, retryable: bool, status_code: int | None = None) -> dict[str, Any] | None:
    """Persist that Zillow blocked us, if block memory is enabled.

    Returns the state written (for tests), or None when blocking is disabled.
    """
    path = _block_state_path()
    if path is None:
        return None
    state = read_block_state() or {"blocks": []}
    blocks = state.setdefault("blocks", [])
    entry: dict[str, Any] = {
        "code": code,
        "retryable": retryable,
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if status_code is not None:
        entry["status_code"] = status_code
    blocks.append(entry)
    write_block_state(state)
    return state
