# Changelog

All notable changes to this project are documented here.

## [Unreleased]

### Fixed

- Value coercion is strict: thousands separators must be grouped correctly,
  scientific notation is rejected, and non-finite or out-of-SQLite-range
  numbers become `None` instead of raising. Normalized string fields are
  always `str` or `None`, even when Zillow sends a number.
- A home-detail target is only a positive integer zpid or an HTTPS zillow.com
  URL. Numeric-looking strings, booleans, zero, negatives, and `http://` URLs
  are rejected with a clear `ValueError`.
- `--where-json` applies the same validation and coercion as `--where`, and a
  negative `limit` is rejected by the library, not only by the CLI.
- Exceptions survive `pickle` and `copy.copy` with the same type, message, and
  attributes. An invalid proxy during geocoding raises `ValueError` rather
  than a retryable `GeocodeUnavailableError`.
- The write path is atomic: it takes the write lock before reading prior
  state, rolls a failed batch back, and records the run's start before the
  fetch. Opening a current database no longer takes the lock just to discover
  there is nothing to do, and a connection that fails during its PRAGMAs is
  closed. `import_rows` reports the id of the run it wrote.
- `load_listings` goes through the shared query engine, so the library and
  `pyzill db query` select and order rows the same way.
- The write path reads a zpid through the shared coercion rule: a float with a
  fraction, a boolean, and an out-of-range value are skipped and counted
  instead of truncated or crashing the batch. Sparse sightings preserve known
  fields and the price.
- The price history and the listing price agree, including when one batch
  carries two sightings of the same zpid and when an out-of-order batch leaves
  no history row. A negative `daysOnZillow` is reported as unknown.
- One rule decides what counts as a listing, so `fetch_listings`,
  `extract_listings`, and `import_rows` keep the same listings.
- A tiled search records `truncated` only when a tile is genuinely unfinished
  or the request budget ran out. A failed tile raises `TiledSearchError` with
  the completed results on `.partial` and the failing request's `code`,
  `retryable`, and `status_code`. Tiling knobs no longer break run
  comparability.
- Database migrations handle a database with a `listings` table but no schema
  version by rebuilding it, carry v1 wide columns across, and take a
  backfilled column's type from the DDL. A backfilled `kind` is
  `NOT NULL DEFAULT 'sale'`, and the fresh schema enforces `kind` and
  `truncated` with CHECK constraints. A `KeyboardInterrupt` during migration
  no longer leaks the connection and its write lock.
- `db sync` reports blocked, not-found, drift, and transient failures with
  exit codes 3/4/5/6 instead of a traceback and code 1. Sync and import
  summaries carry `run_id` and `truncated`, and `--format` is validated
  before any work. Empty `changes` and `runs` output says what is empty.
- The table renderer uses each row set's own columns, so `db changes`,
  `db history`, and `db runs` no longer display a single `price` column.
- `--json-errors` / `--no-json-errors` is honored for errors the CLI raises
  itself, not only for parser errors.

### Added

- `pyzill db prune` / `prune_database` deletes old sync runs and their raw
  sightings while keeping listings and price history, with `--older-than`,
  `--keep-runs`, and `--vacuum`.
- `pyzill db runs` / `list_runs` reads back the sync runs a sync or import
  recorded.
- Reset helpers for the process-wide defaults:
  `reset_default_client`, `reset_default_session`, and
  `reset_default_limiter`.
- `pyzill geocode` resolves a place name to the viewport a search would use.
- `pyzill schema db-listing` and `pyzill schema detail` publish the database
  row shape and the property-detail shape.
- A payload-shape contract under `tests/shapes/`, with a test that names the
  fields the parsers read and reports when Zillow's payload drifts.
  `scripts/build_synthetic_fixtures.py` rebuilds data-free payloads for CI,
  and a test keeps them in step with the baselines.

### Changed

- The public surface is typed for editors: `ZillowClient` search methods take
  `Unpack[SearchParams]` (plus `RentParams` / `TiledSearchParams`), and the
  HTTP session is described by a `SessionLike` protocol instead of `Any`.
