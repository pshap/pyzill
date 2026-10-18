#!/usr/bin/env python
"""Capture sanitized live Zillow responses into tests/fixtures/.

These fixtures let the offline test suite detect Zillow payload drift that
synthetic conftest bodies cannot. This script hits real Zillow endpoints
(rate-limited by pyzill); run it manually when you want to validate against
current payloads. Captured fixtures contain real listing data and are
gitignored -- keep them local. Payloads are trimmed to --max-results
listings and contact/PII-prone fields (agent, broker, phone, email, MLS,
attribution) are redacted; public listing data (zpid, price, address
components) is kept because the parsers depend on it.

Usage:
    python scripts/capture_fixture.py search-sale --confirm
    python scripts/capture_fixture.py search-rent --confirm
    python scripts/capture_fixture.py search-sold --confirm
    python scripts/capture_fixture.py detail-home --target 348914888 --confirm
    python scripts/capture_fixture.py detail-apartment --target 'https://www.zillow.com/apartment/...' --confirm

Search kinds fetch a small default bbox (override with --north/--south/
--east/--west). Detail kinds need --target (zpid/ID or Zillow URL).
"""

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from pyzill import (
    BBox,
    for_rent,
    for_sale,
    get_from_apartment_id,
    get_from_apartment_url,
    get_from_home_id,
    get_from_home_url,
    sold,
)

_FIXTURE_NAMES = {
    "search-sale": "search_sale.json",
    "search-rent": "search_rent.json",
    "search-sold": "search_sold.json",
    "detail-home": "detail_home.json",
    "detail-apartment": "detail_apartment.json",
}

# Small patch of Schenectady NY; narrow enough to stay well under the
# 500-result cap so the fixture exercises a complete (not truncated) response,
# and residential enough to return real listings rather than only building
# cards (a dense downtown-Seattle patch came back entirely building-only).
_DEFAULT_BBOX = BBox(north=42.83, south=42.81, east=-73.92, west=-73.95)

# Keys whose entire subtree is contact info (agent/broker/realtor blocks get
# every string redacted, including inner "name"/"role" fields).
_PII_PARENT_RE = re.compile(r"agent|broker|realtor", re.IGNORECASE)
# Leaf keys redacted anywhere in the payload.
_PII_LEAF_RE = re.compile(r"phone|email|mls|attribution", re.IGNORECASE)
_REDACTED = "[redacted]"
_RESULT_LIST_KEYS = ("mapResults", "listResults")


def sanitize(value: Any, *, under_pii: bool = False) -> Any:
    """Recursively redact contact/PII-prone values, keeping structure.

    String leaves under an agent/broker/realtor key are redacted entirely
    (so nested contacts like ``agent: {"name": ...}`` are covered); string
    leaves keyed phone/email/MLS/attribution are redacted anywhere. Public
    listing data (zpid, price, address components) is untouched.
    """
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for key, item in value.items():
            key_is_pii = isinstance(key, str) and _PII_PARENT_RE.search(key) is not None
            leaf_is_pii = isinstance(key, str) and _PII_LEAF_RE.search(key) is not None
            if isinstance(item, str) and (under_pii or key_is_pii or leaf_is_pii):
                out[key] = _REDACTED
            else:
                out[key] = sanitize(item, under_pii=under_pii or key_is_pii)
        return out
    if isinstance(value, list):
        return [sanitize(item, under_pii=under_pii) for item in value]
    return value


def trim(value: Any, max_results: int) -> Any:
    """Recursively cap mapResults/listResults lists to max_results entries."""
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for key, item in value.items():
            if key in _RESULT_LIST_KEYS and isinstance(item, list):
                out[key] = item[:max_results]
            else:
                out[key] = trim(item, max_results)
        return out
    if isinstance(value, list):
        return [trim(item, max_results) for item in value]
    return value


def fetch_payload(kind: str, target: str | None, bbox: BBox) -> Any:
    """Fetch one live response for the given fixture kind."""
    if kind == "search-sale":
        return for_sale(bbox=bbox)
    if kind == "search-rent":
        return for_rent(bbox=bbox)
    if kind == "search-sold":
        return sold(bbox=bbox)
    if target is None:  # guarded by argparse too; defensive for direct calls
        raise ValueError("--target is required for detail kinds")
    if kind == "detail-home":
        return get_from_home_id(int(target)) if target.isdigit() else get_from_home_url(target)
    if kind == "detail-apartment":
        return get_from_apartment_url(target) if target.startswith("http") else get_from_apartment_id(target)
    raise ValueError(f"Unknown fixture kind: {kind}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("kind", choices=sorted(_FIXTURE_NAMES), help="Which fixture to capture")
    parser.add_argument(
        "--out", type=Path, default=Path("tests/fixtures"), help="Output directory (default: tests/fixtures)"
    )
    parser.add_argument("--target", help="zpid, apartment ID, or Zillow URL (required for detail kinds)")
    parser.add_argument(
        "--max-results", type=int, default=5, help="Cap on captured mapResults/listResults entries (default: 5)"
    )
    parser.add_argument("--north", type=float, default=_DEFAULT_BBOX.north)
    parser.add_argument("--south", type=float, default=_DEFAULT_BBOX.south)
    parser.add_argument("--east", type=float, default=_DEFAULT_BBOX.east)
    parser.add_argument("--west", type=float, default=_DEFAULT_BBOX.west)
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Required: acknowledge this contacts live Zillow",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.confirm:
        print(
            "Refusing to contact Zillow without --confirm.\n"
            "This captures real responses (rate-limited) into tests/fixtures/,\n"
            "which is gitignored: fixtures stay local and are never committed.",
            file=sys.stderr,
        )
        return 2
    if args.kind.startswith("detail-") and not args.target:
        parser.error(f"--target is required for {args.kind}")

    bbox = BBox(north=args.north, south=args.south, east=args.east, west=args.west)
    try:
        payload = fetch_payload(args.kind, args.target, bbox)
    except Exception as e:
        print(f"Capture failed: {e}", file=sys.stderr)
        return 1

    payload = trim(sanitize(payload), args.max_results)
    out_path = args.out / _FIXTURE_NAMES[args.kind]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
