from __future__ import annotations

import csv
from pathlib import Path

import exchange_calendars
import pytest

from future_only_evidence.official_calendar import (
    CALENDAR_ID,
    CALENDAR_PROVIDER,
    CALENDAR_PROVIDER_VERSION,
    validate_official_xnys_calendar,
)


def _write(path: Path, dates: list[str]) -> Path:
    calendar = exchange_calendars.get_calendar("XNYS")
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "session_date",
                "entry_execution_at_utc",
                "exit_execution_at_utc",
                "calendar_id",
                "calendar_provider",
                "calendar_provider_version",
            ],
        )
        writer.writeheader()
        for value in dates:
            writer.writerow(
                {
                    "session_date": value,
                    "entry_execution_at_utc": calendar.session_open(value).isoformat(),
                    "exit_execution_at_utc": calendar.session_close(value).isoformat(),
                    "calendar_id": CALENDAR_ID,
                    "calendar_provider": CALENDAR_PROVIDER,
                    "calendar_provider_version": CALENDAR_PROVIDER_VERSION,
                }
            )
    return path


def test_exact_xnys_calendar_passes(tmp_path: Path) -> None:
    path = _write(tmp_path / "calendar.csv", ["2026-08-20", "2026-08-21", "2026-08-24"])
    assert validate_official_xnys_calendar(path)["official_session_census_pass"] is True


def test_missing_session_is_rejected_even_when_hashes_could_be_recomputed(tmp_path: Path) -> None:
    path = _write(tmp_path / "calendar.csv", ["2026-08-20", "2026-08-24"])
    with pytest.raises(ValueError, match="session census"):
        validate_official_xnys_calendar(path)


def test_added_weekend_session_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "calendar.csv"
    calendar = exchange_calendars.get_calendar("XNYS")
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "session_date",
                "entry_execution_at_utc",
                "exit_execution_at_utc",
                "calendar_id",
                "calendar_provider",
                "calendar_provider_version",
            ],
        )
        writer.writeheader()
        for value in ("2026-08-21", "2026-08-22", "2026-08-24"):
            writer.writerow(
                {
                    "session_date": value,
                    "entry_execution_at_utc": (
                        calendar.session_open(value).isoformat()
                        if value != "2026-08-22"
                        else "2026-08-22T13:30:00+00:00"
                    ),
                    "exit_execution_at_utc": (
                        calendar.session_close(value).isoformat()
                        if value != "2026-08-22"
                        else "2026-08-22T20:00:00+00:00"
                    ),
                    "calendar_id": CALENDAR_ID,
                    "calendar_provider": CALENDAR_PROVIDER,
                    "calendar_provider_version": CALENDAR_PROVIDER_VERSION,
                }
            )
    with pytest.raises(ValueError, match="session census"):
        validate_official_xnys_calendar(path)
