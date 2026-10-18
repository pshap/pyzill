"""Validation helpers for CLI detail targets."""

import re
from urllib.parse import urlsplit


def home_detail_target(value: str) -> tuple[str, int | str]:
    """Return the target kind and normalized value for a home detail command."""
    cleaned = value.strip()
    match = re.fullmatch(r"(\d+)(?:_zpid)?", cleaned)
    if match:
        return "id", int(match.group(1))
    try:
        host = urlsplit(cleaned).hostname
    except ValueError as exc:
        raise ValueError("Home detail input must be a zpid or an https://www.zillow.com URL") from exc
    if cleaned.startswith("https://") and host == "www.zillow.com":
        return "url", cleaned
    raise ValueError("Home detail input must be a zpid or an https://www.zillow.com URL")
