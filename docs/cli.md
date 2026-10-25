# Command line

The `pyzill` command runs searches, fetches details, normalizes saved
payloads, and works with the local database without writing Python.

## Basic Commands

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

## Output Formats

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

A table of listings shows the listing columns. A table of anything else --
the rows of `db changes`, the history of `db history`, the runs of
`db runs` -- shows the columns those rows carry.

Output to a file:

```bash
pyzill search sale "12305" --output results.json
pyzill search sale "12305" --output -  # stdout (default)
```

## Search Filters

```bash
# Price range
pyzill search sale "12305" --min-price 100000 --max-price 500000

# Bedrooms and bathrooms
pyzill search sale "12305" --min-beds 2 --max-beds 4 --min-bathrooms 1

# Rental-specific filters
pyzill search rent "12305" --room              # rooms for rent
pyzill search rent "12305" --entire-place      # entire places for rent
```

## Geocoded search

You don't need to know the bounding box. If you omit
`--north/--south/--east/--west`, the CLI geocodes the search query with the
OpenStreetMap Nominatim service and uses the returned viewport:

```bash
pyzill search sale "Schenectady NY" --max-price 400000 --min-beds 3
```

- Results are cached on disk (`~/.cache/pyzill/geocode.json`, override with
  `XDG_CACHE_HOME`) so repeated searches for the same place are instant. The
  cache is updated under a lock, so concurrent processes don't lose each
  other's entries. Entries expire after 30 days, and the cache keeps the
  1000 most recent places.
- Pass an explicit bbox to skip geocoding entirely.
- `--proxy` applies to Zillow requests; Nominatim geocoding is direct by default.
- If geocoding fails, the CLI exits with code 1 and tells you to pass an
  explicit bbox.

To resolve a place on its own — to get the viewport for your own script, or
to pass to another command — use the `geocode` command:

```bash
pyzill geocode "Schenectady NY"
# {"place": "Schenectady NY", "display_name": "Schenectady, NY, USA",
#  "north": 42.9, "south": 42.6, "east": -73.8, "west": -74.1,
#  "synthetic_bbox": false}
```

The four bounds are the values `--north/--south/--east/--west` take. The
command prints JSON only. A place that does not resolve exits with code 1; a
Nominatim service that cannot be reached exits with code 6 and reports the
error as retryable.

## Offline re-slicing (`query`)

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
- `null` / `none` match missing values. They work with `=` and `!=` only:
  comparing a value with `null` using `>`, `<`, `>=`, `<=` or `~` can never be
  true, so those are rejected as usage errors instead of quietly matching
  nothing.
- `--where-json` accepts a JSON array of `{ "field", "op", "value" }`
    objects and can be combined with `--where`. It applies the same numeric
    validation as the expression form: a non-numeric or boolean value for a
    numeric field is a usage error, and a quoted number is compared as a
    number. `--limit` must be zero or greater (`0` returns no rows).

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


## Exploring detail responses

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

## Raw vs Normalized Output

```bash
# Raw search response (includes mapResults and listResults structure)
pyzill search sale "12305" --raw

# Normalized listings (default: flat dict with numeric-or-None values)
pyzill search sale "12305"
```

## Global Options

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

## Environment Variables

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

## Exit Codes

- `0` - Success (including zero results, --help, --version)
- `2` - Invalid invocation (missing arguments, invalid options, unsupported combinations)
- `3` - Zillow blocked the request, or a recent block cooldown is active
- `4` - Requested Zillow resource was not found
- `5` - Zillow response could not be parsed or its schema drifted
- `6` - Transient server or network failure
- `1` - Other errors

Errors are JSON objects on stderr when stdout is not a TTY. Use
`--json-errors` to force JSON errors or `--no-json-errors` for human-readable
diagnostics. The JSON shape is `{"error":{"code":"blocked","message":"...","retryable":false,"status_code":403}}`;
`status_code` and `retry_after` are omitted when unavailable. Proxy credentials are redacted.

**Safety floors and block memory** — `--max-requests` is capped (default 50; raise with
`PYZILL_MAX_REQUESTS_CEILING`) and `--rate-limit` is floored (default 1.0s; lower with
`PYZILL_MIN_RATE_LIMIT_FLOOR`). Blocked responses are remembered and requests are paused during
an escalating cooldown (15m / 1h / 4h / 24h) at the default state path
`$XDG_STATE_HOME/pyzill/block.json` (or `~/.local/state/pyzill/block.json`). Set
`PYZILL_BLOCK_STATE` to a custom path, or `off` to disable block memory. A successful
network request clears the remembered block. Offline commands and `--plan` never trigger
cooldown enforcement. Network commands support `--plan` to print a JSON plan and exit
without making requests or calling the geocoder.

## Module Invocation

You can also invoke the CLI as a Python module:

```bash
python -m pyzill search sale "12305"
python -m pyzill --version
```


See [database.md](database.md) for `pyzill db`.