- `ruff` enforces a branch limit (`C90`, `max-complexity = 10`). The query
  compiler, the change report, and the tiling loop were split into named
  steps.
- Lint suppressions that hid a rule rather than answered it are gone, and
  every remaining suppression says why it is there.
- Packaging states its contract: `curl_cffi` has an upper bound
  (`>=0.16.3,<0.17`), the license is the SPDX expression `MIT` with the
  license file declared, and `dev` builds on `test`.
- The `pyzill.search`, `pyzill.details`, and `pyzill.utils.remove_space`
  import paths still work but are deprecated and emit `DeprecationWarning`;
  the library itself imports the real modules.
- The CLI reads its filters from `SearchFilters`, and the query engine takes
  one query description instead of ten parameters.
- Documentation moved into focused guides under `docs/`. The README keeps the
  overview, status, scope, legal note, and quick starts.
- The test suite is split by topic (`tests/test_cli_*.py`,
  `tests/test_db_*.py`), coverage is enforced at 90 %, and CI runs the gate
  on Ubuntu, macOS, and Windows across Python 3.12 and 3.13.

## [1.4.0] - 2026-10

### Added

- `pyzill.responses` with host-agnostic `json_body` and `raise_for_status`
  helpers, typed against `ResponseLike`; `pyzill.http` re-exports both.
- A layering test (`tests/test_layering.py`) that enforces the package's
  import map: imports only point downward, the facade is not an internal
  dependency, and no module imports pyzill inside a function.
- A workflow layer (`pyzill.workflows`): `fetch_listings`, `sync_to_db`,
  `import_rows`, and `extract_listings`. It returns data and structured
  notices and never prints or exits.
- Database schema v3: `sync_runs` records the query, viewport, filters,
  truncation, and timing; sightings and price history support change reports.
- `pyzill db changes <since>` reports new listings, price moves, status
  transitions, and delistings.
- `pyzill db` commands for sync, import, query, changes, history, runs, stats,
  and prune; the default database lives in the XDG data directory.
- `load_listings` filters and limits, composite indexes, and one SQLite query
  compiler for saved JSON and database rows.
- `ZillowClient` with a persistent session, shared process-wide throttling,
  and optional proxy rotation after a block.
- Stable CLI error codes and retryability (`blocked`, `not_found`,
  `parse_error`, `transient`, ...), rendered as JSON on stderr when stdout is
  not a TTY.
- `tiled_search(..., max_requests=50)` and `TiledSearchError` with partial
  results.
- `scripts/capture_fixture.py` and an opt-in live smoke suite
  (`PYZILL_LIVE=1 pytest -m live`).

### Changed

- The geocode cache is writes-under-lock, bounded, and kept in a directory of
  its own; the geocoder is split into four small steps.
- CLI output renderers are pure; the query engine moved to `pyzill.query`; the
  database helpers moved into `pyzill.db`; the CLI is wiring only.
- The browser identity (headers, client hints, impersonation profile) comes
  from one module.
- The normalized `Listing` shape is the single source of listing fields; the
  database columns and numeric fields derive from it.
- Transport options are built in one place, and the import cycles between
  transport, request, and client layers are gone.
- The database module is a package (`schema`, `runs`, `write`, `query`), and
  the listing write path is split into prepare, classify, and write steps.
- The CLI is split into focused modules (`options`, `output`, `search`,
  `details`, `db`, `query`, `schema`, `geocode`).

### Changed (breaking)

- Search functions take keyword arguments with sane defaults and reject
  unknown keywords. `search()` remains available for a custom `filter_state`;
  `for_sale`, `for_rent`, and `sold` build their own and reject a
  caller-supplied one.
- A caller-supplied `proxy_url` pins that call's transport and is never
  rotated away.
- `normalize_listing` returns `Listing | None`; callers must narrow it.

### Fixed

- `db changes` orders events by when they happened, compares runs by their
  full identity, and ignores imports and differently filtered runs.
- Sync counts are per listing, not per input row; a duplicate zpid in one
  batch counts once.
