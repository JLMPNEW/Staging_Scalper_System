"""Strict raw-source recomputation for canonical prospective outcomes."""

from __future__ import annotations

import csv
import hashlib
import io
import math
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from .canonical_values import exact_utc
from .official_calendar import validate_official_xnys_calendar_bytes
from .protocol import canonical_sha256, exact_sha256
from .prospective_contracts import (
    RETURN_CONVENTION,
    ProspectiveContract,
    read_calendar_bytes,
    read_json_snapshot,
)
from .trusted_receipts import PinnedEd25519Authority


OUTCOME_SCHEMA = "future_only_outcomes_v3"
OUTCOME_RECEIPT_SCHEMA = "future_outcome_receipt_v3"
ADJUSTMENT_POLICY = "split_dividend_terminal_total_return_v1"
PRE_ENTRY_NONEXECUTION_POLICY = (
    "governed_pre_entry_nonexecution_cash_carry_with_intended_turnover_cost_v1"
)
PRE_ENTRY_NONEXECUTION_REASONS = frozenset(
    {
        "bankruptcy_no_open_execution",
        "cash_liquidation_before_entry",
        "cash_merger_closed_before_entry",
        "delisted_before_entry",
        "exchange_halt_no_entry_execution",
    }
)
TERMINAL_REASON_TO_PRE_ENTRY_REASON = {
    "bankruptcy": "bankruptcy_no_open_execution",
    "cash_liquidation": "cash_liquidation_before_entry",
    "cash_merger": "cash_merger_closed_before_entry",
    "delisting": "delisted_before_entry",
    "exchange_halt_terminal": "exchange_halt_no_entry_execution",
}
OUTCOME_SOURCE_ROLES_V3 = frozenset(
    {
        "total_return_bars",
        "terminal_events",
        "trading_calendar",
        "membership_history",
        "asset_master",
        "corporate_actions",
    }
)


def _utc(value: Any, *, label: str) -> datetime:
    return exact_utc(value, label=label)


def _exact_date(value: Any, *, label: str) -> str:
    text = str(value)
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{label} must be exact YYYY-MM-DD") from exc
    if parsed.isoformat() != text:
        raise ValueError(f"{label} must be exact YYYY-MM-DD")
    return text


def _canonical_ticker(value: Any, *, label: str) -> str:
    if (
        type(value) is not str
        or not value
        or value.strip() != value
        or value.upper() != value
    ):
        raise ValueError(f"{label} must be a canonical uppercase ticker")
    return value


