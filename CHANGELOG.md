# Changelog

All notable changes to this project are documented here.

## [Unreleased]

### Added
- `get_from_department_id` / `get_from_department_url` — correctly-spelled
  names for the apartment-detail functions. `get_from_deparment_id` /
  `get_from_deparment_url` remain as backwards-compatible aliases.
- `Listing` — a `TypedDict` describing the normalized shape produced by
  `normalize_listing` / `normalize_listings` (and `iter_search(..., normalize=True)`).
  Zero-dependency static typing for the flat, numeric-or-`None` output;
  `iter_search` now yields `Mapping[str, Any]`.
- Detail functions accept `max_retries` with the same default and retry policy
  as search functions.

### Changed
- Centralized HTTP handling in a new internal `pyzill.http` module
  (`build_headers`, `build_proxies`, `get_page`, `put_json`, `request`). Search
  and detail functions now share header construction, proxy resolution,
  throttling, and 5xx retry logic.
- `for_sale`, `for_rent`, and `sold` forward arguments via `**kwargs` instead
  of repeating ~18-parameter signatures; `for_rent` still accepts `is_room`
  and `is_entire_place`.
- `get_from_department_id` now builds `https://www.zillow.com/apartments/<id>/`
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
