from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from statistics import median
from typing import Iterable, Mapping


CALIBRATION_CONTRACT_VERSION = (
    "transportation_walk_forward_calibration_contract_v1"
)
FLAG_METRICS = ("going_concern_flag", "pre_revenue_flag")


@dataclass(frozen=True)
class FlagHistory:
    metric_id: str
    value_row_count: int
    ticker_count: int
    median_period_count: float
    observed_values: tuple[float, ...]


def summarize_flag_history(
    rows: Iterable[Mapping[str, str]],
) -> dict[str, FlagHistory]:
    periods: dict[str, dict[str, set[str]]] = {
        metric_id: defaultdict(set) for metric_id in FLAG_METRICS
    }
    values: dict[str, set[float]] = {
        metric_id: set() for metric_id in FLAG_METRICS
    }
    value_counts = {metric_id: 0 for metric_id in FLAG_METRICS}
    for row in rows:
        metric_id = str(row.get("metric_id") or "")
        if metric_id not in periods:
            continue
        raw_value = str(row.get("metric_value") or "").strip()
        if not raw_value:
            continue
        try:
            value = float(raw_value)
        except ValueError:
            continue
        if not math.isfinite(value):
            continue
        ticker = str(row.get("ticker") or "").upper()
        period_end = str(row.get("period_end") or "")[:10]
        value_counts[metric_id] += 1
        values[metric_id].add(value)
        if ticker and period_end:
            periods[metric_id][ticker].add(period_end)
    output: dict[str, FlagHistory] = {}
    for metric_id in FLAG_METRICS:
        counts = [
            len(ticker_periods)
            for ticker_periods in periods[metric_id].values()
            if ticker_periods
        ]
        output[metric_id] = FlagHistory(
            metric_id=metric_id,
            value_row_count=value_counts[metric_id],
            ticker_count=len(periods[metric_id]),
            median_period_count=(
                float(median(counts)) if counts else 0.0
            ),
            observed_values=tuple(sorted(values[metric_id])),
        )
    return output


def flag_exception_decision(
    disposition: Mapping[str, str],
    history: FlagHistory,
) -> tuple[bool, str]:
    failures: list[str] = []
    if str(disposition.get("accepted_breadth_gate_pass") or "") != "1":
        failures.append("accepted_issuer_breadth_gate_failed")
    if str(disposition.get("evidence_precision_gate_pass") or "") != "1":
        failures.append("evidence_precision_gate_failed")
    if history.value_row_count == 0 or history.ticker_count == 0:
        failures.append("no_frozen_pit_flag_values")
    elif history.median_period_count < 2.0:
        failures.append("median_flag_history_below_two_periods")
    if set(history.observed_values) != {0.0, 1.0}:
        failures.append("binary_outcome_variation_not_observed")
    if failures:
        return False, ";".join(failures)
    return True, "flag_specific_two_period_depth_exception_pass"


def purged_split_calendar(
    snapshot_dates: list[str],
    *,
    forward_trading_days: int,
    embargo_days: int,
    train_fraction: float = 0.60,
    validation_fraction: float = 0.20,
) -> dict[str, str]:
    dates = sorted(set(snapshot_dates))
    if len(dates) < 3:
        return {value: "insufficient_history" for value in dates}
    train_cut = max(1, int(math.floor(len(dates) * train_fraction)))
    validation_cut = max(
        train_cut + 1,
        int(
            math.floor(
                len(dates) * (train_fraction + validation_fraction)
            )
        ),
    )
    validation_cut = min(validation_cut, len(dates) - 1)
    base: dict[str, str] = {}
    for index, snapshot_date in enumerate(dates):
        if index < train_cut:
            base[snapshot_date] = "train"
        elif index < validation_cut:
            base[snapshot_date] = "validation"
        else:
            base[snapshot_date] = "holdout"
    window_days = (
        int(math.ceil(max(0, forward_trading_days) * 7.0 / 5.0))
        + max(0, embargo_days)
    )
    first_of: dict[str, date] = {}
    for snapshot_date, split_name in base.items():
        parsed = date.fromisoformat(snapshot_date)
        prior = first_of.get(split_name)
        if prior is None or parsed < prior:
            first_of[split_name] = parsed
    next_split = {"train": "validation", "validation": "holdout"}
    output: dict[str, str] = {}
    for snapshot_date, split_name in base.items():
        following = next_split.get(split_name)
        parsed = date.fromisoformat(snapshot_date)
        if (
            following
            and following in first_of
            and (first_of[following] - parsed).days <= window_days
        ):
            output[snapshot_date] = "embargo"
        else:
            output[snapshot_date] = split_name
    return output
