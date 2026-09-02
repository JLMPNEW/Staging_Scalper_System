from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Mapping

from biotech_index.core.calibration_metrics import (
    MetricSettings,
    equal_weight_returns_by_date,
    finite_float,
    paired_policy_comparison,
    quantile,
    summarize_returns,
)


@dataclass(frozen=True)
class ReliabilityRecord:
    asof_date: str
    ticker: str
    score: float
    return_value: float
    cohort: str = "ALL"


@dataclass(frozen=True)
class ReliabilityThreshold:
    min_score_pct_of_top: float
    max_names: int
    reliability_class: str
    active_weight: float
    validation_objective: float
    validation_metrics: Mapping[str, object]
    max_name_weight: float = 0.25

    def __post_init__(self) -> None:
        if not 0.0 < self.max_name_weight <= 1.0:
            raise ValueError("max_name_weight must be within (0, 1]")

    def as_dict(self) -> dict[str, object]:
        return {
            "min_score_pct_of_top": self.min_score_pct_of_top,
            "max_names": self.max_names,
            "reliability_class": self.reliability_class,
            "active_weight": self.active_weight,
            "max_name_weight": self.max_name_weight,
            "validation_objective": self.validation_objective,
            **dict(self.validation_metrics),
        }


def records_from_rows(
    rows: Iterable[Mapping[str, object]],
    *,
    score_key: str,
    return_key: str,
    cohort_key: str = "biotech_primary_cohort",
) -> list[ReliabilityRecord]:
    records: list[ReliabilityRecord] = []
    for row in rows:
        asof_date = str(row.get("asof_date") or "").strip()
        ticker = str(row.get("ticker") or "").strip().upper()
        score = finite_float(row.get(score_key))
        return_value = finite_float(row.get(return_key))
        if not asof_date or not ticker or score is None or return_value is None:
            continue
        records.append(
            ReliabilityRecord(
                asof_date=asof_date,
                ticker=ticker,
                score=score,
                return_value=return_value,
                cohort=str(row.get(cohort_key) or "ALL").strip() or "ALL",
            )
        )
    return records


def effective_active_weight_by_date(
    evaluation_dates: Iterable[str],
    selected_counts: Mapping[str, int],
    *,
    active_weight: float,
    max_name_weight: float,
) -> dict[str, float]:
    """Cap cohort active exposure so sparse selections cannot create oversized names."""
    requested = max(0.0, min(1.0, float(active_weight)))
    name_cap = max(0.0, min(1.0, float(max_name_weight)))
    return {
        asof_date: min(requested, max(0, int(selected_counts.get(asof_date, 0))) * name_cap)
        for asof_date in sorted({str(value).strip() for value in evaluation_dates if str(value).strip()})
    }


def blend_active_alpha_with_benchmark(
    active_alpha_returns: Mapping[str, float],
    evaluation_dates: Iterable[str],
    *,
    active_weight: float,
    selected_counts: Mapping[str, int] | None = None,
    max_name_weight: float = 1.0,
) -> dict[str, float]:
    """Return full-sleeve XBI-relative alpha with optional per-name concentration caps."""
    dates = sorted({str(value).strip() for value in evaluation_dates if str(value).strip()})
    counts = selected_counts or {
        asof_date: 1 if finite_float(active_alpha_returns.get(asof_date)) is not None else 0
        for asof_date in dates
    }
    weights = effective_active_weight_by_date(
        dates,
        counts,
        active_weight=active_weight,
        max_name_weight=max_name_weight,
    )
    output: dict[str, float] = {}
    for asof_date in dates:
        active_return = finite_float(active_alpha_returns.get(asof_date))
        output[asof_date] = weights[asof_date] * (0.0 if active_return is None else active_return)
    return output


def build_reliability_curve(
    records: Iterable[ReliabilityRecord],
    *,
    bins: int,
    settings: MetricSettings,
) -> list[dict[str, object]]:
    clean = sorted(records, key=lambda item: (item.score, item.asof_date, item.ticker))
    if not clean:
        return []
    bin_count = max(2, min(int(bins), len(clean)))
    output: list[dict[str, object]] = []
    for bin_index in range(bin_count):
        start = math.floor(bin_index * len(clean) / bin_count)
        end = math.floor((bin_index + 1) * len(clean) / bin_count)
        bucket = clean[start:end]
        if not bucket:
            continue
        date_returns = equal_weight_returns_by_date(
            [
                {"asof_date": item.asof_date, "return_value": item.return_value}
                for item in bucket
            ],
            return_key="return_value",
        )
        output.append(
            {
                "bin": bin_index + 1,
                "bin_count": bin_count,
                "score_min": min(item.score for item in bucket),
                "score_max": max(item.score for item in bucket),
                "score_median": quantile([item.score for item in bucket], 0.5),
                "observation_count": len(bucket),
                "date_count": len(date_returns),
                **summarize_returns(date_returns.values(), settings),
            }
        )
    return output


def apply_reliability_threshold(
    records: Iterable[ReliabilityRecord],
    *,
    min_score_pct_of_top: float,
    max_names: int,
) -> tuple[list[ReliabilityRecord], dict[str, float], dict[str, int]]:
    grouped: dict[str, list[ReliabilityRecord]] = {}
    for record in records:
        grouped.setdefault(record.asof_date, []).append(record)
    selected: list[ReliabilityRecord] = []
    counts: dict[str, int] = {}
    for asof_date, date_records in sorted(grouped.items()):
        ranked = sorted(date_records, key=lambda item: (-item.score, item.ticker))
        top_score = ranked[0].score if ranked else 0.0
        floor = top_score * max(0.0, min(100.0, min_score_pct_of_top)) / 100.0
        date_selected = [item for item in ranked if item.score >= floor]
        if max_names > 0:
            date_selected = date_selected[:max_names]
        selected.extend(date_selected)
        counts[asof_date] = len(date_selected)
    returns = equal_weight_returns_by_date(
        [
            {"asof_date": item.asof_date, "return_value": item.return_value}
            for item in selected
        ],
        return_key="return_value",
    )
    return selected, returns, counts


