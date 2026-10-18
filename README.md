- `--proxy` applies to Zillow requests; Nominatim geocoding is direct by default.
`zpid`, `price`, `beds`, `baths`, `area`, `address`, `city`, `state`, `zipcode`,
# pyzill — Zillow scraper in Python

## Overview

An open-source Python library for extracting real-estate data from Zillow.
It supports two capabilities:

- **Search** — query for-sale / for-rent / sold listings inside a bounding box.
- **Details** — fetch full property data for a home or apartment by Zillow URL or ID.

## Features

- Search for sale, for rent, and sold listings
- Property detail extraction by URL or ID (homes and apartment complexes)
- Listing normalization: prices and missing fields coerced to numeric-or-`None`
- Browser TLS impersonation via [`curl_cffi`](https://github.com/yifeikong/curl_cffi)
- Configurable proxy (explicit or via environment variable)
- Built-in request throttling to avoid 403s
- A reusable `ZillowClient` with a persistent session, per-client rate limiter,
  and optional proxy rotation after a block
- Clear `ZillowBlockedError` when Zillow blocks a request

## Current status (as of 2025-10)

Zillow's anti-bot stack (PerimeterX) is aggressive. What works today:

| Capability | Status | Notes |
|---|---|---|
| `get_from_home_url` / `get_from_home_id` | ✅ Works unauthenticated | No proxy required in testing |
| `get_from_apartment_url` / `get_from_apartment_id` | ✅ Works unauthenticated | No proxy required in testing |
| `for_sale` / `for_rent` / `sold` | ⚠️ Works, but flaky without a proxy | Rapid repeated requests from one IP get **403** |

**Search endpoints are rate-limited per IP.** A single request often
succeeds, but repeated requests will be blocked. For anything beyond a quick
test, use a rotating **residential** proxy. Zillow fingerprints datacenter IPs
and will block them.

When Zillow blocks a request it returns a PerimeterX challenge page. pyzill
detects this and raises `pyzill.ZillowBlockedError` (never a bare
`JSONDecodeError`).

## Install

```bash
pip install git+https://github.com/pshap/pyzill.git
```

## Legal / responsible use

Scraping may conflict with Zillow's Terms of Use. This tool is intended for
personal, low-volume research; users are responsible for complying with
applicable terms and laws.

## CLI Usage

pyzill includes a command-line interface for quick searches and data extraction without writing Python code.

### Basic Commands

```bash
# Search for-sale listings (place name is geocoded automatically)
pyzill search sale "Schenectady NY" --min-beds 2 --max-price 400000

# Or give an explicit bounding box (skips geocoding)
pyzill search sale "12305" \
  --north 42.825 --south 42.805 --east -73.925 --west -73.955 \
  --zoom 15

# Search for-rent listings
pyzill search rent "12305" --room

# Fetch home details by zpid or URL
pyzill detail home 348914888
pyzill detail home "https://www.zillow.com/homedetails/27-Washington-Ave/348914888_zpid/"

# Fetch apartment details
pyzill detail apartment CgKZT4
pyzill detail apartment "https://www.zillow.com/apartments/kissimmee-fl/the-nexus-at-overbrook/9DSWrh/"

# Normalize raw listing data from file or stdin
pyzill normalize listings.json
cat raw_search.json | pyzill normalize -

# Offline re-slice of previously saved search results (no network)
pyzill query listings.json --where 'city==Schenectady' --where 'ppsf>=130' --sort ppsf:asc
```

### Output Formats

The CLI supports multiple output formats:

```bash
# JSON (default)
pyzill search sale "12305" --format json

# JSON Lines (one JSON object per line)
pyzill search sale "12305" --format jsonl

# CSV
pyzill search sale "12305" --format csv

# Human-readable table
pyzill search sale "12305" --format table
```

Output to a file:

```bash
pyzill search sale "12305" --output results.json
pyzill search sale "12305" --output -  # stdout (default)
```

### Search Filters

```bash
# Price range
pyzill search sale "12305" --min-price 100000 --max-price 500000

# Bedrooms and bathrooms
pyzill search sale "12305" --min-beds 2 --max-beds 4 --min-bathrooms 1

# Rental-specific filters
pyzill search rent "12305" --room              # rooms for rent
pyzill search rent "12305" --entire-place      # entire places for rent
```

### Geocoded search

You don't need to know the bounding box. If you omit
`--north/--south/--east/--west`, the CLI geocodes the search query with the
OpenStreetMap Nominatim service and uses the returned viewport:

```bash
pyzill search sale "Schenectady NY" --max-price 400000 --min-beds 3
```

- Results are cached on disk (`~/.cache/pyzill/geocode.json`, override with
  `XDG_CACHE_HOME`) so repeated searches for the same place are instant.
- Pass an explicit bbox to skip geocoding entirely.
- `--proxy` applies to Zillow requests; Nominatim geocoding is direct by default.
- If geocoding fails, the CLI exits with code 1 and tells you to pass an
  explicit bbox.

### Offline re-slicing (`query`)

`pyzill query` filters previously saved search results on your machine —
no network, no rate limit, no 403 risk. Fetch once, slice many times:

```bash
# Fetch and save
pyzill search sale "Schenectady NY" --output listings.json

# Re-slice offline as many times as you like
pyzill query listings.json --where 'city==Schenectady' --where 'area>=1000'
pyzill query listings.json --where 'home_type==SINGLE_FAMILY' --sort price:asc --limit 20
pyzill query listings.json --where 'ppsf>=130' --where 'ppsf<=200' --format csv
cat listings.json | pyzill query - --where 'zipcode==12308' --format table
pyzill query listings.json --where-json '[{"field":"price","op":"<=","value":300000}]'
```

**Filtering** — repeat `--where FIELD OP VALUE` for AND logic:

| Operator | Meaning |
|---|---|
| `==` or `=` | equals |
| `!=` | not equals |
| `>`, `>=`, `<`, `<=` | numeric order |
| `~` | case-insensitive substring |

- Numbers compare numerically; `--where 'price>=200000'` and
  `--where 'price>=$200,000'` both work.
- Numeric fields (`price`, `beds`, `baths`, `area`, `ppsf`, estimates, and
    coordinates) compare numerically. Identifier and descriptive fields stay
    strings, so `zipcode==02134` preserves the leading zero. Quoted values are
    always treated as strings.
- Strings compare case-insensitively, so `city==schenectady` matches
  `"Schenectady"`.
- `null` / `none` match missing values.
- `--where-json` accepts a JSON array of `{ "field", "op", "value" }`
    objects and can be combined with `--where`.

**Fields** — the normalized listing fields (`zpid`, `price`, `beds`, `baths`,
`area`, `address`, `city`, `state`, `zipcode`, `latitude`, `longitude`,
`home_type`, `status`, `status_type`, `detail_url`, `image_url`, `zestimate`,
`rent_zestimate`) plus one computed field: `ppsf` (price per square foot,
rounded to 2 decimals; listings without price or area are excluded by
`ppsf` filters).

**Sorting** — `--sort FIELD:asc` or `--sort FIELD:desc` (default `asc`);
missing values always sort last.

**Columns** — `--cols zpid,price,beds,area,ppsf` selects and orders output
columns in table and CSV formats.

### Local database

pyzill includes a SQLite database for tracking listings over time with price history. The database lives in the XDG data directory (`$XDG_DATA_HOME/pyzill/pyzill.db`, falling back to `~/.local/share/pyzill/pyzill.db`) by default (configurable via `--db` or `PYZILL_DB`).


```bash
# Sync from Zillow (fetch + upsert)
pyzill db sync sale "Schenectady NY" --max-price 400000 --min-beds 2
pyzill db sync rent "12305" --room
pyzill db sync sold "Albany NY"

# Import from a saved file (offline ingest)
pyzill db import listings.json
pyzill db import - --fetched-at 2026-10-17T12:00:00Z  # stdin with custom timestamp
pyzill db import rentals.json --kind rent

# Query the database (reuses --where/--sort/--cols/--limit from pyzill query)
pyzill db query --where 'city==Schenectady' --where 'last_seen_utc>=2026-10-01' --sort price:asc
pyzill db query --format table --cols address,price,first_seen_utc,last_seen_utc

# Query listing changes (new, price drops/increases, status transitions, delistings)
pyzill db changes 2026-10-01T00:00:00Z

# Show price history for a single listing
pyzill db history 32463997

# Database statistics
pyzill db stats
```

**Database path resolution** — `--db PATH` > `PYZILL_DB` env var > `$XDG_DATA_HOME/pyzill/pyzill.db` (falling back to `~/.local/share/pyzill/pyzill.db`). If `./pyzill.db` exists in the working directory and no explicit path is given, a warning is printed to stderr pointing to the new default location. The schema is created automatically on first use (no init command needed).

**Schema** (version 3) — The database tracks:
- `listings` table: latest state view with composite primary key `(zpid, kind)` so a sale and rent for the same zpid do not overwrite each other, plus `first_seen_utc` and `last_seen_utc`
- `price_history` table: change-only priced sightings keyed by `(zpid, kind, seen_utc)`
- `status_history` table: transition history tracking status changes (pending/contingent/active) keyed by `(zpid, kind, status, first_seen_utc)`
- `sync_runs` table: search sync runs recording query, bounds, filters, result count, and whether the run was truncated
- `sightings` table: each search observation per run, preserving the raw payload (`raw_json`) for re-normalization


**Upsert semantics** (one transaction per sync/import):
- New zpid → insert row and its first priced sighting
- Existing zpid → preserve known fields when a sighting omits them; `price: null` keeps the last known price of that kind
- Older sightings extend history but cannot overwrite current listing state; first/last-seen timestamps track the actual minimum/maximum
- A kind change updates the current listing kind and is not reported as a price change
- Rows without zpid are skipped and counted

**Timestamps** are fixed-width UTC ISO strings (`%Y-%m-%dT%H:%M:%SZ`) — sortable lexicographically, so the existing `--where`/`--sort` machinery works on them unchanged.

**Workflow example**:

```bash
# Initial sync
pyzill db sync sale "Schenectady NY" --max-price 400000
# Synced 15 listings: 15 new, 0 updated (0 price changes), 0 skipped

# Re-sync later (price changes tracked)
pyzill db sync sale "Schenectady NY" --max-price 400000
# Synced 15 listings: 0 new, 15 updated (2 price changes), 0 skipped

# Query with timestamp filter
pyzill db query --where 'last_seen_utc>=2026-10-01' --format table

# Check price history for a specific property
pyzill db history 32463997
# [{"price": 250000, "seen_utc": "2026-10-01T12:00:00Z"}, {"price": 260000, "seen_utc": "2026-10-15T12:00:00Z"}]
```

`db history` accepts `--format json|jsonl|csv|table`. `db stats` accepts
`--format json`; it uses human-readable text on a terminal and JSON when
stdout is redirected. `db sync` and `db import` accept `--format json` to emit
their count summary to stdout instead of the human summary on stderr.

`pyzill schema listing` prints the versioned JSON Schema for normalized
listings.

### Exploring detail responses

Detail responses are large nested JSON blobs. Two flags help you find your
way around before you write code:

```bash
# List top-level fields with their types
pyzill detail home 32463997 --keys

# Extract one field or nested path (dotted, with [n] list indices)
pyzill detail home 32463997 --path address.city          # -> "Schenectady"
pyzill detail home 32463997 --path address               # -> full dict
pyzill detail home 32463997 --path "homeInsights[0].insights[0].phrases"
```

`--keys` and `--path` cannot be combined (usage error, exit 2). `--path`
exits 1 with a message showing where the path broke if the field is missing.

### Raw vs Normalized Output

```bash
# Raw search response (includes mapResults and listResults structure)
pyzill search sale "12305" --raw

# Normalized listings (default: flat dict with numeric-or-None values)
pyzill search sale "12305"
```

### Global Options

Global options must come before the command:

```bash
# Proxy configuration
pyzill --proxy "http://user:pass@proxy:8080" search sale "12305"

# TLS impersonation profile
pyzill --impersonate chrome120 search sale "12305"

# Request timeout and retries
pyzill --timeout 60 --max-retries 3 search sale "12305"

# Rate limit (seconds between requests; 0 disables throttling)
pyzill --rate-limit 2.5 search sale "12305"

# Quiet mode (suppress progress and diagnostics)
pyzill --quiet search sale "12305"

# Verbose mode (additional diagnostics)
pyzill --verbose search sale "12305"

# Disable colors
pyzill --no-color search sale "12305"

# Show version
pyzill --version
```

### Environment Variables

The CLI respects these environment variables:

- `PYZILL_PROXY` - Proxy URL (overridden by `--proxy`; legacy `ZILLOW_PROXY` is a fallback)
- `PYZILL_IMPERSONATE` - TLS impersonation profile (overridden by `--impersonate`)
- `PYZILL_RATE_LIMIT` - Request interval in seconds (overridden by `--rate-limit`; legacy `ZILLOW_MIN_INTERVAL` is a fallback)
- `PYZILL_DB` - Database path for local tracking (overridden by `--db`)
- `NO_COLOR` / `PYZILL_NO_COLOR` - Disable colors (same as `--no-color`)

Example:

```bash
export PYZILL_PROXY="http://user:pass@proxy:8080"
pyzill search sale "12305"  # uses proxy from environment
```

### Exit Codes

- `0` - Success (including zero results, --help, --version)
- `2` - Invalid invocation (missing arguments, invalid options, unsupported combinations)
- `3` - Zillow blocked the request
- `4` - Requested Zillow resource was not found
- `5` - Zillow response could not be parsed or its schema drifted
- `6` - Transient server or network failure
- `1` - Other errors

Errors are JSON objects on stderr when stdout is not a TTY. Use
`--json-errors` to force JSON errors or `--no-json-errors` for human-readable
diagnostics. The JSON shape is `{"error":{"code":"blocked","message":"...","retryable":false,"status_code":403}}`;
`status_code` is omitted when unavailable. Proxy credentials are redacted.

### Module Invocation

You can also invoke the CLI as a Python module:

```bash
python -m pyzill search sale "12305"
python -m pyzill --version
```

## Quick start

```python
import pyzill

results = pyzill.for_sale(
    search_value="Schenectady NY",
    bbox=pyzill.BBox(north=42.85, east=-73.90, south=42.78, west=-74.00),
    zoom_value=12,
)

map_results = results["mapResults"]  # up to 500 listings
print(len(map_results), "listings")
```

For repeated work, create one client to reuse its cookie jar and connections:

```python
with pyzill.ZillowClient(proxy=["http://proxy-a:8080", "http://proxy-b:8080"]) as client:
    sale = client.search_sale(
        search_value="Schenectady NY",
        bbox=pyzill.BBox(north=42.85, east=-73.90, south=42.78, west=-74.00),
    )
    home = client.home(sale["mapResults"][0]["zpid"])
```

Module-level functions remain available and use a lazy default client. Search
and detail requests from a client share its limiter and session; on a 403 with
multiple proxies, the client retries once with a fresh session and the next
proxy. Retry-After is capped at 60 seconds by default and can be configured on
the client.

> **Coordinates define the results.** Zillow searches the `mapBounds`
> viewport you pass. `search_value` labels the search but does **not**
> tighten the viewport. Omitting `bbox` raises `ValueError` — a missing bbox
> is an explicit choice, not a silent country-wide search. Either pass a
> `BBox` (or use `split_bbox` to tile a region), resolve one with
> `pyzill.geocode.geocode("Schenectady NY")`, or opt in to the
> continental-US viewport with `allow_nationwide=True`.

### Search with filters and an explicit bounding box

```python
import pyzill

results = pyzill.for_sale(
    search_value="Schenectady NY",
    bbox=pyzill.BBox(north=42.85, east=-73.90, south=42.78, west=-74.00),
    zoom_value=12,
    min_beds=2,
    max_beds=4,
    min_bathrooms=1,
    min_price=100_000,
    max_price=500_000,
)
for listing in results["mapResults"]:
    print(listing["zpid"], listing.get("address"))
```

`for_rent` and `sold` take the same arguments. `for_rent` also accepts
`is_entire_place` and `is_room`.

`for_sale`, `for_rent`, and `sold` build their own `filter_state`, so they
**reject** a caller-supplied `filter_state` with a `ValueError`. Use `search()`
when you need a custom `filter_state`. All search functions **require** a
`bbox` unless you pass `allow_nationwide=True`.

### Proxy configuration

Search is much more reliable through a residential proxy. Pass one explicitly:

```python
import pyzill

bbox = pyzill.BBox(north=42.85, east=-73.90, south=42.78, west=-74.00)
proxy_url = pyzill.parse_proxy("proxy.example.com", "8080", "username", "password")
results = pyzill.for_sale(search_value="Schenectady NY", bbox=bbox, proxy_url=proxy_url)
```

Or set `PYZILL_PROXY` once and omit `proxy_url` (`ZILLOW_PROXY` remains a
fallback for existing environments)
everywhere:

```bash
export PYZILL_PROXY="http://user:pass@proxy.example.com:8080"
```

```python
import pyzill

bbox = pyzill.BBox(north=42.85, east=-73.90, south=42.78, west=-74.00)
results = pyzill.for_sale(search_value="Schenectady NY", bbox=bbox)  # uses PYZILL_PROXY
```

An explicit `proxy_url` argument always wins over environment settings. Supported
schemes: `http`, `https`, `socks5`, `socks5h`.

### Handling blocks

```python
import pyzill

bbox = pyzill.BBox(north=42.85, east=-73.90, south=42.78, west=-74.00)
try:
    results = pyzill.for_sale(search_value="Schenectady NY", bbox=bbox)
except pyzill.ZillowBlockedError as exc:
    print("blocked:", exc.status_code)
    # retry through a residential proxy, or back off
```

### Iterating and working around the 500-listing cap

`mapResults` caps at **500 listings per query**. `pagination` does *not*
increase this cap — it only affects the sidebar `listResults`. To page through
individual for-sale listings use `iter_search` (or use the explicit
`iter_sale`, `iter_rent`, and `iter_sold` variants):

```python
import pyzill

bbox = pyzill.BBox(north=42.85, east=-73.90, south=42.78, west=-74.00)
for listing in pyzill.iter_sale(search_value="Schenectady NY", bbox=bbox):
    print(listing["zpid"])
```

At exactly 500 results Zillow may have truncated the response. The CLI warns
and supports bounded recursive tiling with `--tile` (the default maximum depth
is 4; adjust it with `--max-depth`, up to 6). Tiling is also bounded by a
request budget: `--max-requests` (default 50) caps how many search requests
`tiled_search` will issue. Without a budget, deep tiling can fire tens of
thousands of requests; when the budget runs out, a warning goes to stderr and
the partial results are returned (the library flags them with
`budget_exhausted=True`). Progress (tile count and listings so far) is
printed to stderr and suppressed by `--quiet`.

```bash
pyzill search sale "Schenectady NY" --tile --max-depth 5 --max-requests 100
pyzill db sync sale "Schenectady NY" --tile
```

For manual control, split an area with `split_bbox` and run one search per tile:

```python
import pyzill

quadrants = pyzill.split_bbox(pyzill.BBox(north=42.85, east=-73.90, south=42.78, west=-74.00))
for bbox in quadrants:
    results = pyzill.for_sale(search_value="Schenectady NY", bbox=bbox)
    print(len(results["mapResults"]))
```

### Tuning requests

All search functions accept:

- `impersonate` (default `"chrome136"`) — the `curl_cffi` TLS profile.
- `timeout` (default `30.0` seconds).
- `max_retries` (default `1`) — retries on 5xx, 429, and network errors. 403
    responses are not retried.

```python
results = pyzill.for_sale(
    search_value="Schenectady NY",
    bbox=pyzill.BBox(north=42.85, east=-73.90, south=42.78, west=-74.00),
    impersonate="chrome136",
    timeout=60,
    max_retries=3,
)
```

### Normalizing listings (prices and missing fields)

Raw Zillow listings are inconsistent: `price` is a preformatted string
(`"$329,900"`), and `beds`/`baths`/`area` can be missing, `None`, or `0`.
`normalize_listing` flattens a listing into predictable, numeric-or-`None`
fields, falling back to `hdpData.homeInfo` when a top-level field is absent:

Non-dict entries are skipped by the normalization helpers. Zero beds are
preserved for studios unless positive nested data is available; zero area is
treated as unknown and falls back to positive nested data. `zpid` is always an
integer or `None`.

```python
import pyzill

results = pyzill.for_sale(
    search_value="12305",
    bbox=pyzill.BBox(north=42.825, east=-73.925, south=42.805, west=-73.955),
    zoom_value=15,
)

for home in pyzill.normalize_listings(results["mapResults"]):
    print(home["address"], home["price"], home["beds"], home["home_type"])
```

`normalize_listing` returns keys: `zpid`, `price` (int or `None`), `price_label`,
`beds`, `baths`, `area`, `address`, `street`, `city`, `state`, `zipcode`,
`latitude`, `longitude`, `home_type`, `status`, `status_type`, `detail_url`
(absolute), `image_url`, `zestimate`, `rent_zestimate`.

The output shape is available as a `TypedDict` for static analysis (no runtime
dependency):

```python
from pyzill import Listing

home: Listing = pyzill.normalize_listing(results["mapResults"][0])
```

Use `parse_price` on its own to coerce a single value. It handles K/M/B
suffixes and rental adornments (`/mo`, `/month`, trailing `+`):

```python
>>> pyzill.parse_price("$1,200,000")
1200000
>>> pyzill.parse_price("$550K")
550000
>>> pyzill.parse_price("$2,500/mo")   # rental price
2500
>>> pyzill.parse_price("$1,800+")     # "starting at" price
1800
>>> pyzill.parse_price("$0")   # Zillow uses 0 for "unknown"
None
>>> pyzill.parse_price("Contact for price")
None
```

Normalize iterator output explicitly with `normalize_listings`:

```python
import pyzill

bbox = pyzill.BBox(north=42.825, east=-73.925, south=42.805, west=-73.955)
for home in pyzill.normalize_listings(pyzill.iter_sale(search_value="12305", bbox=bbox)):
    print(home["price"])
```

### Rate limiting

pyzill throttles all requests to Zillow so that consecutive calls are spaced
apart (default **1.5s** plus up to 0.5s jitter). This is enough to avoid most
403s from single-shot loops without a proxy:

```python
import pyzill

bbox = pyzill.BBox(north=42.85, east=-73.90, south=42.78, west=-74.00)
for _ in range(5):
    results = pyzill.for_sale(search_value="Schenectady NY", bbox=bbox)
    print(len(results["mapResults"]))
```

Configure the interval with `PYZILL_RATE_LIMIT` (seconds; `ZILLOW_MIN_INTERVAL`
is the legacy fallback) or at runtime:

```bash
export PYZILL_RATE_LIMIT=3   # wait 3s between requests
```

```python
import pyzill

pyzill.set_min_interval(3)  # or 0 to disable
print(pyzill.get_min_interval())
```

For high-volume use, still use a rotating residential proxy — per-process
throttling cannot hide that all requests come from one IP.

## For homes (details)

Details work unauthenticated — no proxy required.

Detail functions also accept `proxy_url`, `impersonate`, `timeout`, and
`max_retries` (default `1`, retrying 5xx responses and curl request errors).

```python
import pyzill

data = pyzill.get_from_home_id(348914888)
print(data["address"])  # {'streetAddress': '27 Washington Ave', ...}
```

By URL:

```python
import pyzill

data = pyzill.get_from_home_url(
    "https://www.zillow.com/homedetails/27-Washington-Ave-Schenectady-NY-12305/348914888_zpid/"
)
```

## For apartments (apartment complexes)

```python
import pyzill

data = pyzill.get_from_apartment_id("CgKZT4")
```

```python
import pyzill

data = pyzill.get_from_apartment_url("https://www.zillow.com/apartments/kissimmee-fl/the-nexus-at-overbrook/9DSWrh/")
```

> The old `department` names and historical `deparment` misspellings remain
> as deprecated aliases and emit `DeprecationWarning`.

## API

| Function | Description |
|---|---|
| `for_sale(...)` | For-sale listings inside a bounding box (bbox required unless `allow_nationwide=True`) |
| `for_rent(...)` | For-rent listings (also `is_entire_place`, `is_room`) |
| `sold(...)` | Recently-sold listings |
| `search(...)` | Raw search with a custom `filter_state` (the three above reject `filter_state`) |
| `iter_search(max_items=None, **kwargs)` / `iter_sale(...)` / `iter_rent(...)` / `iter_sold(...)` | Yield individual listings from the first `mapResults` response |
| `tiled_search(search_fn, bbox=..., max_depth=4, max_requests=50, **kwargs)` | Recursively split capped results, deduplicate by zpid, stop at the request budget |
| `split_bbox(bbox)` | Split a `BBox` into 4 quadrants |
| `get_from_home_url(url, ...)` / `get_from_home_id(id, ...)` | Home details |
| `get_from_apartment_url(url, ...)` / `get_from_apartment_id(id, ...)` | Apartment details |
| `parse_proxy(host, port, user, password)` | Build a proxy URL |
| `resolve_proxy(proxy_url=None)` | Resolve `proxy_url`, `PYZILL_PROXY`, or legacy `ZILLOW_PROXY` |
| `set_min_interval(seconds)` / `get_min_interval()` | Configure request throttling |
| `normalize_listing(listing)` / `normalize_listings(listings)` | Flatten raw listings to numeric-or-`None` fields |
| `Listing` | `TypedDict` describing the normalized listing shape |
| `parse_price(value)` | Parse a Zillow price string/number into an int |
| `ZillowBlockedError` | Raised for block/rate-limit responses (also remains a base for server errors) |
| `ZillowNotFoundError` / `ZillowServerError` | HTTP 404 / final 5xx response |
| `ZillowParseError` | Successful page response with unexpected or missing data |

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest            # run tests
ruff check .      # lint
ruff format .     # format
mypy              # type-check (strict; config in pyproject.toml)
```

The `@pytest.mark.live` smoke checks are skipped by default and in CI. To opt
in, set `PYZILL_LIVE=1`. They hit a fixed Schenectady viewport for
sale/rent/sold search, resolve a zpid from the sale search for the home-detail
check, and exercise the live Nominatim geocode path once.

## License

MIT
