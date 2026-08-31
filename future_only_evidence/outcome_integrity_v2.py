"""Exact-execution return recomputation with lifecycle and membership controls."""

from __future__ import annotations

import csv
import json
import math
from pathlib import Path
from typing import Any, Mapping, Sequence

from .outcome_integrity import OUTCOME_SOURCE_ROLES
from .protocol import canonical_sha256, file_sha256
from .trusted_receipts import PinnedEd25519Authority


ADJUSTMENT_POLICY = "split_dividend_terminal_total_return_v1"


def _rows(path: Path) -> list[dict[str, str]]:
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _bar_index(path: Path) -> dict[tuple[str, str], float]:
    rows = _rows(path)
    required = {"ticker", "execution_at_utc", "total_return_index", "adjustment_policy_id"}
    if not rows or not required <= set(rows[0]):
        raise ValueError("execution-time total-return bars have an invalid schema")
    result: dict[tuple[str, str], float] = {}
    for row in rows:
        if row["adjustment_policy_id"] != ADJUSTMENT_POLICY:
            raise ValueError("total-return bar adjustment policy changed")
        key = (row["ticker"].strip().upper(), row["execution_at_utc"])
        value = float(row["total_return_index"])
        if key in result or not math.isfinite(value) or value <= 0:
            raise ValueError("total-return bars contain duplicate/invalid execution values")
        result[key] = value
    return result


def _membership_index(path: Path) -> dict[tuple[str, str, str], dict[str, str]]:
    rows = _rows(path)
    required = {
        "capture_id",
        "ticker",
        "entry_execution_at_utc",
        "sleeve_id",
        "group_id",
        "eligible_at_entry_flag",
        "lifecycle_status_at_entry",
    }
    if not rows or not required <= set(rows[0]):
        raise ValueError("membership history has an invalid schema")
    result: dict[tuple[str, str, str], dict[str, str]] = {}
    for row in rows:
        key = (
            row["capture_id"],
            row["ticker"].strip().upper(),
            row["entry_execution_at_utc"],
        )
        if key in result:
            raise ValueError("membership history contains duplicate capture/ticker/entry")
        result[key] = row
    return result


def _terminal_index(path: Path) -> dict[str, dict[str, str]]:
    rows = _rows(path)
    if not rows:
        return {}
    required = {
        "ticker",
        "terminal_event_date",
        "terminal_execution_at_utc",
        "terminal_event_status",
        "terminal_total_return_index_includes_proceeds_flag",
        "cash_carry_return_after_terminal",
        "adjustment_policy_id",
    }
    if not required <= set(rows[0]):
        raise ValueError("terminal-event source has an invalid schema")
    result: dict[str, dict[str, str]] = {}
    for row in rows:
        ticker = row["ticker"].strip().upper()
        if ticker in result:
            raise ValueError("terminal-event source contains multiple terminal events per ticker")
        if row["adjustment_policy_id"] != ADJUSTMENT_POLICY:
            raise ValueError("terminal-event adjustment policy changed")
        result[ticker] = row
    return result


