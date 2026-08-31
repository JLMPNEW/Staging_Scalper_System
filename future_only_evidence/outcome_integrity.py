"""Recompute future returns from hash-bound raw total-return sources."""

from __future__ import annotations

import csv
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .protocol import canonical_sha256, file_sha256
from .trusted_receipts import PinnedEd25519Authority


OUTCOME_SOURCE_ROLES = frozenset(
    {"total_return_bars", "terminal_events", "trading_calendar", "membership_history"}
)


def _utc(value: Any, *, label: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(None):
        raise ValueError(f"{label} must be timezone-aware UTC")
    return parsed


def _bars(path: Path) -> dict[tuple[str, str], float]:
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    required = {"ticker", "session_date", "total_return_index"}
    if not rows or not required <= set(rows[0]):
        raise ValueError("total-return bars have an invalid schema")
    result: dict[tuple[str, str], float] = {}
    for row in rows:
        key = (str(row["ticker"]).strip().upper(), str(row["session_date"])[:10])
        value = float(row["total_return_index"])
        if key in result or not math.isfinite(value) or value <= 0:
            raise ValueError("total-return bars contain duplicate/invalid values")
        result[key] = value
    return result


def _terminal_events(path: Path) -> dict[tuple[str, str], dict[str, str]]:
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        return {}
    required = {
        "ticker",
        "exit_date",
        "terminal_event_status",
        "total_return_index_includes_terminal_proceeds_flag",
    }
    if not required <= set(rows[0]):
        raise ValueError("terminal-event outcome source has an invalid schema")
    result: dict[tuple[str, str], dict[str, str]] = {}
    for row in rows:
        key = (str(row["ticker"]).strip().upper(), str(row["exit_date"])[:10])
        if key in result:
            raise ValueError("terminal-event source contains duplicate ticker/exit")
        result[key] = dict(row)
    return result


def validate_and_recompute_outcomes(
    *,
    family: str,
    capture_paths: Sequence[Path],
    outcome_path: Path,
    outcome_source_paths: Mapping[str, Path],
    outcome_receipt_path: Path,
    expected_outcome_receipt_sha256: str,
    authority: PinnedEd25519Authority,
    benchmark_ticker: str | None,
) -> dict[str, Any]:
    if set(outcome_source_paths) != OUTCOME_SOURCE_ROLES:
        raise ValueError("outcome source roles do not exactly match the contract")
    source_hashes = {
        role: file_sha256(path)
        for role, path in sorted(outcome_source_paths.items())
    }
    payload = json.loads(Path(outcome_path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("outcome payload must be a JSON object")
    if payload.get("source_sha256") != source_hashes:
        raise ValueError("outcome payload does not bind exact raw source bytes")
    rows = payload.get("rows")
    if not isinstance(rows, list) or payload.get("rows_sha256") != canonical_sha256(rows):
        raise ValueError("outcome rows are missing or hash-tampered")
    receipt = json.loads(Path(outcome_receipt_path).read_text(encoding="utf-8"))
    if file_sha256(outcome_receipt_path) != expected_outcome_receipt_sha256:
        raise ValueError("outcome receipt SHA-256 mismatch")
    authority.verify(outcome_receipt_path, expected_outcome_receipt_sha256, receipt)
    if receipt.get("schema_version") != "future_outcome_receipt_v1":
        raise ValueError("unsupported signed outcome receipt")
    if receipt.get("family") != family:
        raise ValueError("outcome receipt family mismatch")
    if receipt.get("outcome_rows_sha256") != payload.get("rows_sha256"):
        raise ValueError("outcome receipt does not bind exact outcome rows")
    if receipt.get("source_sha256") != source_hashes:
        raise ValueError("outcome receipt does not bind exact raw source bytes")
    capture_index: dict[str, dict[str, Any]] = {}
    for path in capture_paths:
        capture = json.loads(Path(path).read_text(encoding="utf-8"))
        capture_index[str(capture["capture_id"])] = capture
    if sorted(receipt.get("capture_ids") or []) != sorted(capture_index):
        raise ValueError("outcome receipt capture census mismatch")
    bars = _bars(outcome_source_paths["total_return_bars"])
    terminal_events = _terminal_events(outcome_source_paths["terminal_events"])
    benchmark = str(benchmark_ticker or "").upper()
    recomputed = 0
    benchmark_by_period: dict[str, float] = {}
    for row in rows:
        capture_id = str(row.get("capture_id") or "")
        if capture_id not in capture_index:
            raise ValueError("outcome row references an unbound capture")
        capture = capture_index[capture_id]
        timing = capture.get("trusted_capture_timing")
        if not isinstance(timing, dict):
            raise ValueError("capture lacks trusted pre-entry timing")
        if row.get("entry_execution_at_utc") != timing.get("entry_execution_at_utc"):
            raise ValueError("outcome entry timestamp differs from trusted capture entry")
        if not _utc(capture["captured_at_utc"], label="captured_at_utc") < _utc(
            row["entry_execution_at_utc"],
            label="entry_execution_at_utc",
        ):
            raise ValueError("outcome entry occurred before trusted signal capture")
        ticker = str(row["ticker"]).strip().upper()
        entry = str(row["entry_date"])[:10]
        exit_date = str(row["exit_date"])[:10]
        try:
            stock_return = bars[(ticker, exit_date)] / bars[(ticker, entry)] - 1.0
        except KeyError as exc:
            raise ValueError(f"missing total-return bar for {ticker} outcome") from exc
        benchmark_return = 0.0
        if benchmark:
            try:
                benchmark_return = bars[(benchmark, exit_date)] / bars[(benchmark, entry)] - 1.0
            except KeyError as exc:
                raise ValueError(f"missing {benchmark} benchmark total-return bar") from exc
        expected_model_return = stock_return - benchmark_return if benchmark else stock_return
        submitted = {
            "stock_total_return": float(row.get("stock_total_return")),
            "benchmark_total_return": float(row.get("benchmark_total_return") or 0.0),
            "gross_return": float(row.get("gross_return")),
        }
        expected = {
            "stock_total_return": stock_return,
            "benchmark_total_return": benchmark_return,
            "gross_return": expected_model_return,
        }
        if any(abs(submitted[field] - expected[field]) > 1e-12 for field in expected):
            raise ValueError(f"{ticker}: submitted return arithmetic differs from raw total-return bars")
        terminal_status = str(row.get("terminal_event_status") or "")
        if terminal_status != "none":
            event = terminal_events.get((ticker, exit_date))
            if (
                event is None
                or event["terminal_event_status"] != terminal_status
                or event["total_return_index_includes_terminal_proceeds_flag"] != "1"
            ):
                raise ValueError(f"{ticker}: terminal proceeds are not governed in total-return bars")
        available = _utc(row["outcome_available_at_utc"], label="outcome_available_at_utc")
        if available > _utc(receipt["outcomes_available_at_utc"], label="receipt outcomes_available_at_utc"):
            raise ValueError("outcome row availability exceeds signed receipt availability")
        period_key = f"{capture_id}|{int(row['horizon_sessions'])}"
        if period_key in benchmark_by_period and abs(benchmark_by_period[period_key] - benchmark_return) > 1e-12:
            raise ValueError("benchmark return differs within one capture/horizon")
        benchmark_by_period[period_key] = benchmark_return
        recomputed += 1
    return {
        "return_arithmetic_recomputed_pass": True,
        "recomputed_row_count": recomputed,
        "benchmark_ticker": benchmark,
        "benchmark_by_period": benchmark_by_period,
        "outcome_receipt_sha256": file_sha256(outcome_receipt_path),
        "outcome_source_sha256": source_hashes,
        "trusted_authority": authority.identity(),
    }


__all__ = ["OUTCOME_SOURCE_ROLES", "validate_and_recompute_outcomes"]
