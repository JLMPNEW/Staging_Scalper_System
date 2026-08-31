"""Exact session/timestamp chronology for prospective signal captures."""

from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .canonical_values import exact_date, exact_utc
from .protocol import file_sha256
from .trusted_receipts import PinnedEd25519Authority


def _utc(value: Any, *, label: str) -> datetime:
    return exact_utc(value, label=label)


def validate_capture_receipt_timing(
    *,
    receipt_path: Path,
    authority: PinnedEd25519Authority,
    asof_date: str,
    trading_calendar_path: Path,
) -> dict[str, Any]:
    payload = json.loads(Path(receipt_path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("capture receipt must be a JSON object")
    authority.verify(receipt_path, file_sha256(receipt_path), payload)
    if payload.get("schema_version") != "future_signal_capture_receipt_v1":
        raise ValueError("unsupported signed signal-capture receipt")
    if payload.get("trading_calendar_sha256") != file_sha256(trading_calendar_path):
        raise ValueError("capture receipt does not bind exact trading-calendar bytes")
    with Path(trading_calendar_path).open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows or not {"session_date", "entry_execution_at_utc"} <= set(rows[0]):
        raise ValueError("trading calendar needs session_date and entry_execution_at_utc")
    asof = exact_date(asof_date, label="capture asof_date")
    future = [
        row
        for row in rows
        if exact_date(row["session_date"], label="calendar session_date") > asof
    ]
    if not future:
        raise ValueError("trading calendar has no next execution session")
    expected_entry = min(future, key=lambda row: row["session_date"])
    entry_date = exact_date(
        payload.get("entry_session_date"), label="receipt entry_session_date"
    ).isoformat()
    entry_execution = _utc(payload.get("entry_execution_at_utc"), label="entry_execution_at_utc")
    captured_at = _utc(payload.get("captured_at_utc"), label="captured_at_utc")
    information_cutoff = _utc(
        payload.get("signal_information_cutoff_at_utc"),
        label="signal_information_cutoff_at_utc",
    )
    if entry_date != expected_entry["session_date"]:
        raise ValueError("capture receipt entry is not the next bound calendar session")
    if entry_execution != _utc(
        expected_entry["entry_execution_at_utc"],
        label="calendar entry_execution_at_utc",
    ):
        raise ValueError("capture receipt entry timestamp differs from bound calendar")
    if not information_cutoff <= captured_at < entry_execution:
        raise ValueError("signal receipt was not anchored after cutoff and before entry execution")
    return {
        "authority": authority.identity(),
        "receipt_sha256": file_sha256(receipt_path),
        "trading_calendar_sha256": file_sha256(trading_calendar_path),
        "asof_date": asof.isoformat(),
        "entry_session_date": entry_date,
        "entry_execution_at_utc": entry_execution.isoformat(),
        "captured_at_utc": captured_at.isoformat(),
        "signal_information_cutoff_at_utc": information_cutoff.isoformat(),
        "capture_before_entry_pass": True,
    }


__all__ = ["validate_capture_receipt_timing"]
