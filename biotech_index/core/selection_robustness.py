from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable, Mapping

from biotech_index.core.calibration_metrics import (
    MetricSettings,
    finite_float,
    paired_policy_comparison,
)


@dataclass(frozen=True)
class FoldSelectionPolicy:
    active_weight: float
    max_name_weight: float


def _fold_policies(
    comparison_rows: Iterable[Mapping[str, object]],
    *,
    horizon: int,
) -> dict[str, FoldSelectionPolicy]:
    output: dict[str, FoldSelectionPolicy] = {}
    for row in comparison_rows:
        if int(finite_float(row.get("horizon_days")) or 0) != horizon:
            continue
        fold_id = str(row.get("fold_id") or "").strip()
        if not fold_id:
            continue
        active_weight = finite_float(row.get("active_weight"))
        max_name_weight = finite_float(row.get("frozen_max_name_weight"))
        output[fold_id] = FoldSelectionPolicy(
            active_weight=max(0.0, min(1.0, active_weight if active_weight is not None else 0.0)),
            max_name_weight=max(
                1e-9,
                min(1.0, max_name_weight if max_name_weight is not None else 0.25),
            ),
        )
    return output


def _records_by_fold_date(
    selected_rows: Iterable[Mapping[str, object]],
    *,
    split: str,
    return_lookup: Mapping[tuple[str, str], float] | None,
    exclude_ticker: str = "",
) -> dict[tuple[str, str], list[tuple[str, float]]]:
    grouped: dict[tuple[str, str], list[tuple[str, float]]] = defaultdict(list)
    excluded = str(exclude_ticker).strip().upper()
    for row in selected_rows:
        if str(row.get("evaluation_split") or "") != split:
            continue
        fold_id = str(row.get("fold_id") or "").strip()
        asof_date = str(row.get("asof_date") or "").strip()
        ticker = str(row.get("ticker") or "").strip().upper()
        if not fold_id or not asof_date or not ticker or ticker == excluded:
            continue
        return_value = (
            finite_float(return_lookup.get((asof_date, ticker)))
            if return_lookup is not None
            else finite_float(row.get("objective_return"))
        )
        if return_value is not None:
            grouped[(fold_id, asof_date)].append((ticker, return_value))
    return grouped


def replay_selected_policy_returns(
    *,
    selected_rows: Iterable[Mapping[str, object]],
    sleeve_rows: Iterable[Mapping[str, object]],
    comparison_rows: Iterable[Mapping[str, object]],
    horizon: int,
    return_lookup: Mapping[tuple[str, str], float] | None = None,
    exclude_candidate_ticker: str = "",
) -> tuple[dict[str, float], dict[str, float], dict[tuple[str, str], float]]:
    """Reproduce candidate and actual-incumbent alpha on aligned fold dates."""
    policies = _fold_policies(comparison_rows, horizon=horizon)
    candidate = _records_by_fold_date(
        selected_rows,
        split="outer_test_candidate",
        return_lookup=return_lookup,
        exclude_ticker=exclude_candidate_ticker,
    )
    incumbent = _records_by_fold_date(
        selected_rows,
        split="outer_test_incumbent",
        return_lookup=return_lookup,
    )
    evaluation_keys = {
        (str(row.get("fold_id") or "").strip(), str(row.get("asof_date") or "").strip())
        for row in sleeve_rows
        if int(finite_float(row.get("horizon_days")) or 0) == horizon
        and str(row.get("fold_id") or "").strip()
        and str(row.get("asof_date") or "").strip()
    }
    candidate_returns: dict[str, float] = {}
    incumbent_returns: dict[str, float] = {}
    contributions: dict[tuple[str, str], float] = {}
    for fold_id, asof_date in sorted(evaluation_keys):
        if asof_date in candidate_returns:
            raise ValueError(f"Overlapping outer-test date across folds: {asof_date}")
        policy = policies.get(fold_id, FoldSelectionPolicy(0.0, 0.25))
        candidate_rows = candidate.get((fold_id, asof_date), [])
        candidate_count = len(candidate_rows)
        effective_weight = min(
            policy.active_weight,
            candidate_count * policy.max_name_weight,
        )
        candidate_returns[asof_date] = (
            effective_weight
            * sum(return_value for _ticker, return_value in candidate_rows)
            / candidate_count
            if candidate_count
            else 0.0
        )
        for ticker, return_value in candidate_rows:
            contributions[(asof_date, ticker)] = effective_weight * return_value / candidate_count

        incumbent_rows = incumbent.get((fold_id, asof_date), [])
        incumbent_returns[asof_date] = (
            sum(return_value for _ticker, return_value in incumbent_rows) / len(incumbent_rows)
            if incumbent_rows
            else 0.0
        )
    return candidate_returns, incumbent_returns, contributions