def reliability_class_from_metrics(metrics: Mapping[str, object]) -> str:
    delta_lcb = finite_float(metrics.get("paired_delta_bootstrap_lcb_pct"))
    profit = finite_float(metrics.get("candidate_profit_factor"))
    robust_profit = finite_float(metrics.get("candidate_profit_factor_ex_largest_winner"))
    if (
        delta_lcb is not None
        and delta_lcb > 0.0
        and profit is not None
        and profit >= 1.15
        and robust_profit is not None
        and robust_profit >= 1.0
    ):
        return "high"
    if (
        delta_lcb is not None
        and delta_lcb > 0.0
        and profit is not None
        and profit >= 1.0
        and (robust_profit is None or robust_profit >= 0.9)
    ):
        return "medium"
    return "low"


def active_weight_for_class(reliability_class: str, bounds: Mapping[str, object]) -> float:
    defaults = {"high": 0.90, "medium": 0.55, "low": 0.20}
    raw = finite_float(bounds.get(reliability_class))
    return max(0.0, min(1.0, defaults[reliability_class] if raw is None else raw))


def select_reliability_threshold(
    records: Iterable[ReliabilityRecord],
    incumbent_returns: Mapping[str, float],
    *,
    score_pct_candidates: Iterable[float],
    max_name_candidates: Iterable[int],
    settings: MetricSettings,
    active_weight_by_class: Mapping[str, object],
    min_dates: int,
    max_name_weight_candidates: Iterable[float] = (0.25,),
) -> ReliabilityThreshold | None:
    clean = list(records)
    best: ReliabilityThreshold | None = None
    weight_caps = sorted(
        {
            max(1e-9, min(1.0, float(value)))
            for value in max_name_weight_candidates
            if math.isfinite(float(value))
        }
    )
    if not weight_caps:
        raise ValueError("Expected at least one finite max_name_weight candidate")
    for score_pct in sorted({float(value) for value in score_pct_candidates}):
        for max_names in sorted({max(1, int(value)) for value in max_name_candidates}):
            _selected, active_returns, counts = apply_reliability_threshold(
                clean,
                min_score_pct_of_top=score_pct,
                max_names=max_names,
            )
            active_metrics = paired_policy_comparison(active_returns, incumbent_returns, settings)
            reliability_class = reliability_class_from_metrics(active_metrics)
            active_weight = active_weight_for_class(reliability_class, active_weight_by_class)
            for max_name_weight in weight_caps:
                effective_weights = effective_active_weight_by_date(
                    incumbent_returns,
                    counts,
                    active_weight=active_weight,
                    max_name_weight=max_name_weight,
                )
                sleeve_returns = blend_active_alpha_with_benchmark(
                    active_returns,
                    incumbent_returns,
                    active_weight=active_weight,
                    selected_counts=counts,
                    max_name_weight=max_name_weight,
                )
                metrics = paired_policy_comparison(sleeve_returns, incumbent_returns, settings)
                paired_dates = int(finite_float(metrics.get("paired_date_count")) or 0)
                if paired_dates < min_dates:
                    continue
                raw_delta_lcb = finite_float(metrics.get("paired_delta_bootstrap_lcb_pct"))
                raw_pf = finite_float(metrics.get("candidate_profit_factor"))
                raw_robust_pf = finite_float(metrics.get("candidate_profit_factor_ex_largest_winner"))
                raw_loss20 = finite_float(metrics.get("candidate_loss20_rate_pct"))
                delta_lcb = -1e9 if raw_delta_lcb is None else raw_delta_lcb
                pf = 0.0 if raw_pf is None else raw_pf
                robust_pf = 0.0 if raw_robust_pf is None else raw_robust_pf
                loss20 = 100.0 if raw_loss20 is None else raw_loss20
                avg_names = sum(counts.values()) / len(counts) if counts else 0.0
                avg_effective_weight = (
                    sum(effective_weights.values()) / len(effective_weights)
                    if effective_weights
                    else 0.0
                )
                max_single_name_weight = min(
                    max_name_weight,
                    max(effective_weights.values(), default=0.0),
                )
                objective = (
                    delta_lcb
                    + 2.0 * (pf - 1.0)
                    + (robust_pf - 1.0)
                    - 0.02 * loss20
                )
                threshold = ReliabilityThreshold(
                    min_score_pct_of_top=score_pct,
                    max_names=max_names,
                    reliability_class=reliability_class,
                    active_weight=active_weight,
                    validation_objective=objective,
                    validation_metrics={
                        **metrics,
                        "candidate_return_contract": "active_stock_alpha_plus_xbi_residual_capped",
                        "active_selection_paired_delta_bootstrap_lcb_pct": active_metrics.get(
                            "paired_delta_bootstrap_lcb_pct", ""
                        ),
                        "active_selection_profit_factor": active_metrics.get("candidate_profit_factor", ""),
                        "avg_selected_names": round(avg_names, 6),
                        "avg_effective_active_weight": round(avg_effective_weight, 6),
                        "max_single_name_weight": round(max_single_name_weight, 6),
                        "active_date_count": len([count for count in counts.values() if count > 0]),
                        "evaluation_date_count": len(sleeve_returns),
                    },
                    max_name_weight=max_name_weight,
                )
                if best is None or threshold.validation_objective > best.validation_objective:
                    best = threshold
    return best
