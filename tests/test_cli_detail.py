"""CLI tests: detail."""

import json

import pytest

from pyzill.cli import app
from tests.cli_helpers import runner, write_json

pytestmark = pytest.mark.usefixtures("_stub_geocode")


class TestDetailCommands:
    """Test detail command routing, options, and exploration flags."""

    @pytest.mark.parametrize(
        "target",
        [
            "348914888",
            "https://www.zillow.com/homedetails/27-Washington-Ave/348914888_zpid/",
        ],
    )
    def test_detail_home_accepts_an_id_or_a_url(self, fake_http, home_detail_body, target):
        fake_http.install("GET", content=home_detail_body)
        result = runner.invoke(app, ["detail", "home", target])
        assert result.exit_code == 0

    @pytest.mark.parametrize(
        "target",
        [
            "CgKZT4",
            "https://www.zillow.com/apartments/kissimmee-fl/the-nexus-at-overbrook/9DSWrh/",
        ],
    )
    def test_detail_apartment_accepts_an_id_or_a_url(self, fake_http, department_detail_body, target):
        fake_http.install("GET", content=department_detail_body)
        result = runner.invoke(app, ["detail", "apartment", target])
        assert result.exit_code == 0

    def test_detail_apartment_rejects_non_zillow_url_as_usage_error(self, fake_http):
        fake_http.install("GET")
        result = runner.invoke(app, ["detail", "apartment", "https://example.com/listing"])
        assert result.exit_code == 2
        assert fake_http.calls == []

    def test_detail_apartment_encodes_id_in_url(self, fake_http, department_detail_body):
        fake_http.install("GET", content=department_detail_body)
        result = runner.invoke(app, ["detail", "apartment", "Cg KZT4"])
        assert result.exit_code == 0
        assert fake_http.calls[-1]["url"] == "https://www.zillow.com/apartments/Cg%20KZT4/"

    def test_detail_requires_url_or_id(self):
        result = runner.invoke(app, ["detail", "home"])
        assert result.exit_code == 2

    @pytest.mark.parametrize("value", ["garbage", "https://example.com/listing"])
    def test_detail_home_rejects_invalid_input(self, value, fake_http):
        fake_http.install("GET")
        result = runner.invoke(app, ["detail", "home", value])
        assert result.exit_code == 2
        assert fake_http.calls == []

    def test_detail_parse_failure_has_schema_guidance(self, fake_http):
        fake_http.install("GET", content=b"<html>unexpected page</html>")

        result = runner.invoke(app, ["detail", "home", "123"])

        assert result.exit_code == 5
        assert "schema may have changed" in result.stderr
        assert "blocked the request" not in result.stderr

    @pytest.mark.parametrize(("option", "value"), [("--max-retries", "-1"), ("--timeout", "0")])
    def test_global_request_limits_reject_invalid_values(self, option, value):
        result = runner.invoke(app, [option, value, "detail", "home", "123"])
        assert result.exit_code == 2

    def test_timeout_zero_honors_json_errors(self):
        # stdout is captured (not a TTY), so errors default to JSON.
        result = runner.invoke(app, ["--timeout", "0", "detail", "home", "123"])
        assert result.exit_code == 2
        payload = json.loads(result.stderr)
        assert payload["error"]["code"] == "usage"
        assert "--timeout" in payload["error"]["message"]

    def test_detail_rejects_non_json_format(self, fake_http, home_detail_body):
        fake_http.install("GET", content=home_detail_body)
        result = runner.invoke(app, ["detail", "home", "348914888", "--format", "csv"])
        assert result.exit_code == 2
        assert "only support JSON" in result.stderr

    def test_keys_lists_fields(self, fake_http, home_detail_body):
        fake_http.install("GET", content=home_detail_body)
        result = runner.invoke(app, ["detail", "home", "348914888", "--keys"])
        assert result.exit_code == 0
        assert "zpid" in result.stdout
        assert "address" in result.stdout

    def test_path_extracts_nested_value(self, fake_http, home_detail_body):
        fake_http.install("GET", content=home_detail_body)
        result = runner.invoke(app, ["detail", "home", "348914888", "--path", "address.city"])
        assert result.exit_code == 0
        assert json.loads(result.stdout) == "Schenectady"

    def test_path_list_index(self, fake_http):
        body = {
            "props": {
                "pageProps": {
                    "componentProps": {
                        "gdpClientCache": json.dumps(
                            {"cache": {"property": {"zpid": 1, "tags": [{"name": "first"}, {"name": "second"}]}}}
                        )
                    }
                }
            }
        }
        html = (
            "<html><head></head><body>"
            f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(body)}</script>'
            "</body></html>"
        )
        fake_http.install("GET", content=html.encode())
        result = runner.invoke(app, ["detail", "home", "1", "--path", "tags[1].name"])
        assert result.exit_code == 0
        assert json.loads(result.stdout) == "second"

    def test_path_not_found_is_clean_error(self, fake_http, home_detail_body):
        fake_http.install("GET", content=home_detail_body)
        result = runner.invoke(app, ["detail", "home", "348914888", "--path", "nope.deep"])
        assert result.exit_code == 1
        assert "not found" in result.stderr

    def test_keys_and_path_conflict(self, fake_http, home_detail_body):
        fake_http.install("GET", content=home_detail_body)
        result = runner.invoke(app, ["detail", "home", "348914888", "--keys", "--path", "address"])
        assert result.exit_code == 2

    def test_keys_respects_output_file(self, fake_http, home_detail_body, tmp_path):
        fake_http.install("GET", content=home_detail_body)
        out = tmp_path / "keys.txt"
        result = runner.invoke(app, ["detail", "home", "348914888", "--keys", "-o", str(out)])
        assert result.exit_code == 0
        assert "zpid" in out.read_text()


class TestNormalizeCommand:
    """Test normalize command."""

    def test_normalize_from_file(self, normalize_file, search_success_body):
        result = normalize_file(search_success_body)
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert isinstance(data, list)

    def test_normalize_from_stdin(self, search_success_body):
        result = runner.invoke(app, ["normalize", "-"], input=json.dumps(search_success_body))
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert isinstance(data, list)

    @pytest.mark.parametrize(
        "payload",
        [
            {"mapResults": [{"zpid": "1", "address": "test"}]},
            {"listResults": [{"zpid": "1", "address": "test"}]},
            [{"zpid": "1", "address": "test"}],
            {"zpid": "1", "address": "test"},
        ],
    )
    def test_normalize_accepts_every_listing_shape(self, normalize_file, payload):
        result = normalize_file(payload)
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert isinstance(data, list)
        assert len(data) == 1

    def test_normalize_rejects_invalid_json(self, tmp_path):
        input_file = tmp_path / "invalid.json"
        input_file.write_text("not json")

        result = runner.invoke(app, ["normalize", str(input_file)])
        assert result.exit_code == 2
        assert "Invalid JSON" in result.stderr

    def test_normalize_rejects_a_payload_that_is_neither_object_nor_array(self, tmp_path):
        input_file = write_json(tmp_path / "input.json", "string")

        result = runner.invoke(app, ["normalize", str(input_file)])
        assert result.exit_code == 2
        assert "must be a JSON object or array" in result.stderr

    def test_normalize_rejects_missing_file(self):
        result = runner.invoke(app, ["normalize", "/nonexistent/file.json"])
        assert result.exit_code == 2
        assert "not found" in result.stderr