- Comparing a field with `null` using an ordering operator is a usage error,
  and `--where` keeps identifier fields as strings while coercing numbers
  against numeric fields.
- `db query` writes `[]` for an empty result; `db sync` rejects an empty
  query with exit 2; a rejected `--format` is caught before the work starts.
- The geocode cache is written under an exclusive lock, so concurrent
  processes cannot corrupt it.
- The search payload's `filterState` comes from the filters, not from
  dictionary ordering.
- `pyzill.geocode` is bound to the module, so patching it in tests works.
- Raw listings containing `NaN`/`Infinity` no longer abort an import.
- The default client is resolved once per request; several clients share one
  request budget; duplicate proxies are collapsed.
- Invalid proxy URLs no longer leak credentials into error messages, and
  every `http.request` failure raises a `ZillowError`.
- The CLI imports cleanly, sends same-origin XHR headers, and matches its
  `User-Agent` and client hints to the requested `--impersonate` profile.
- A malformed Nominatim response raises a typed geocode error, and geocode
  requests use a separate one-request-per-second limiter.
- `db.connect` closes the connection on `KeyboardInterrupt`/`SystemExit`.
- Home-detail parsing raises `ZillowParseError` when the requested zpid is
  missing, and `detail apartment` validates its input up front.
- `--where` numeric fields reject non-numeric literals; a per-call timeout
  does not leak into the shared client.
- PerimeterX challenge detection is centralized, including for a 200 HTML
  body. HTTP 404, block, and parse failures map to distinct exceptions.
- Empty `PYZILL_PROXY`/`PYZILL_DB` values fall back to the default instead of
  producing an empty path or URL. `--timeout 0` is reported through the
  normal JSON error channel.
- Zero area is treated as unknown; zero beds are preserved for studios.
- Database sightings preserve known fields on sparse updates, and in-memory
  database statistics report zero file bytes.
- `Retry-After` is measured from the moment the response arrives.
- `normalize_property_detail` reports lot size in square feet and carries
  `lot_size_unit` and `hoa_fee_unit`; an unlabeled number is reported as it
  arrived.
- Opening a database at the current version backfills missing columns.

### Notes

- `for_rent` does not expand building cards into unit rows; that would need an
  extra detail request per building and is deliberately out of scope.
- Nominatim ZIP viewports are synthetic point-like boxes, not ZCTA
  boundaries. The CLI warns rather than treating them as exact.
- Live sale/rent/sold and home/apartment responses are captured locally with
  `scripts/capture_fixture.py`; real listing data stays out of the repository.


## [1.3.0] - 2026-10

### Added
- Command-line interface: `pyzill search sale|rent|sold`, `pyzill detail
  home|apartment`, `pyzill query`, and `pyzill normalize`, with bbox/price/
  bed/bath filters, `--raw`, json/jsonl/csv/table output to stdout or file, and
  global `--quiet`/`--verbose`/`--no-color`/`--proxy`/`--impersonate`/`--timeout`/
  `--max-retries`/`--rate-limit`/`--version`. Environment variables
  `PYZILL_PROXY`, `PYZILL_IMPERSONATE`, and `PYZILL_RATE_LIMIT` mirror the
  corresponding flags. Exit codes: 0 success (including zero results), 1
  operational failure, 2 invalid invocation. Available as the `pyzill`
  console script and `python -m pyzill`.
- Geocoded search: `search` without an explicit bounding box geocodes the
  query via OpenStreetMap Nominatim and uses the returned viewport. Results
  are cached on disk (`~/.cache/pyzill/geocode.json`); the proxy setting is
  honored for the geocode request. Geocoding failure exits 1 with a hint to
  pass an explicit bbox.
- `pyzill query FILE` — offline re-slicing of saved search results (file
  path or `-` for stdin; no network). Repeatable `--where FIELD OP VALUE`
  for AND logic (`==`, `!=`, `>`, `>=`, `<`, `<=`, `~` contains; numbers
  compare numerically, including `$`/`,`-adorned literals and the computed
  `ppsf` field; strings compare case-insensitively; `null`/`none` match
  missing). `--sort FIELD:asc|desc` (missing values last), `--cols
  a,b,c` column selection, `--limit N`, plus the standard format/output
  options.
