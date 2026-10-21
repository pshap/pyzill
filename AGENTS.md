# Agent Guide

## Workflows

- Install the project with its development extra, then use the repository virtual environment for checks.
- Before each phase checkpoint, run `scripts/check.sh`; it runs every gate (the formatter, the linter, the type checker, the dead-code check, and the offline tests) and fails on the first one that fails. Use `scripts/check.sh --fix` to let the formatter and the linter fix what they can.
- The default test suite is offline. Live requests are opt-in with `PYZILL_LIVE=1 pytest -m live`; never enable them for routine checks.
- Record user-visible changes in `README.md` and `CHANGELOG.md`.

## Package Layers

Imports may only point downward. The map below is enforced by
`tests/test_layering.py` (offline, AST-based, no new dependency).

| Layer | Modules | Holds |
|---|---|---|
| 0 core | `config`, `exceptions`, `_kinds`, `bbox`, `proxy`, `urls`, `utils` | constants, the exception taxonomy, the listing kinds, the URL and credential rules |
| 1 transport | `http`, `http_headers`, `ratelimit`, `responses` | the retry loop, the browser identity, the request budget, the generic response helpers |
| 2 data | `parse`, `normalize`, `property_detail`, `values` | page parsing, the normalized listing and property-detail shapes, the shared scalar coercion rules |
| 3 request/response | `search_request`, `detail_request`, `geocode`, `tiling` | building requests, checking responses |
| 4 client | `client` | sessions, proxy rotation, the rate limiter |
| 5 convenience API | `api` (re-exported by `search` and `details`) | the module-level entry points over the default client |
| 6 database | `db/*`, `query` | the schema, the write path, the query engine |
| 7 services | `workflows/*` | the search/sync/import workflows |
| 8 CLI | `cli/*` | Typer wiring: flags to request objects, exceptions to exit codes, results to output |
| 9 facade | `pyzill/__init__.py` | the public names; only the outside world may import it |

Layer rules:

- No function-local imports of pyzill modules: a lazy import is how an import cycle hides.
- The package facade is not a dependency of the package's own modules.
- The workflow layer never prints and never exits the process; it returns data and structured notices, and the CLI decides what to report.
- One listing-kind type (`pyzill._kinds`), one credential redaction helper (`pyzill.proxy.redact_credentials`), one Zillow URL rule (`pyzill.urls`), one listing field list (`pyzill.normalize.Listing`, from which the database columns and the numeric fields are derived).
- One set of scalar coercion rules (`pyzill.values`), shared by the listing shape (`pyzill.normalize`) and the property-detail shape (`pyzill.property_detail`).
- The workflow package is split by job: `model` (the shapes), `viewport` (resolving a viewport), `extract` (pulling the real listings out of a response), `search` (running one search), `sync` (the sync and import pipelines). A workflow submodule imports its siblings by module, never through the package `__init__`.
- The sync-run lifecycle belongs to `pyzill.db.runs`: the caller creates the run (with the query, viewport, and filters that make two runs comparable) and the write path finishes it.

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
