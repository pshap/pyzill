# Changelog

All notable changes to this project are documented here.

## [Unreleased]

### Added
- CLI failures now expose stable error codes and retryability, distinct exit
  codes for blocked/not-found/schema/server/network failures, and structured
  JSON diagnostics on stderr when stdout is not a TTY (overridable with
  `--json-errors` / `--no-json-errors`). Added `pyzill schema listing` and
  JSON-capable DB history, stats, sync, and import summaries.
- `tiled_search` now takes `max_requests` (default 50) bounding the total
  number of search requests in a recursive tiling run; when the budget runs
  out mid-tree partial results are returned with a `budget_exhausted` flag
  and a `UserWarning` is emitted (an `on_budget` callback can override the
  warning). An optional `on_progress`
  callback reports `(requests_used, unique_map_results_so_far)` after each
  tile. The CLI exposes this as `--max-requests` on `search`/`db sync`
  (default 50), lowers the `--max-depth` ceiling from 8 to 6, and prints
  tile progress to stderr (suppressed by `--quiet`).
- `scripts/capture_fixture.py` captures sanitized live Zillow responses
  (sale/rent/sold search, home/apartment detail) into `tests/fixtures/` so
  the offline suite can detect payload drift synthetic bodies cannot. It
  requires `--confirm`, trims results to `--max-results` (default 5), and
  redacts agent/broker/phone/email/MLS/attribution fields while keeping
  public listing data. `tests/test_fixtures.py` validates captured fixtures
  when present (skipping until then) and unit-tests the capture script's
  sanitizing, trimming, and gating offline. Hypothesis is now a dev
  dependency backing a property test that `upsert_listings` final state is
  independent of batch application order.
- The opt-in live smoke suite (`PYZILL_LIVE=1 pytest -m live`) now covers
  sale/rent/sold search on a fixed Schenectady viewport, resolving a zpid
  from the sale search for the home-detail check, and one live Nominatim
  geocode call. Search assertions check the response shape *and* that
  normalization yields only real listings (integer zpids), so payload drift
  and building-card regressions surface between releases. `pytest-cov` is a
  dev dependency for coverage runs.

### Changed
- `pyzill.cli` no longer re-exports the private `_coerce_value` helper; it
  was only ever used by tests, which now import `coerce_value` from
  `pyzill.cli.query` directly. The package exports `app` and `main_entry`.
- `upsert_listings` internals were rewritten for bulk syncs: existing-row
  state and prior prices are now read in two batched (chunked) queries
  instead of up to three SELECTs per row, and the write path is a single
  generated `INSERT ... ON CONFLICT(zpid) DO UPDATE` per row instead of a
  wide hand-written UPDATE. Behavior (stats, missing-field semantics,
  kind transitions, price history) is unchanged; all database tests pass
  without modification.
- The CLI package was split into focused modules: `cli/options.py` (shared
  Typer option types), `cli/search.py` (search commands and the
  fetch/normalize pipeline), `cli/detail.py` (detail commands),
  `cli/query.py` (offline query/normalize commands and filtering engines),
  `cli/db.py` (database commands), and `cli/console.py` (diagnostics and
  input helpers). `cli/app.py` now only wires the Typer app together. The
  command surface and flags are unchanged.

### Changed (breaking)
- Search functions (`for_sale`, `for_rent`, `sold`, `search`, and the
  `iter_*` helpers) no longer fall back to a continental-US viewport when
  `bbox` is omitted. A missing bbox now raises `ValueError` unless the caller
  passes `allow_nationwide=True`. Previously a bare
  `for_sale(search_value="12305")` silently searched the whole country.
  Resolve a viewport with `pyzill.geocode.geocode(...)`, build a
  `pyzill.BBox`, or opt in with `allow_nationwide=True`.

### Fixed
- `--where` now keeps identifier and descriptive fields as strings, preserving
  leading-zero ZIP codes in JSON and database queries. Quoted literals remain
  strings; only numeric listing fields are coerced.
- Normalization treats zero area as unknown and prefers positive nested beds,
  baths, and living area over top-level zero sentinels while retaining valid
  zero-bed studios.
