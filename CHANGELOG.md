# Changelog

All notable changes to this project are documented here.

## [Unreleased]

### Fixed
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

### Unverified (live Zillow validation required)
- TODO: Confirm Zillow's default rent filter payload semantics and whether
  rental `mapResults` contain building-only entries that need unit expansion.
- TODO: Compare Nominatim ZIP viewports with known ZCTA boundaries. Synthetic
  point-like viewports now trigger a CLI warning, but are not treated as ZIP
  boundaries.
- Zillow responses were not fetched or captured during this work; these items
  remain open rather than relying on speculative payload assumptions.

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