- `pyzill detail home|apartment ID --keys` lists top-level fields with
  types; `--path address.city` (dotted paths with `[n]` list indices)
  extracts one value. `--keys` and `--path` are mutually exclusive
  (exit 2); a missing path exits 1 showing where the walk broke.
- Table output honors an explicit `--cols` selection in `query`
  (previously the concise-column subset always won).
- `BBox` and `CONTINENTAL_US` define typed search viewports; search functions
  accept `bbox=` and `split_bbox` returns `BBox` quadrants.
- `get_from_apartment_id` / `get_from_apartment_url` — canonical names for
  apartment-detail functions. The former department spellings and historical
  `deparment` misspellings remain as deprecated aliases.
- `Listing` — a `TypedDict` describing the normalized shape produced by
  `normalize_listing` / `normalize_listings`.
  Zero-dependency static typing for the flat, numeric-or-`None` output;
  `iter_search` now yields `Mapping[str, Any]`.
- Detail functions accept `max_retries` with the same default and retry policy
  as search functions.
- Normalization skips non-dict listing entries consistently in
  `normalize_listing` and `normalize_listings`.
- Added explicit `iter_sale`, `iter_rent`, and `iter_sold` helpers; `iter_search`
  remains the for-sale shorthand.
- Apartment detail functions are now named `get_from_apartment_id` and
  `get_from_apartment_url`; the former department spellings remain as
  deprecated aliases that emit `DeprecationWarning`.
- Local SQLite database for tracking listings over time: `pyzill db
  sync sale|rent|sold` fetches and upserts (all search filters apply,
  geocoding included), `pyzill db import FILE|-` ingests a saved raw response
  or a normalized array (`--fetched-at` overrides the batch timestamp),
  `pyzill db query` reuses `--where`/`--sort`/`--cols`/`--limit`/`--format`/
  `--output` with the added `first_seen_utc`/`last_seen_utc` fields,
  `pyzill db history ZPID` prints one listing's price timeline, and
  `pyzill db stats` summarizes the store. Path resolution: `--db` > `PYZILL_DB`
  > `./pyzill.db`; the schema is created on first use. Listings carry a
  never-overwritten `first_seen_utc`, a refreshed `last_seen_utc`, and an
  append-only `price_history` table. Library entry points: `connect`,
  `upsert_listings`, `load_listings`, `get_price_history`, `db_stats`,
  `SyncStats`, `DBStats`, `utc_now`, `default_db_path`.

### Fixed
- `pyzill normalize` and `pyzill db import` now unwrap full saved search
  bodies (`cat1.searchResults`) instead of treating the envelope as a single
  bogus listing, detect raw `mapResults` rows and normalize them (so
  `"$459,000"` prices and camelCase fields ingest correctly) while leaving
  already-normalized rows untouched, and tolerate a price change landing in
  the same second as an earlier sighting instead of violating the
  `(zpid, seen_utc)` primary key.

### Changed
- Centralized HTTP handling in a new internal `pyzill.http` module
  (`build_headers`, `build_proxies`, `get_page`, `put_json`, `request`). Search
  and detail functions now share header construction, proxy resolution,
  throttling, and 5xx retry logic.
- `for_sale`, `for_rent`, and `sold` expose their keyword parameters through
  typed `Unpack` signatures; `for_rent` explicitly exposes `is_room` and
  `is_entire_place`.
- `get_from_apartment_id` builds `https://www.zillow.com/apartments/<id>/`
  (previously a hardcoded example URL).

