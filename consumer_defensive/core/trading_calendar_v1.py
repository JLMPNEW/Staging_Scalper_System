"""Consumer-owned XNYS session helpers for signal/allocation chronology."""

from __future__ import annotations

from datetime import date
from typing import Any


def _iso_date(value: Any, *, label: str) -> str:
    text = str(value)
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{label} must be an ISO date") from exc
    if parsed.isoformat() != text:
        raise ValueError(f"{label} must be canonical YYYY-MM-DD")
    return text


def prior_xnys_session(allocation_asof_date: str) -> str:
    """Return the immediately prior XNYS session for an allocation session."""

    allocation = _iso_date(allocation_asof_date, label="allocation_asof_date")
    try:
        import exchange_calendars as exchange_calendar  # type: ignore
        import pandas as pd  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "exchange_calendars and pandas are required for exact session chronology"
        ) from exc
    calendar = exchange_calendar.get_calendar("XNYS")
    timestamp = pd.Timestamp(allocation)
    if not bool(calendar.is_session(timestamp)):
        raise ValueError("allocation_asof_date is not an XNYS trading session")
    return calendar.previous_session(timestamp).date().isoformat()


def assert_one_session_lag(
    *, signal_asof_date: str, allocation_asof_date: str
) -> None:
    """Fail unless signal is exactly one XNYS session before allocation."""

    signal = _iso_date(signal_asof_date, label="signal_asof_date")
    expected = prior_xnys_session(allocation_asof_date)
    if signal != expected:
        raise ValueError(
            "signal_asof_date must be exactly one XNYS session before "
            f"allocation_asof_date; expected {expected}"
        )


__all__ = ["assert_one_session_lag", "prior_xnys_session"]
