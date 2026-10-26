# Phase 2 Audit (report-only)

## Summary

Audit of modules not previously reviewed. Findings ranked High/Medium/Low by
bug risk then maintenance cost. No code changes in this commit.

## Findings

### High

- `src/pyzill/property_detail.py:113` `normalize_property_detail` (complexity 34):
  single function doing parsing, coercion, HOA/tax derivation, school shaping.
  Decompose into: `_detail_scalars`, `_detail_hoa`, `_detail_taxes`,
  `_detail_schools`, `_detail_photos`, `_detail_description`.
- `src/pyzill/normalize.py:195` `normalize_listing` (complexity 21): mixes
  field extraction, status derivation, price/area coercion. Decompose into
  `_extract_raw_fields`, `_derive_status`, `_coerce_numeric_fields`,
  `_finalize_listing`.
- `src/pyzill/http.py:323` `_send` (complexity 16): retry/backoff loop with
  broad except and proxy handling in one body. Decompose into
  `_classify_response`, `_should_retry`, `_apply_backoff`, `_rotate_proxy_on_failure`.
- `src/pyzill/db/details.py:125` `upsert_property_details` (complexity 16):
  combines insert/update classification and column binding. Decompose into
  `_split_new_vs_existing`, `_bind_columns`, `_execute_upsert`.

### Medium

- `src/pyzill/cli/db.py:490` `db_prune` (complexity 13, handled in Group J).
- `src/pyzill/values.py:155` `parse_price` (complexity 13): manual suffix
  parsing; could use regex groups + comprehension.
- `src/pyzill/parse.py:19` `parse_body_home` (complexity 12): nested JSON
  traversal with multiple fallbacks; extract `_walk_path` helper.
- `src/pyzill/http_headers.py:111` `_resolve_profile` (complexity 11):
  isinstance ladder over profile shapes; convert to dispatch table.
- `src/pyzill/workflows/extract.py:60` `select_listings` (complexity 11):
  filter + dedupe + sort in one loop; split into `_filter`, `_dedupe`, `_sort`.
- `src/pyzill/geocode.py`: long module, broad except around HTTP; verify
  proxy credentials never leak into error messages.
- `src/pyzill/cli/console.py`: rendering helpers mix rich console and JSON
  output; consider separating formatters.
- `src/pyzill/cli/search.py`: option building duplicated with cli/db.py
  (see Group J1).
- `src/pyzill/query.py:123` `coerce_value` and `:187` `_parse_where_json`
  (complexity 11 each): handled in Group G3/G4.

### Low

- Mutable module-level state in http_headers profile cache (verify thread
  safety).
- `Any` usage in normalize/property_detail where TypedDict fields could
  narrow types.
- String building in geocode URL construction could use f-strings consistently.

## DRY notes

- HOA/tax derivation shared between migration and upsert (already addressed
  in prior pass).
- Condition parsing duplicated between `_parse_where` and `_parse_where_json`
  (Group G3).

## Risk notes

- Retry/backoff in `http._send` must not loop forever on 429 without a
  retry-after cap.
- Time zone naivety in any `datetime.now()` calls in geocode/workflows.
- Proxy credentials must be redacted in all log/error paths (verify
  `proxy.redact_credentials` usage).