### Tooling
- Added [mypy](https://mypy-lang.org/) in **strict** mode, configured in
  `pyproject.toml` (`files = ["src/pyzill", "tests"]`; `tests/` relaxes
  untyped-def/untyped-call rules). Run `mypy`. The source tree is fully typed
  and passes strict mode.

## [1.2.0] - 2025-10

### Added
- `parse_price(value)` — coerce Zillow price strings (`"$329,900"`) or numbers
  into an `int`; returns `None` for unknown/`0`/non-numeric values.
- `normalize_listing(listing)` — flatten a raw listing into predictable,
  numeric-or-`None` fields, falling back to `hdpData.homeInfo` when top-level
  fields are missing. Handles the common case where `price` is a preformatted
  string and `beds`/`baths`/`area` are absent or `None`.
- `normalize_listings(listings)` — same, for an iterable; skips non-dict items.
- `iter_search(..., normalize=True)` — yield normalized listings.

### Tooling
- Added [Ruff](https://docs.astral.sh/ruff/) for linting and formatting, with
  config in `pyproject.toml` and a `dev` optional-dependency group. Run
  `ruff check .` and `ruff format .`. Enabled rule sets include `E`, `W`, `F`,
  `I`, `B`, `UP`, `C4`, `SIM`, `RUF`, `N`, `A`, `RET`, `PIE`, `PERF`, `FURB`,
  `TID`, `PGH`, `PTH`, `ISC`, `ICN`, `LOG`, `G`, `RSE`, `PLW`, `PLE`, `DTZ`,
  `ERA`, `PT`, `S`, `PLR`, and `ARG`. Test-only false positives (`assert`,
  magic values, unused fake-HTTP-function arguments) are ignored under
  `tests/`; legitimate `PLR0913` on the filter-heavy public API functions use
  targeted `noqa` comments.

### Changed
- Added `requires-python = ">=3.12"` to `pyproject.toml`, matching the Ruff
  `target-version`.


## [1.1.0] - 2025-10

### Added
- Built-in request throttling: all Zillow requests are spaced by a global
  minimum interval (default **1.5s** plus up to 0.5s jitter) to avoid 403s.
  Configure via the `ZILLOW_MIN_INTERVAL` env var or
  `pyzill.set_min_interval(seconds)`. `pyzill.get_min_interval()` reads it.
- `ZillowBlockedError` — raised on any non-2xx response from Zillow, with
  PerimeterX detection in the message. Search functions no longer crash with a
  bare `JSONDecodeError` when blocked.
- `impersonate` parameter (default `"chrome136"`) on all search functions and
  detail functions, so the TLS fingerprint is configurable without editing code.
- `timeout` (default `30.0s`) and `max_retries` (default `1`, retries on 5xx /
  network errors) on all search functions.
- `iter_search(search_fn, max_items=None, **kwargs)` — yields individual
  `mapResults` listings and documents the 500-listing cap.
- `split_bbox(...)` — splits a bounding box into four quadrants for working
  around the 500-listing cap.
- `resolve_proxy(proxy_url=None)` — resolves a proxy URL with a `ZILLOW_PROXY`
  environment-variable fallback; validates scheme and hostname.
- Test suite under `tests/` with mocked `curl_cffi` responses (success, 403,
  retry, defaults, proxy fallback, detail paths).

### Fixed
- Search endpoints returned HTTP 403 after Zillow tightened bot detection.
  Fixed by using the `chrome136` impersonation profile plus `Referer` and
  `Sec-Fetch-Site: same-site` headers (both required as of 2025-10).
- `parse_proxy` now percent-encodes all credential characters (previously `/`
  and other reserved characters were left unescaped).
- Removed dead `regx_price`.

### Changed
- **Breaking:** search function signatures now accept keyword arguments with
  sane defaults (`for_sale(search_value="...")` works). The coordinate and
  filter arguments are optional and use a continental-US bounding box by
  default.
- Detail functions accept `impersonate` and `timeout`, and align to the
  `chrome136` header set.
- `search()` is no longer re-exported at the package root to avoid shadowing
  the `pyzill.search` module; use `pyzill.search.search` or
  `for_sale` / `for_rent` / `sold`.
- README rewritten to match the real API and to state honestly which endpoints
  work unauthenticated vs. which need a residential proxy.

### Notes
- Search endpoints are rate-limited per IP. A single request often succeeds,
  but repeated requests require a rotating residential proxy.
- Detail endpoints work unauthenticated without a proxy in testing.

## [1.0.3]

- Prior upstream release (see `johnbalvin/pyzill`).
