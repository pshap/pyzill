"""Shared builders and stubs for the CLI tests."""

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from pyzill.bbox import BBox
from pyzill.cli import app
from pyzill.geocode import GeoResult

runner = CliRunner()


def search_body(*listings: dict[str, Any], **extra: Any) -> dict[str, Any]:
    """Wrap listing rows in the envelope a Zillow search response uses.

    ``extra`` adds the sibling result lists a response may carry, such as
    ``listResults``.
    """
    results: dict[str, Any] = {"mapResults": list(listings)}
    results.update(extra)
    return {"cat1": {"searchResults": results}}


def empty_body() -> dict[str, Any]:
    """A search response that holds no listings."""
    return search_body()


def capped_body(count: int = 500) -> dict[str, Any]:
    """A search response with enough rows to hit Zillow's 500-row request cap."""
    return search_body(*({"zpid": str(index)} for index in range(count)))


def write_json(path: Path, data: Any) -> Path:
    """Write data as JSON and return the path."""
    path.write_text(json.dumps(data))
    return path


def run_module(*args: str) -> subprocess.CompletedProcess[str]:
    """Run the CLI as a module, the way a user would from a shell."""
    return subprocess.run(  # noqa: S603 - the command is this test's own argv
        [sys.executable, "-m", "pyzill", *args],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
    )


def run_query(path: Path | str, *args: str) -> list[dict[str, Any]]:
    """Run the offline query command and return the rows it selected."""
    result = runner.invoke(app, ["query", str(path), *args])
    assert result.exit_code == 0, result.stderr
    return json.loads(result.stdout)


class _GeoStub:
    """Recording stand-in for pyzill.cli.search.geocode (installed autouse)."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.kwargs: list[dict[str, object]] = []
        self.result = GeoResult(
            display_name="Testville",
            bbox=BBox(north=42.9, south=42.7, east=-73.8, west=-74.0),
        )
        self.error: Exception | None = None

    def __call__(self, place: str, **kwargs: object) -> GeoResult:
        self.calls.append(place)
        self.kwargs.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.result

    def reset(self) -> None:
        self.calls.clear()
        self.kwargs.clear()
        self.error = None


_GEO_STUB = _GeoStub()


class _FakeConsoleContext:
    """Minimal stand-in for ``pyzill.cli.console.Context`` with a capture console."""

    def __init__(self, stdout_console) -> None:
        self.stdout_console = stdout_console
