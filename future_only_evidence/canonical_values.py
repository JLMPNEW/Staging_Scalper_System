"""Canonical scalar parsers shared by future-only evidence validators."""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Any


_RFC3339_UTC = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?(?:Z|\+00:00)\Z"
)
_ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")


def exact_date(value: Any, *, label: str) -> date:
    """Parse an exact ISO calendar date without coercion or truncation."""
    if type(value) is not str or _ISO_DATE.fullmatch(value) is None:
        raise ValueError(f"{label} must be exact YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be a valid YYYY-MM-DD date") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{label} must be exact YYYY-MM-DD")
    return parsed


def exact_utc(value: Any, *, label: str) -> datetime:
    """Parse an exact RFC3339 UTC timestamp without string coercion."""
    if type(value) is not str or _RFC3339_UTC.fullmatch(value) is None:
        raise ValueError(
            f"{label} must be exact RFC3339 UTC with seconds and Z or +00:00"
        )
    normalized = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"{label} must be a valid RFC3339 UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(None):
        raise ValueError(f"{label} must be UTC")
    return parsed


__all__ = ["exact_date", "exact_utc"]
