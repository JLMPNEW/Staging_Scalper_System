from __future__ import annotations

import csv
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Mapping

from biotech_index.core.calibration_metrics import finite_float
from biotech_index.core.portfolio_profitability import (
    ReplayCostModel,
    ReplayResult,
    ReplayTarget,
    TerminalRecovery,
    compare_daily_replays,
    run_daily_portfolio_replay,
    summarize_daily_replay,
    target_allocations_equal,
)


@dataclass(frozen=True)
class ReplayVerificationSettings:
    benchmark_ticker: str
    effective_trials: int
    bootstrap_iterations: int = 500
    bootstrap_block_days: int = 20
    bootstrap_seed: int = 1729
    numeric_tolerance: float = 1e-6


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(path)
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _parse_date(raw: object, *, label: str) -> date:
    try:
        return date.fromisoformat(str(raw or "").strip())
    except ValueError as exc:
        raise ValueError(f"Invalid {label}: {raw!r}") from exc


def _parse_bool(raw: object) -> bool:
    return str(raw or "").strip().lower() in {"1", "true", "yes", "on"}


def _chain_results(results: list[tuple[str, ReplayResult]], model: ReplayCostModel) -> ReplayResult:
    if not results:
        raise ValueError("Cannot verify an empty replay")
    rows: list[dict[str, object]] = []
    trades: list[dict[str, object]] = []
    wealth = model.initial_capital
    dates: set[str] = set()
    for fold_id, result in results:
        for source in result.daily_rows:
            row = dict(source)
            row_date = str(row.get("date") or "")
            if row_date in dates:
                raise ValueError(f"Normalized replay folds overlap on {row_date}")
            dates.add(row_date)
            daily_return = finite_float(row.get("daily_net_return"))
            if daily_return is None:
                raise ValueError(f"Normalized replay row has no daily return: {row}")
            wealth *= 1.0 + daily_return
            row["fold_id"] = fold_id
            row["equity"] = wealth
            rows.append(row)
        trades.extend({"fold_id": fold_id, **dict(row)} for row in result.trade_rows)
    summary = summarize_daily_replay(
        rows,
        initial_capital=model.initial_capital,
        periods_per_year=model.periods_per_year,
    )
    for field in (
        "total_transaction_cost",
        "gross_traded_notional",
        "trade_count",
        "partial_fill_count",
        "missing_adv_trade_count",
        "missing_target_price_count",
        "target_expiry_rebalance_count",
    ):
        summary[field] = sum(finite_float(result.summary.get(field)) or 0.0 for _, result in results)
    total_cost = finite_float(summary.get("total_transaction_cost")) or 0.0
    gross_notional = finite_float(summary.get("gross_traded_notional")) or 0.0
    summary["total_transaction_cost_pct_initial"] = round(
        100.0 * total_cost / model.initial_capital,
        6,
    )
    summary["gross_turnover_multiple"] = round(
        gross_notional / model.initial_capital,
        6,
    )
    return ReplayResult(tuple(rows), tuple(trades), summary)


def load_normalized_replay_inputs(
    output_dir: Path,
) -> tuple[
    dict[str, dict[date, float]],
    dict[str, TerminalRecovery],
    dict[tuple[str, str], list[ReplayTarget]],
    list[dict[str, str]],
]:
    prices: dict[str, dict[date, float]] = defaultdict(dict)
    for row in _read_csv(output_dir / "portfolio_replay_price_inputs.csv"):
        ticker = str(row.get("ticker") or "").strip().upper()
        close = finite_float(row.get("close"))
        if not ticker or close is None or close <= 0.0:
            raise ValueError(f"Invalid normalized price row: {row}")
        prices[ticker][_parse_date(row.get("bar_date"), label="bar_date")] = close

    terminal_events: dict[str, TerminalRecovery] = {}
    terminal_path = output_dir / "portfolio_replay_terminal_events.csv"
    if terminal_path.exists():
        for row in _read_csv(terminal_path):
            ticker = str(row.get("ticker") or "").strip().upper()
            recovery = finite_float(row.get("equity_recovery"))
            if not ticker or recovery is None or recovery < 0.0:
                raise ValueError(f"Invalid normalized terminal-event row: {row}")
            terminal_events[ticker] = TerminalRecovery(
                terminal_date=_parse_date(row.get("terminal_date"), label="terminal_date"),
                equity_recovery=recovery,
                recovery_type=str(row.get("recovery_type") or "").strip(),
                drop_otc_tape=_parse_bool(row.get("drop_otc_tape")),
            )

    grouped: dict[tuple[str, str, date], dict[str, object]] = {}
    for row in _read_csv(output_dir / "portfolio_replay_targets.csv"):
        fold_id = str(row.get("fold_id") or "").strip()
        strategy = str(row.get("strategy") or "").strip().lower()
        signal_date = _parse_date(row.get("signal_date"), label="signal_date")
        ticker = str(row.get("ticker") or "").strip().upper()
        weight = finite_float(row.get("target_weight"))
        if not fold_id or strategy not in {"challenger", "production"} or not ticker or weight is None:
            raise ValueError(f"Invalid normalized target row: {row}")
        key = (fold_id, strategy, signal_date)
        payload = grouped.setdefault(key, {"weights": {}, "adv": {}})
        weights = payload["weights"]
        if not isinstance(weights, dict):
            raise TypeError("Normalized target weights must be a dictionary")
        weights[ticker] = float(weights.get(ticker, 0.0)) + weight
        adv = finite_float(row.get("avg_dollar_volume"))
        if adv is not None and adv > 0.0:
            adv_payload = payload["adv"]
            if not isinstance(adv_payload, dict):
                raise TypeError("Normalized target ADV must be a dictionary")
            adv_payload[ticker] = adv
    targets: dict[tuple[str, str], list[ReplayTarget]] = defaultdict(list)
    for (fold_id, strategy, signal_date), payload in sorted(grouped.items()):
        weights = payload["weights"]
        adv = payload["adv"]
        if not isinstance(weights, dict) or not isinstance(adv, dict):
            raise TypeError("Invalid normalized target payload")
        targets[(fold_id, strategy)].append(ReplayTarget(signal_date, dict(weights), dict(adv)))

    fold_rows = _read_csv(output_dir / "portfolio_replay_folds.csv")
    return dict(prices), terminal_events, dict(targets), fold_rows