def _strict_int(
    value: Any,
    *,
    label: str,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    if type(value) is not int:
        raise ValueError(f"{label} must be a canonical integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{label} is below its minimum")
    if maximum is not None and value > maximum:
        raise ValueError(f"{label} exceeds its maximum")
    return value


def _csv_rows(payload_bytes: bytes, *, label: str) -> tuple[list[dict[str, str]], set[str]]:
    try:
        text = payload_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{label} must be valid UTF-8 CSV") from exc
    reader = csv.DictReader(io.StringIO(text, newline=""))
    return [dict(row) for row in reader], set(reader.fieldnames or [])


def _bar_index(payload_bytes: bytes) -> dict[tuple[str, str], float]:
    rows, fields = _csv_rows(payload_bytes, label="raw total-return bars")
    required = {
        "ticker",
        "execution_at_utc",
        "total_return_index",
        "adjustment_policy_id",
        "price_convention_id",
    }
    if not rows or not required <= fields:
        raise ValueError("raw execution total-return bars have an invalid schema")
    result: dict[tuple[str, str], float] = {}
    for row in rows:
        if row["adjustment_policy_id"] != ADJUSTMENT_POLICY:
            raise ValueError("total-return adjustment policy changed")
        if row["price_convention_id"] != RETURN_CONVENTION:
            raise ValueError("raw bars do not use the registered open-execution convention")
        execution = _utc(row["execution_at_utc"], label="raw execution timestamp").isoformat()
        key = (_canonical_ticker(row["ticker"], label="raw-bar ticker"), execution)
        value = float(row["total_return_index"])
        if key in result or not math.isfinite(value) or value < 0:
            raise ValueError("raw execution bars contain duplicate/invalid values")
        result[key] = value
    return result


def _membership_index(payload_bytes: bytes) -> dict[tuple[str, str, str], dict[str, str]]:
    rows, fields = _csv_rows(payload_bytes, label="raw membership history")
    required = {
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
    }
    if not rows or not required <= fields:
        raise ValueError("raw membership history has an invalid schema")
    result: dict[tuple[str, str, str], dict[str, str]] = {}
    for row in rows:
        for field in (
            "eligible_at_entry_flag",
            "captured_eligible_flag",
            "pre_entry_nonexecution_flag",
        ):
            if row[field] not in {"0", "1"}:
                raise ValueError(f"membership {field} must be canonical CSV 0/1")
        key = (
            str(row["capture_id"]),
            _canonical_ticker(row["ticker"], label="membership ticker"),
            _utc(row["entry_execution_at_utc"], label="membership entry").isoformat(),
        )
        if key in result:
            raise ValueError("membership history has duplicate capture/ticker/entry")
        result[key] = row
    return result


def _terminal_index(payload_bytes: bytes) -> dict[str, dict[str, str]]:
    rows, fields = _csv_rows(payload_bytes, label="raw terminal events")
    required = {
        "ticker",
        "terminal_event_date",
        "terminal_execution_at_utc",
        "terminal_event_status",
        "terminal_event_reason",
        "terminal_total_return_index_includes_proceeds_flag",
        "cash_carry_return_after_terminal",
        "adjustment_policy_id",
        "price_convention_id",
    }
    if not required <= fields:
        raise ValueError("raw terminal-event source has an invalid schema")
    if not rows:
        return {}
    result: dict[str, dict[str, str]] = {}
    for row in rows:
        ticker = _canonical_ticker(row["ticker"], label="terminal-event ticker")
        if ticker in result:
            raise ValueError("terminal-event source contains duplicate ticker events")
        if row["adjustment_policy_id"] != ADJUSTMENT_POLICY:
            raise ValueError("terminal-event adjustment policy changed")
        if row["price_convention_id"] != RETURN_CONVENTION:
            raise ValueError("terminal-event source changed the price convention")
        terminal_execution = _utc(
            row["terminal_execution_at_utc"], label="terminal execution"
        )
        if _exact_date(
            row["terminal_event_date"], label="terminal-event date"
        ) != terminal_execution.date().isoformat():
            raise ValueError("terminal-event date differs from execution timestamp")
        if str(row["terminal_event_reason"]) not in TERMINAL_REASON_TO_PRE_ENTRY_REASON:
            raise ValueError("terminal-event reason is outside the governed census")
        if row["terminal_total_return_index_includes_proceeds_flag"] not in {"0", "1"}:
            raise ValueError("terminal proceeds flag must be canonical CSV 0/1")
        if row["cash_carry_return_after_terminal"] != "0":
            raise ValueError("terminal cash carry must be the canonical zero literal")
        result[ticker] = row
    return result


def validate_and_recompute_outcomes_v3(
    *,
    contract: ProspectiveContract,
    captures: Sequence[Mapping[str, Any]],
    outcome_path: Path,
    outcome_source_paths: Mapping[str, Path],
    outcome_receipt_path: Path,
    expected_outcome_receipt_sha256: str,
    authority: PinnedEd25519Authority,
    trading_calendar_path: Path,
    evaluated_at_utc: str,
    outcome_source_snapshot_bytes: Mapping[str, bytes] | None = None,
    trading_calendar_snapshot_bytes: bytes | None = None,
    outcome_receipt_snapshot_bytes: bytes | None = None,
) -> dict[str, Any]:
    if set(outcome_source_paths) != OUTCOME_SOURCE_ROLES_V3:
        raise ValueError("outcome source roles do not exactly match the contract")
    if (
        outcome_source_snapshot_bytes is not None
        and set(outcome_source_snapshot_bytes) != set(outcome_source_paths)
    ):
        raise ValueError("outcome source snapshots differ from the exact source-role census")
    source_bytes = (
        {
            role: bytes(payload)
            for role, payload in sorted(outcome_source_snapshot_bytes.items())
        }
        if outcome_source_snapshot_bytes is not None
        else {
            role: Path(path).expanduser().resolve().read_bytes()
            for role, path in sorted(outcome_source_paths.items())
        }
    )
    source_hashes = {
        role: hashlib.sha256(payload).hexdigest()
        for role, payload in source_bytes.items()
    }
    calendar_bytes = (
        bytes(trading_calendar_snapshot_bytes)
        if trading_calendar_snapshot_bytes is not None
        else Path(trading_calendar_path).expanduser().resolve().read_bytes()
    )
    validate_official_xnys_calendar_bytes(calendar_bytes)
    calendar_hash = hashlib.sha256(calendar_bytes).hexdigest()
    if source_hashes["trading_calendar"] != calendar_hash:
        raise ValueError("raw outcomes use a different trading calendar")
    outcome, outcome_package_hash, outcome_resolved, _ = read_json_snapshot(
        outcome_path,
        label="canonical outcome package",
    )
    if outcome.get("schema_version") != OUTCOME_SCHEMA:
        raise ValueError("legacy/self-reported outcomes cannot satisfy the canonical gate")
    expected_top = {
        "state": "outcomes_observed_after_capture",
        "evidence_class": "prospective_future_only",
        "family": contract.family,
        "policy_id": contract.policy_id,
        "benchmark_ticker": contract.benchmark_ticker,
        "horizons": list(contract.horizons),
        "return_convention": RETURN_CONVENTION,
        "pre_entry_nonexecution_policy_id": PRE_ENTRY_NONEXECUTION_POLICY,
        "trading_calendar_sha256": calendar_hash,
        "source_sha256": source_hashes,
        "contract_identity_sha256": canonical_sha256(contract.identity()),
        "historical_results_can_authorize_production": False,
        "production_activation_authorized": False,
        "portfolio_write_enabled": False,
        "optimizer_cap": 0.0,
    }
    for field, expected in expected_top.items():
        if outcome.get(field) != expected:
            raise ValueError(f"outcome package changed canonical field: {field}")
    optimizer_cap = outcome.get("optimizer_cap")
    if (
        type(optimizer_cap) not in {int, float}
        or not math.isfinite(float(optimizer_cap))
        or float(optimizer_cap) != 0.0
    ):
        raise ValueError("outcome optimizer cap must remain an explicit numeric zero")
    rows = outcome.get("rows")
    if not isinstance(rows, list) or not rows:
        raise ValueError("canonical outcome package contains no rows")
    if outcome.get("rows_sha256") != canonical_sha256(rows):
        raise ValueError("outcome row hash mismatch")
    receipt_bytes = (
        bytes(outcome_receipt_snapshot_bytes)
        if outcome_receipt_snapshot_bytes is not None
        else Path(outcome_receipt_path).expanduser().resolve().read_bytes()
    )
    receipt, receipt_hash, _, _ = read_json_snapshot(
        outcome_receipt_path,
        label="outcome receipt",
        payload_snapshot_bytes=receipt_bytes,
    )
    if receipt_hash != exact_sha256(expected_outcome_receipt_sha256, label="outcome receipt sha256"):
        raise ValueError("outcome receipt SHA-256 mismatch")
    authority.verify_snapshot(receipt_bytes, receipt_hash, receipt)
    capture_ids = [str(capture["capture_id"]) for capture in captures]
    expected_receipt = {
        "schema_version": OUTCOME_RECEIPT_SCHEMA,
        "family": contract.family,
        "policy_id": contract.policy_id,
        "benchmark_ticker": contract.benchmark_ticker,
        "contract_identity_sha256": canonical_sha256(contract.identity()),
        "horizons": list(contract.horizons),
        "return_convention": RETURN_CONVENTION,
        "adjustment_policy_id": ADJUSTMENT_POLICY,
        "pre_entry_nonexecution_policy_id": PRE_ENTRY_NONEXECUTION_POLICY,
        "trading_calendar_sha256": calendar_hash,
        "outcome_rows_sha256": outcome["rows_sha256"],
        "outcome_package_sha256": outcome_package_hash,
        "source_sha256": source_hashes,
        "capture_ids": capture_ids,
    }
    for field, expected in expected_receipt.items():
        if receipt.get(field) != expected:
            raise ValueError(f"signed outcome receipt identity mismatch: {field}")
    evaluated_at = _utc(evaluated_at_utc, label="evaluated_at_utc")
    anchored_at = _utc(receipt.get("anchored_at_utc"), label="outcome anchored_at_utc")
    if anchored_at > evaluated_at:
        raise ValueError("outcome receipt is dated after evaluation")
    calendar_rows, session_index = read_calendar_bytes(calendar_bytes)
    capture_index = {str(capture["capture_id"]): dict(capture) for capture in captures}
    if len(capture_index) != len(captures):
        raise ValueError("outcome capture census contains duplicate capture ids")
    bars = _bar_index(source_bytes["total_return_bars"])
    memberships = _membership_index(source_bytes["membership_history"])
    terminals = _terminal_index(source_bytes["terminal_events"])
    expected_memberships: set[tuple[str, str, str]] = set()
    required_bar_keys: set[tuple[str, str]] = set()
    pre_entry_nonexecution_count = 0
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, str, int]] = set()
    expected_outcomes: set[tuple[str, str, int]] = set()
    capture_entry_indexes: dict[str, int] = {}
    for capture in captures:
        capture_id = str(capture["capture_id"])
        timing = capture.get("trusted_capture_timing")
        if not isinstance(timing, dict):
            raise ValueError("capture lacks signed entry timing")
        entry_index_value = _strict_int(
            timing.get("entry_session_index"),
            label="capture entry session index",
            minimum=0,
        )
        capture_entry_indexes[capture_id] = entry_index_value
        signal_rows = capture.get("signal_rows")
        if not isinstance(signal_rows, list):
            raise ValueError("capture signal rows are invalid")
        for signal in signal_rows:
            if not isinstance(signal, dict):
                raise ValueError("capture signal row is invalid")
            eligible = _strict_int(
                signal.get("eligible_flag"),
                label="captured signal eligibility",
                minimum=0,
                maximum=1,
            )
            if eligible != 1:
                continue
            for governed_horizon in contract.horizons:
                exit_position = entry_index_value + governed_horizon
                if exit_position < len(calendar_rows) and _utc(
                    calendar_rows[exit_position]["entry_execution_at_utc"],
                    label="calendar horizon open",
                ) < anchored_at:
                    expected_outcomes.add(
                        (
                            capture_id,
                            _canonical_ticker(
                                signal["ticker"], label="captured signal ticker"
                            ),
                            governed_horizon,
                        )
                    )
    benchmark_by_period: dict[str, float] = {}
    required_fields = {
        "capture_id",
        "ticker",
        "sleeve_id",
        "group_id",
        "horizon_sessions",
        "entry_date",
        "exit_date",
        "entry_session_index",
        "exit_session_index",
        "entry_execution_at_utc",
        "exit_execution_at_utc",
        "security_exit_execution_at_utc",
        "stock_total_return",
        "benchmark_total_return",
        "gross_return",
        "membership_status",
        "terminal_event_status",
        "pre_entry_nonexecution_flag",
        "pre_entry_nonexecution_reason",
        "pre_entry_nonexecution_policy_id",
        "terminal_cash_carry_policy",
        "outcome_available_at_utc",
    }
    for raw in rows:
        if required_fields - set(raw):
            raise ValueError(f"outcome row missing fields={sorted(required_fields - set(raw))}")
        row = dict(raw)
        capture_id = str(row["capture_id"])
        ticker = _canonical_ticker(row["ticker"], label="outcome ticker")
        horizon = _strict_int(
            row["horizon_sessions"],
            label="outcome horizon",
            minimum=1,
        )
        identity = (capture_id, ticker, horizon)
        if identity in seen:
            raise ValueError("outcome package contains duplicate capture/ticker/horizon")
        seen.add(identity)
        if horizon not in contract.horizons or capture_id not in capture_index:
            raise ValueError("outcome references an unbound capture/horizon")
        capture = capture_index[capture_id]
        signal = next(
            (
                signal_row
                for signal_row in capture["signal_rows"]
                if signal_row["ticker"] == ticker and signal_row["eligible_flag"] == 1
            ),
            None,
        )
        if signal is None:
            raise ValueError(f"{ticker}: outcome has no eligible captured signal")
        if row["sleeve_id"] != signal["sleeve_id"] or row["group_id"] != signal["group_id"]:
            raise ValueError(f"{ticker}: outcome sleeve/group differs from capture")
        timing = capture.get("trusted_capture_timing")
        if not isinstance(timing, dict):
            raise ValueError("capture lacks signed entry timing")
        entry_date = _exact_date(row["entry_date"], label="outcome entry date")
        exit_date = _exact_date(row["exit_date"], label="outcome exit date")
        if entry_date != _exact_date(
            timing["entry_session_date"], label="signed capture entry date"
        ):
            raise ValueError("outcome entry date differs from signed capture entry")
        if entry_date not in session_index or exit_date not in session_index:
            raise ValueError("outcome interval is absent from the bound calendar")
        entry_index = session_index[entry_date]
        exit_index = session_index[exit_date]
        if entry_index != capture_entry_indexes[capture_id]:
            raise ValueError("outcome entry session index differs from signed capture")
        if exit_index - entry_index != horizon:
            raise ValueError("outcome interval does not span the claimed sessions")
        submitted_entry_index = _strict_int(
            row["entry_session_index"],
            label="outcome entry session index",
            minimum=0,
        )
        submitted_exit_index = _strict_int(
            row["exit_session_index"],
            label="outcome exit session index",
            minimum=0,
        )
        if (submitted_entry_index, submitted_exit_index) != (
            entry_index,
            exit_index,
        ):
            raise ValueError("submitted outcome session indexes differ from calendar")
        entry_execution = _utc(row["entry_execution_at_utc"], label="outcome entry execution")
        common_exit_execution = _utc(row["exit_execution_at_utc"], label="outcome exit execution")
        expected_entry = _utc(
            calendar_rows[entry_index]["entry_execution_at_utc"],
            label="calendar entry execution",
        )
        expected_exit = _utc(
            calendar_rows[exit_index]["entry_execution_at_utc"],
            label="calendar horizon open execution",
        )
        if entry_execution != expected_entry or common_exit_execution != expected_exit:
            raise ValueError("outcome does not use exact open-execution interval endpoints")
        if entry_execution.isoformat() != str(timing["entry_execution_at_utc"]):
            raise ValueError("outcome entry timestamp differs from signed capture")
        available_at = _utc(row["outcome_available_at_utc"], label="outcome availability")
        if not common_exit_execution < available_at <= anchored_at:
            raise ValueError("outcome was not observed and anchored strictly after exit")
        membership_key = (capture_id, ticker, entry_execution.isoformat())
        expected_memberships.add(membership_key)
        membership = memberships.get(membership_key)
        if membership is None:
            raise ValueError(f"{ticker}: exact raw entry membership is absent")
        pre_entry_value = row["pre_entry_nonexecution_flag"]
        if type(pre_entry_value) is not int or pre_entry_value not in (0, 1):
            raise ValueError(f"{ticker}: pre-entry nonexecution flag must be strict 0/1")
        pre_entry = pre_entry_value == 1
        pre_entry_reason = str(row["pre_entry_nonexecution_reason"])
        if (
            row["pre_entry_nonexecution_policy_id"]
            != PRE_ENTRY_NONEXECUTION_POLICY
            or membership["captured_eligible_flag"] != "1"
            or membership["sleeve_id"] != signal["sleeve_id"]
            or membership["group_id"] != signal["group_id"]
            or membership["pre_entry_nonexecution_flag"] != str(pre_entry_value)
            or membership["pre_entry_nonexecution_reason"] != pre_entry_reason
        ):
            raise ValueError(f"{ticker}: raw membership/lifecycle differs from capture")
        if pre_entry:
            pre_entry_nonexecution_count += 1
            if (
                pre_entry_reason not in PRE_ENTRY_NONEXECUTION_REASONS
                or membership["eligible_at_entry_flag"] != "0"
                or membership["lifecycle_status_at_entry"]
                != "governed_pre_entry_nonexecution"
                or row["membership_status"]
                != "captured_eligible_pre_entry_nonexecution_cash"
            ):
                raise ValueError(f"{ticker}: pre-entry nonexecution lifecycle is not governed")
        elif (
            pre_entry_reason != "none"
            or membership["eligible_at_entry_flag"] != "1"
            or membership["lifecycle_status_at_entry"]
            not in {"active", "active_terminal_event_later"}
            or row["membership_status"] not in {"member_at_entry", "eligible_at_entry"}
        ):
            raise ValueError(f"{ticker}: raw membership/lifecycle differs from capture")
        terminal = terminals.get(ticker)
        terminal_status = str(row["terminal_event_status"])
        security_exit_execution = common_exit_execution
        if pre_entry:
            if terminal is None or terminal["terminal_event_status"] != terminal_status:
                raise ValueError(f"{ticker}: pre-entry terminal event is missing/mismatched")
            security_exit_execution = _utc(
                terminal["terminal_execution_at_utc"],
                label="pre-entry terminal execution",
            )
            cutoff_at = _utc(
                timing["signal_information_cutoff_at_utc"],
                label="signal information cutoff",
            )
            expected_reason = TERMINAL_REASON_TO_PRE_ENTRY_REASON[
                str(terminal["terminal_event_reason"])
            ]
            if (
                terminal_status == "none"
                or expected_reason != pre_entry_reason
                or not cutoff_at < security_exit_execution <= entry_execution
                or terminal["terminal_total_return_index_includes_proceeds_flag"] != "0"
                or terminal["cash_carry_return_after_terminal"] != "0"
                or row["terminal_cash_carry_policy"] != PRE_ENTRY_NONEXECUTION_POLICY
            ):
                raise ValueError(f"{ticker}: pre-entry nonexecution timing/cash policy changed")
        elif terminal_status == "none":
            if terminal is not None and _utc(
                terminal["terminal_execution_at_utc"], label="terminal execution"
            ) <= common_exit_execution:
                raise ValueError(f"{ticker}: terminal event was omitted")
            if row["terminal_cash_carry_policy"] != "not_applicable":
                raise ValueError(f"{ticker}: non-terminal row changed cash-carry policy")
        else:
            if terminal is None or terminal["terminal_event_status"] != terminal_status:
                raise ValueError(f"{ticker}: governed terminal event is missing/mismatched")
            security_exit_execution = _utc(
                terminal["terminal_execution_at_utc"], label="terminal execution"
            )
            if not entry_execution < security_exit_execution <= common_exit_execution:
                raise ValueError(f"{ticker}: terminal execution is outside the governed interval")
            if (
                terminal["terminal_total_return_index_includes_proceeds_flag"] != "1"
                or terminal["cash_carry_return_after_terminal"] != "0"
                or row.get("terminal_cash_carry_policy")
                != "zero_return_cash_to_common_exit"
            ):
                raise ValueError(f"{ticker}: terminal proceeds/cash carry are not governed")
        if _utc(row["security_exit_execution_at_utc"], label="security exit") != security_exit_execution:
            raise ValueError(f"{ticker}: submitted security exit differs from raw lifecycle source")
        try:
            benchmark_entry = bars[(contract.benchmark_ticker, entry_execution.isoformat())]
            benchmark_exit = bars[(contract.benchmark_ticker, common_exit_execution.isoformat())]
        except KeyError as exc:
            raise ValueError(f"{ticker}: exact benchmark open-execution TRI is missing") from exc
        required_bar_keys.update(
            {
                (contract.benchmark_ticker, entry_execution.isoformat()),
                (contract.benchmark_ticker, common_exit_execution.isoformat()),
            }
        )
        if benchmark_entry <= 0 or benchmark_exit <= 0:
            raise ValueError(f"{ticker}: benchmark TRI must be strictly positive")
        if pre_entry:
            stock_return = 0.0
        else:
            try:
                stock_entry = bars[(ticker, entry_execution.isoformat())]
                stock_exit = bars[(ticker, security_exit_execution.isoformat())]
            except KeyError as exc:
                raise ValueError(f"{ticker}: exact stock open-execution TRI is missing") from exc
            required_bar_keys.update(
                {
                    (ticker, entry_execution.isoformat()),
                    (ticker, security_exit_execution.isoformat()),
                }
            )
            if stock_entry <= 0:
                raise ValueError(f"{ticker}: stock entry TRI must be strictly positive")
            stock_return = stock_exit / stock_entry - 1.0
        benchmark_return = benchmark_exit / benchmark_entry - 1.0
        residual_return = stock_return - benchmark_return
        submitted = (
            float(row["stock_total_return"]),
            float(row["benchmark_total_return"]),
            float(row["gross_return"]),
        )
        if not all(math.isfinite(value) for value in submitted):
            raise ValueError(f"{ticker}: submitted return is non-finite")
        recomputed = (stock_return, benchmark_return, residual_return)
        if any(abs(left - right) > 1e-12 for left, right in zip(submitted, recomputed)):
            raise ValueError(f"{ticker}: submitted return differs from raw TRI arithmetic")
        period_key = f"{capture_id}|{horizon}"
        previous = benchmark_by_period.setdefault(period_key, benchmark_return)
        if abs(previous - benchmark_return) > 1e-12:
            raise ValueError("benchmark return differs within one capture/horizon")
        normalized.append(
            {
                **row,
                "ticker": ticker,
                "horizon_sessions": horizon,
                "entry_date": entry_date,
                "exit_date": exit_date,
                "entry_execution_at_utc": entry_execution.isoformat(),
                "exit_execution_at_utc": common_exit_execution.isoformat(),
                "security_exit_execution_at_utc": security_exit_execution.isoformat(),
                "stock_total_return": stock_return,
                "benchmark_total_return": benchmark_return,
                "gross_return": residual_return,
            }
        )
    if seen != expected_outcomes:
        raise ValueError("outcome package is not the exact eligible capture/horizon census")
    if set(memberships) != expected_memberships:
        raise ValueError("raw membership history is not the exact outcome census")
    if set(bars) != required_bar_keys:
        raise ValueError("raw total-return bars are not the exact recomputation endpoint census")
    return {
        "normalized_rows": normalized,
        "outcome_rows_sha256": canonical_sha256(normalized),
        "outcome_package_sha256": outcome_package_hash,
        "outcome_source_sha256": source_hashes,
        "outcome_receipt_sha256": receipt_hash,
        "outcome_receipt_anchored_at_utc": anchored_at.isoformat(),
        "return_arithmetic_recomputed_pass": True,
        "exact_open_execution_price_convention_pass": True,
        "early_terminal_zero_cash_carry_pass": True,
        "pre_entry_nonexecution_policy_id": PRE_ENTRY_NONEXECUTION_POLICY,
        "pre_entry_nonexecution_count": pre_entry_nonexecution_count,
        "pre_entry_nonexecution_zero_return_cash_pass": True,
        "pre_entry_nonexecution_no_reselection_pass": True,
        "pre_entry_nonexecution_intended_turnover_cost_required": True,
        "membership_history_exact_census_pass": True,
        "benchmark_ticker": contract.benchmark_ticker,
        "benchmark_by_period": benchmark_by_period,
        "trusted_authority": authority.identity(),
    }


__all__ = [
    "ADJUSTMENT_POLICY",
    "OUTCOME_RECEIPT_SCHEMA",
    "OUTCOME_SCHEMA",
    "OUTCOME_SOURCE_ROLES_V3",
    "PRE_ENTRY_NONEXECUTION_POLICY",
    "PRE_ENTRY_NONEXECUTION_REASONS",
    "validate_and_recompute_outcomes_v3",
]
