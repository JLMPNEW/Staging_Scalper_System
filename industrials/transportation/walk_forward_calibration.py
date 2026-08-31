from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from statistics import mean
from typing import Any

from industrials.transportation.financial_contract import MetricDefinition
from industrials.transportation.oos_outcomes import finite_float
from industrials.transportation.scoring import (
    COMPONENT_FIELD,
    OBSERVED_STATUSES,
    metric_percentiles,
)


CALIBRATION_VERSION = "transportation_bounded_walk_forward_calibration_v1"
TOP_BOTTOM_FRACTION = 0.20


@dataclass(frozen=True)
class Sleeve:
    top: tuple[tuple[Mapping[str, object], float], ...]
    bottom: tuple[tuple[Mapping[str, object], float], ...]


def _quantile(sorted_values: Sequence[float], fraction: float) -> float:
    if not sorted_values:
        raise ValueError("quantile requires at least one value")
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    position = (len(sorted_values) - 1) * fraction
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return float(sorted_values[lower])
    weight = position - lower
    return (
        float(sorted_values[lower]) * (1.0 - weight)
        + float(sorted_values[upper]) * weight
    )


def percentile_scores(
    values: Mapping[str, float],
    *,
    winsor_lower: float = 0.05,
    winsor_upper: float = 0.95,
) -> dict[str, float]:
    finite = {
        str(key): float(value)
        for key, value in values.items()
        if math.isfinite(float(value))
    }
    if not finite:
        return {}
    ordered = sorted(finite.values())
    low = _quantile(ordered, winsor_lower)
    high = _quantile(ordered, winsor_upper)
    clipped = {
        key: min(high, max(low, value))
        for key, value in finite.items()
    }
    if len(clipped) == 1:
        return {key: 50.0 for key in clipped}
    ordered_items = sorted(clipped.items(), key=lambda item: (item[1], item[0]))
    ranks: dict[str, float] = {}
    cursor = 0
    while cursor < len(ordered_items):
        end = cursor + 1
        while (
            end < len(ordered_items)
            and ordered_items[end][1] == ordered_items[cursor][1]
        ):
            end += 1
        average_rank = (cursor + end - 1) / 2.0
        for key, _value in ordered_items[cursor:end]:
            ranks[key] = average_rank
        cursor = end
    scale = 100.0 / (len(ordered_items) - 1)
    return {key: ranks[key] * scale for key in clipped}


def generic_baseline_scores(
    rows: Sequence[Mapping[str, str]],
    *,
    definitions: Sequence[MetricDefinition],
    component_weights: Mapping[str, float],
) -> dict[tuple[str, str], dict[str, float]]:
    generic_definitions = [
        definition for definition in definitions if not definition.specialized
    ]
    if not generic_definitions:
        raise ValueError("generic metric definition set is empty")
    if set(component_weights) != set(COMPONENT_FIELD):
        raise ValueError("component weights do not match scoring contract")
    if any(float(value) < 0 for value in component_weights.values()):
        raise ValueError("component weights must be non-negative")
    if not math.isclose(
        sum(float(value) for value in component_weights.values()),
        1.0,
        abs_tol=1e-9,
    ):
        raise ValueError("component weights must sum to one")

    member_by_date: dict[str, dict[str, dict[str, str]]] = defaultdict(dict)
    metric_rows_by_date: dict[
        str, dict[str, dict[str, dict[str, Any]]]
    ] = defaultdict(lambda: defaultdict(dict))
    for row in rows:
        asof = str(row.get("asof_date") or "")
        ticker = str(row.get("ticker") or "")
        metric_id = str(row.get("metric_id") or "")
        if not asof or not ticker or not metric_id:
            continue
        member_by_date[asof][ticker] = {
            "ticker": ticker,
            "calibration_cohort_id": str(
                row.get("calibration_cohort") or ""
            ),
            "industry": str(row.get("industry") or ""),
        }
        metric_rows_by_date[asof][ticker][metric_id] = {
            "availability_status": str(
                row.get("availability_status") or ""
            ),
            "metric_value": str(row.get("metric_value") or ""),
        }

    output: dict[tuple[str, str], dict[str, float]] = {}
    definitions_by_component: dict[str, list[MetricDefinition]] = defaultdict(
        list
    )
    for definition in generic_definitions:
        definitions_by_component[definition.component].append(definition)
    for asof in sorted(member_by_date):
        members = list(member_by_date[asof].values())
        metric_rows = metric_rows_by_date[asof]
        percentiles = metric_percentiles(
            members,
            generic_definitions,
            metric_rows,
        )
        for member in members:
            ticker = member["ticker"]
            cohort = member["calibration_cohort_id"]
            industry = member["industry"]
            ticker_rows = metric_rows.get(ticker, {})
            ticker_percentiles = percentiles.get(ticker, {})
            component_scores: dict[str, float] = {}
            observed_generic = 0
            for component, component_definitions in (
                definitions_by_component.items()
            ):
                scores: list[float] = []
                for definition in component_definitions:
                    if not definition.applies_to(
                        cohort=cohort,
                        industry=industry,
                    ):
                        continue
                    if definition.birthdate and asof < definition.birthdate:
                        continue
                    status = str(
                        ticker_rows.get(definition.metric_id, {}).get(
                            "availability_status"
                        )
                        or ""
                    )
                    if status not in OBSERVED_STATUSES:
                        continue
                    score = ticker_percentiles.get(definition.metric_id)
                    if score is not None:
                        scores.append(float(score))
                if scores:
                    component_scores[component] = sum(scores) / len(scores)
                    observed_generic += len(scores)
            weighted = [
                (score, float(component_weights[component]))
                for component, score in component_scores.items()
                if float(component_weights[component]) > 0
            ]
            total_weight = sum(weight for _, weight in weighted)
            if total_weight <= 0:
                continue
            final_score = (
                sum(score * weight for score, weight in weighted)
                / total_weight
            )
            output[(asof, ticker)] = {
                "baseline_score": max(0.0, min(100.0, final_score)),
                "baseline_component_count": float(len(weighted)),
                "baseline_generic_metric_count": float(observed_generic),
            }
    return output


