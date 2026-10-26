"""Writing and reading property details."""

import json
import math
import sqlite3
from collections.abc import Mapping
from typing import Any

from pyzill.db.derive import hoa_monthly, tax_latest
from pyzill.db.runs import utc_now
from pyzill.property_detail import normalize_property_detail
from pyzill.values import to_zpid

__all__ = [
    "clear_detail_failure",
    "detail_normalization_failed",
    "get_detail_failure",
    "get_property_details",
    "record_detail_failure",
    "upsert_property_details",
]

# Keys that mark a payload as a raw Zillow detail page rather than an already
# normalized detail record. A payload carrying any of these is flattened
# through normalize_property_detail unless it already holds normalized fields.
_RAW_DETAIL_MARKERS = ("resoFacts", "monthlyHoaFee", "taxHistory", "schools", "property")
_NORMALIZED_DETAIL_MARKERS = ("hoa_fee", "photos_count")

# Exceptions normalize_property_detail can raise when a payload cannot be
# flattened.  Anything else (a programming error) should not be swallowed.
_NORMALIZATION_ERRORS = (ValueError, TypeError, KeyError, AttributeError)


def _json_safe(value: Any) -> Any:
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    return value


def _coerce_detail(detail: Mapping[str, Any]) -> tuple[Mapping[str, Any], bool]:
    """Return a normalized detail record and whether normalization failed.

    Already-normalized records (carrying hoa_fee or photos_count) are used
    as-is.  Raw Zillow detail payloads are flattened through
    normalize_property_detail.  Only normalization errors are caught: anything
    else propagates so it is not silently hidden.  When normalization fails,
    the raw payload is returned unchanged with ``normalization_failed=True`` so
    the caller can still persist what it has but the failure is visible.
    """
    if not isinstance(detail, Mapping) or "zpid" not in detail:
        return detail, False
    has_normalized = any(field in detail for field in _NORMALIZED_DETAIL_MARKERS)
    has_raw = any(field in detail for field in _RAW_DETAIL_MARKERS)
    if has_normalized and not has_raw:
        return detail, False
    try:
        return normalize_property_detail(detail), False
    except _NORMALIZATION_ERRORS:
        return detail, True


def _detail_values(  # noqa: PLR0913, PLR0917 - internal helper, all fields map 1:1 to table columns
    norm: Mapping[str, Any],
    kind: str,
    zpid: int,
    raw_json: str | None,
    enriched_at: str,
    normalization_failed: bool,
) -> tuple[Any, ...]:
    """Return the parameter tuple for one property_details row."""
    hoa_fee = norm.get("hoa_fee")
    hoa_fee_unit = norm.get("hoa_fee_unit")
    tax = tax_latest(norm.get("tax_history"))
    return (
        zpid,
        kind,
        hoa_fee,
        hoa_fee_unit,
        hoa_monthly(hoa_fee, hoa_fee_unit),
        norm.get("description"),
        norm.get("photos_count"),
        norm.get("year_built"),
        norm.get("lot_size"),
        norm.get("lot_size_unit"),
        tax.assessed,
        tax.year,
        tax.paid,
        json.dumps(_json_safe(norm.get("tax_history"))) if norm.get("tax_history") is not None else None,
        json.dumps(_json_safe(norm.get("schools"))) if norm.get("schools") is not None else None,
        raw_json,
        enriched_at,
        1 if normalization_failed else 0,
    )


_UPSERT_SQL = """INSERT INTO property_details (
    zpid, kind, hoa_fee, hoa_fee_unit, hoa_monthly, description, photos_count,
    year_built, lot_size, lot_size_unit, tax_assessed_latest, tax_year_latest,
    tax_paid_latest, tax_history, schools, raw_json, enriched_at_utc,
    normalization_failed
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(zpid, kind) DO UPDATE SET
    hoa_fee=excluded.hoa_fee,
    hoa_fee_unit=excluded.hoa_fee_unit,
    hoa_monthly=excluded.hoa_monthly,
    description=excluded.description,
    photos_count=excluded.photos_count,
    year_built=excluded.year_built,
    lot_size=excluded.lot_size,
    lot_size_unit=excluded.lot_size_unit,
    tax_assessed_latest=excluded.tax_assessed_latest,
    tax_year_latest=excluded.tax_year_latest,
    tax_paid_latest=excluded.tax_paid_latest,
    tax_history=excluded.tax_history,
    schools=excluded.schools,
    raw_json=COALESCE(excluded.raw_json, property_details.raw_json),
    enriched_at_utc=excluded.enriched_at_utc,
    normalization_failed=excluded.normalization_failed"""


