# pyzill — Zillow scraper in Python

An open-source Python library and CLI for extracting real-estate data from
Zillow.

- **Search** — for-sale, for-rent, and sold listings inside a bounding box.
- **Details** — full property data for a home or apartment by Zillow URL or ID.
- **History** — optional SQLite storage that tracks price and status changes
  across searches, with offline queries.

## Features

- Search sale, rent, and sold listings, with optional bounded tiling for
  results beyond Zillow's 500-listing cap.
- Property details by URL or zpid (homes and apartment complexes).
- Listing normalization: prices and missing fields coerced to
  numeric-or-`None`.
- Browser TLS impersonation via [`curl_cffi`](https://github.com/yifeikong/curl_cffi),
  with optional proxy rotation after a block.
- Built-in, process-wide request throttling and a bounded request budget.
- A reusable `ZillowClient`, plus module-level entry points over a lazy default
  client.
- Clear exception types (`ZillowBlockedError`, `ZillowNotFoundError`,
  `ZillowParseError`, `TiledSearchError`, ...) with stable CLI exit codes.
- A local SQLite database with sync runs, sightings, price history, and
  offline queries.

## Current status (as of 2026-10)

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

Python 3.12 or newer is required.

## Quick start

Search and normalize from Python:

```python
import pyzill

results = pyzill.for_sale(
    search_value="Schenectady NY",
    bbox=pyzill.BBox(north=42.85, east=-73.90, south=42.78, west=-74.00),
    zoom_value=12,
)
for home in pyzill.normalize_listings(results["mapResults"]):
    print(home["zpid"], home["price"], home["address"])
```

Or from the command line:

```bash
pyzill search sale "Schenectady NY" --min-beds 2 --max-price 400000
pyzill detail home 348914888
pyzill db sync sale "Schenectady NY" --db pyzill.db
```

See the [command-line guide](docs/cli.md), the [Python API guide](docs/python.md),
and the [database guide](docs/database.md) for the full surface.

## Scope and non-goals

pyzill is a small, request-bounded client for Zillow's public pages.

In scope:

- Reading public search results and property-detail pages.
- Normalizing those payloads into documented, stable shapes.
- Tracking the listings you have already seen in a local SQLite database.
- Reporting failures in a way a script can act on.

Out of scope (deliberately):

- **No official API access.** pyzill does not use a Zillow account, API key,
  or authenticated endpoint, and it does not manage saved searches.
- **No browser automation or CAPTCHA solving.** Blocked requests are reported,
  not worked around.
- **No bulk scraping.** Tiling is bounded to a default of depth 4 and 50
  requests; those limits exist to keep a single run polite.
- **No guarantee of completeness or freshness.** Zillow changes its payloads
  and caps each response at 500 listings. Treat the data as a snapshot, not
  as a source of truth.
- **No financial advice.** Prices, estimates, and status fields are reported
  as Zillow sent them.

## Legal / responsible use

Scraping may conflict with Zillow's Terms of Use. This tool is intended for
personal, low-volume research; users are responsible for complying with
applicable terms and laws. pyzill is not affiliated with or endorsed by
Zillow. Do not use it to overload Zillow, to collect personal data, or to
redistribute listing content.

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
scripts/check.sh        # format, lint, types, dead code, offline tests
```

See [docs/development.md](docs/development.md) for the test layout and the
payload-shape contract.

## License

MIT