def overlay_score(
    baseline_score: float,
    specialized_percentile: float,
    weight: float,
) -> float:
    if not 0.0 <= weight <= 1.0:
        raise ValueError("overlay weight must be within 0..1")
    return (
        (1.0 - weight) * float(baseline_score)
        + weight * float(specialized_percentile)
    )


def _average_ranks(values: Sequence[float]) -> list[float]:
    indexed = sorted(enumerate(values), key=lambda item: item[1])
    ranks = [0.0] * len(values)
    start = 0
    while start < len(indexed):
        end = start + 1
        while end < len(indexed) and indexed[end][1] == indexed[start][1]:
            end += 1
        average_rank = (start + end - 1) / 2.0
        for index in range(start, end):
            ranks[indexed[index][0]] = average_rank
        start = end
    return ranks


def spearman(
    scores: Sequence[float],
    outcomes: Sequence[float],
) -> float | None:
    if len(scores) != len(outcomes) or len(scores) < 3:
        return None
    score_ranks = _average_ranks(scores)
    outcome_ranks = _average_ranks(outcomes)
    score_mean = mean(score_ranks)
    outcome_mean = mean(outcome_ranks)
    numerator = sum(
        (left - score_mean) * (right - outcome_mean)
        for left, right in zip(score_ranks, outcome_ranks)
    )
    left_scale = math.sqrt(
        sum((value - score_mean) ** 2 for value in score_ranks)
    )
    right_scale = math.sqrt(
        sum((value - outcome_mean) ** 2 for value in outcome_ranks)
    )
    if left_scale <= 0 or right_scale <= 0:
        return None
    value = numerator / (left_scale * right_scale)
    return value if math.isfinite(value) else None


def ranked_sleeves(
    rows_and_scores: Sequence[tuple[Mapping[str, object], float]],
    *,
    fraction: float = TOP_BOTTOM_FRACTION,
) -> Sleeve | None:
    if len(rows_and_scores) < 3:
        return None
    if not 0.0 < fraction <= 0.5:
        raise ValueError("sleeve fraction must be within (0, 0.5]")
    count = min(
        len(rows_and_scores) // 2,
        max(1, int(math.ceil(len(rows_and_scores) * fraction))),
    )
    if count < 1:
        return None
    descending = sorted(
        rows_and_scores,
        key=lambda item: (
            -float(item[1]),
            str(item[0].get("ticker") or ""),
        ),
    )
    ascending = sorted(
        rows_and_scores,
        key=lambda item: (
            float(item[1]),
            str(item[0].get("ticker") or ""),
        ),
    )
    top = tuple(descending[:count])
    top_tickers = {
        str(row.get("ticker") or "") for row, _ in top
    }
    bottom = tuple(
        item
        for item in ascending
        if str(item[0].get("ticker") or "") not in top_tickers
    )[:count]
    if len(bottom) != count:
        return None
    return Sleeve(top=top, bottom=bottom)


