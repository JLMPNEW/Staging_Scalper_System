from __future__ import annotations

import csv
import json
from datetime import date
from pathlib import Path

import exchange_calendars
import pytest

from future_only_evidence.official_calendar import (
    CALENDAR_ID,
    CALENDAR_PROVIDER,
    CALENDAR_PROVIDER_VERSION,
)
from future_only_evidence.outcome_integrity_v3 import (
    ADJUSTMENT_POLICY,
    OUTCOME_RECEIPT_SCHEMA,
    OUTCOME_SCHEMA,
    PRE_ENTRY_NONEXECUTION_POLICY,
    _bar_index,
    _membership_index,
    _terminal_index,
    validate_and_recompute_outcomes_v3,
)
from future_only_evidence.protocol import canonical_sha256, file_sha256
from future_only_evidence.prospective_contracts import RETURN_CONVENTION, ProspectiveContract


class _Authority:
    def verify(self, *_args, **_kwargs) -> bool:
        return True

    def verify_snapshot(self, *_args, **_kwargs) -> bool:
        return True

    def identity(self) -> dict[str, str]:
        return {"authority_id": "test-evidence-authority"}


def _csv(path: Path, fields: list[str], rows: list[dict[str, object]]) -> Path:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    return path


def _calendar(path: Path) -> tuple[Path, list[dict[str, str]]]:
    calendar = exchange_calendars.get_calendar(CALENDAR_ID)
    sessions = calendar.sessions_in_range("2026-09-04", "2026-09-09")
    rows = [
        {
            "session_date": session.date().isoformat(),
            "entry_execution_at_utc": calendar.session_open(session).isoformat(),
            "exit_execution_at_utc": calendar.session_close(session).isoformat(),
            "calendar_id": CALENDAR_ID,
            "calendar_provider": CALENDAR_PROVIDER,
            "calendar_provider_version": CALENDAR_PROVIDER_VERSION,
        }
        for session in sessions
    ]
    return _csv(path, list(rows[0]), rows), rows


