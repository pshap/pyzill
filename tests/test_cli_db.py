"""CLI tests: db."""

import json
import sys
from typing import Any

import pytest

from pyzill.cli import app
from pyzill.db import begin_sync_run, connect, finish_sync_run
from tests.cli_helpers import _FakeConsoleContext, empty_body, runner, search_body, write_json

pytestmark = pytest.mark.usefixtures("_stub_geocode")


class TestDatabaseCommands:
    """Test db sub-app commands."""

    def test_db_query_empty_result_emits_json_array(self, tmp_path):
        """An empty match must still produce valid JSON on stdout."""
        db_path = tmp_path / "empty.db"
        input_file = write_json(tmp_path / "input.json", [{"zpid": 1, "price": 100000}])

        assert runner.invoke(app, ["db", "import", str(input_file), "--db", str(db_path)]).exit_code == 0

        result = runner.invoke(app, ["db", "query", "--db", str(db_path), "--where", "price>999999999"])

        assert result.exit_code == 0
        assert json.loads(result.stdout) == []

    def test_db_query_empty_result_warns_for_human_output(self, tmp_path):
        db_path = tmp_path / "empty.db"
        input_file = write_json(tmp_path / "input.json", [{"zpid": 1, "price": 100000}])

        assert runner.invoke(app, ["db", "import", str(input_file), "--db", str(db_path)]).exit_code == 0

        result = runner.invoke(app, ["db", "query", "--db", str(db_path), "--where", "price>999999999", "-f", "table"])

        assert result.exit_code == 0
        assert "No listings matched the query" in result.stderr

    def test_db_sync_rejects_empty_query(self, tmp_path):
        """An empty query would make a search run look like a file import."""
        result = runner.invoke(app, ["db", "sync", "sale", "", "--db", str(tmp_path / "q.db")])

        assert result.exit_code == 2
        assert "Query must not be empty" in result.stderr

    def test_db_runs_lists_the_recorded_runs(self, tmp_path):
        """A run id handed out by a sync must be resolvable afterwards."""
        db_path = tmp_path / "runs.db"
        conn = connect(db_path)
        run_id = begin_sync_run(
            conn,
            kind="sale",
            query="Albany",
            bbox={"north": 43.0, "south": 42.0},
            filters={"max_price": 500000},
            started_utc="2026-10-01T00:00:00Z",
        )
        finish_sync_run(conn, run_id, result_count=7, finished_utc="2026-10-01T00:01:00Z")
        conn.commit()
        conn.close()

        result = runner.invoke(app, ["db", "runs", "--db", str(db_path)])

        assert result.exit_code == 0
        runs = json.loads(result.stdout)
        assert len(runs) == 1
        assert runs[0]["id"] == run_id
        assert runs[0]["kind"] == "sale"
        assert runs[0]["query"] == "Albany"
        assert runs[0]["result_count"] == 7
        assert runs[0]["truncated"] is False
        assert runs[0]["bbox"] == {"north": 43.0, "south": 42.0}
        assert runs[0]["filters"] == {"max_price": 500000}

    def test_db_runs_filters_by_kind(self, tmp_path):
        db_path = tmp_path / "runs.db"
        conn = connect(db_path)
        begin_sync_run(conn, kind="sale", query="Albany", started_utc="2026-10-01T00:00:00Z")
        begin_sync_run(conn, kind="rent", query="Albany", started_utc="2026-10-02T00:00:00Z")
        conn.commit()
        conn.close()

        result = runner.invoke(app, ["db", "runs", "--db", str(db_path), "--kind", "rent"])

        assert result.exit_code == 0
        runs = json.loads(result.stdout)
        assert [run["kind"] for run in runs] == ["rent"]

    def test_db_sync_zip_only_stores_listings_in_requested_zip(self, run_sync, tmp_path):
        db_path = tmp_path / "zip.db"

        result = run_sync(
            search_body(
                {"zpid": 1, "addressZipcode": "12305", "price": 100000},
                {"zpid": 2, "addressZipcode": "12309", "price": 200000},
                {"zpid": 3, "price": 300000},
            ),
            db_path=db_path,
        )

        assert result.exit_code == 0
        conn = connect(db_path)
        assert conn.execute("SELECT zpid FROM listings ORDER BY zpid").fetchall() == [(1,)]
        assert conn.execute("SELECT result_count FROM sync_runs").fetchone() == (1,)
        conn.close()

    def test_db_sync_zip_selects_results_containing_requested_zip(self, run_sync, tmp_path):
        db_path = tmp_path / "zip.db"

        result = run_sync(
            search_body(
                {"zpid": 1, "addressZipcode": "12305"},
                listResults=[
                    {"zpid": 2, "addressZipcode": "12309"},
                    {"zpid": 3, "addressZipcode": "12309"},
                ],
            ),
            db_path=db_path,
        )

        assert result.exit_code == 0
        conn = connect(db_path)
        assert conn.execute("SELECT zpid FROM listings ORDER BY zpid").fetchall() == [(1,)]
        assert conn.execute("SELECT result_count FROM sync_runs").fetchone() == (1,)
        conn.close()

    def test_db_sync_creates_database(self, run_sync, search_success_body, tmp_path):
        db_path = tmp_path / "test.db"
        result = run_sync(search_success_body, db_path=db_path)
        assert result.exit_code == 0
        assert db_path.exists()
        assert "Synced 2 listings: 2 new, 0 updated (0 price changes), 0 skipped" in result.stderr

    def test_db_sync_json_summary(self, run_sync, search_success_body, tmp_path):
        result = run_sync(search_success_body, "--format", "json", db_path=tmp_path / "summary.db")

        assert result.exit_code == 0
        assert json.loads(result.stdout) == {
            "action": "synced",
            "total": 2,
            "added": 2,
            "updated": 0,
            "price_changes": 0,
            "skipped": 0,
            "run_id": 1,
            "truncated": False,
        }

    def test_db_import_json_summary(self, tmp_path):
        db_path = tmp_path / "summary.db"
        source = write_json(tmp_path / "rows.json", [{"zpid": 1, "price": 100}])

        result = runner.invoke(app, ["db", "import", str(source), "--format", "json", "--db", str(db_path)])

        assert result.exit_code == 0
        assert json.loads(result.stdout) == {
            "action": "imported",
            "total": 1,
            "added": 1,
            "updated": 0,
            "price_changes": 0,
            "skipped": 0,
            "run_id": 1,
            "truncated": False,
        }
        assert db_path.exists()

    def test_db_import_accepts_kind(self, tmp_path):
        rows_file = write_json(tmp_path / "rent.json", [{"zpid": 1, "price": 2200}])
        db_path = tmp_path / "rent.db"

        result = runner.invoke(app, ["db", "import", str(rows_file), "--kind", "rent", "--db", str(db_path)])

        assert result.exit_code == 0
        conn = connect(db_path)
        assert conn.execute("SELECT kind FROM listings WHERE zpid = 1").fetchone()[0] == "rent"
        assert conn.execute("SELECT kind FROM price_history WHERE zpid = 1").fetchone()[0] == "rent"
        conn.close()

    def test_db_import_rejects_a_payload_that_is_not_a_listing_array(self, tmp_path):
        """A payload that is not an object or an array is a usage error."""
        input_file = write_json(tmp_path / "scalar.json", "hello")

        result = runner.invoke(
            app,
            ["db", "import", str(input_file), "--db", str(tmp_path / "test.db")],
        )

        assert result.exit_code == 2
        assert "Traceback" not in result.stderr

    def test_db_sync_blocked_request_reports_the_error_not_a_traceback(self, fake_http, tmp_path):
        """A blocked request must be a stable exit code, not an unhandled error.

        A script has to be able to tell "Zillow blocked us" from "the tool
        has a bug", so the sync command reports the library failures the
        same way the search command does.
        """
        fake_http.install("PUT", status_code=403, text="blocked")

        result = runner.invoke(
            app,
            ["--json-errors", "db", "sync", "sale", "12305", "--db", str(tmp_path / "blocked.db")],
        )

        assert result.exit_code == 3
        payload = json.loads(result.stderr)
        assert payload["error"]["code"] == "blocked"
        assert payload["error"]["status_code"] == 403

    def test_db_sync_rejects_a_format_it_cannot_render_before_it_searches(self, fake_http, tmp_path):
        """The format check must not depend on whether the search found rows."""
        fake_http.install("PUT", status_code=403, text="blocked")

        result = runner.invoke(
            app,
            ["db", "sync", "sale", "12305", "--format", "csv", "--db", str(tmp_path / "test.db")],
        )

        assert result.exit_code == 2

    def test_db_import_rejects_a_format_it_cannot_render_on_an_empty_file(self, tmp_path):
        """An empty file must not skip the format check a full file gets."""
        rows_file = write_json(tmp_path / "empty.json", [])

        result = runner.invoke(
            app,
            ["db", "import", str(rows_file), "--format", "csv", "--db", str(tmp_path / "test.db")],
        )

        assert result.exit_code == 2

    def test_db_sync_json_summary_reports_a_truncated_run(self, run_sync, search_success_body, tmp_path):
        """A script must be able to tell a complete run from a partial one."""
        db_path = tmp_path / "truncated.db"
        first = run_sync(search_success_body, db_path=db_path)
        assert first.exit_code == 0

        # Same query/bbox/filters, now empty: a soft block, not a real clear-out.
        result = run_sync(empty_body(), "--format", "json", db_path=db_path)

        assert result.exit_code == 0
        summary = json.loads(result.stdout)
        assert summary["truncated"] is True
        assert summary["run_id"] > 0

    def test_db_sync_empty_results_warns(self, run_sync, tmp_path):
        db_path = tmp_path / "test.db"
        result = run_sync(empty_body(), db_path=db_path)
        assert result.exit_code == 0
        assert "No listings found" in result.stderr
        conn = connect(db_path)
        assert conn.execute("SELECT result_count, truncated FROM sync_runs").fetchone() == (0, 0)
        conn.close()

    def test_db_sync_empty_after_prior_results_is_truncated(self, run_sync, search_success_body, tmp_path):
        db_path = tmp_path / "test.db"
        first = run_sync(search_success_body, db_path=db_path)
        assert first.exit_code == 0

        # Same query/bbox/filters, now empty: a soft block, not a real clear-out.
        second = run_sync(empty_body(), db_path=db_path)
        assert second.exit_code == 0
        assert "truncated" in second.stderr

        conn = connect(db_path)
        runs = conn.execute("SELECT result_count, truncated FROM sync_runs ORDER BY id").fetchall()
        conn.close()
        assert runs[0] == (2, 0)
        assert runs[-1] == (0, 1)

    def test_db_sync_confirm_empty_reports_delistings(self, run_sync, search_success_body, tmp_path):
        from pyzill.db import get_changes

        db_path = tmp_path / "confirmed_empty.db"
        assert run_sync(search_success_body, db_path=db_path).exit_code == 0

        assert run_sync(empty_body(), db_path=db_path).exit_code == 0
        confirmed = run_sync(empty_body(), "--confirm-empty", db_path=db_path)

        assert confirmed.exit_code == 0
        assert "truncated" not in confirmed.stderr
        conn = connect(db_path)
        assert conn.execute("SELECT result_count, truncated FROM sync_runs ORDER BY id").fetchall() == [
            (2, 0),
            (0, 1),
            (0, 0),
        ]
        changes = get_changes(conn, "2000-01-01T00:00:00Z")
        assert {event["zpid"] for event in changes if event["event"] == "delisted"} == {348914888, 348914889}
        conn.close()

    def test_db_sync_re_sync_updates_last_seen(self, run_sync, search_success_body, tmp_path):
        db_path = tmp_path / "test.db"

        # First sync
        result1 = run_sync(search_success_body, db_path=db_path)
        assert result1.exit_code == 0
        assert "new" in result1.stderr

        # Re-sync (same data)
        result2 = run_sync(search_success_body, db_path=db_path)
        assert result2.exit_code == 0
        assert "updated" in result2.stderr

    def test_db_sync_price_change_detection(self, fake_http, tmp_path, monkeypatch):
        # Pin the sync clock so the later --fetched-at import is always newer.
        # The sync run's timestamp is taken by the workflow layer, which owns
        # the run lifecycle.
        monkeypatch.setattr("pyzill.workflows.sync.utc_now", lambda: "2026-10-19T10:00:00Z")

        # First sync with price 250000
        body1 = search_body({"zpid": "123", "price": 250000, "addressZipcode": "12305"})
        fake_http.install("PUT", json_data=body1)
        db_path = tmp_path / "test.db"

        result1 = runner.invoke(app, ["db", "sync", "sale", "12305", "--db", str(db_path)])
        assert result1.exit_code == 0

        # Import instead of sync to control timestamp
        rows = [{"zpid": "123", "price": 260000, "address": "test"}]
        input_file = write_json(tmp_path / "input.json", rows)
        result2 = runner.invoke(
            app, ["db", "import", str(input_file), "--fetched-at", "2026-10-19T12:00:00Z", "--db", str(db_path)]
        )
        assert result2.exit_code == 0
        assert "1 price changes" in result2.stderr

    def test_db_sync_invalid_kind(self, fake_http, search_success_body):
        fake_http.install("PUT", json_data=search_success_body)
        result = runner.invoke(app, ["db", "sync", "invalid", "12305"])
        assert result.exit_code == 2
        assert "sale" in result.stderr
        assert "rent" in result.stderr
        assert "sold" in result.stderr

    def test_db_sync_room_with_non_rent(self, fake_http, search_success_body):
        fake_http.install("PUT", json_data=search_success_body)
        result = runner.invoke(app, ["db", "sync", "sale", "12305", "--room"])
        assert result.exit_code == 2
        assert "only work with 'rent' kind" in result.stderr

    def test_db_import_raw_response(self, db_rows, tmp_path):
        body = search_body({"zpid": "123", "price": 250000, "address": "test"})
        input_file = write_json(tmp_path / "input.json", body)
        db_path = tmp_path / "test.db"

        result = runner.invoke(app, ["db", "import", str(input_file), "--db", str(db_path)])
        assert result.exit_code == 0
        assert "Imported 1 listings: 1 new, 0 updated (0 price changes), 0 skipped" in result.stderr
        assert db_path.exists()

        # The nested response envelope must yield its mapResults rows, not be
        # treated as a single bogus listing.
        data = db_rows(db_path)
        assert len(data) == 1
        assert data[0]["zpid"] == 123
        assert data[0]["price"] == 250000

    def test_db_import_normalizes_raw_rows(self, db_rows, tmp_path):
        body = search_body(
            {
                "zpid": "32463997",
                "price": "$459,000",
                "address": "1230 State St, Schenectady, NY 12305",
                "detailUrl": "/homes/1230-State-St.html",
                "imgSrc": "https://img.example/x.jpg",
                "statusText": "House for sale",
            }
        )
        input_file = write_json(tmp_path / "input.json", body)
        db_path = tmp_path / "test.db"

        result = runner.invoke(app, ["db", "import", str(input_file), "--db", str(db_path)])
        assert result.exit_code == 0

        data = db_rows(db_path)
        assert len(data) == 1
        row = data[0]
        assert row["price"] == 459000
        assert row["detail_url"] == "https://www.zillow.com/homes/1230-State-St.html"
        assert row["image_url"] == "https://img.example/x.jpg"
        assert row["status"] == "House for sale"

    def test_db_import_preserves_normalized_fields(self, db_rows, tmp_path):
        # Rows exported by `db query` carry the timestamp columns; they must be
        # imported as-is rather than re-normalized (which would null out city).
        rows = [
            {
                "zpid": 123,
                "price": 250000,
                "address": "123 Main St",
                "city": "Schenectady",
                "first_seen_utc": "2026-10-01T12:00:00Z",
                "last_seen_utc": "2026-10-01T12:00:00Z",
            }
        ]
        input_file = write_json(tmp_path / "input.json", rows)
        db_path = tmp_path / "test.db"

        result = runner.invoke(app, ["db", "import", str(input_file), "--db", str(db_path)])
        assert result.exit_code == 0
        assert "Imported 1 listings: 1 new" in result.stderr

        assert db_rows(db_path)[0]["city"] == "Schenectady"

    def test_db_import_same_timestamp_price_change(self, tmp_path):
        db_path = tmp_path / "test.db"
        first = write_json(tmp_path / "first.json", [{"zpid": 123, "price": 250000}])
        second = write_json(tmp_path / "second.json", [{"zpid": 123, "price": 260000}])

        # Two imports stamped at the same second must not collide on the
        # (zpid, seen_utc) primary key of price_history.
        for path in (first, second):
            result = runner.invoke(
                app,
                ["db", "import", str(path), "--fetched-at", "2026-10-02T12:00:00Z", "--db", str(db_path)],
            )
            assert result.exit_code == 0
        assert "1 price changes" in result.stderr

        history = runner.invoke(app, ["db", "history", "123", "--db", str(db_path)])
        assert history.exit_code == 0
        data = json.loads(history.stdout)
        assert len(data) == 1
        assert data[0]["price"] == 260000

    def test_db_import_normalized_array(self, tmp_path):
        rows = [{"zpid": "123", "price": 250000, "address": "test", "city": "Schenectady"}]
        input_file = write_json(tmp_path / "input.json", rows)
        db_path = tmp_path / "test.db"

        result = runner.invoke(app, ["db", "import", str(input_file), "--db", str(db_path)])
        assert result.exit_code == 0
        assert "Imported 1 listings: 1 new, 0 updated (0 price changes), 0 skipped" in result.stderr

    def test_db_import_stdin(self, tmp_path):
        rows = [{"zpid": "123", "price": 250000, "address": "test"}]
        db_path = tmp_path / "test.db"

        result = runner.invoke(app, ["db", "import", "-", "--db", str(db_path)], input=json.dumps(rows))
        assert result.exit_code == 0
        assert db_path.exists()

    def test_db_import_custom_timestamp(self, tmp_path):
        rows = [{"zpid": "123", "price": 250000, "address": "test"}]
        input_file = write_json(tmp_path / "input.json", rows)
        db_path = tmp_path / "test.db"

        result = runner.invoke(
            app, ["db", "import", str(input_file), "--fetched-at", "2026-10-01T12:00:00Z", "--db", str(db_path)]
        )
        assert result.exit_code == 0

    def test_db_import_invalid_timestamp(self, tmp_path):
        rows = [{"zpid": "123", "price": 250000, "address": "test"}]
        input_file = write_json(tmp_path / "input.json", rows)
        db_path = tmp_path / "test.db"

        result = runner.invoke(app, ["db", "import", str(input_file), "--fetched-at", "invalid", "--db", str(db_path)])
        assert result.exit_code == 2
        assert "Invalid --fetched-at timestamp" in result.stderr

    @pytest.mark.parametrize("command", [["query"], ["history", "123"], ["stats"], ["runs"]])
    def test_db_commands_report_a_missing_database(self, tmp_path, command):
        result = runner.invoke(app, ["db", *command, "--db", str(tmp_path / "nonexistent.db")])

        assert result.exit_code == 1
        assert "Database not found" in result.stderr

    @pytest.mark.parametrize("command", [["query"], ["history", "123"], ["stats"]])
    def test_db_commands_report_corrupt_database_without_traceback(self, tmp_path, command):
        db_path = tmp_path / "corrupt.db"
        db_path.write_text("not a SQLite database")

        result = runner.invoke(app, ["db", *command, "--db", str(db_path)])

        assert result.exit_code == 1
        assert "database" in result.stderr.lower()
        assert "Traceback" not in result.stderr

    def test_db_query_binds_filter_values(self, db_rows, import_rows):
        records = [{"zpid": 123, "city": "Albany' OR 1=1 --"}, {"zpid": 456, "city": "Troy"}]
        db_path = import_rows(records)

        rows = db_rows(db_path, "--where", "city==Albany' OR 1=1 --")

        assert [row["zpid"] for row in rows] == [123]

    def test_db_query_with_timestamp_filter(self, db_rows, import_rows):
        # Import data with specific timestamp
        rows = [
            {"zpid": "123", "price": 250000, "address": "test", "city": "Schenectady"},
            {"zpid": "456", "price": 300000, "address": "test2", "city": "Albany"},
        ]
        db_path = import_rows(rows, fetched_at="2026-10-01T12:00:00Z")

        # Query with timestamp filter
        assert len(db_rows(db_path, "--where", "last_seen_utc>=2026-10-01")) == 2

    def test_db_query_reuses_where_sort_cols_limit(self, db_rows, import_rows):
        rows = [
            {"zpid": "123", "price": 250000, "address": "123 Main St", "city": "Schenectady"},
            {"zpid": "456", "price": 300000, "address": "456 Oak Ave", "city": "Albany"},
        ]
        db_path = import_rows(rows)

        # Test --where
        assert len(db_rows(db_path, "--where", "city==Schenectady")) == 1

        # Test --sort
        assert db_rows(db_path, "--sort", "price:desc")[0]["price"] == 300000

        # Test --cols
        assert list(db_rows(db_path, "--cols", "zpid,price")[0].keys()) == ["zpid", "price"]

        # Test --limit
        assert len(db_rows(db_path, "--limit", "1")) == 1

    def test_db_query_cols_details_expands(self, db_rows, import_rows):
        rows = [{"zpid": "123", "price": 250000, "address": "test", "city": "Schenectady"}]
        db_path = import_rows(rows)

        result = db_rows(db_path, "--cols", "details")
        assert len(result) == 1
        assert "hoa_monthly" in result[0]
        assert "tax_assessed_latest" in result[0]
        assert "zpid" in result[0]
        assert "kind" in result[0]

    def test_db_query_cols_details_with_where(self, db_rows, import_rows):
        rows = [
            {"zpid": "123", "price": 250000, "address": "test", "city": "Schenectady"},
            {"zpid": "456", "price": 300000, "address": "test2", "city": "Albany"},
        ]
        db_path = import_rows(rows)

        result = db_rows(db_path, "--cols", "details", "--where", "city==Albany")
        assert len(result) == 1
        assert result[0]["zpid"] == 456
        assert "description" in result[0]

    def test_db_history_shows_price_timeline(self, import_rows):
        rows = [{"zpid": "123", "price": 250000, "address": "test"}]
        db_path = import_rows(rows, fetched_at="2026-10-01T12:00:00Z")

        # Update with price 260000
        rows[0]["price"] = 260000
        import_rows(rows, db_path=db_path, fetched_at="2026-10-02T12:00:00Z")

        # Check history
        result = runner.invoke(app, ["db", "history", "123", "--db", str(db_path)])
        assert result.exit_code == 0
        history = json.loads(result.stdout)
        assert len(history) == 2
        assert history[0]["price"] == 250000
        assert history[1]["price"] == 260000

    def test_db_history_empty_for_unknown_zpid(self, import_rows):
        db_path = import_rows([{"zpid": "123", "price": 250000, "address": "test"}])

        result = runner.invoke(app, ["db", "history", "999", "--db", str(db_path)])
        assert result.exit_code == 0
        assert json.loads(result.stdout) == []

    def test_db_changes_reports_price_drop_from_imports(self, import_rows):
        """Imports still report price changes, but never a delisting."""
        db_path = import_rows(
            [{"zpid": 1, "price": 300000}, {"zpid": 2, "price": 200000}],
            fetched_at="2026-10-01T00:00:00Z",
        )
        import_rows([{"zpid": 1, "price": 250000}], db_path=db_path, fetched_at="2026-10-02T00:00:00Z")

        result = runner.invoke(app, ["db", "changes", "2026-10-02T00:00:00Z", "--db", str(db_path)])

        assert result.exit_code == 0
        event_types = {(event["event"], event["zpid"]) for event in json.loads(result.stdout)}
        assert ("price_drop", 1) in event_types
        # The second import simply did not mention zpid 2; that is not a
        # delisting, so the two imports must not be compared with each other.
        assert ("delisted", 2) not in event_types

    def test_db_changes_table_shows_the_columns_of_the_events(self, import_rows):
        """A change is not a listing: the table shows what an event carries."""
        db_path = import_rows([{"zpid": 1, "price": 250000}], fetched_at="2026-10-01T00:00:00Z")
        import_rows([{"zpid": 1, "price": 240000}], db_path=db_path, fetched_at="2026-10-02T00:00:00Z")

        result = runner.invoke(app, ["db", "changes", "2026-10-01T00:00:00Z", "--db", str(db_path), "-f", "table"])

        assert result.exit_code == 0
        assert "event" in result.stdout
        assert "zpid" in result.stdout
        assert "price_drop" in result.stdout

    def test_db_changes_with_no_events_says_so(self, import_rows):
        db_path = import_rows([{"zpid": 1, "price": 250000}], fetched_at="2026-10-01T00:00:00Z")

        result = runner.invoke(app, ["db", "changes", "2030-01-01T00:00:00Z", "--db", str(db_path), "-f", "table"])

        assert result.exit_code == 0
        assert "No changes found" in result.stderr
        assert "No listings found" not in result.stderr

    def test_db_history_table_shows_the_columns_of_the_history(self, import_rows):
        db_path = import_rows([{"zpid": 1, "price": 250000}], fetched_at="2026-10-01T00:00:00Z")

        result = runner.invoke(app, ["db", "history", "1", "--db", str(db_path), "-f", "table"])

        assert result.exit_code == 0
        assert "seen_utc" in result.stdout
        assert "kind" in result.stdout

    def test_db_changes_reports_delisted_from_searches(self, run_sync, tmp_path):
        """Two searches of the same query detect a listing that disappeared."""
        db_path = tmp_path / "searches.db"

        def body(*zpids: int) -> dict[str, Any]:
            return search_body(*({"zpid": zpid, "addressZipcode": "12305", "price": 300000} for zpid in zpids))

        first = run_sync(body(1, 2), db_path=db_path)
        assert first.exit_code == 0

        second = run_sync(body(1), db_path=db_path)
        assert second.exit_code == 0

        result = runner.invoke(app, ["db", "changes", "2020-01-01T00:00:00Z", "--db", str(db_path)])

        assert result.exit_code == 0
        event_types = {(event["event"], event["zpid"]) for event in json.loads(result.stdout)}
        assert ("delisted", 2) in event_types

    def test_db_changes_normalizes_since_with_offset(self, import_rows):
        db_path = import_rows([{"zpid": 1, "price": 250000}], fetched_at="2026-10-01T12:00:00Z")

        # An offset timestamp must compare the same as its canonical UTC form.
        result = runner.invoke(app, ["db", "changes", "2026-10-01T14:00:00+02:00", "--db", str(db_path)])
        assert result.exit_code == 0

    def test_db_changes_rejects_invalid_since(self, import_rows):
        db_path = import_rows([{"zpid": 1, "price": 250000}])
        result = runner.invoke(app, ["db", "changes", "not-a-date", "--db", str(db_path)])
        assert result.exit_code == 2

    def test_db_changes_normalizes_since_with_milliseconds(self, import_rows):
        # A sub-second suffix must compare the same as its canonical UTC form;
        # otherwise a "2026-10-01T12:00:00.500Z" SINCE would sort after the
        # fixed-width stored timestamps and silently drop the listing.
        db_path = import_rows([{"zpid": 1, "price": 250000}], fetched_at="2026-10-01T12:00:00Z")

        result = runner.invoke(app, ["db", "changes", "2026-10-01T12:00:00.500Z", "--db", str(db_path)])
        assert result.exit_code == 0
        events = json.loads(result.stdout)
        assert any(event["event"] == "new" and event["zpid"] == 1 for event in events)

    def test_db_stats_shows_statistics(self, import_rows):
        rows = [
            {"zpid": "123", "price": 250000, "address": "test"},
            {"zpid": "456", "price": 300000, "address": "test2"},
        ]
        db_path = import_rows(rows, fetched_at="2026-10-01T12:00:00Z")

        result = runner.invoke(app, ["db", "stats", "--db", str(db_path)])
        assert result.exit_code == 0
        stats = json.loads(result.stdout)
        assert stats["total_listings"] == 2
        assert stats["db_path"] == str(db_path)

    def test_db_history_honors_csv_format(self, import_rows):
        db_path = import_rows([{"zpid": 1, "price": 100}])

        result = runner.invoke(app, ["db", "history", "1", "--format", "csv", "--db", str(db_path)])

        assert result.exit_code == 0
        assert result.stdout.splitlines()[0] == "price,seen_utc,kind"
        assert result.stdout.endswith(",sale\n")

    def test_db_prune_requires_a_selector(self, tmp_path):
        db_path = tmp_path / "prune.db"
        runner.invoke(app, ["db", "import", "-", "--db", str(db_path)], input='[{"zpid": 1}]')

        result = runner.invoke(app, ["db", "prune", "--db", str(db_path)])

        assert result.exit_code == 2
        assert "Nothing to prune" in result.stderr

    def test_db_prune_keep_runs_reports_json_and_keeps_listings(self, import_rows):
        db_path = import_rows([{"zpid": 1, "price": 100}], fetched_at="2026-10-01T00:00:00Z")
        import_rows([{"zpid": 2, "price": 200}], db_path=db_path, fetched_at="2026-10-02T00:00:00Z")

        result = runner.invoke(app, ["db", "prune", "--keep-runs", "1", "--format", "json", "--db", str(db_path)])

        assert result.exit_code == 0
        assert json.loads(result.stdout) == {
            "runs_deleted": 1,
            "sightings_deleted": 1,
            "vacuumed": False,
        }
        stats = runner.invoke(app, ["db", "stats", "--db", str(db_path)])
        assert json.loads(stats.stdout)["total_listings"] == 2

    def test_db_prune_rejects_negative_limits(self, import_rows):
        db_path = import_rows([{"zpid": 1, "price": 100}])

        result = runner.invoke(app, ["db", "prune", "--older-than", "-1", "--db", str(db_path)])

        assert result.exit_code == 2
        assert "must be non-negative" in result.stderr

    def test_db_stats_can_force_human_format(self, tmp_path):
        db_path = tmp_path / "stats.db"
        runner.invoke(app, ["db", "import", "-", "--db", str(db_path)], input='[{"zpid": 1}]')

        result = runner.invoke(app, ["--no-json-errors", "db", "stats", "--format", "table", "--db", str(db_path)])

        assert result.exit_code == 2
        assert "only supports --format json" in result.stderr

    def test_db_stats_human_output_on_tty(self, tmp_path, monkeypatch):
        import io

        from rich.console import Console

        from pyzill.cli import db as db_module

        db_path = tmp_path / "stats_human.db"
        runner.invoke(
            app,
            ["db", "import", "-", "--fetched-at", "2026-10-01T12:00:00Z", "--db", str(db_path)],
            input='[{"zpid": 1, "price": 100000}, {"zpid": 2, "price": 200000}]',
        )

        class _TtyStdout(io.StringIO):
            def isatty(self) -> bool:
                return True

        buffer = _TtyStdout()
        monkeypatch.setattr(sys, "stdout", buffer)
        monkeypatch.setattr(
            db_module,
            "context",
            lambda: _FakeConsoleContext(Console(file=buffer, force_terminal=False, no_color=True, width=200)),
        )

        db_module.db_stats_cmd(db_path=str(db_path), output_format=None)

        assert "Total listings: 2" in buffer.getvalue()
        assert str(db_path) in buffer.getvalue()

    def test_db_env_var_path(self, tmp_path, monkeypatch):
        rows = [{"zpid": "123", "price": 250000, "address": "test"}]
        input_file = write_json(tmp_path / "input.json", rows)
        db_path = tmp_path / "test.db"

        monkeypatch.setenv("PYZILL_DB", str(db_path))
        result = runner.invoke(app, ["db", "import", str(input_file)])
        assert result.exit_code == 0
        assert db_path.exists()

    def test_db_default_path(self, tmp_path, monkeypatch):
        rows = [{"zpid": "123", "price": 250000, "address": "test"}]
        input_file = write_json(tmp_path / "input.json", rows)
        xdg_home = tmp_path / "xdg"
        monkeypatch.setenv("XDG_DATA_HOME", str(xdg_home))

        result = runner.invoke(app, ["db", "import", str(input_file)])
        assert result.exit_code == 0
        assert (xdg_home / "pyzill" / "pyzill.db").exists()

    def test_db_legacy_warning_when_local_pyzill_db_exists(self, tmp_path, monkeypatch):
        import pyzill.cli.db as cli_db

        monkeypatch.setattr(cli_db._warning_state, "legacy_db_shown", False)
        legacy_db = tmp_path / "pyzill.db"

        legacy_db.touch()
        xdg_home = tmp_path / "xdg"
        monkeypatch.setenv("XDG_DATA_HOME", str(xdg_home))
        monkeypatch.chdir(tmp_path)

        rows = [{"zpid": "123", "price": 250000, "address": "test"}]
        input_file = write_json(tmp_path / "input.json", rows)

        result = runner.invoke(app, ["db", "import", str(input_file)])
        assert result.exit_code == 0
        assert "default database is now" in result.stderr

    def test_db_enrich_persists_fetched_details(self, tmp_path, fake_http, home_detail_body):
        """Enriching a shortlisted listing fetches its detail and persists it."""
        rows = [{"zpid": "348914888", "price": 300000, "city": "Albany"}]
        input_file = write_json(tmp_path / "input.json", rows)
        db_path = tmp_path / "enrich.db"
        assert runner.invoke(app, ["db", "import", str(input_file), "--db", str(db_path)]).exit_code == 0

        fake_http.install("GET", content=home_detail_body)
        result = runner.invoke(app, ["db", "enrich", "--db", str(db_path), "--max-requests", "1"])
        assert result.exit_code == 0, result.stderr
        assert "Enriched 1 listings" in result.stderr

        conn = connect(db_path)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM property_details WHERE zpid = 348914888 AND kind = 'sale'"
            ).fetchone()[0]
            assert count == 1
            row = conn.execute(
                "SELECT enriched_at_utc, raw_json FROM property_details WHERE zpid = 348914888"
            ).fetchone()
            assert row is not None
            assert row[0] is not None
            assert row[1] is not None
        finally:
            conn.close()

    def test_db_enrich_with_no_listings(self, tmp_path):
        db_path = tmp_path / "empty.db"
        connect(db_path).close()  # create the schema without any listings
        result = runner.invoke(app, ["db", "enrich", "--db", str(db_path), "--format", "json"])
        assert result.exit_code == 0
        assert json.loads(result.stdout) == {
            "enriched": 0,
            "skipped_gone": 0,
            "stopped_early": False,
            "reason": None,
            "normalization_failed": 0,
        }

    def test_db_enrich_max_age_skips_recently_enriched(self, tmp_path, fake_http, home_detail_body):
        rows = [{"zpid": "348914888", "price": 300000, "city": "Albany"}]
        input_file = write_json(tmp_path / "input.json", rows)
        db_path = tmp_path / "enrich.db"
        assert runner.invoke(app, ["db", "import", str(input_file), "--db", str(db_path)]).exit_code == 0

        fake_http.install("GET", content=home_detail_body)
        result = runner.invoke(app, ["db", "enrich", "--db", str(db_path), "--max-requests", "1"])
        assert result.exit_code == 0
        assert "Enriched 1 listings" in result.stderr

        # Same enrichment within --max-age days: skipped, no fetch attempted.
        result2 = runner.invoke(app, ["db", "enrich", "--db", str(db_path), "--max-requests", "1"])
        assert result2.exit_code == 0
        assert "No listings to enrich" in result2.stderr

    def test_db_enrich_refresh_ignores_max_age(self, tmp_path, fake_http, home_detail_body):
        rows = [{"zpid": "348914888", "price": 300000, "city": "Albany"}]
        input_file = write_json(tmp_path / "input.json", rows)
        db_path = tmp_path / "enrich.db"
        assert runner.invoke(app, ["db", "import", str(input_file), "--db", str(db_path)]).exit_code == 0

        fake_http.install("GET", content=home_detail_body)
        result = runner.invoke(app, ["db", "enrich", "--db", str(db_path), "--max-requests", "1"])
        assert result.exit_code == 0

        # --refresh re-enriches even within --max-age days.
        result2 = runner.invoke(app, ["db", "enrich", "--db", str(db_path), "--refresh", "--max-requests", "1"])
        assert result2.exit_code == 0
        assert "Enriched 1 listings" in result2.stderr

    def test_db_enrich_rent_listing_attempted(self, tmp_path, fake_http, department_detail_body):
        """A rent listing is shortlisted and the detail fetch is attempted."""
        rows = [{"zpid": "12345678", "price": 2200, "city": "Albany"}]
        input_file = write_json(tmp_path / "input.json", rows)
        db_path = tmp_path / "enrich.db"
        assert (
            runner.invoke(app, ["db", "import", str(input_file), "--kind", "rent", "--db", str(db_path)]).exit_code == 0
        )

        fake_http.install("GET", content=department_detail_body)
        result = runner.invoke(app, ["db", "enrich", "--db", str(db_path), "--max-requests", "1", "--format", "json"])
        assert result.exit_code == 0, result.stderr
        payload = json.loads(result.stdout)
        # The apartment fetcher receives a zpid int (not a string apartment id),
        # so the fetch may fail at the transport layer; what matters is the
        # listing was selected and an attempt was made, not that it succeeded.
        assert payload["enriched"] + payload["skipped_gone"] + (1 if payload["stopped_early"] else 0) == 1

    def test_db_enrich_goes_records_detail_failure(self, tmp_path, fake_http):
        """A 404 detail response marks the listing gone and records the failure."""
        rows = [{"zpid": "99999999", "price": 300000, "city": "Albany"}]
        input_file = write_json(tmp_path / "input.json", rows)
        db_path = tmp_path / "enrich.db"
        assert runner.invoke(app, ["db", "import", str(input_file), "--db", str(db_path)]).exit_code == 0

        fake_http.install("GET", status_code=404, text="not found")
        result = runner.invoke(app, ["db", "enrich", "--db", str(db_path), "--max-requests", "1", "--format", "json"])
        assert result.exit_code == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["enriched"] == 0
        assert payload["skipped_gone"] == 1

        conn = connect(db_path)
        try:
            row = conn.execute(
                "SELECT reason, status_code FROM detail_failures WHERE zpid = 99999999 AND kind = 'sale'"
            ).fetchone()
            assert row is not None
            assert row[1] == 404
        finally:
            conn.close()
