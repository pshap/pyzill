# Changelog

All notable changes to this project are documented here.

## [Unreleased]


### Added
- `db enrich` supports `--max-age` (default 7), `--refresh`, and `--plan`; JSON output now includes `skipped_gone`, `stopped_early`, `reason`, and `normalization_failed`.
- `db enrich`/`db sync`/`search` support `--plan` to print a JSON request plan without network use or geocoding.
- `--cols details` presets and queryable detail fields including `hoa_monthly`, `tax_paid_latest`, `tax_year_latest`, `tax_assessed_latest`, `normalization_failed`, `enriched_at_utc`.
- `db schema` and `pyzill schema` expose `db-detail` and detail schemas; property details use schema v5 with derived `hoa_monthly` and `tax_*_latest` columns.
- Block memory is on by default at `$XDG_STATE_HOME/pyzill/block.json` (`~/.local/state/pyzill/block.json`), configurable via `PYZILL_BLOCK_STATE` (path or `off`); cooldown errors include `cooling_down` and `retry_after`.
- Environment overrides `PYZILL_MIN_RATE_LIMIT_FLOOR` and `PYZILL_MAX_REQUESTS_CEILING` adjust safety floors dynamically.

### Changed
- `db enrich` uses per-kind detail endpoints (apartment building URL → apartment endpoint, stamped with the listing zpid), catches `ZillowError` to stop batches with reason codes, and clears block state on success.
- `db enrich` candidate selection uses SQL-side max-age filtering and includes `detail_url`; gone listings are skipped and recorded.
- `tax_assessed_latest` is derived from tax history (not copied from paid tax); `tax_assessed_value` remains the listings-table value.
- `--force` removed; safety is enforced by floors/env overrides.
- Cooldown is enforced in network commands (after `--plan`) and cleared on successful requests.

### Fixed
- Detail normalization failures are recorded (`normalization_failed`) without breaking the batch; invalid zpids are skipped.
- v4→v5 property_details migration preserves rows and correctly derives `hoa_monthly`/`tax_*_latest` from stored tax history.
- Tax figures whose preferred alias is null or non-numeric now fall through to the next alias in `tax_history`; the migration and the detail upsert share one derivation.
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