- Opening a current-schema database no longer starts a write transaction;
  read-only DB commands use read-only SQLite connections. Price history now
  records only the first price and actual price changes.
- Search response shape/JSON failures raise `ZillowParseError`, PerimeterX
  challenge pages raise `ZillowBlockedError`, and server errors no longer
  inherit from the block exception.
- Database sightings now preserve known fields on sparse updates, maintain
  order-independent first/last-seen and current-state values, and isolate
  sale/rent/sold price history by kind. Schema version 2 migrates existing
  version 1 stores; `db import --kind` labels imported sightings.
- Runtime search calls reject unknown keyword arguments, request options reject
  invalid retry/timeout values, home detail inputs and library detail URLs are
  restricted to valid Zillow targets, and `--where` numeric coercion no longer
  treats `nan`, `inf`, or scientific notation as numbers.
- In-memory database statistics now report zero file bytes. SQLite connections
  use WAL and a busy timeout.
- Normalized zpids are integer-or-None; zero beds/area are preserved. HTTP 404,
  5xx, parse/schema failures, and block responses have distinguishable error
  types. Retry delays are exponential with jitter, honor `Retry-After` for 429,
  and do not retry 403 responses.
- Nominatim requests use a separate one-request-per-second limiter, no proxy
  unless explicitly passed, an atomic 30-day cache keyed by country/format,
  and a warning for synthetically expanded point-like viewports. Search warns
  on the 500-result cap and supports `--tile --max-depth` recursive splitting.
- CLI internals are organized under `pyzill.cli` modules, search flags share a
  `SearchOptions` value, and command context is invocation-scoped. Database
  filters/sort/limit now execute in parameterized SQLite queries. `PYZILL_*`
  environment settings take precedence over `ZILLOW_*` fallbacks; proxy parsing
  validates scheme/port and brackets IPv6. Detail functions accept
  `RequestOptions`, and `db sync`/`db import --kind` use validated kind enums.
- The v1-to-v2 database migration no longer appends `kind` after
  `last_seen_utc` via `ALTER TABLE ADD COLUMN`; it rebuilds the listings
  table so a migrated database has the same column order as a fresh one
  (`kind` second). The rebuild stages `price_history` in a temporary
  FK-free table first, so history rows are neither cascade-deleted nor
  repointed by the parent-table rename. Schema DDL is now shared between
  the fresh-create and migration paths (`_CREATE_LISTINGS_SQL` etc.) so
  they cannot drift.

### Unverified (live Zillow validation required)
- TODO: Decide whether `for_rent` should expand building cards into units.
  Building-only search results are now understood and handled rather than
  guessed at: a card is a building when `isBuilding` is true *and* it has no
  integer `zpid` at the top level or under `hdpData.homeInfo`. Zillow
  sometimes stamps a `buildingId` (a lat-lng string such as
  `"47.61503--122.34274"`) into the `zpid` field, which does not parse to an
  integer, so the check is on the normalized zpid and not the flag alone.
  Building cards are dropped from `normalize_listings` and the raw
  `iter_sale`/`iter_rent`/`iter_sold` iterators, and `extract_listings`
  prefers whichever of `mapResults`/`listResults` holds real listings --
  `listResults` carries the individual units. Captures on 2026-10-18 against
  a dense downtown-Seattle bbox returned *only* building cards for both sale
  and rent, while a Schenectady NY bbox returned real listings (5/5 for
  sale/sold, 1/5 for rent), so the effect is area-dependent. The remaining
  open question is whether `for_rent` should fetch the building-detail
  `floorPlans` list (unit beds/baths/description) to synthesize unit rows;
  that is an added request per building and is deliberately not done.
- TODO: Compare Nominatim ZIP viewports with known ZCTA boundaries. Synthetic
  point-like viewports now trigger a CLI warning, but are not treated as ZIP
  boundaries.
- Live sale/rent/sold search and home/apartment-detail responses were
  captured and sanitized locally on 2026-10-18 via
  `scripts/capture_fixture.py` (Schenectady NY samples; mixed building and
  individual-listing results). Captured fixtures contain real listing
  data and are intentionally not committed (`tests/fixtures/*.json` is
  gitignored); run the capture script on demand to regenerate them, and
  the fixture tests skip until then.

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
