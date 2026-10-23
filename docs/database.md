# Local database

`pyzill db` stores searches and imports in SQLite and answers questions
about them offline.

## Stored data

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

# List the recorded sync runs, newest first
pyzill db runs
pyzill db runs --kind rent --limit 10

# Delete old runs and their raw sightings (listings and prices are kept)
pyzill db prune --older-than 90
pyzill db prune --keep-runs 20 --vacuum

# Fetch property details for shortlisted listings and store them
pyzill db enrich --where 'price<400000' --max-requests 50

# Database statistics
pyzill db stats
```

**Database path resolution** — `--db PATH` > `PYZILL_DB` env var > `$XDG_DATA_HOME/pyzill/pyzill.db` (falling back to `~/.local/share/pyzill/pyzill.db`). If `./pyzill.db` exists in the working directory and no explicit path is given, a warning is printed to stderr pointing to the new default location. The schema is created automatically on first use (no init command needed).

**Schema** (version 4) — The database tracks:
- `listings` table: latest state view with composite primary key `(zpid, kind)` so a sale and rent for the same zpid do not overwrite each other, plus `first_seen_utc` and `last_seen_utc`
- `price_history` table: change-only priced sightings keyed by `(zpid, kind, seen_utc)`
- `status_history` table: transition history tracking status changes (pending/contingent/active) keyed by `(zpid, kind, status, first_seen_utc)`
- `property_details` table: the detail page fields for shortlisted listings — HOA fee and unit, description, photo count, year built, lot size and unit, tax assessed value, tax history, price history detail, schools, the raw payload, and the moment it was enriched. Keyed by `(zpid, kind)` with a foreign key that cascades deletes into listings
- `sync_runs` table: search sync runs recording query, bounds, filters, result count, and whether the run was truncated
- `sightings` table: each search observation per run, preserving the raw payload (`raw_json`) for re-normalization


**Upsert semantics** (one transaction per sync/import):
- New zpid → insert row and its first priced sighting
- Existing zpid → preserve known fields when a sighting omits them; `price: null` keeps the last known price of that kind
- Older sightings extend history but cannot overwrite current listing state; first/last-seen timestamps track the actual minimum/maximum
- Each `(zpid, kind)` pair keeps its own row, so a sale and a sold listing for the same zpid are separate rows with separate price histories
- Rows without zpid are skipped and counted

A search drops a card without a usable zpid before it reaches the write path,
so it is not counted there; an import passes such rows through, so the count
reports them.

**Sync counts** are per listing, not per input row: a zpid that appears twice
in one batch is one listing, counted once, and a listing whose price changed
counts one price change however many sightings of it the batch carried. The
summary line therefore reports how many listings were synced, and a run's
stored `result_count` matches the number of sightings it kept.

**Change events** — every event `db changes` reports carries the timestamp it
happened at (`seen_utc`), and the events are ordered by it, so they can be
replayed in the order they occurred. A `new` event is stamped with the moment
the listing was first seen.

**Delisting detection** — `db changes` reports a listing as delisted when it
appeared in one search result set and is missing from a later one. Only search
runs are compared with each other: two `db sync` runs of the same query,
viewport, and filters. The tiling knobs (`--tile`, `--max-depth`,
`--max-requests`) are not part of that comparison — they describe how a run was
carried out, not what it searched. File imports (`db import`) are never
compared, because a listing missing from an imported file was never observed as
gone — only the price and status changes they bring in are reported.

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
their count summary to stdout instead of the human summary on stderr. That
summary names the run (`run_id`) and says whether the run was complete
(`truncated`), so a script can tell a complete run from a partial one without
reading the warnings on stderr.

**Pruning growth** — the listings and their price history are the durable
record, while each sync run adds a sighting with the raw payload. `db prune`
drops whole old runs and lets the foreign key cascade their sightings.
`--older-than DAYS` deletes runs that started before the cutoff and
`--keep-runs N` keeps only the newest N runs; either or both may be given.
Listings and price history are never deleted, so a pruned database still
answers "what do I know about this property?". `--vacuum` rebuilds the file
afterwards to reclaim the space. The command emits a JSON summary
(`runs_deleted`, `sightings_deleted`, `vacuumed`), and the same operation is
available from Python as `pyzill.prune_database`.

`db query` always writes valid JSON to stdout in `--format json`, including an
empty array (`[]`) when nothing matches, so its output can be parsed without
special-casing an empty response. The other formats print a warning and no
rows instead.

**Enriching** — the search tables record what a search returned, but the detail
page — HOA fees, tax history, descriptions, schools, and photos — only ever
appeared as one-off command output, so it could not be filtered or searched.
`db enrich` fetches the detail for shortlisted listings and stores it in the
`property_details` table. Shortlist with the same `--where`/`--sort`/`--limit`
flags the query command uses, and bound the request volume with `--max-requests`
(default 50; `0` disables the budget). The detail fields then become queryable
through the same engine, so "no HOA, taxes under $6k, description mentions
'garage'" is a query:

```bash
pyzill db enrich --where 'price<400000' --max-requests 50
pyzill db query --where 'hoa_fee=0' --where 'tax_assessed_value<6000' --where 'description ~ garage'
```

`tax_assessed_value` prefers the listings column and falls back to the details
column, so a listing enriched before the field existed on the detail page still
filters. `--cols` can project detail fields, and `--sort` can order by them.
Enriching is idempotent: re-running it overwrites the stored detail and updates
`enriched_at_utc`, and a listing with no detail row is simply absent from a
detail-field query. The command emits a JSON summary (`enriched`) when stdout is
redirected or `--format json` is given, and is available from Python as
`pyzill.upsert_property_details` / `pyzill.get_property_details`.

`pyzill schema listing` prints the versioned JSON Schema for normalized
listings. Four more schemas are published for the other shapes pyzill emits:

```bash
pyzill schema db-listing   # a row of the database's listings table, as db query returns it
pyzill schema db-detail    # a row of the database's property_details table, as db query returns it when it projects a detail field
pyzill schema detail       # the normalized property detail
```

`schema db-listing` describes every column the `listings` table has -- the
listing fields plus the `kind` and the two timestamps -- and the computed
`ppsf` that `--cols ppsf` projects. No property is required, because
`--cols` projects a subset of them; a property the table does not have is
still rejected.

