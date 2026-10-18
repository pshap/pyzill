"""Tests for the fixture capture script and captured Zillow fixtures.

Offline tests cover the script's sanitize/trim/gating logic. The validation
tests below only run when a fixture has actually been captured into
tests/fixtures/ (via scripts/capture_fixture.py); until then they skip, so
the suite stays green without live data.
"""

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from pyzill.cli.search import extract_listings
from pyzill.normalize import normalize_listings

FIXTURES = Path(__file__).parent / "fixtures"
_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "capture_fixture.py"


@pytest.fixture(scope="module")
def capture() -> ModuleType:
    """Load scripts/capture_fixture.py as a module (scripts/ is not a package)."""
    spec = importlib.util.spec_from_file_location("capture_fixture", _SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestSanitize:
    def test_redacts_contact_fields_recursively(self, capture: ModuleType) -> None:
        payload = {
            "cat1": {
                "mapResults": [
                    {
                        "zpid": 1,
                        "price": 100_000,
                        "agent": {"name": "Jane Doe", "phone": "555-0100"},
                        "brokerName": "Acme Realty",
                        "mlsNumber": "ML123",
                        "attributionInfo": "see mls",
                        "address": {"city": "Seattle", "state": "WA"},
                    }
                ]
            }
        }

        listing = capture.sanitize(payload)["cat1"]["mapResults"][0]

        assert listing["zpid"] == 1
        assert listing["price"] == 100_000
        assert listing["address"] == {"city": "Seattle", "state": "WA"}
        assert listing["agent"] == {"name": "[redacted]", "phone": "[redacted]"}
        assert listing["brokerName"] == "[redacted]"
        assert listing["mlsNumber"] == "[redacted]"
        assert listing["attributionInfo"] == "[redacted]"

    def test_leaves_non_matching_values_alone(self, capture: ModuleType) -> None:
        payload = [{"zpid": 2, "homeType": "SINGLE_FAMILY", "emailDomainHint": None}]
        assert capture.sanitize(payload) == payload


class TestTrim:
    def test_caps_result_lists_recursively(self, capture: ModuleType) -> None:
        payload = {
            "cat1": {"mapResults": [{"zpid": i} for i in range(10)]},
            "listResults": [{"zpid": i} for i in range(10)],
            "other": [{"zpid": i} for i in range(10)],  # not a result list: untouched
        }

        trimmed = capture.trim(payload, 3)

        assert len(trimmed["cat1"]["mapResults"]) == 3
        assert len(trimmed["listResults"]) == 3
        assert len(trimmed["other"]) == 10


class TestMainGating:
    def test_refuses_without_confirm(self, capture: ModuleType, capsys: pytest.CaptureFixture[str]) -> None:
        assert capture.main(["search-sale"]) == 2
        assert "--confirm" in capsys.readouterr().err

    def test_detail_kinds_require_target(self, capture: ModuleType) -> None:
        with pytest.raises(SystemExit) as excinfo:
            capture.main(["detail-home", "--confirm"])
        assert excinfo.value.code == 2

    def test_search_writes_sanitized_fixture(
        self, capture: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            capture,
            "fetch_payload",
            lambda kind, target, bbox: {
                "mapResults": [{"zpid": i, "price": 100_000 + i, "agentName": "Jane"} for i in range(10)]
            },
        )

        rc = capture.main(["search-sale", "--confirm", "--out", str(tmp_path), "--max-results", "2"])

        assert rc == 0
        written = json.loads((tmp_path / "search_sale.json").read_text())
        assert len(written["mapResults"]) == 2
        assert written["mapResults"][0]["agentName"] == "[redacted]"
        assert written["mapResults"][0]["zpid"] == 0

    def test_failed_fetch_returns_1(self, capture: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(kind: str, target: str | None, bbox: object) -> None:
            raise RuntimeError("blocked")

        monkeypatch.setattr(capture, "fetch_payload", boom)
        assert capture.main(["search-sale", "--confirm", "--out", str(tmp_path)]) == 1


def _load_fixture(name: str) -> Any | None:
    path = FIXTURES / name
    if not path.exists():
        pytest.skip(f"{name} not captured yet; run scripts/capture_fixture.py --confirm")
    return json.loads(path.read_text())


@pytest.mark.parametrize("name", ["search_sale.json", "search_rent.json", "search_sold.json"])
def test_search_fixture_extracts_and_normalizes(name: str) -> None:
    """A captured search fixture must produce normalized listings with zpids.

    Rent fixtures are only structurally checked: whether rental mapResults
    contain building-only entries needing unit expansion is an open question
    (see CHANGELOG Unverified), so no behavioral assertion is made here.
    """
    payload = _load_fixture(name)
    listings = normalize_listings(extract_listings(payload))
    assert listings, f"{name} produced no listings"
    assert all(isinstance(listing["zpid"], int) for listing in listings)


@pytest.mark.parametrize("name", ["detail_home.json", "detail_apartment.json"])
def test_detail_fixture_is_a_populated_object(name: str) -> None:
    payload = _load_fixture(name)
    assert isinstance(payload, dict)
    assert payload, f"{name} is empty"
