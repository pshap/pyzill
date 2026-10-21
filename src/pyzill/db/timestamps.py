"""Timestamps for stored data.

The database stores fixed-width UTC timestamps so that string comparisons
against them stay correct; these helpers produce and parse that form.
"""

from datetime import UTC, datetime

__all__ = ["normalize_utc_timestamp", "utc_now"]


def utc_now() -> str:
    """Return current UTC time as ISO string (fixed-width %Y-%m-%dT%H:%M:%SZ)."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_utc_timestamp(value: str, *, label: str) -> str:
    """Parse an ISO-8601 timestamp and return the canonical ``...Z`` form.

    Normalizing keeps string comparisons against stored timestamps (which are
    fixed-width UTC) correct even when the caller passes an offset or
    milliseconds. A timestamp without a timezone is read as UTC.

    Args:
        value: The timestamp to parse.
        label: What the timestamp is for, used in the error message.

    Returns:
        str: The timestamp as ``YYYY-MM-DDTHH:MM:SSZ``.

    Raises:
        ValueError: If the timestamp cannot be parsed.
    """
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"Invalid {label} timestamp: {value}. Use ISO format like 2026-10-17T12:00:00Z") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
