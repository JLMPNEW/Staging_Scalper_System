from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from future_only_evidence.outcome_integrity_v2 import (
    ADJUSTMENT_POLICY,
    validate_and_recompute_outcomes_v2,
)
from future_only_evidence.protocol import canonical_sha256, file_sha256


class _Authority:
    def verify(self, *_):
        return True

    def identity(self):
        return {"authority_id": "test-independent"}


def _csv(path: Path, fields: list[str], rows: list[dict[str, object]]) -> Path:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    return path


def _case(tmp_path: Path, *, outcome_terminal_status: str, eligible: str = "1"):
    capture_id = "a" * 64
    entry = "2026-08-25T13:30:00+00:00"
    terminal_time = "2026-08-26T20:00:00+00:00"
    common_exit = "2026-08-27T20:00:00+00:00"
    capture = tmp_path / "capture.json"
    capture.write_text(
        json.dumps(
            {
                "capture_id": capture_id,
                "signal_rows": [
                    {
                        "ticker": "AAA",
                        "sleeve_id": "consumer",
                        "group_id": "beverages",
                        "eligible_flag": 1,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    bars = _csv(
        tmp_path / "bars.csv",
        ["ticker", "execution_at_utc", "total_return_index", "adjustment_policy_id"],
        [
            {"ticker": "AAA", "execution_at_utc": entry, "total_return_index": 100, "adjustment_policy_id": ADJUSTMENT_POLICY},
            {"ticker": "AAA", "execution_at_utc": terminal_time, "total_return_index": 110, "adjustment_policy_id": ADJUSTMENT_POLICY},
            {"ticker": "XLP", "execution_at_utc": entry, "total_return_index": 100, "adjustment_policy_id": ADJUSTMENT_POLICY},
            {"ticker": "XLP", "execution_at_utc": common_exit, "total_return_index": 102, "adjustment_policy_id": ADJUSTMENT_POLICY},
        ],
    )
    terminal = _csv(
        tmp_path / "terminal.csv",
        [
            "ticker",
            "terminal_event_date",
            "terminal_execution_at_utc",
            "terminal_event_status",
            "terminal_total_return_index_includes_proceeds_flag",
            "cash_carry_return_after_terminal",
            "adjustment_policy_id",
        ],
        [
            {
                "ticker": "AAA",
                "terminal_event_date": "2026-08-26",
                "terminal_execution_at_utc": terminal_time,
                "terminal_event_status": "cash_settled",
                "terminal_total_return_index_includes_proceeds_flag": "1",
                "cash_carry_return_after_terminal": "0",
                "adjustment_policy_id": ADJUSTMENT_POLICY,
            }
        ],
    )
    membership = _csv(
        tmp_path / "membership.csv",
        [
            "capture_id",
            "ticker",
            "entry_execution_at_utc",
            "sleeve_id",
            "group_id",
            "eligible_at_entry_flag",
            "lifecycle_status_at_entry",
        ],
        [
            {
                "capture_id": capture_id,
                "ticker": "AAA",
                "entry_execution_at_utc": entry,
                "sleeve_id": "consumer",
                "group_id": "beverages",
                "eligible_at_entry_flag": eligible,
                "lifecycle_status_at_entry": "active_terminal_event_later",
            }
        ],
    )
    calendar = tmp_path / "calendar.csv"
    calendar.write_text("session_date\n", encoding="utf-8")
    sources = {
        "total_return_bars": bars,
        "terminal_events": terminal,
        "trading_calendar": calendar,
        "membership_history": membership,
    }
    row = {
        "capture_id": capture_id,
        "ticker": "AAA",
        "horizon_sessions": 2,
        "entry_execution_at_utc": entry,
        "exit_execution_at_utc": common_exit,
        "terminal_event_status": outcome_terminal_status,
        "security_exit_execution_at_utc": terminal_time,
        "terminal_cash_carry_policy": "zero_return_cash_to_common_exit",
        "stock_total_return": 0.10,
        "benchmark_total_return": 0.02,
        "gross_return": 0.08,
    }
    rows = [row]
    source_hashes = {role: file_sha256(path) for role, path in sources.items()}
    outcome = tmp_path / "outcome.json"
    outcome.write_text(
        json.dumps(
            {"rows": rows, "rows_sha256": canonical_sha256(rows), "source_sha256": source_hashes}
        ),
        encoding="utf-8",
    )
    receipt_payload = {
        "schema_version": "future_outcome_receipt_v1",
        "family": "consumer_defensive",
        "outcome_rows_sha256": canonical_sha256(rows),
        "source_sha256": source_hashes,
        "capture_ids": [capture_id],
    }
    receipt = tmp_path / "receipt.json"
    receipt.write_text(json.dumps(receipt_payload), encoding="utf-8")
    return capture, outcome, sources, receipt


def test_early_terminal_proceeds_then_zero_cash_carry_is_supported(tmp_path: Path) -> None:
    capture, outcome, sources, receipt = _case(
        tmp_path,
        outcome_terminal_status="cash_settled",
    )
    result = validate_and_recompute_outcomes_v2(
        family="consumer_defensive",
        capture_paths=[capture],
        outcome_path=outcome,
        outcome_source_paths=sources,
        outcome_receipt_path=receipt,
        expected_outcome_receipt_sha256=file_sha256(receipt),
        authority=_Authority(),  # type: ignore[arg-type]
        benchmark_ticker="XLP",
    )
    assert result["early_terminal_cash_carry_policy"] == "zero_return_cash_to_common_exit"


def test_terminal_event_cannot_be_omitted(tmp_path: Path) -> None:
    capture, outcome, sources, receipt = _case(tmp_path, outcome_terminal_status="none")
    with pytest.raises(ValueError, match="terminal event was omitted"):
        validate_and_recompute_outcomes_v2(
            family="consumer_defensive",
            capture_paths=[capture],
            outcome_path=outcome,
            outcome_source_paths=sources,
            outcome_receipt_path=receipt,
            expected_outcome_receipt_sha256=file_sha256(receipt),
            authority=_Authority(),  # type: ignore[arg-type]
            benchmark_ticker="XLP",
        )


def test_raw_membership_cannot_be_self_reported_eligible(tmp_path: Path) -> None:
    capture, outcome, sources, receipt = _case(
        tmp_path,
        outcome_terminal_status="cash_settled",
        eligible="0",
    )
    with pytest.raises(ValueError, match="membership/lifecycle"):
        validate_and_recompute_outcomes_v2(
            family="consumer_defensive",
            capture_paths=[capture],
            outcome_path=outcome,
            outcome_source_paths=sources,
            outcome_receipt_path=receipt,
            expected_outcome_receipt_sha256=file_sha256(receipt),
            authority=_Authority(),  # type: ignore[arg-type]
            benchmark_ticker="XLP",
        )