def _case(
    tmp_path: Path,
    *,
    terminal_at: str = "2026-09-05T12:00:00+00:00",
    terminal_date: str | None = None,
):
    calendar_path, calendar_rows = _calendar(tmp_path / "calendar.csv")
    entry_at = calendar_rows[1]["entry_execution_at_utc"]
    exit_at = calendar_rows[2]["entry_execution_at_utc"]
    cutoff_at = calendar_rows[0]["exit_execution_at_utc"]
    contract = ProspectiveContract(
        family="consumer_defensive",
        policy_id="test-pre-entry-policy",
        effective_from=date(2026, 9, 4),
        first_signal_date=date(2026, 9, 4),
        horizons=(1,),
        minimum_counts={1: 1},
        benchmark_ticker="XLP",
        cadence_id="monthly_true_month_end_v1",
        minimum_ic=0.0,
        minimum_efficacy=0.0,
        minimum_top_minus_bottom=0.0,
        minimum_hit_rate=0.0,
        transaction_cost_bps=20.0,
        top_minus_bottom_basis="net",
    )
    capture = {
        "capture_id": "capture-1",
        "signal_rows": [
            {
                "ticker": "AAA",
                "sleeve_id": "beverages",
                "group_id": "beverages",
                "eligible_flag": 1,
            }
        ],
        "trusted_capture_timing": {
            "entry_session_index": 1,
            "entry_session_date": calendar_rows[1]["session_date"],
            "entry_execution_at_utc": entry_at,
            "signal_information_cutoff_at_utc": cutoff_at,
        },
    }
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
            "captured_eligible_flag",
            "pre_entry_nonexecution_flag",
            "pre_entry_nonexecution_reason",
        ],
        [
            {
                "capture_id": "capture-1",
                "ticker": "AAA",
                "entry_execution_at_utc": entry_at,
                "sleeve_id": "beverages",
                "group_id": "beverages",
                "eligible_at_entry_flag": 0,
                "lifecycle_status_at_entry": "governed_pre_entry_nonexecution",
                "captured_eligible_flag": 1,
                "pre_entry_nonexecution_flag": 1,
                "pre_entry_nonexecution_reason": "cash_merger_closed_before_entry",
            }
        ],
    )
    terminals = _csv(
        tmp_path / "terminals.csv",
        [
            "ticker",
            "terminal_event_date",
            "terminal_execution_at_utc",
            "terminal_event_status",
            "terminal_event_reason",
            "terminal_total_return_index_includes_proceeds_flag",
            "cash_carry_return_after_terminal",
            "adjustment_policy_id",
            "price_convention_id",
        ],
        [
            {
                "ticker": "AAA",
                "terminal_event_date": terminal_date or terminal_at[:10],
                "terminal_execution_at_utc": terminal_at,
                "terminal_event_status": "cash_merger_terminal",
                "terminal_event_reason": "cash_merger",
                "terminal_total_return_index_includes_proceeds_flag": 0,
                "cash_carry_return_after_terminal": 0,
                "adjustment_policy_id": ADJUSTMENT_POLICY,
                "price_convention_id": RETURN_CONVENTION,
            }
        ],
    )
    bars = _csv(
        tmp_path / "bars.csv",
        [
            "ticker",
            "execution_at_utc",
            "total_return_index",
            "adjustment_policy_id",
            "price_convention_id",
        ],
        [
            {
                "ticker": "XLP",
                "execution_at_utc": entry_at,
                "total_return_index": 100.0,
                "adjustment_policy_id": ADJUSTMENT_POLICY,
                "price_convention_id": RETURN_CONVENTION,
            },
            {
                "ticker": "XLP",
                "execution_at_utc": exit_at,
                "total_return_index": 101.0,
                "adjustment_policy_id": ADJUSTMENT_POLICY,
                "price_convention_id": RETURN_CONVENTION,
            },
        ],
    )
    assets = _csv(
        tmp_path / "assets.csv",
        [
            "ticker",
            "asset_id",
            "provider_id",
            "dataset_id",
            "exchange_mic",
            "currency",
            "effective_from",
            "effective_to",
        ],
        [
            {
                "ticker": "AAA",
                "asset_id": "asset-aaa",
                "provider_id": "provider",
                "dataset_id": "dataset",
                "exchange_mic": "XNYS",
                "currency": "USD",
                "effective_from": "2020-01-01",
                "effective_to": "2026-09-05",
            },
            {
                "ticker": "XLP",
                "asset_id": "asset-xlp",
                "provider_id": "provider",
                "dataset_id": "dataset",
                "exchange_mic": "ARCX",
                "currency": "USD",
                "effective_from": "2020-01-01",
                "effective_to": "",
            },
        ],
    )
    actions = _csv(
        tmp_path / "actions.csv",
        [
            "ticker",
            "asset_id",
            "action_id",
            "action_type",
            "terminal_event_status",
            "terminal_event_reason",
            "effective_at_utc",
            "source_observation_id",
        ],
        [
            {
                "ticker": "AAA",
                "asset_id": "asset-aaa",
                "action_id": "action-1",
                "action_type": "merger_cash",
                "terminal_event_status": "cash_merger_terminal",
                "terminal_event_reason": "cash_merger",
                "effective_at_utc": terminal_at,
                "source_observation_id": "action-observation-1",
            }
        ],
    )
    sources = {
        "total_return_bars": bars,
        "terminal_events": terminals,
        "trading_calendar": calendar_path,
        "membership_history": membership,
        "asset_master": assets,
        "corporate_actions": actions,
    }
    source_hashes = {role: file_sha256(path) for role, path in sources.items()}
    benchmark_return = 101.0 / 100.0 - 1.0
    row = {
        "capture_id": "capture-1",
        "ticker": "AAA",
        "sleeve_id": "beverages",
        "group_id": "beverages",
        "horizon_sessions": 1,
        "entry_date": calendar_rows[1]["session_date"],
        "exit_date": calendar_rows[2]["session_date"],
        "entry_session_index": 1,
        "exit_session_index": 2,
        "entry_execution_at_utc": entry_at,
        "exit_execution_at_utc": exit_at,
        "security_exit_execution_at_utc": terminal_at,
        "stock_total_return": 0.0,
        "benchmark_total_return": benchmark_return,
        "gross_return": -benchmark_return,
        "membership_status": "captured_eligible_pre_entry_nonexecution_cash",
        "terminal_event_status": "cash_merger_terminal",
        "pre_entry_nonexecution_flag": 1,
        "pre_entry_nonexecution_reason": "cash_merger_closed_before_entry",
        "pre_entry_nonexecution_policy_id": PRE_ENTRY_NONEXECUTION_POLICY,
        "terminal_cash_carry_policy": PRE_ENTRY_NONEXECUTION_POLICY,
        "outcome_available_at_utc": "2026-09-09T13:31:00+00:00",
    }
    outcome_payload = {
        "schema_version": OUTCOME_SCHEMA,
        "state": "outcomes_observed_after_capture",
        "evidence_class": "prospective_future_only",
        "family": contract.family,
        "policy_id": contract.policy_id,
        "benchmark_ticker": contract.benchmark_ticker,
        "horizons": [1],
        "return_convention": RETURN_CONVENTION,
        "pre_entry_nonexecution_policy_id": PRE_ENTRY_NONEXECUTION_POLICY,
        "trading_calendar_sha256": file_sha256(calendar_path),
        "source_sha256": source_hashes,
        "contract_identity_sha256": canonical_sha256(contract.identity()),
        "historical_results_can_authorize_production": False,
        "production_activation_authorized": False,
        "portfolio_write_enabled": False,
        "optimizer_cap": 0.0,
        "rows": [row],
        "rows_sha256": canonical_sha256([row]),
    }
    outcome = tmp_path / "outcome.json"
    outcome.write_text(json.dumps(outcome_payload), encoding="utf-8")
    receipt_payload = {
        "schema_version": OUTCOME_RECEIPT_SCHEMA,
        "family": contract.family,
        "policy_id": contract.policy_id,
        "benchmark_ticker": contract.benchmark_ticker,
        "contract_identity_sha256": canonical_sha256(contract.identity()),
        "horizons": [1],
        "return_convention": RETURN_CONVENTION,
        "adjustment_policy_id": ADJUSTMENT_POLICY,
        "pre_entry_nonexecution_policy_id": PRE_ENTRY_NONEXECUTION_POLICY,
        "trading_calendar_sha256": file_sha256(calendar_path),
        "outcome_rows_sha256": outcome_payload["rows_sha256"],
        "outcome_package_sha256": file_sha256(outcome),
        "source_sha256": source_hashes,
        "capture_ids": ["capture-1"],
        "anchored_at_utc": "2026-09-09T14:00:00+00:00",
    }
    receipt = tmp_path / "receipt.json"
    receipt.write_text(json.dumps(receipt_payload), encoding="utf-8")
    return contract, capture, outcome, sources, receipt, calendar_path


