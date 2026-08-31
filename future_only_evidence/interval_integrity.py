"""Bind outcome intervals to exact signed entry and calendar exit timestamps."""

from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from .canonical_values import exact_date, exact_utc


def _utc(value: Any, *, label: str) -> datetime:
    return exact_utc(value, label=label)


def validate_interval_timestamps(
    *,
    capture_paths: Sequence[Path],
    outcome_path: Path,
    trading_calendar_path: Path,
) -> dict[str, Any]:
    capture_index = {
        str(payload["capture_id"]): payload
        for payload in (
            json.loads(Path(path).read_text(encoding="utf-8"))
            for path in capture_paths
        )
    }
    with Path(trading_calendar_path).open("r", encoding="utf-8-sig", newline="") as handle:
        calendar_rows = list(csv.DictReader(handle))
    required = {"session_date", "entry_execution_at_utc", "exit_execution_at_utc"}
    if not calendar_rows or not required <= set(calendar_rows[0]):
        raise ValueError("calendar needs exact entry and exit execution timestamps")
    calendar = {
        exact_date(
            row["session_date"], label="calendar session_date"
        ).isoformat(): row
        for row in calendar_rows
    }
    if len(calendar) != len(calendar_rows):
        raise ValueError("calendar contains duplicate session dates")
    outcome = json.loads(Path(outcome_path).read_text(encoding="utf-8"))
    rows = outcome.get("rows")
    if not isinstance(rows, list):
        raise ValueError("outcome rows are missing")
    for row in rows:
        capture = capture_index.get(str(row.get("capture_id") or ""))
        if capture is None:
            raise ValueError("outcome references an unknown capture")
        timing = capture.get("trusted_capture_timing")
        if not isinstance(timing, dict):
            raise ValueError("capture lacks signed entry timing")
        entry_date = exact_date(
            row.get("entry_date"), label="outcome entry_date"
        ).isoformat()
        exit_date = exact_date(
            row.get("exit_date"), label="outcome exit_date"
        ).isoformat()
        signed_entry_date = exact_date(
            timing.get("entry_session_date"),
            label="signed entry_session_date",
        ).isoformat()
        if entry_date != signed_entry_date:
            raise ValueError("outcome entry date differs from signed capture entry")
        if entry_date not in calendar or exit_date not in calendar:
            raise ValueError("outcome entry/exit is absent from bound calendar")
        entry_execution = _utc(row.get("entry_execution_at_utc"), label="entry_execution_at_utc")
        exit_execution = _utc(row.get("exit_execution_at_utc"), label="exit_execution_at_utc")
        available = _utc(row.get("outcome_available_at_utc"), label="outcome_available_at_utc")
        if entry_execution != _utc(
            calendar[entry_date]["entry_execution_at_utc"],
            label="calendar entry_execution_at_utc",
        ):
            raise ValueError("outcome entry execution differs from bound calendar")
        if entry_execution != _utc(
            timing["entry_execution_at_utc"],
            label="signed entry_execution_at_utc",
        ):
            raise ValueError("outcome entry execution differs from signed capture")
        if exit_execution != _utc(
            calendar[exit_date]["exit_execution_at_utc"],
            label="calendar exit_execution_at_utc",
        ):
            raise ValueError("outcome exit execution differs from bound calendar")
        if not entry_execution < exit_execution < available:
            raise ValueError("outcome availability is not strictly after bound exit execution")
    return {
        "exact_entry_identity_pass": True,
        "exact_exit_identity_pass": True,
        "availability_after_exit_execution_pass": True,
        "validated_row_count": len(rows),
    }


__all__ = ["validate_interval_timestamps"]
