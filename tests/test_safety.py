"""Tests for the CLI safety floors and persisted block memory."""

import json
from pathlib import Path

import pytest

from pyzill.cli.safety import (
    DEFAULT_MAX_REQUESTS_CAP,
    DEFAULT_MIN_RATE_LIMIT,
    clear_block_state,
    cooldown_for,
    cooldown_until,
    is_in_cooldown,
    max_requests_cap,
    min_rate_limit,
    read_block_state,
    record_block,
    seconds_until_unblock,
    write_block_state,
)


def test_constants_match_the_agent_safety_floor():
    assert DEFAULT_MIN_RATE_LIMIT == 1.0
    assert DEFAULT_MAX_REQUESTS_CAP == 50


def test_floors_are_dynamic_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PYZILL_MIN_RATE_LIMIT_FLOOR", "0.5")
    monkeypatch.setenv("PYZILL_MAX_REQUESTS_CEILING", "200")
    assert min_rate_limit() == 0.5
    assert max_requests_cap() == 200


def test_read_block_state_is_default_on_when_unset(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("PYZILL_BLOCK_STATE", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert read_block_state() is None


def test_record_block_writes_and_reads_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PYZILL_BLOCK_STATE", str(tmp_path / "block.json"))
    state = record_block("blocked", retryable=False, status_code=403)
    assert state is not None
    assert state["blocks"][-1]["code"] == "blocked"
    assert state["blocks"][-1]["retryable"] is False
    assert state["blocks"][-1]["status_code"] == 403
    assert "recorded_at" in state["blocks"][-1]
    assert read_block_state() == state


def test_record_block_appends(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PYZILL_BLOCK_STATE", str(tmp_path / "block.json"))
    record_block("blocked", retryable=True)
    record_block("server_error", retryable=True, status_code=503)
    state = read_block_state()
    assert state is not None
    assert [entry["code"] for entry in state["blocks"]] == ["blocked", "server_error"]


def test_clear_block_state_removes_the_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "block.json"
    monkeypatch.setenv("PYZILL_BLOCK_STATE", str(path))
    write_block_state({"blocks": []})
    assert path.exists()
    clear_block_state()
    assert not path.exists()


def test_record_block_writes_default_state_when_unset(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("PYZILL_BLOCK_STATE", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    state = record_block("blocked", retryable=False, status_code=403)
    assert state is not None
    assert (tmp_path / "pyzill" / "block.json").exists()


def test_block_state_disabled_when_off(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = tmp_path / "block.json"
    monkeypatch.setenv("PYZILL_BLOCK_STATE", "off")
    state = record_block("blocked", retryable=False)
    assert state is None
    assert read_block_state() is None
    assert not path.exists()


def test_read_block_state_treats_garbage_as_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "block.json"
    path.write_text("not json", encoding="utf-8")
    monkeypatch.setenv("PYZILL_BLOCK_STATE", str(path))
    assert read_block_state() is None


def test_cooldown_ladder_and_active_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "block.json"
    monkeypatch.setenv("PYZILL_BLOCK_STATE", str(path))
    assert cooldown_for() == 0
    assert is_in_cooldown() is False
    assert seconds_until_unblock() == 0
    assert cooldown_until() is None

    record_block("blocked", retryable=False)
    assert cooldown_for() == 900
    assert is_in_cooldown() is True
    assert seconds_until_unblock() > 0
    assert cooldown_until() is not None

    # expired cooldown (recorded far in the past) is not active
    state = json.loads(path.read_text())
    state["blocks"][0]["recorded_at"] = "2000-01-01T00:00:00Z"
    path.write_text(json.dumps(state))
    assert is_in_cooldown() is False
    assert seconds_until_unblock() == 0
    assert cooldown_until() is None