def test_pre_entry_terminal_name_remains_as_zero_return_cash(tmp_path: Path) -> None:
    contract, capture, outcome, sources, receipt, calendar = _case(tmp_path)
    audit = validate_and_recompute_outcomes_v3(
        contract=contract,
        captures=[capture],
        outcome_path=outcome,
        outcome_source_paths=sources,
        outcome_receipt_path=receipt,
        expected_outcome_receipt_sha256=file_sha256(receipt),
        authority=_Authority(),  # type: ignore[arg-type]
        trading_calendar_path=calendar,
        evaluated_at_utc="2026-09-09T15:00:00+00:00",
    )
    assert audit["normalized_rows"][0]["stock_total_return"] == 0.0
    assert audit["normalized_rows"][0]["gross_return"] < 0.0
    assert audit["pre_entry_nonexecution_count"] == 1
    assert audit["pre_entry_nonexecution_no_reselection_pass"] is True


def test_pre_entry_nonexecution_cannot_predate_signal_cutoff(tmp_path: Path) -> None:
    contract, capture, outcome, sources, receipt, calendar = _case(
        tmp_path,
        terminal_at="2026-09-04T19:00:00+00:00",
    )
    with pytest.raises(ValueError, match="pre-entry nonexecution timing"):
        validate_and_recompute_outcomes_v3(
            contract=contract,
            captures=[capture],
            outcome_path=outcome,
            outcome_source_paths=sources,
            outcome_receipt_path=receipt,
            expected_outcome_receipt_sha256=file_sha256(receipt),
            authority=_Authority(),  # type: ignore[arg-type]
            trading_calendar_path=calendar,
            evaluated_at_utc="2026-09-09T15:00:00+00:00",
        )