def equal_weights(
    sleeve: Sequence[tuple[Mapping[str, object], float]],
) -> dict[str, float]:
    if not sleeve:
        return {}
    weight = 1.0 / len(sleeve)
    return {
        str(row.get("ticker") or ""): weight for row, _ in sleeve
    }


def turnover(
    current: Mapping[str, float],
    previous: Mapping[str, float] | None,
) -> tuple[float, float]:
    prior = previous or {}
    traded_notional = sum(
        abs(current.get(ticker, 0.0) - prior.get(ticker, 0.0))
        for ticker in set(current) | set(prior)
    )
    one_way = traded_notional if previous is None else traded_notional / 2.0
    return one_way, traded_notional


def purged_expanding_walk_forward_blocks(
    rows: Sequence[Mapping[str, object]],
    *,
    horizon_sessions: int,
    block_count: int = 4,
    minimum_initial_dates: int = 52,
) -> list[dict[str, tuple[str, ...] | str | int]]:
    """Build expanding walk-forward blocks with outcome-window purging.

    A training signal is admissible only when its realized benchmark exit
    precedes the first test signal. This uses the actual stored outcome window
    rather than approximating a trading-session horizon with calendar days.
    """
    if block_count < 1:
        raise ValueError("block_count must be positive")
    exits_by_date: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        try:
            row_horizon = int(float(str(row.get("horizon_sessions") or 0)))
        except ValueError:
            continue
        if row_horizon != horizon_sessions:
            continue
        if str(row.get("calibration_eligible_flag") or "") != "1":
            continue
        if str(row.get("outcome_available_flag") or "") != "1":
            continue
        asof = str(row.get("asof_date") or "")[:10]
        exit_date = str(row.get("benchmark_exit_date") or "")[:10]
        if not asof or not exit_date:
            continue
        date.fromisoformat(asof)
        date.fromisoformat(exit_date)
        exits_by_date[asof].add(exit_date)
    dates = sorted(exits_by_date)
    initial = max(minimum_initial_dates, int(len(dates) * 0.50))
    if initial >= len(dates):
        return []
    remaining = dates[initial:]
    block_size = max(1, len(remaining) // block_count)
    output: list[dict[str, tuple[str, ...] | str | int]] = []
    for block_index in range(block_count):
        start_index = initial + block_index * block_size
        end_index = (
            len(dates)
            if block_index == block_count - 1
            else min(len(dates), start_index + block_size)
        )
        evaluation_dates = tuple(dates[start_index:end_index])
        if not evaluation_dates:
            continue
        test_start = evaluation_dates[0]
        raw_training_dates = dates[:start_index]
        training_dates = tuple(
            signal_date
            for signal_date in raw_training_dates
            if max(exits_by_date[signal_date]) < test_start
        )
        embargo_dates = tuple(
            signal_date
            for signal_date in raw_training_dates
            if signal_date not in set(training_dates)
        )
        if not training_dates:
            continue
        output.append(
            {
                "block": block_index + 1,
                "training_dates": training_dates,
                "embargo_dates": embargo_dates,
                "evaluation_dates": evaluation_dates,
                "train_start": training_dates[0],
                "train_end": training_dates[-1],
                "test_start": test_start,
                "test_end": evaluation_dates[-1],
            }
        )
    return output


def aggregate_period_rows(
    rows: Sequence[Mapping[str, object]],
) -> dict[str, float | int | None]:
    def values(field: str) -> list[float]:
        return [
            value
            for row in rows
            if (value := finite_float(row.get(field))) is not None
        ]

    def average(field: str) -> float | None:
        members = values(field)
        return sum(members) / len(members) if members else None

    return {
        "period_count": len(rows),
        "row_count": sum(
            int(str(row.get("cross_section_count") or "0")) for row in rows
        ),
        "mean_rank_ic": average("rank_ic"),
        "mean_top_excess_return": average("top_mean_excess_return"),
        "mean_bottom_excess_return": average(
            "bottom_mean_excess_return"
        ),
        "mean_gross_top_bottom_spread": average(
            "gross_top_bottom_spread"
        ),
        "average_top_one_way_turnover": average(
            "top_one_way_turnover"
        ),
        "average_bottom_one_way_turnover": average(
            "bottom_one_way_turnover"
        ),
        "mean_base_transaction_cost": average(
            "base_transaction_cost"
        ),
        "mean_stress_transaction_cost": average(
            "stress_transaction_cost"
        ),
        "mean_net_top_bottom_spread_base": average(
            "net_top_bottom_spread_base"
        ),
        "mean_net_top_bottom_spread_stress": average(
            "net_top_bottom_spread_stress"
        ),
    }
