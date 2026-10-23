"""Writing and reading property details."""

import json
import math
import sqlite3
from collections.abc import Mapping
from typing import Any

from pyzill.db.runs import utc_now
from pyzill.property_detail import normalize_property_detail

__all__ = ["get_property_details", "upsert_property_details"]

# Keys that mark a payload as a raw Zillow detail page rather than an already
# normalized detail record. A payload carrying any of these is flattened
# through normalize_property_detail unless it already holds normalized fields.
_RAW_DETAIL_MARKERS = ("resoFacts", "monthlyHoaFee", "taxHistory", "schools", "property")
_NORMALIZED_DETAIL_MARKERS = ("hoa_fee", "photos_count")


def _json_safe(value: Any) -> Any:
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    return value


def _coerce_detail(detail: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return a normalized detail record.

    Already-normalized records (carrying hoa_fee or photos_count) are used
    as-is. Raw Zillow detail payloads are flattened through
    normalize_property_detail; anything that cannot be normalized is returned
    unchanged so the caller can still persist what it has.
    """
    if not isinstance(detail, Mapping) or "zpid" not in detail:
        return detail
    has_normalized = any(field in detail for field in _NORMALIZED_DETAIL_MARKERS)
    has_raw = any(field in detail for field in _RAW_DETAIL_MARKERS)
    if has_normalized and not has_raw:
        return detail
    try:
        return normalize_property_detail(detail)
    except Exception:
        return detail


def _detail_values(
    norm: Mapping[str, Any],
    kind: str,
    zpid: int,
    raw_json: str | None,
    enriched_at: str,
) -> tuple[Any, ...]:
    """Return the parameter tuple for one property_details row."""
    return (
        zpid,
        kind,
        norm.get("hoa_fee"),
        norm.get("hoa_fee_unit"),
        norm.get("description"),
        norm.get("photos_count"),
        norm.get("year_built"),
        norm.get("lot_size"),
        norm.get("lot_size_unit"),
        None,
        json.dumps(_json_safe(norm.get("tax_history"))) if norm.get("tax_history") is not None else None,
        json.dumps(_json_safe(norm.get("price_history"))) if norm.get("price_history") is not None else None,
        json.dumps(_json_safe(norm.get("schools"))) if norm.get("schools") is not None else None,
        raw_json,
        enriched_at,
    )


_UPSERT_SQL = """INSERT INTO property_details (
    zpid, kind, hoa_fee, hoa_fee_unit, description, photos_count,
    year_built, lot_size, lot_size_unit, tax_assessed_value,
    tax_history, price_history_detail, schools, raw_json, enriched_at_utc
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(zpid, kind) DO UPDATE SET
    hoa_fee=excluded.hoa_fee,
    hoa_fee_unit=excluded.hoa_fee_unit,
    description=excluded.description,
    photos_count=excluded.photos_count,
    year_built=excluded.year_built,
    lot_size=excluded.lot_size,
    lot_size_unit=excluded.lot_size_unit,
    tax_assessed_value=excluded.tax_assessed_value,
    tax_history=excluded.tax_history,
    price_history_detail=excluded.price_history_detail,
    schools=excluded.schools,
    raw_json=COALESCE(excluded.raw_json, property_details.raw_json),
    enriched_at_utc=excluded.enriched_at_utc"""


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
            norm = _coerce_detail(detail)
            zpid = norm.get("zpid") if isinstance(norm, Mapping) else None
            if zpid is None and isinstance(detail, Mapping):
                zpid = detail.get("zpid")
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
                _detail_values(norm, kind, int(zpid), raw_json, enriched_at),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


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