def ticker_jackknife_diagnostics(
    *,
    selected_rows: Iterable[Mapping[str, object]],
    sleeve_rows: Iterable[Mapping[str, object]],
    comparison_rows: Iterable[Mapping[str, object]],
    horizon: int,
    settings: MetricSettings,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    materialized = [dict(row) for row in selected_rows]
    candidate, incumbent, contributions = replay_selected_policy_returns(
        selected_rows=materialized,
        sleeve_rows=sleeve_rows,
        comparison_rows=comparison_rows,
        horizon=horizon,
    )
    baseline = paired_policy_comparison(candidate, incumbent, settings)
    tickers = sorted(
        {
            str(row.get("ticker") or "").strip().upper()
            for row in materialized
            if str(row.get("evaluation_split") or "") == "outer_test_candidate"
            and str(row.get("ticker") or "").strip()
        }
    )
    contribution_by_ticker: dict[str, float] = defaultdict(float)
    appearance_by_ticker: dict[str, int] = defaultdict(int)
    for (_asof_date, ticker), contribution in contributions.items():
        contribution_by_ticker[ticker] += contribution
        appearance_by_ticker[ticker] += 1
    gross_positive = sum(max(value, 0.0) for value in contribution_by_ticker.values())
    gross_negative = sum(max(-value, 0.0) for value in contribution_by_ticker.values())

    rows: list[dict[str, object]] = []
    for ticker in tickers:
        leaveout_candidate, leaveout_incumbent, _ = replay_selected_policy_returns(
            selected_rows=materialized,
            sleeve_rows=sleeve_rows,
            comparison_rows=comparison_rows,
            horizon=horizon,
            exclude_candidate_ticker=ticker,
        )
        leaveout = paired_policy_comparison(leaveout_candidate, leaveout_incumbent, settings)
        contribution = contribution_by_ticker.get(ticker, 0.0)
        rows.append(
            {
                "horizon_days": horizon,
                "ticker": ticker,
                "selected_date_count": appearance_by_ticker.get(ticker, 0),
                "cumulative_candidate_return_contribution_pct": round(100.0 * contribution, 6),
                "positive_contribution_share_pct": (
                    round(100.0 * max(contribution, 0.0) / gross_positive, 6)
                    if gross_positive > 0.0
                    else 0.0
                ),
                "negative_contribution_share_pct": (
                    round(100.0 * max(-contribution, 0.0) / gross_negative, 6)
                    if gross_negative > 0.0
                    else 0.0
                ),
                "leave_one_out_paired_delta_mean_pct": leaveout.get("paired_delta_mean_pct", ""),
                "leave_one_out_paired_delta_bootstrap_lcb_pct": leaveout.get(
                    "paired_delta_bootstrap_lcb_pct",
                    "",
                ),
                "leave_one_out_candidate_profit_factor": leaveout.get("candidate_profit_factor", ""),
                "leave_one_out_paired_date_count": leaveout.get("paired_date_count", 0),
            }
        )
    largest_gain = max(
        rows,
        key=lambda row: finite_float(row.get("positive_contribution_share_pct")) or 0.0,
        default=None,
    )
    min_lcb_row = min(
        (
            row
            for row in rows
            if finite_float(row.get("leave_one_out_paired_delta_bootstrap_lcb_pct")) is not None
        ),
        key=lambda row: finite_float(
            row.get("leave_one_out_paired_delta_bootstrap_lcb_pct")
        )
        or 0.0,
        default=None,
    )
    summary: dict[str, object] = {
        **baseline,
        "ticker_count": len(tickers),
        "largest_positive_ticker_contribution_share_pct": (
            largest_gain["positive_contribution_share_pct"] if largest_gain else 0.0
        ),
        "largest_positive_contribution_ticker": largest_gain["ticker"] if largest_gain else "",
        "leave_one_out_min_paired_delta_lcb_pct": (
            min_lcb_row["leave_one_out_paired_delta_bootstrap_lcb_pct"] if min_lcb_row else ""
        ),
        "leave_one_out_worst_removed_ticker": min_lcb_row["ticker"] if min_lcb_row else "",
    }
    return summary, rows
