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
- Clear `ZillowBlockedError` when Zillow blocks a request

## Current status (as of 2025-10)

Zillow's anti-bot stack (PerimeterX) is aggressive. What works today:

| Capability | Status | Notes |
|---|---|---|
| `get_from_home_url` / `get_from_home_id` | ✅ Works unauthenticated | No proxy required in testing |
| `get_from_department_url` / `get_from_department_id` | ✅ Works unauthenticated | No proxy required in testing |
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
pip install pyzill
```

## Quick start

```python
import pyzill

results = pyzill.for_sale(
    search_value="Schenectady NY",
    ne_lat=42.85,
    ne_long=-73.90,  # north-east corner
    sw_lat=42.78,
    sw_long=-74.00,  # south-west corner
    zoom_value=12,
)

map_results = results["mapResults"]  # up to 500 listings
print(len(map_results), "listings")
```

> **Coordinates define the results.** Zillow searches the `mapBounds`
> viewport you pass. `search_value` labels the search but does **not**
> tighten the viewport. If you omit the coordinates, pyzill falls back to a
> continental-US bounding box, so a bare `for_sale(search_value="Schenectady NY")`
> returns up to 500 listings from across the country — not Schenectady. Always
> pass a bounding box (or use `split_bbox` to tile a region) for local results.

### Search with filters and an explicit bounding box

```python
import pyzill

results = pyzill.for_sale(
    search_value="Schenectady NY",
    ne_lat=42.85,
    ne_long=-73.90,
    sw_lat=42.78,
    sw_long=-74.00,
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

### Proxy configuration

Search is much more reliable through a residential proxy. Pass one explicitly:

```python
import pyzill

proxy_url = pyzill.parse_proxy("proxy.example.com", "8080", "username", "password")
results = pyzill.for_sale(search_value="Schenectady NY", proxy_url=proxy_url)
```

Or set the `ZILLOW_PROXY` environment variable once and omit `proxy_url`
everywhere:

```bash
export ZILLOW_PROXY="http://user:pass@proxy.example.com:8080"
```

```python
import pyzill

results = pyzill.for_sale(search_value="Schenectady NY")  # uses ZILLOW_PROXY
```

An explicit `proxy_url` argument always wins over `ZILLOW_PROXY`. Supported
schemes: `http`, `https`, `socks5`, `socks5h`.

### Handling blocks

```python
import pyzill

try:
    results = pyzill.for_sale(search_value="Schenectady NY")
except pyzill.ZillowBlockedError as exc:
    print("blocked:", exc.status_code)
    # retry through a residential proxy, or back off
```

### Iterating and working around the 500-listing cap

`mapResults` caps at **500 listings per query**. `pagination` does *not*
increase this cap — it only affects the sidebar `listResults`. To page through
individual listings use `iter_search`:

```python
import pyzill

for listing in pyzill.iter_search(pyzill.for_sale, search_value="Schenectady NY"):
    print(listing["zpid"])
```

If you hit exactly 500 and need more, split the area into quadrants with
`split_bbox` and run one search per quadrant:

```python
import pyzill

quadrants = pyzill.split_bbox(ne_lat=42.85, ne_long=-73.90, sw_lat=42.78, sw_long=-74.00)
for bbox in quadrants:
    results = pyzill.for_sale(search_value="Schenectady NY", **bbox)
    print(len(results["mapResults"]))
```

### Tuning requests

All search functions accept:

- `impersonate` (default `"chrome136"`) — the `curl_cffi` TLS profile.
- `timeout` (default `30.0` seconds).
- `max_retries` (default `1`) — retries on 5xx / network errors.

```python
results = pyzill.for_sale(
    search_value="Schenectady NY",
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

```python
import pyzill

results = pyzill.for_sale(
    search_value="12305", ne_lat=42.825, ne_long=-73.925, sw_lat=42.805, sw_long=-73.955, zoom_value=15
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

Use `parse_price` on its own to coerce a single value:

```python
>>> pyzill.parse_price("$1,200,000")
1200000
>>> pyzill.parse_price("$0")   # Zillow uses 0 for "unknown"
None
```

`iter_search` can normalize on the fly:

```python
import pyzill

for home in pyzill.iter_search(pyzill.for_sale, normalize=True, search_value="12305"):
    print(home["price"])
```

### Rate limiting

pyzill throttles all requests to Zillow so that consecutive calls are spaced
apart (default **1.5s** plus up to 0.5s jitter). This is enough to avoid most
403s from single-shot loops without a proxy:

```python
import pyzill

for _ in range(5):
    results = pyzill.for_sale(search_value="Schenectady NY")
    print(len(results["mapResults"]))
```

Configure the interval with `ZILLOW_MIN_INTERVAL` (seconds) or at runtime:

```bash
export ZILLOW_MIN_INTERVAL=3   # wait 3s between requests
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

## For departments (apartment complexes)

```python
import pyzill

data = pyzill.get_from_department_id("CgKZT4")
```

```python
import pyzill

data = pyzill.get_from_department_url("https://www.zillow.com/apartments/kissimmee-fl/the-nexus-at-overbrook/9DSWrh/")
```

> Note: the historical `deparment` misspelling (`get_from_deparment_id` /
> `get_from_deparment_url`) is kept as a backwards-compatible alias.

## API

| Function | Description |
|---|---|
| `for_sale(...)` | For-sale listings inside a bounding box |
| `for_rent(...)` | For-rent listings (also `is_entire_place`, `is_room`) |
| `sold(...)` | Recently-sold listings |
| `iter_search(search_fn, max_items=None, **kwargs)` | Yield individual listings |
| `split_bbox(ne_lat, ne_long, sw_lat, sw_long)` | Split a bbox into 4 quadrants |
| `get_from_home_url(url, ...)` / `get_from_home_id(id, ...)` | Home details |
| `get_from_department_url(url, ...)` / `get_from_department_id(id, ...)` | Apartment details |
| `parse_proxy(host, port, user, password)` | Build a proxy URL |
| `resolve_proxy(proxy_url=None)` | Resolve `proxy_url` with `ZILLOW_PROXY` fallback |
| `set_min_interval(seconds)` / `get_min_interval()` | Configure request throttling |
| `normalize_listing(listing)` / `normalize_listings(listings)` | Flatten raw listings to numeric-or-`None` fields |
| `Listing` | `TypedDict` describing the normalized listing shape |
| `parse_price(value)` | Parse a Zillow price string/number into an int |
| `ZillowBlockedError` | Raised on non-2xx responses from Zillow |

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest            # run tests
ruff check .      # lint
ruff format .     # format
mypy              # type-check (strict; config in pyproject.toml)
```

## License

MIT