def test_terminal_event_date_rejects_trailing_timestamp_text(tmp_path: Path) -> None:
    contract, capture, outcome, sources, receipt, calendar = _case(
        tmp_path,
        terminal_date="2026-09-05T00:00:00Z",
    )
    with pytest.raises(ValueError, match="exact YYYY-MM-DD"):
        validate_and_recompute_outcomes_v3(
            contract=contract,
            captures=[capture],
            outcome_path=outcome,
            outcome_source_paths=sources,
            outcome_receipt_path=receipt,
            expected_outcome_receipt_sha256=file_sha256(receipt),
            authority=_Authority(),  # type: ignore[arg-type]
            trading_calendar_path=calendar,
            evaluated_at_utc="2026-09-09T15:00:00+00:00",
        )


def test_outcome_receipt_snapshot_is_not_reread_after_binding(tmp_path: Path) -> None:
    contract, capture, outcome, sources, receipt, calendar = _case(tmp_path)
    receipt_bytes = receipt.read_bytes()
    expected_receipt_sha256 = file_sha256(receipt)
    receipt.write_text("{}", encoding="utf-8")
    audit = validate_and_recompute_outcomes_v3(
        contract=contract,
        captures=[capture],
        outcome_path=outcome,
        outcome_source_paths=sources,
        outcome_receipt_path=receipt,
        expected_outcome_receipt_sha256=expected_receipt_sha256,
        authority=_Authority(),  # type: ignore[arg-type]
        trading_calendar_path=calendar,
        evaluated_at_utc="2026-09-09T15:00:00+00:00",
        outcome_receipt_snapshot_bytes=receipt_bytes,
    )
    assert audit["outcome_receipt_sha256"] == expected_receipt_sha256


@pytest.mark.parametrize(
    ("source_role", "parser", "old", "new"),
    [
        ("total_return_bars", _bar_index, "XLP,", "xlp,"),
        ("membership_history", _membership_index, "AAA,", " AAA,"),
        ("terminal_events", _terminal_index, "AAA,", "aaa,"),
    ],
)
def test_raw_outcome_source_tickers_are_not_normalized(
    tmp_path: Path,
    source_role: str,
    parser,
    old: str,
    new: str,
) -> None:
    _, _, _, sources, _, _ = _case(tmp_path)
    payload = sources[source_role].read_bytes().replace(
        old.encode("utf-8"), new.encode("utf-8"), 1
    )
    with pytest.raises(ValueError, match="canonical uppercase ticker"):
        parser(payload)


def test_outcome_row_ticker_is_not_normalized(tmp_path: Path) -> None:
    contract, capture, outcome, sources, receipt, calendar = _case(tmp_path)
    outcome_payload = json.loads(outcome.read_text(encoding="utf-8"))
    outcome_payload["rows"][0]["ticker"] = "aaa"
    outcome_payload["rows_sha256"] = canonical_sha256(outcome_payload["rows"])
    outcome.write_text(json.dumps(outcome_payload), encoding="utf-8")
    receipt_payload = json.loads(receipt.read_text(encoding="utf-8"))
    receipt_payload["outcome_rows_sha256"] = outcome_payload["rows_sha256"]
    receipt_payload["outcome_package_sha256"] = file_sha256(outcome)
    receipt.write_text(json.dumps(receipt_payload), encoding="utf-8")
    with pytest.raises(ValueError, match="canonical uppercase ticker"):
        validate_and_recompute_outcomes_v3(
            contract=contract,
            captures=[capture],
            outcome_path=outcome,
            outcome_source_paths=sources,
            outcome_receipt_path=receipt,
            expected_outcome_receipt_sha256=file_sha256(receipt),
            authority=_Authority(),  # type: ignore[arg-type]
            trading_calendar_path=calendar,
            evaluated_at_utc="2026-09-09T15:00:00+00:00",
        )
