"""Agent-safety floors and persisted block memory.

An automated agent can point ``--rate-limit 0`` or ``--max-requests 100000`` at
the CLI and burn Zillow's PerimeterX. The floors here are the enforcement:
they live in the CLI layer, so the library stays permissive for callers who
know what they are doing, and the CLI refuses the dangerous values unless
``--force`` is passed on the same invocation.

A blocked response is also remembered between runs, so a loop that keeps
hitting exit code 3 does not keep hitting Zillow. The state file is opt-in
via ``PYZILL_BLOCK_STATE``; unset means "do not remember blocks".
"""

import json
import os
import time
from pathlib import Path
from typing import Any

__all__ = ["MAX_REQUESTS_CAP", "MIN_RATE_LIMIT", "clear_block_state", "read_block_state", "write_block_state"]

MIN_RATE_LIMIT = 1.0
MAX_REQUESTS_CAP = 50
_BLOCK_STATE_ENV = "PYZILL_BLOCK_STATE"


def _block_state_path() -> Path | None:
    raw = os.environ.get(_BLOCK_STATE_ENV)
    if raw is None or not raw.strip():
        return None
    return Path(raw).expanduser()


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


def record_block(code: str, *, retryable: bool, status_code: int | None = None) -> dict[str, Any] | None:
    """Persist that Zillow blocked us, if block memory is enabled.

    Returns the state written (for tests), or None when blocking is disabled
    (no ``PYZILL_BLOCK_STATE`` was set, so nothing was persisted).
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