def upsert_property_details(
    conn: sqlite3.Connection,
    details: Mapping[str, Any] | list[Mapping[str, Any]],
    *,
    kind: str = "sale",
    enriched_at_utc: str | None = None,
    raw: Mapping[str, Any] | list[Mapping[str, Any]] | None = None,
) -> None:
    if isinstance(details, Mapping):
        detail_list = [details]
        raw_list = [raw] if isinstance(raw, Mapping) else None
    else:
        detail_list = list(details)
        raw_list = list(raw) if isinstance(raw, list) else None

    enriched_at = enriched_at_utc or utc_now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        for i, detail in enumerate(detail_list):
            norm, normalization_failed = _coerce_detail(detail)
            zpid = norm.get("zpid") if isinstance(norm, Mapping) else None
            if zpid is None:
                zpid = detail.get("zpid") if isinstance(detail, Mapping) else None
            zpid = to_zpid(zpid)
            if zpid is None:
                continue
            raw_json = None
            if raw_list is not None and i < len(raw_list) and raw_list[i] is not None:
                raw_json = json.dumps(_json_safe(raw_list[i]), ensure_ascii=False)
            elif isinstance(detail, Mapping):
                try:
                    raw_json = json.dumps(_json_safe(detail), ensure_ascii=False)
                except Exception:
                    raw_json = None
            conn.execute(
                _UPSERT_SQL,
                _detail_values(norm, kind, zpid, raw_json, enriched_at, normalization_failed),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


_FAILURE_UPSERT_SQL = """INSERT INTO detail_failures (
    zpid, kind, recorded_at_utc, reason, status_code
) VALUES (?, ?, ?, ?, ?)
ON CONFLICT(zpid, kind) DO UPDATE SET
    recorded_at_utc=excluded.recorded_at_utc,
    reason=excluded.reason,
    status_code=excluded.status_code"""


def record_detail_failure(  # noqa: PLR0913 - caller supplies all failure fields
    conn: sqlite3.Connection,
    zpid: int,
    kind: str,
    *,
    reason: str,
    status_code: int | None = None,
    recorded_at_utc: str | None = None,
) -> None:
    """Record that a detail fetch failed for a listing, so it is not retried
    within ``--max-age`` days."""
    conn.execute(
        _FAILURE_UPSERT_SQL,
        (zpid, kind, recorded_at_utc or utc_now(), reason, status_code),
    )


def get_detail_failure(conn: sqlite3.Connection, zpid: int, kind: str = "sale") -> dict[str, Any] | None:
    """Return the recorded failure for a listing, or None if none was recorded."""
    cur = conn.execute(
        "SELECT * FROM detail_failures WHERE zpid = ? AND kind = ?",
        (zpid, kind),
    )
    row = cur.fetchone()
    if row is None:
        return None
    cols = [d[0] for d in cur.description]
    return dict(zip(cols, row, strict=True))


def clear_detail_failure(conn: sqlite3.Connection, zpid: int, kind: str = "sale") -> None:
    """Forget a recorded failure, because a listing that comes back is not gone."""
    conn.execute(
        "DELETE FROM detail_failures WHERE zpid = ? AND kind = ?",
        (zpid, kind),
    )


def detail_normalization_failed(conn: sqlite3.Connection, zpid: int, kind: str = "sale") -> bool:
    """Whether the stored detail row for a listing could not be normalized."""
    row = conn.execute(
        "SELECT normalization_failed FROM property_details WHERE zpid = ? AND kind = ?",
        (zpid, kind),
    ).fetchone()
    return bool(row is not None and row[0])


def get_property_details(conn: sqlite3.Connection, zpid: int, kind: str = "sale") -> dict[str, Any] | None:
    cur = conn.execute(
        "SELECT * FROM property_details WHERE zpid = ? AND kind = ?",
        (zpid, kind),
    )
    row = cur.fetchone()
    if row is None:
        return None
    cols = [d[0] for d in cur.description]
    return dict(zip(cols, row, strict=True))
