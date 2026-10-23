# Python API

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
and detail requests from a client share its session and the process-wide rate
limit; on a 403 with multiple proxies, the client retries once with a fresh
session and the next proxy, and stays on that proxy afterwards. A `proxy_url`
passed for a single call pins that call to that transport, so it is never
rotated away. Retry-After is capped at 60 seconds by default and can be
configured on the client.

> **Coordinates define the results.** Zillow searches the `mapBounds`
> viewport you pass. `search_value` labels the search but does **not**
> tighten the viewport. Omitting `bbox` raises `ValueError` — a missing bbox
> is an explicit choice, not a silent country-wide search. Either pass a
> `BBox` (or use `split_bbox` to tile a region), resolve one with
> `pyzill.geocode.geocode("Schenectady NY")`, or opt in to the
> continental-US viewport with `allow_nationwide=True`.

## Search with filters and an explicit bounding box

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

## Proxy configuration

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

## Handling blocks

```python
import pyzill

bbox = pyzill.BBox(north=42.85, east=-73.90, south=42.78, west=-74.00)
try:
    results = pyzill.for_sale(search_value="Schenectady NY", bbox=bbox)
except pyzill.ZillowBlockedError as exc:
    print("blocked:", exc.status_code)
    # retry through a residential proxy, or back off
```

## Iterating and working around the 500-listing cap

`mapResults` caps at **500 listings per query**. `pagination` does *not*
increase this cap — it only affects the sidebar `listResults`. The
`iter_search` helper (and the explicit `iter_sale`, `iter_rent`, and
`iter_sold` variants) iterate the listings from a *single* response; they do
not paginate. Use `tiled_search` (or `split_bbox`) to go deeper:

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

If a tile fails after earlier tiles returned listings, `tiled_search` raises
`pyzill.TiledSearchError`. The listings from the completed tiles are on the
error's `partial` attribute (the same shape a successful call returns), and
the error carries the underlying failure's `code`, `retryable`, and
`status_code`, so a blocked run is still reported as blocked rather than
losing the work the earlier tiles did.

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

## Tuning requests

All search functions accept:

- `impersonate` (default `"chrome136"`) — the `curl_cffi` TLS profile. The
  HTTP `User-Agent` and client hints are derived from the same profile, so a
  Safari or Firefox profile no longer presents a Chrome identity.
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

## Normalizing listings (prices and missing fields)

Raw Zillow listings are inconsistent: `price` is a preformatted string
(`"$329,900"`), and `beds`/`baths`/`area` can be missing, `None`, or `0`.
`normalize_listing` flattens a listing into predictable, numeric-or-`None`
fields, falling back to `hdpData.homeInfo` when a top-level field is absent:

Non-dict entries are skipped by the normalization helpers. Zero beds are
preserved for studios unless positive nested data is available; zero area is
treated as unknown and falls back to positive nested data. `zpid` is always an
integer or `None`. String fields are always `str` or `None`, even when
Zillow sends a number — a numeric `addressZipcode` becomes `"12305"`, so it
still matches the zipcode a search was made for.

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

A home-detail page is a different payload with a different shape;
`normalize_property_detail` flattens it into a `PropertyDetail` (lot size
in square feet when its unit says so, HOA fee with its unit). A lot size
whose unit is not stated anywhere is reported as it arrived, with no unit,
rather than assumed to be acres.

The output shape is available as a `TypedDict` for static analysis (no runtime
dependency). `normalize_listing` returns `None` for an entry that is not an
object, so narrow the result before using it:

```python
from pyzill import Listing

home = pyzill.normalize_listing(results["mapResults"][0])
if home is not None:
    listing: Listing = home
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

## Rate limiting

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

The interval is a **process-wide** budget: every `ZillowClient` in a process
shares it, so running several clients cannot multiply the request rate
against Zillow. A client may ask for a stricter interval, never a looser one:

```python
client = pyzill.ZillowClient(rate_limit=3.0)  # at least 3s, and at least the shared budget
client = pyzill.ZillowClient(rate_limit=0)  # explicit opt-out: no throttling
```

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

## Workflows (search, normalize, and sync from Python)

`pyzill.workflows` holds the application workflows the CLI runs, so a
library user can do the same things without going through the command
line. Nothing in this layer prints or exits the process: it returns data
and structured notices, and the caller decides what to report.

The names above are all importable from `pyzill.workflows`; the package is
split by job underneath (`model`, `viewport`, `extract`, `search`, `sync`)
for anyone who wants to import a single piece.

Search and normalize in one call:

```python
from pyzill.geocode import geocode
from pyzill.workflows import SearchFilters, SearchRequest, fetch_listings

bbox = geocode("Schenectady NY").bbox
request = SearchRequest(query="12305", filters=SearchFilters(north=bbox.north, south=bbox.south))
result = fetch_listings("sale", request)

print(len(result.listings), "listings")  # normalized Listing dicts
print(result.truncated)  # did Zillow cap the result?
print(result.notices)  # structured, non-fatal conditions
```

Sync the same search into the local database:

```python
from pyzill.db import connect
from pyzill.workflows import sync_to_db

with connect(":memory:") as conn:
    outcome = sync_to_db(conn, "sale", request)
    print(outcome.stats.added, "new listings")
```

`import_rows` does the same for listings you already have on disk, and
`extract_listings` pulls the listings out of a saved Zillow response.
`select_listings` returns both the response's listing list and the real
listings in it, which is what the sync pipeline uses.

`fetch_listings` returns the real listings of a search: building cards and
cards without a usable zpid are dropped, and when the query is a zipcode only
that zipcode's listings are kept.

## API

| Function | Description |
|---|---|
| `for_sale(...)` | For-sale listings inside a bounding box (bbox required unless `allow_nationwide=True`) |
| `for_rent(...)` | For-rent listings (also `is_entire_place`, `is_room`) |
| `sold(...)` | Recently-sold listings |
| `search(...)` | Raw search with a custom `filter_state` (the three above reject `filter_state`) |
| `iter_search(max_items=None, **kwargs)` / `iter_sale(...)` / `iter_rent(...)` / `iter_sold(...)` | Yield individual listings from the first `mapResults` response |
| `tiled_search(search_fn, bbox=..., max_depth=4, max_requests=50, **kwargs)` | Recursively split capped results, deduplicate by zpid, stop at the request budget |
| `TiledSearchError` | A tile failed mid-run; completed tiles are on `.partial` |
| `split_bbox(bbox)` | Split a `BBox` into 4 quadrants |
| `get_from_home_url(url, ...)` / `get_from_home_id(id, ...)` | Home details |
| `get_from_apartment_url(url, ...)` / `get_from_apartment_id(id, ...)` | Apartment details |
| `parse_proxy(host, port, user, password)` | Build a proxy URL |
| `resolve_proxy(proxy_url=None)` | Resolve `proxy_url`, `PYZILL_PROXY`, or legacy `ZILLOW_PROXY` |
| `set_min_interval(seconds)` / `get_min_interval()` | Configure request throttling |
| `normalize_listing(listing)` / `normalize_listings(listings)` | Flatten raw listings to numeric-or-`None` fields |
| `Listing` | `TypedDict` describing the normalized listing shape |
| `parse_price(value)` | Parse a Zillow price string/number into an int |
| `normalize_property_detail(property_data)` | Flatten a Zillow home-detail page into a `PropertyDetail` (lot size in square feet when its unit says so, HOA fee with its unit) |
| `ZillowBlockedError` | Raised for block/rate-limit responses (also remains a base for server errors) |
| `ZillowNotFoundError` / `ZillowServerError` | HTTP 404 / final 5xx response |
| `ZillowParseError` | Successful page response with unexpected or missing data |

