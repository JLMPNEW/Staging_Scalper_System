"""Validate evidence calendars against a pinned official XNYS schedule."""

from __future__ import annotations

import csv
import hashlib
import io
from datetime import date
from importlib.metadata import version
from pathlib import Path
from typing import Any

import exchange_calendars


CALENDAR_ID = "XNYS"
CALENDAR_PROVIDER = "exchange_calendars"
CALENDAR_PROVIDER_VERSION = "4.13.2"


def validate_official_xnys_calendar_bytes(
    payload_bytes: bytes,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    if version(CALENDAR_PROVIDER) != CALENDAR_PROVIDER_VERSION:
        raise ValueError("installed exchange-calendar version differs from evidence contract")
    try:
        text = payload_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("canonical calendar must be valid UTF-8 CSV") from exc
    reader = csv.DictReader(io.StringIO(text, newline=""))
    rows = [dict(row) for row in reader]
    required = {
        "session_date",
        "entry_execution_at_utc",
        "exit_execution_at_utc",
        "calendar_id",
        "calendar_provider",
        "calendar_provider_version",
    }
    if not rows or not required <= set(rows[0]):
        raise ValueError("canonical calendar lacks provider/version/session metadata")
    for row in rows:
        if (
            row["calendar_id"] != CALENDAR_ID
            or row["calendar_provider"] != CALENDAR_PROVIDER
            or row["calendar_provider_version"] != CALENDAR_PROVIDER_VERSION
        ):
            raise ValueError("calendar provider identity differs from pinned XNYS contract")
    dates: list[str] = []
    for row in rows:
        session_date = str(row["session_date"])
        try:
            parsed = date.fromisoformat(session_date)
        except ValueError as exc:
            raise ValueError("canonical calendar session must be exact YYYY-MM-DD") from exc
        if parsed.isoformat() != session_date:
            raise ValueError("canonical calendar session must be exact YYYY-MM-DD")
        dates.append(session_date)
    if dates != sorted(set(dates)):
        raise ValueError("canonical calendar sessions are not unique and sorted")
    calendar = exchange_calendars.get_calendar(CALENDAR_ID)
    official = calendar.sessions_in_range(dates[0], dates[-1])
    expected_dates = [item.date().isoformat() for item in official]
    if dates != expected_dates:
        raise ValueError("calendar session census differs from official XNYS sessions")
    for row, session in zip(rows, official):
        expected_open = calendar.session_open(session).isoformat()
        expected_close = calendar.session_close(session).isoformat()
        if row["entry_execution_at_utc"].replace("Z", "+00:00") != expected_open:
            raise ValueError("calendar open timestamp differs from official XNYS open")
        if row["exit_execution_at_utc"].replace("Z", "+00:00") != expected_close:
            raise ValueError("calendar close timestamp differs from official XNYS close")
    return rows, {
        "calendar_id": CALENDAR_ID,
        "calendar_provider": CALENDAR_PROVIDER,
        "calendar_provider_version": CALENDAR_PROVIDER_VERSION,
        "session_count": len(rows),
        "first_session": dates[0],
        "last_session": dates[-1],
        "official_session_census_pass": True,
        "official_open_close_timestamp_pass": True,
    }


def read_official_xnys_calendar_snapshot(
    path: Path,
) -> tuple[list[dict[str, str]], dict[str, Any], str]:
    payload_bytes = Path(path).expanduser().resolve().read_bytes()
    rows, audit = validate_official_xnys_calendar_bytes(payload_bytes)
    return rows, audit, hashlib.sha256(payload_bytes).hexdigest()


def validate_official_xnys_calendar(path: Path) -> dict[str, Any]:
    _, audit, _ = read_official_xnys_calendar_snapshot(path)
    return audit


__all__ = [
    "CALENDAR_ID",
    "CALENDAR_PROVIDER",
    "CALENDAR_PROVIDER_VERSION",
    "read_official_xnys_calendar_snapshot",
    "validate_official_xnys_calendar",
    "validate_official_xnys_calendar_bytes",
]
