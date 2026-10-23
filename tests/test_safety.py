"""Tests for the CLI safety floors and persisted block memory."""

from pathlib import Path

import pytest

from pyzill.cli.safety import (
    MAX_REQUESTS_CAP,
    MIN_RATE_LIMIT,
    clear_block_state,
    read_block_state,
    record_block,
    write_block_state,
)


def test_constants_match_the_agent_safety_floor():
    assert MIN_RATE_LIMIT == 1.0
    assert MAX_REQUESTS_CAP == 50


def test_read_block_state_is_none_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PYZILL_BLOCK_STATE", raising=False)
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


def test_record_block_is_a_noop_without_state_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PYZILL_BLOCK_STATE", raising=False)
    assert record_block("blocked", retryable=False) is None


def test_read_block_state_treats_garbage_as_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "block.json"
    path.write_text("not json", encoding="utf-8")
    monkeypatch.setenv("PYZILL_BLOCK_STATE", str(path))
    assert read_block_state() is None
