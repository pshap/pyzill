"""CLI tests: geocode."""

import json

import pytest

from pyzill.bbox import BBox
from pyzill.cli import app
from pyzill.geocode import GeocodeError, GeoResult
from tests.cli_helpers import _GEO_STUB, runner

pytestmark = pytest.mark.usefixtures("_stub_geocode")


class TestGeocode:
    """The geocode command and the viewport a search resolves."""

    def test_no_bbox_geocodes_query(self, fake_http, search_success_body):
        fake_http.install("PUT", json_data=search_success_body)
        result = runner.invoke(app, ["search", "sale", "Schenectady NY"])
        assert result.exit_code == 0
        assert _GEO_STUB.calls == ["Schenectady NY"]

    def test_explicit_bbox_skips_geocode(self, fake_http, search_success_body):
        fake_http.install("PUT", json_data=search_success_body)
        result = runner.invoke(
            app,
            ["search", "sale", "12305", "--north", "42.85", "--south", "42.78", "--east", "-73.9", "--west", "-74.0"],
        )
        assert result.exit_code == 0
        assert _GEO_STUB.calls == []

    def test_geocode_failure_is_clean_error(self):
        _GEO_STUB.error = GeocodeError("Geocoding 'nowhere' failed: DNS error. Pass --north/--south/--east/--west.")
        result = runner.invoke(app, ["search", "sale", "nowhere"])
        assert result.exit_code == 1
        assert "Geocoding 'nowhere' failed" in result.stderr
        assert "--east/--west" in result.stderr.replace("\n", "")

    def test_partial_bbox_still_usage_error(self):
        result = runner.invoke(app, ["search", "sale", "12305", "--north", "42.85"])
        assert result.exit_code == 2

    def test_geocode_prints_the_resolved_viewport(self, patch_geocode):
        calls = patch_geocode(
            GeoResult(
                display_name="Schenectady, NY, USA",
                bbox=BBox(north=42.9, south=42.6, east=-73.8, west=-74.1),
            )
        )

        result = runner.invoke(app, ["geocode", "Schenectady NY"])

        assert result.exit_code == 0
        assert calls == ["Schenectady NY"]
        payload = json.loads(result.stdout)
        assert payload["display_name"] == "Schenectady, NY, USA"
        assert payload["north"] == 42.9
        assert payload["south"] == 42.6
        assert payload["east"] == -73.8
        assert payload["west"] == -74.1

    def test_geocode_rejects_an_empty_place(self):
        result = runner.invoke(app, ["geocode", "  "])

        assert result.exit_code == 2
        assert "Place name must not be empty" in result.stderr

    def test_geocode_rejects_a_format_it_cannot_render(self, patch_geocode):
        patch_geocode(GeoResult(display_name="x", bbox=BBox(north=1, south=0, east=1, west=0)))

        result = runner.invoke(app, ["geocode", "Schenectady NY", "--format", "table"])

        assert result.exit_code == 2
        assert "only supports JSON format" in result.stderr

    def test_geocode_reports_an_unreachable_service_as_retryable(self, patch_geocode):
        from pyzill.geocode import GeocodeUnavailableError

        patch_geocode(error=GeocodeUnavailableError("the service is down"))

        result = runner.invoke(app, ["--json-errors", "geocode", "Schenectady NY"])

        assert result.exit_code == 6
        error_payload = json.loads(result.stderr)["error"]
        assert error_payload["retryable"] is True

    def test_geocode_reports_an_unresolved_place_as_an_error(self, patch_geocode):
        patch_geocode(error=GeocodeError("no such place"))

        result = runner.invoke(app, ["--json-errors", "geocode", "Nowhere"])

        assert result.exit_code == 1
        error_payload = json.loads(result.stderr)["error"]
        assert error_payload["retryable"] is False