def replay_normalized_artifacts(
    output_dir: Path,
    *,
    model: ReplayCostModel,
    settings: ReplayVerificationSettings,
) -> dict[str, object]:
    prices, terminal_events, targets, fold_rows = load_normalized_replay_inputs(output_dir)
    candidate_results: list[tuple[str, ReplayResult]] = []
    incumbent_results: list[tuple[str, ReplayResult]] = []
    independent_challenger_folds: list[str] = []
    incumbent_fallback_folds: list[str] = []
    for row in sorted(fold_rows, key=lambda value: str(value.get("fold_id") or "")):
        fold_id = str(row.get("fold_id") or "").strip()
        if not fold_id:
            raise ValueError("Normalized fold row has no fold_id")
        start = _parse_date(row.get("start_date"), label="start_date")
        end = _parse_date(row.get("end_date"), label="end_date")
        candidate_targets = targets.get((fold_id, "challenger"), ())
        incumbent_targets = targets.get((fold_id, "production"), ())
        if target_allocations_equal(candidate_targets, incumbent_targets):
            incumbent_fallback_folds.append(fold_id)
        else:
            independent_challenger_folds.append(fold_id)
        candidate = run_daily_portfolio_replay(
            prices,
            candidate_targets,
            benchmark_ticker=settings.benchmark_ticker,
            model=model,
            terminal_events=terminal_events,
            start_date=start,
            end_date=end,
        )
        incumbent = run_daily_portfolio_replay(
            prices,
            incumbent_targets,
            benchmark_ticker=settings.benchmark_ticker,
            model=model,
            terminal_events=terminal_events,
            start_date=start,
            end_date=end,
        )
        candidate_results.append((fold_id, candidate))
        incumbent_results.append((fold_id, incumbent))
    aggregate_candidate = _chain_results(candidate_results, model)
    aggregate_incumbent = _chain_results(incumbent_results, model)
    comparison = compare_daily_replays(
        aggregate_candidate,
        aggregate_incumbent,
        effective_trials=settings.effective_trials,
        bootstrap_iterations=settings.bootstrap_iterations,
        bootstrap_block_days=settings.bootstrap_block_days,
        bootstrap_seed=settings.bootstrap_seed,
        periods_per_year=model.periods_per_year,
    )
    if independent_challenger_folds and incumbent_fallback_folds:
        candidate_replay_type = "mixed_challenger_and_production_fallback"
    elif independent_challenger_folds:
        candidate_replay_type = "independent_challenger_all_folds"
    else:
        candidate_replay_type = "production_incumbent_fallback_only"
    comparison.update(
        {
            "candidate_replay_type": candidate_replay_type,
            "independent_challenger_fold_count": len(independent_challenger_folds),
            "production_fallback_fold_count": len(incumbent_fallback_folds),
            "independent_challenger_folds": "|".join(independent_challenger_folds),
            "production_fallback_folds": "|".join(incumbent_fallback_folds),
        }
    )
    return comparison


def compare_replay_payloads(
    expected: Mapping[str, object],
    actual: Mapping[str, object],
    *,
    tolerance: float = 1e-6,
) -> dict[str, object]:
    mismatches: list[str] = []
    for key in sorted(set(expected).union(actual)):
        left = expected.get(key)
        right = actual.get(key)
        left_number = finite_float(left)
        right_number = finite_float(right)
        if left_number is not None and right_number is not None:
            if not math.isclose(left_number, right_number, rel_tol=0.0, abs_tol=tolerance):
                mismatches.append(f"{key}:expected={left_number}:actual={right_number}")
        elif str(left) != str(right):
            mismatches.append(f"{key}:expected={left!r}:actual={right!r}")
    return {
        "verification_status": "pass" if not mismatches else "fail",
        "verified_field_count": len(set(expected).union(actual)),
        "mismatch_count": len(mismatches),
        "mismatches": mismatches,
    }

