from __future__ import annotations

from datetime import date, timedelta
from typing import Iterable, Mapping

from biotech_index.core.calibration_metrics import finite_float
from biotech_index.core.portfolio_governance import evaluate_champion_challenger_monitoring
from biotech_index.core.portfolio_profitability import (
    ReplayResult,
    compare_daily_replays,
    summarize_daily_replay,
)


def _result_from_returns(rows: Iterable[Mapping[str, object]], return_field: str) -> ReplayResult:
    equity = 1.0
    daily_rows: list[dict[str, object]] = []
    for row in sorted(rows, key=lambda value: str(value.get("date") or "")):
        value = finite_float(row.get(return_field))
        if value is None or value <= -1.0:
            continue
        equity *= 1.0 + value
        daily_rows.append(
            {
                "date": str(row.get("date") or ""),
                "daily_net_return": value,
                "equity": equity,
            }
        )
    summary = summarize_daily_replay(daily_rows, initial_capital=1.0)
    summary.update(
        {
            "total_transaction_cost": 0.0,
            "gross_traded_notional": 0.0,
            "trade_count": 0,
            "partial_fill_count": 0,
            "missing_adv_trade_count": 0,
            "missing_target_price_count": 0,
            "total_transaction_cost_pct_initial": 0.0,
            "gross_turnover_multiple": 0.0,
        }
    )
    return ReplayResult(tuple(daily_rows), (), summary)


def evaluate_live_monitoring_windows(
    rows: Iterable[Mapping[str, object]],
    *,
    asof_date: date,
    windows_days: Iterable[int],
    expected_contract_id: str,
    effective_trials: int,
    min_live_paired_days: int,
    max_drawdown_deterioration_pct: float,
    max_daily_cvar_deterioration_pct: float,
    bootstrap_iterations: int = 500,
    bootstrap_block_days: int = 20,
    bootstrap_seed: int = 1729,
) -> list[dict[str, object]]:
    materialized = [dict(row) for row in rows]
    output: list[dict[str, object]] = []
    for window in sorted({max(1, int(value)) for value in windows_days}):
        start = asof_date - timedelta(days=window - 1)
        window_rows = []
        policy_consistent = True
        for row in materialized:
            try:
                row_date = date.fromisoformat(str(row.get("date") or ""))
            except ValueError:
                continue
            if not start <= row_date <= asof_date:
                continue
            window_rows.append(row)
            policy_consistent = policy_consistent and str(row.get("contract_id") or "") == expected_contract_id
        if not window_rows:
            output.append(
                {
                    "window_days": window,
                    "window_start_date": start.isoformat(),
                    "window_end_date": asof_date.isoformat(),
                    "monitoring_status": "continue_shadow_observation",
                    "monitoring_action": "hold_provisional_weight",
                    "monitoring_reason_codes": "no_live_paired_rows",
                    "paired_daily_count": 0,
                    "policy_hash_consistent": policy_consistent,
                }
            )
            continue
        candidate = _result_from_returns(window_rows, "candidate_net_return")
        incumbent = _result_from_returns(window_rows, "incumbent_net_return")
        comparison = compare_daily_replays(
            candidate,
            incumbent,
            effective_trials=effective_trials,
            bootstrap_iterations=bootstrap_iterations,
            bootstrap_block_days=bootstrap_block_days,
            bootstrap_seed=bootstrap_seed,
        )
        decision = evaluate_champion_challenger_monitoring(
            comparison,
            min_live_paired_days=min_live_paired_days,
            max_drawdown_deterioration_pct=max_drawdown_deterioration_pct,
            max_daily_cvar_deterioration_pct=max_daily_cvar_deterioration_pct,
            policy_hash_consistent=policy_consistent,
        )
        output.append(
            {
                "window_days": window,
                "window_start_date": start.isoformat(),
                "window_end_date": asof_date.isoformat(),
                **decision,
            }
        )
    return output


def overall_monitoring_action(rows: Iterable[Mapping[str, object]]) -> tuple[str, str]:
    materialized = list(rows)
    if any(row.get("monitoring_status") == "rollback_to_champion" for row in materialized):
        return "rollback_to_champion", "xbi_residual_only"
    if any(row.get("monitoring_status") == "continue_shadow_observation" for row in materialized):
        return "continue_shadow_observation", "hold_provisional_weight"
    if any(row.get("monitoring_status") == "retain_provisional_weight" for row in materialized):
        return "retain_provisional_weight", "do_not_scale"
    return "eligible_to_scale", "advance_one_weight_stage"

