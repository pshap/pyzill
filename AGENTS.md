# Agent Guide

## Workflows

- Install the project with its development extra, then use the repository virtual environment for checks.
- Before each phase checkpoint, run `pytest -q`, `ruff check src tests scripts`, and `mypy --strict`; do not proceed with a failing gate.
- The default test suite is offline. Live requests are opt-in with `PYZILL_LIVE=1 pytest -m live`; never enable them for routine checks.
- Record user-visible changes in `README.md` and `CHANGELOG.md`.

## Request Budget and Rate Limits

- Requests are rate-limited per configured minimum interval (default 1.5 seconds); `--rate-limit 0` disables the delay.
- Zillow search results cap at 500 listings per request. Tiling is opt-in, defaults to depth 4, and is bounded to 50 requests by default. Do not remove these request bounds.
- Do not increase request volume or perform live scraping in tests without explicit user direction.

## CLI Automation

- Exit codes: `0` success, `1` other error, `2` usage error, `3` blocked, `4` not found, `5` parse/schema drift, `6` transient server/network.
- When stdout is not a TTY, errors are JSON on stderr by default. Use `--json-errors` or `--no-json-errors` to select explicitly. Shape: `{"error":{"code":"blocked","message":"...","retryable":false,"status_code":403}}`; `status_code` is optional and credentials are redacted.
- Listing/search output is JSON by default. `db sync` and `db import` accept `--format json` for count summaries; `db stats` and `db history` also support JSON output.
- Normalized listings use the `Listing` TypedDict shape. Obtain the versioned schema with `pyzill schema listing`.
- Filter saved/database rows with repeated `--where FIELD OP VALUE` or `--where-json` condition arrays. Both sources use the same SQLite query engine.
- Search commands may return up to 500 listings. Avoid pulling all fields or rows when automating: use `--cols` to project fields, `--limit` to bound rows, and `--where` to filter server-independent saved/database data before consuming results.
