from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from future_only_evidence.interval_integrity import validate_interval_timestamps


def _files(tmp_path: Path, *, available: str, entry_date: str = "2026-08-25"):
    calendar = tmp_path / "calendar.csv"
    with calendar.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["session_date", "entry_execution_at_utc", "exit_execution_at_utc"],
        )
        writer.writeheader()
        writer.writerows(
            [
                {
                    "session_date": "2026-08-25",
                    "entry_execution_at_utc": "2026-08-25T13:30:00+00:00",
                    "exit_execution_at_utc": "2026-08-25T20:00:00+00:00",
                },
                {
                    "session_date": "2026-08-26",
                    "entry_execution_at_utc": "2026-08-26T13:30:00+00:00",
                    "exit_execution_at_utc": "2026-08-26T20:00:00+00:00",
                },
            ]
        )
    capture = tmp_path / "capture.json"
    capture.write_text(
        json.dumps(
            {
                "capture_id": "a" * 64,
                "trusted_capture_timing": {
                    "entry_session_date": "2026-08-25",
                    "entry_execution_at_utc": "2026-08-25T13:30:00+00:00",
                },
            }
        ),
        encoding="utf-8",
    )
    outcome = tmp_path / "outcome.json"
    outcome.write_text(
        json.dumps(
            {
                "rows": [
                    {
                        "capture_id": "a" * 64,
                        "entry_date": entry_date,
                        "exit_date": "2026-08-26",
                        "entry_execution_at_utc": "2026-08-25T13:30:00+00:00",
                        "exit_execution_at_utc": "2026-08-26T20:00:00+00:00",
                        "outcome_available_at_utc": available,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return capture, outcome, calendar


def test_same_day_or_wrong_entry_cannot_replace_signed_entry(tmp_path: Path) -> None:
    capture, outcome, calendar = _files(
        tmp_path,
        available="2026-08-26T21:00:00+00:00",
        entry_date="2026-08-24",
    )
    with pytest.raises(ValueError, match="differs from signed capture entry"):
        validate_interval_timestamps(
            capture_paths=[capture],
            outcome_path=outcome,
            trading_calendar_path=calendar,
        )


def test_outcome_must_be_available_after_exit_execution(tmp_path: Path) -> None:
    capture, outcome, calendar = _files(
        tmp_path,
        available="2026-08-26T19:00:00+00:00",
    )
    with pytest.raises(ValueError, match="strictly after"):
        validate_interval_timestamps(
            capture_paths=[capture],
            outcome_path=outcome,
            trading_calendar_path=calendar,
        )


@pytest.mark.parametrize(
    ("entry_date", "available"),
    [
        ("2026-08-25T00:00:00Z", "2026-08-26T21:00:00+00:00"),
        ("2026-08-25", "2026-08-26 21:00:00+00:00"),
        ("2026-08-25", "2026-08-26T16:00:00-05:00"),
    ],
)
def test_interval_rejects_noncanonical_dates_and_timestamps(
    tmp_path: Path,
    entry_date: str,
    available: str,
) -> None:
    capture, outcome, calendar = _files(
        tmp_path,
        available=available,
        entry_date=entry_date,
    )
    with pytest.raises(ValueError, match="exact (YYYY-MM-DD|RFC3339 UTC)"):
        validate_interval_timestamps(
            capture_paths=[capture],
            outcome_path=outcome,
            trading_calendar_path=calendar,
        )