def validate_and_recompute_outcomes_v2(
    *,
    family: str,
    capture_paths: Sequence[Path],
    outcome_path: Path,
    outcome_source_paths: Mapping[str, Path],
    outcome_receipt_path: Path,
    expected_outcome_receipt_sha256: str,
    authority: PinnedEd25519Authority,
    benchmark_ticker: str,
) -> dict[str, Any]:
    if set(outcome_source_paths) != OUTCOME_SOURCE_ROLES:
        raise ValueError("outcome source roles do not exactly match the contract")
    source_hashes = {role: file_sha256(path) for role, path in sorted(outcome_source_paths.items())}
    payload = json.loads(Path(outcome_path).read_text(encoding="utf-8"))
    rows = payload.get("rows")
    if not isinstance(rows, list) or payload.get("rows_sha256") != canonical_sha256(rows):
        raise ValueError("outcome rows are missing or hash-tampered")
    if payload.get("source_sha256") != source_hashes:
        raise ValueError("outcome payload does not bind exact raw source bytes")
    receipt = json.loads(Path(outcome_receipt_path).read_text(encoding="utf-8"))
    if file_sha256(outcome_receipt_path) != expected_outcome_receipt_sha256:
        raise ValueError("outcome receipt SHA-256 mismatch")
    authority.verify(outcome_receipt_path, expected_outcome_receipt_sha256, receipt)
    if (
        receipt.get("schema_version") != "future_outcome_receipt_v1"
        or receipt.get("family") != family
        or receipt.get("outcome_rows_sha256") != payload.get("rows_sha256")
        or receipt.get("source_sha256") != source_hashes
    ):
        raise ValueError("signed outcome receipt identity mismatch")
    captures = {
        str(capture["capture_id"]): capture
        for capture in (
            json.loads(Path(path).read_text(encoding="utf-8"))
            for path in capture_paths
        )
    }
    if sorted(receipt.get("capture_ids") or []) != sorted(captures):
        raise ValueError("signed outcome receipt capture census mismatch")
    bars = _bar_index(outcome_source_paths["total_return_bars"])
    memberships = _membership_index(outcome_source_paths["membership_history"])
    terminals = _terminal_index(outcome_source_paths["terminal_events"])
    expected_memberships: set[tuple[str, str, str]] = set()
    benchmark = benchmark_ticker.strip().upper()
    benchmark_by_period: dict[str, float] = {}
    recomputed = 0
    for row in rows:
        capture_id = str(row["capture_id"])
        capture = captures.get(capture_id)
        if capture is None:
            raise ValueError("outcome references unbound capture")
        ticker = str(row["ticker"]).strip().upper()
        entry_execution = str(row["entry_execution_at_utc"])
        common_exit_execution = str(row["exit_execution_at_utc"])
        membership_key = (capture_id, ticker, entry_execution)
        expected_memberships.add(membership_key)
        membership = memberships.get(membership_key)
        if membership is None:
            raise ValueError(f"{ticker}: exact entry membership is absent")
        signal = next(
            (
                signal_row
                for signal_row in capture["signal_rows"]
                if signal_row["ticker"] == ticker and int(signal_row["eligible_flag"]) == 1
            ),
            None,
        )
        if signal is None:
            raise ValueError(f"{ticker}: membership outcome has no eligible captured signal")
        if (
            membership["eligible_at_entry_flag"] != "1"
            or membership["sleeve_id"] != signal["sleeve_id"]
            or membership["group_id"] != signal["group_id"]
            or membership["lifecycle_status_at_entry"] not in {"active", "active_terminal_event_later"}
        ):
            raise ValueError(f"{ticker}: raw entry membership/lifecycle differs from capture")
        try:
            entry_index = bars[(ticker, entry_execution)]
            benchmark_entry = bars[(benchmark, entry_execution)]
            benchmark_exit = bars[(benchmark, common_exit_execution)]
        except KeyError as exc:
            raise ValueError(f"{ticker}: missing exact execution total-return bar") from exc
        terminal_status = str(row.get("terminal_event_status") or "")
        terminal = terminals.get(ticker)
        if terminal_status == "none":
            if terminal is not None and terminal["terminal_execution_at_utc"] <= common_exit_execution:
                raise ValueError(f"{ticker}: terminal event was omitted from outcome")
            security_exit_execution = common_exit_execution
        else:
            if terminal is None or terminal["terminal_event_status"] != terminal_status:
                raise ValueError(f"{ticker}: governed terminal event is missing/mismatched")
            security_exit_execution = terminal["terminal_execution_at_utc"]
            if security_exit_execution > common_exit_execution:
                raise ValueError(f"{ticker}: terminal event occurs after common horizon exit")
            if (
                terminal["terminal_total_return_index_includes_proceeds_flag"] != "1"
                or float(terminal["cash_carry_return_after_terminal"]) != 0.0
                or row.get("terminal_cash_carry_policy") != "zero_return_cash_to_common_exit"
            ):
                raise ValueError(f"{ticker}: early terminal proceeds/cash carry are not governed")
            if str(row.get("security_exit_execution_at_utc") or "") != security_exit_execution:
                raise ValueError(f"{ticker}: submitted security exit differs from terminal source")
        try:
            security_exit_index = bars[(ticker, security_exit_execution)]
        except KeyError as exc:
            raise ValueError(f"{ticker}: terminal/common exit total-return index is missing") from exc
        stock_return = security_exit_index / entry_index - 1.0
        benchmark_return = benchmark_exit / benchmark_entry - 1.0
        model_return = stock_return - benchmark_return
        expected = (stock_return, benchmark_return, model_return)
        submitted = (
            float(row["stock_total_return"]),
            float(row["benchmark_total_return"]),
            float(row["gross_return"]),
        )
        if any(abs(left - right) > 1e-12 for left, right in zip(expected, submitted)):
            raise ValueError(f"{ticker}: submitted return differs from exact raw execution indexes")
        period_key = f"{capture_id}|{int(row['horizon_sessions'])}"
        previous = benchmark_by_period.setdefault(period_key, benchmark_return)
        if abs(previous - benchmark_return) > 1e-12:
            raise ValueError("benchmark return differs within one capture/horizon")
        recomputed += 1
    if set(memberships) != expected_memberships:
        raise ValueError("membership history census is not exact for submitted outcomes")
    return {
        "return_arithmetic_recomputed_pass": True,
        "exact_execution_price_convention": ADJUSTMENT_POLICY,
        "early_terminal_cash_carry_policy": "zero_return_cash_to_common_exit",
        "membership_history_exact_census_pass": True,
        "recomputed_row_count": recomputed,
        "benchmark_ticker": benchmark,
        "benchmark_by_period": benchmark_by_period,
        "outcome_receipt_sha256": file_sha256(outcome_receipt_path),
        "outcome_source_sha256": source_hashes,
        "trusted_authority": authority.identity(),
    }


__all__ = ["ADJUSTMENT_POLICY", "validate_and_recompute_outcomes_v2"]
