#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import itertools
import math
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from statistics import mean, median
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from med_devices.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from med_devices.core.fda_states import MANUAL_FDA_REVIEW_STATES  # noqa: E402
from med_devices.core.logging_utils import configure_utc_logging  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
HARD_EXCLUDED_CLASSIFICATIONS = {
    "manual_review_regulatory_risk",
    "avoid",
    "avoid_confirmed_regulatory_risk",
    "data_review_required",
}
HARD_EXCLUDED_REGULATORY_MODELS = set(MANUAL_FDA_REVIEW_STATES)
GATE_FIELDS = (
    "raw_composite_score",
    "cohort_percentile",
    "fundamental_quality_score",
    "fda_product_score",
    "reimbursement_score",
    "valuation_score",
    "technical_entry_score",
)
BASE_FIELDS = [
    "calibration_cohort",
    "parameter_set_id",
    "raw_score_min",
    "cohort_percentile_min",
    "fundamental_quality_min",
    "fda_product_min",
    "reimbursement_min",
    "valuation_min",
    "technical_entry_min",
    "value_trap_max",
    "train_end_asof",
    "effective_train_end_asof",
    "embargo_days",
    "validation_start_asof",
    "validation_end_asof",
    "objective_score",
    "pass_fail",
    "rejection_reason",
    "validation_cohort_obs_120d",
    "validation_cohort_unique_tickers_120d",
    "validation_selected_ticker_coverage_120d",
    "validation_improved_selected_ticker_rate_120d",
    "selected_tickers_validation",
]


@dataclass(frozen=True)
class PreparedRow:
    ticker: str
    asof_date: str
    static_allowed: bool
    raw_composite_score: float | None
    cohort_percentile: float | None
    fundamental_quality_score: float | None
    fda_product_score: float | None
    reimbursement_score: float | None
    valuation_score: float | None
    technical_entry_score: float | None
    value_trap_score: float | None
    returns_by_horizon: dict[int, float | None]


@dataclass(frozen=True)
class PreparedCohort:
    cohort: str
    rows: list[PreparedRow]
    base_mask: int
    train_mask: int
    validation_mask: int
    validation_all_mask: int
    return_masks: dict[int, int]
    field_threshold_masks: dict[str, dict[float, int]]
    value_trap_threshold_masks: dict[float, int]
    validation_all_metrics: dict[int, dict[str, Any]]
    validation_cohort_tickers: dict[int, set[str]]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Optimize conservative gate recommendations by calibration cohort.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--input-csv", type=Path, default=None)
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument(
        "--max-rows-per-cohort",
        type=int,
        default=0,
        help="Keep only the top N rows per cohort. Default 0 writes the full tested grid.",
    )
    return parser.parse_args()


def parse_float_list(raw: object, default: str) -> list[float]:
    text = str(raw if raw is not None else default)
    return [float(item.strip()) for item in text.split(",") if item.strip()]


def parse_int_list(raw: object, default: str) -> list[int]:
    text = str(raw if raw is not None else default)
    return [int(item.strip()) for item in text.split(",") if item.strip()]


def to_float(raw: object) -> float | None:
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def parse_date(raw: object) -> datetime | None:
    text = str(raw or "").strip()[:10]
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        return None


def is_auto_date(raw: object) -> bool:
    return str(raw or "").strip().lower() in {"", "auto", "latest", "latest_market_date", "auto_latest_complete"}


def available_asof_dates(rows: list[dict[str, str]]) -> list[str]:
    dates = {
        str(row.get("asof_date") or "").strip()[:10]
        for row in rows
        if parse_date(row.get("asof_date")) is not None
    }
    return sorted(dates)


def previous_asof(dates: list[str], target: str) -> str:
    prior = [item for item in dates if item < target]
    if not prior:
        raise ValueError(f"Cannot derive train_end_asof before validation_start_asof={target}")
    return prior[-1]


def resolve_calibration_dates(config: dict[str, Any], rows: list[dict[str, str]]) -> tuple[str, str, str]:
    dates = available_asof_dates(rows)
    if not dates:
        raise ValueError("Cannot resolve calibration windows: input rows have no valid asof_date values.")
    train_raw = cfg_get(config, "calibration.train_end_asof", "auto")
    validation_start_raw = cfg_get(config, "calibration.validation_start_asof", "auto")
    validation_end_raw = cfg_get(config, "calibration.validation_end_asof", "auto")
    validation_window_asofs = max(1, int(cfg_get(config, "calibration.validation_window_asofs", 26)))

    validation_end = dates[-1] if is_auto_date(validation_end_raw) else str(validation_end_raw).strip()[:10]
    eligible_dates = [item for item in dates if item <= validation_end]
    if not eligible_dates:
        raise ValueError(f"No calibration rows on or before validation_end_asof={validation_end}")
    if is_auto_date(validation_start_raw):
        validation_start = eligible_dates[max(0, len(eligible_dates) - validation_window_asofs)]
    else:
        validation_start = str(validation_start_raw).strip()[:10]
    train_end = previous_asof(dates, validation_start) if is_auto_date(train_raw) else str(train_raw).strip()[:10]

    for label, value in (
        ("train_end_asof", train_end),
        ("validation_start_asof", validation_start),
        ("validation_end_asof", validation_end),
    ):
        if parse_date(value) is None:
            raise ValueError(f"Invalid {label}: {value}")
    if not (train_end < validation_start <= validation_end):
        raise ValueError(
            "Invalid calibration window ordering: "
            f"train_end_asof={train_end}, validation_start_asof={validation_start}, validation_end_asof={validation_end}"
        )
    return train_end, validation_start, validation_end


def effective_train_end(train_end_asof: str, validation_start_asof: str, embargo_days: int) -> str:
    train_end = parse_date(train_end_asof)
    validation_start = parse_date(validation_start_asof)
    if train_end is None or validation_start is None or embargo_days <= 0:
        return train_end_asof
    embargo_end = validation_start - timedelta(days=embargo_days)
    return min(train_end, embargo_end).strftime("%Y-%m-%d")


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def metrics(values: list[float], tickers: list[str]) -> dict[str, Any]:
    if not values:
        return {
            "count": 0,
            "unique_tickers": 0,
            "mean": "",
            "median": "",
            "hit_rate": "",
            "loss_rate": "",
            "lcb": "",
            "worst_loss": "",
            "sortino": "",
            "profit_factor": "",
        }
    avg = mean(values)
    if len(values) == 1:
        lcb_value = values[0]
    else:
        variance = sum((value - avg) ** 2 for value in values) / (len(values) - 1)
        lcb_value = avg - 1.64 * math.sqrt(variance) / math.sqrt(len(values))
    downside = [value for value in values if value < 0]
    if downside:
        downside_dev = math.sqrt(sum(value * value for value in downside) / len(downside))
        sortino = avg / downside_dev if downside_dev > 1e-12 else 999.0
    else:
        sortino = 999.0 if avg > 0 else 0.0
    gains = sum(value for value in values if value > 0)
    losses = -sum(value for value in values if value < 0)
    profit_factor = 999.0 if losses <= 1e-12 and gains > 0 else (gains / losses if losses > 1e-12 else 0.0)
    return {
        "count": len(values),
        "unique_tickers": len({ticker for ticker in tickers if ticker}),
        "mean": f"{avg:.6f}",
        "median": f"{median(values):.6f}",
        "hit_rate": f"{sum(1 for value in values if value > 0) / len(values):.4f}",
        "loss_rate": f"{sum(1 for value in values if value < 0) / len(values):.4f}",
        "lcb": f"{lcb_value:.6f}",
        "worst_loss": f"{min(values):.6f}",
        "sortino": f"{sortino:.4f}",
        "profit_factor": f"{profit_factor:.4f}",
    }


def selected_values(rows: list[dict[str, str]], *, horizon: int) -> tuple[list[float], list[str]]:
    values: list[float] = []
    tickers: list[str] = []
    field = f"cohort_excess_return_{horizon}d"
    for row in rows:
        value = to_float(row.get(field))
        if value is None:
            continue
        values.append(value)
        tickers.append(str(row.get("ticker") or ""))
    return values, tickers


def unique_tickers_with_returns(rows: list[dict[str, str]], *, horizon: int) -> set[str]:
    out: set[str] = set()
    field = f"cohort_excess_return_{horizon}d"
    for row in rows:
        if to_float(row.get(field)) is not None:
            ticker = str(row.get("ticker") or "")
            if ticker:
                out.add(ticker)
    return out


def selected_ticker_improvement_rate(rows: list[dict[str, str]], *, horizon: int) -> float | None:
    grouped: dict[str, list[float]] = {}
    field = f"cohort_excess_return_{horizon}d"
    for row in rows:
        value = to_float(row.get(field))
        ticker = str(row.get("ticker") or "")
        if value is None or not ticker:
            continue
        grouped.setdefault(ticker, []).append(value)
    if not grouped:
        return None
    improved = sum(1 for values in grouped.values() if median(values) > 0)
    return improved / len(grouped)


def passes_static_exclusions(row: dict[str, str]) -> bool:
    classification = str(row.get("classification") or "").strip()
    regulatory_model = str(row.get("regulatory_model") or "").strip()
    return classification not in HARD_EXCLUDED_CLASSIFICATIONS and regulatory_model not in HARD_EXCLUDED_REGULATORY_MODELS


def passes_gates(
    row: dict[str, str],
    *,
    raw_score_min: float,
    cohort_percentile_min: float,
    fundamental_quality_min: float,
    fda_product_min: float,
    reimbursement_min: float,
    valuation_min: float,
    technical_entry_min: float,
    value_trap_max: float,
) -> bool:
    if not passes_static_exclusions(row):
        return False
    checks = [
        (row.get("raw_composite_score"), raw_score_min, ">="),
        (row.get("cohort_percentile"), cohort_percentile_min, ">="),
        (row.get("fundamental_quality_score"), fundamental_quality_min, ">="),
        (row.get("fda_product_score"), fda_product_min, ">="),
        (row.get("reimbursement_score"), reimbursement_min, ">="),
        (row.get("valuation_score"), valuation_min, ">="),
        (row.get("technical_entry_score"), technical_entry_min, ">="),
    ]
    for raw, threshold, op in checks:
        value = to_float(raw)
        if value is None:
            return False
        if op == ">=" and value < threshold:
            return False
    value_trap = to_float(row.get("value_trap_score"))
    if value_trap is None:
        return False
    if value_trap > value_trap_max:
        return False
    return True


def iter_mask_indices(mask: int) -> Any:
    while mask:
        lowest_bit = mask & -mask
        yield lowest_bit.bit_length() - 1
        mask ^= lowest_bit


def mask_values(prepared: PreparedCohort, mask: int, *, horizon: int) -> tuple[list[float], list[str]]:
    values: list[float] = []
    tickers: list[str] = []
    eligible = mask & prepared.return_masks[horizon]
    for idx in iter_mask_indices(eligible):
        row = prepared.rows[idx]
        value = row.returns_by_horizon.get(horizon)
        if value is None:
            continue
        values.append(value)
        tickers.append(row.ticker)
    return values, tickers


def unique_tickers_from_mask(prepared: PreparedCohort, mask: int, *, horizon: int | None = None) -> set[str]:
    out: set[str] = set()
    eligible = mask & prepared.return_masks[horizon] if horizon is not None else mask
    for idx in iter_mask_indices(eligible):
        ticker = prepared.rows[idx].ticker
        if ticker:
            out.add(ticker)
    return out


def ticker_improvement_rate_from_mask(prepared: PreparedCohort, mask: int, *, horizon: int) -> float | None:
    grouped: dict[str, list[float]] = {}
    eligible = mask & prepared.return_masks[horizon]
    for idx in iter_mask_indices(eligible):
        row = prepared.rows[idx]
        value = row.returns_by_horizon.get(horizon)
        if value is None or not row.ticker:
            continue
        grouped.setdefault(row.ticker, []).append(value)
    if not grouped:
        return None
    improved = sum(1 for values in grouped.values() if median(values) > 0)
    return improved / len(grouped)


def metrics_from_mask(prepared: PreparedCohort, mask: int, *, horizon: int) -> dict[str, Any]:
    values, tickers = mask_values(prepared, mask, horizon=horizon)
    return metrics(values, tickers)


def prepare_cohort(
    rows: list[dict[str, str]],
    *,
    cohort: str,
    horizons: list[int],
    effective_train_end_asof: str,
    validation_start_asof: str,
    validation_end_asof: str,
    raw_mins: list[float],
    cohort_percentile_mins: list[float],
    fundamental_mins: list[float],
    fda_mins: list[float],
    reimbursement_mins: list[float],
    valuation_mins: list[float],
    technical_mins: list[float],
    value_trap_maxes: list[float],
) -> PreparedCohort:
    cohort_rows: list[PreparedRow] = []
    base_mask = 0
    train_mask = 0
    validation_mask = 0
    validation_all_mask = 0
    return_masks = {horizon: 0 for horizon in horizons}
    threshold_lists = {
        "raw_composite_score": raw_mins,
        "cohort_percentile": cohort_percentile_mins,
        "fundamental_quality_score": fundamental_mins,
        "fda_product_score": fda_mins,
        "reimbursement_score": reimbursement_mins,
        "valuation_score": valuation_mins,
        "technical_entry_score": technical_mins,
    }
    field_threshold_masks = {
        field: {threshold: 0 for threshold in thresholds}
        for field, thresholds in threshold_lists.items()
    }
    value_trap_threshold_masks = {threshold: 0 for threshold in value_trap_maxes}

    for row in rows:
        if str(row.get("calibration_cohort") or "") != cohort:
            continue
        returns_by_horizon = {
            horizon: to_float(row.get(f"cohort_excess_return_{horizon}d"))
            for horizon in horizons
        }
        item = PreparedRow(
            ticker=str(row.get("ticker") or ""),
            asof_date=str(row.get("asof_date") or "")[:10],
            static_allowed=passes_static_exclusions(row),
            raw_composite_score=to_float(row.get("raw_composite_score")),
            cohort_percentile=to_float(row.get("cohort_percentile")),
            fundamental_quality_score=to_float(row.get("fundamental_quality_score")),
            fda_product_score=to_float(row.get("fda_product_score")),
            reimbursement_score=to_float(row.get("reimbursement_score")),
            valuation_score=to_float(row.get("valuation_score")),
            technical_entry_score=to_float(row.get("technical_entry_score")),
            value_trap_score=to_float(row.get("value_trap_score")),
            returns_by_horizon=returns_by_horizon,
        )
        idx = len(cohort_rows)
        bit = 1 << idx
        cohort_rows.append(item)
        if item.static_allowed:
            base_mask |= bit
        if item.asof_date <= effective_train_end_asof:
            train_mask |= bit
        if validation_start_asof <= item.asof_date <= validation_end_asof:
            validation_mask |= bit
            validation_all_mask |= bit
        for horizon, value in returns_by_horizon.items():
            if value is not None:
                return_masks[horizon] |= bit
        for field, thresholds in threshold_lists.items():
            value = getattr(item, field)
            if value is None:
                continue
            for threshold in thresholds:
                if value >= threshold:
                    field_threshold_masks[field][threshold] |= bit
        for threshold in value_trap_maxes:
            if item.value_trap_score is not None and item.value_trap_score <= threshold:
                value_trap_threshold_masks[threshold] |= bit

    prepared = PreparedCohort(
        cohort=cohort,
        rows=cohort_rows,
        base_mask=base_mask,
        train_mask=train_mask,
        validation_mask=validation_mask,
        validation_all_mask=validation_all_mask,
        return_masks=return_masks,
        field_threshold_masks=field_threshold_masks,
        value_trap_threshold_masks=value_trap_threshold_masks,
        validation_all_metrics={},
        validation_cohort_tickers={},
    )
    object.__setattr__(
        prepared,
        "validation_all_metrics",
        {
            horizon: metrics_from_mask(prepared, validation_all_mask, horizon=horizon)
            for horizon in horizons
        },
    )
    object.__setattr__(
        prepared,
        "validation_cohort_tickers",
        {
            horizon: unique_tickers_from_mask(prepared, validation_all_mask, horizon=horizon)
            for horizon in horizons
        },
    )
    return prepared


def score_objective(metrics_by_horizon: dict[int, dict[str, Any]], weights: dict[str, float]) -> float:
    if not metrics_by_horizon:
        return -999.0
    total = 0.0
    used = 0
    for values in metrics_by_horizon.values():
        median_value = to_float(values.get("median")) or 0.0
        lcb_value = to_float(values.get("lcb")) or 0.0
        mean_value = to_float(values.get("mean")) or 0.0
        sortino = min(3.0, max(-3.0, to_float(values.get("sortino")) or 0.0))
        profit = min(3.0, max(0.0, to_float(values.get("profit_factor")) or 0.0))
        worst_loss = min(0.0, to_float(values.get("worst_loss")) or 0.0)
        total += (
            weights.get("median_excess_return", 0.35) * median_value * 100.0
            + weights.get("lower_confidence_bound", 0.25) * lcb_value * 100.0
            + weights.get("sortino", 0.15) * sortino
            + weights.get("profit_factor", 0.15) * (profit - 1.0)
            + weights.get("mean_excess_return", 0.10) * mean_value * 100.0
            + weights.get("worst_loss", 0.10) * worst_loss * 100.0
        )
        used += 1
    return total / used if used else -999.0


def parameter_id(*values: object) -> str:
    raw = "|".join(str(value) for value in values)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def validation_lcb_rejections(
    metrics_by_horizon: dict[int, dict[str, Any]],
    *,
    required_horizons: list[int],
    min_lcb_excess: float,
) -> list[str]:
    out: list[str] = []
    for horizon in required_horizons:
        payload = metrics_by_horizon.get(horizon)
        lcb = to_float(payload.get("lcb")) if payload else None
        if lcb is None or lcb < min_lcb_excess:
            out.append(f"{horizon}d_lcb_below_min")
    return out


def evaluate_parameter_set(
    rows: list[dict[str, str]],
    *,
    cohort: str,
    horizons: list[int],
    train_end_asof: str,
    effective_train_end_asof: str,
    embargo_days: int,
    validation_start_asof: str,
    validation_end_asof: str,
    min_train_obs: int,
    min_validation_obs: int,
    min_unique_tickers: int,
    min_selected_validation: int,
    min_selected_ticker_coverage: float,
    min_improved_selected_ticker_rate: float,
    min_validation_lcb_excess: float,
    required_positive_lcb_horizons: list[int],
    objective_weights: dict[str, float],
    raw_score_min: float,
    cohort_percentile_min: float,
    fundamental_quality_min: float,
    fda_product_min: float,
    reimbursement_min: float,
    valuation_min: float,
    technical_entry_min: float,
    value_trap_max: float,
) -> dict[str, Any]:
    selected = [
        row
        for row in rows
        if str(row.get("calibration_cohort") or "") == cohort
        and passes_gates(
            row,
            raw_score_min=raw_score_min,
            cohort_percentile_min=cohort_percentile_min,
            fundamental_quality_min=fundamental_quality_min,
            fda_product_min=fda_product_min,
            reimbursement_min=reimbursement_min,
            valuation_min=valuation_min,
            technical_entry_min=technical_entry_min,
            value_trap_max=value_trap_max,
        )
    ]
    train = [row for row in selected if str(row.get("asof_date") or "") <= effective_train_end_asof]
    validation = [
        row
        for row in selected
        if validation_start_asof <= str(row.get("asof_date") or "") <= validation_end_asof
    ]
    validation_all = [
        row
        for row in rows
        if str(row.get("calibration_cohort") or "") == cohort
        and validation_start_asof <= str(row.get("asof_date") or "") <= validation_end_asof
    ]
    metrics_train: dict[int, dict[str, Any]] = {}
    metrics_validation: dict[int, dict[str, Any]] = {}
    metrics_validation_all: dict[int, dict[str, Any]] = {}
    for horizon in horizons:
        train_values, train_tickers = selected_values(train, horizon=horizon)
        validation_values, validation_tickers = selected_values(validation, horizon=horizon)
        validation_all_values, validation_all_tickers = selected_values(validation_all, horizon=horizon)
        metrics_train[horizon] = metrics(train_values, train_tickers)
        metrics_validation[horizon] = metrics(validation_values, validation_tickers)
        metrics_validation_all[horizon] = metrics(validation_all_values, validation_all_tickers)
    ref_horizon = max(horizons)
    train_ref = metrics_train[ref_horizon]
    validation_ref = metrics_validation[ref_horizon]
    validation_all_ref = metrics_validation_all[ref_horizon]
    validation_cohort_tickers = unique_tickers_with_returns(validation_all, horizon=ref_horizon)
    selected_tickers = unique_tickers_with_returns(validation, horizon=ref_horizon)
    selected_coverage = (len(selected_tickers) / len(validation_cohort_tickers)) if validation_cohort_tickers else 0.0
    improved_rate = selected_ticker_improvement_rate(validation, horizon=ref_horizon)
    rejection: list[str] = []
    if int(train_ref["count"]) < min_train_obs:
        rejection.append("insufficient_train_obs")
    if int(validation_all_ref["count"]) < min_validation_obs:
        rejection.append("insufficient_validation_obs")
    if int(validation_ref["count"]) < min_selected_validation:
        rejection.append("insufficient_selected_validation")
    if int(validation_ref["unique_tickers"]) < min_unique_tickers:
        rejection.append("insufficient_unique_tickers")
    if selected_coverage < min_selected_ticker_coverage:
        rejection.append("insufficient_selected_ticker_coverage")
    if improved_rate is None or improved_rate < min_improved_selected_ticker_rate:
        rejection.append("insufficient_improved_selected_ticker_rate")
    if (to_float(validation_ref.get("median")) or 0.0) <= 0:
        rejection.append("nonpositive_validation_median_excess")
    if (to_float(train_ref.get("median")) or 0.0) > 0 and (to_float(validation_ref.get("median")) or 0.0) < 0:
        rejection.append("train_validation_sign_flip")
    rejection.extend(
        validation_lcb_rejections(
            metrics_validation,
            required_horizons=required_positive_lcb_horizons,
            min_lcb_excess=min_validation_lcb_excess,
        )
    )
    objective = score_objective(metrics_validation, objective_weights)
    base = {
        "calibration_cohort": cohort,
        "parameter_set_id": parameter_id(
            cohort,
            raw_score_min,
            cohort_percentile_min,
            fundamental_quality_min,
            fda_product_min,
            reimbursement_min,
            valuation_min,
            technical_entry_min,
            value_trap_max,
        ),
        "raw_score_min": raw_score_min,
        "cohort_percentile_min": cohort_percentile_min,
        "fundamental_quality_min": fundamental_quality_min,
        "fda_product_min": fda_product_min,
        "reimbursement_min": reimbursement_min,
        "valuation_min": valuation_min,
        "technical_entry_min": technical_entry_min,
        "value_trap_max": value_trap_max,
        "train_end_asof": train_end_asof,
        "effective_train_end_asof": effective_train_end_asof,
        "embargo_days": embargo_days,
        "validation_start_asof": validation_start_asof,
        "validation_end_asof": validation_end_asof,
        "objective_score": f"{objective:.6f}",
        "pass_fail": "fail" if rejection else "pass",
        "rejection_reason": ";".join(dict.fromkeys(rejection)),
        "validation_cohort_obs_120d": int(validation_all_ref["count"]),
        "validation_cohort_unique_tickers_120d": len(validation_cohort_tickers),
        "validation_selected_ticker_coverage_120d": f"{selected_coverage:.4f}",
        "validation_improved_selected_ticker_rate_120d": "" if improved_rate is None else f"{improved_rate:.4f}",
        "selected_tickers_validation": ";".join(sorted({str(row.get("ticker") or "") for row in validation}))[:500],
    }
    for horizon in horizons:
        for prefix, payload in (("train", metrics_train[horizon]), ("validation", metrics_validation[horizon])):
            for key, value in payload.items():
                base[f"{prefix}_{key}_{horizon}d"] = value
    return base


def evaluate_prepared_parameter_set(
    prepared: PreparedCohort,
    *,
    horizons: list[int],
    train_end_asof: str,
    effective_train_end_asof: str,
    embargo_days: int,
    validation_start_asof: str,
    validation_end_asof: str,
    min_train_obs: int,
    min_validation_obs: int,
    min_unique_tickers: int,
    min_selected_validation: int,
    min_selected_ticker_coverage: float,
    min_improved_selected_ticker_rate: float,
    min_validation_lcb_excess: float,
    required_positive_lcb_horizons: list[int],
    objective_weights: dict[str, float],
    raw_score_min: float,
    cohort_percentile_min: float,
    fundamental_quality_min: float,
    fda_product_min: float,
    reimbursement_min: float,
    valuation_min: float,
    technical_entry_min: float,
    value_trap_max: float,
) -> dict[str, Any]:
    selected_mask = (
        prepared.base_mask
        & prepared.field_threshold_masks["raw_composite_score"][raw_score_min]
        & prepared.field_threshold_masks["cohort_percentile"][cohort_percentile_min]
        & prepared.field_threshold_masks["fundamental_quality_score"][fundamental_quality_min]
        & prepared.field_threshold_masks["fda_product_score"][fda_product_min]
        & prepared.field_threshold_masks["reimbursement_score"][reimbursement_min]
        & prepared.field_threshold_masks["valuation_score"][valuation_min]
        & prepared.field_threshold_masks["technical_entry_score"][technical_entry_min]
        & prepared.value_trap_threshold_masks[value_trap_max]
    )
    train_mask = selected_mask & prepared.train_mask
    validation_mask = selected_mask & prepared.validation_mask
    metrics_train = {
        horizon: metrics_from_mask(prepared, train_mask, horizon=horizon)
        for horizon in horizons
    }
    metrics_validation = {
        horizon: metrics_from_mask(prepared, validation_mask, horizon=horizon)
        for horizon in horizons
    }
    ref_horizon = max(horizons)
    train_ref = metrics_train[ref_horizon]
    validation_ref = metrics_validation[ref_horizon]
    validation_all_ref = prepared.validation_all_metrics[ref_horizon]
    validation_cohort_tickers = prepared.validation_cohort_tickers[ref_horizon]
    selected_tickers = unique_tickers_from_mask(prepared, validation_mask, horizon=ref_horizon)
    selected_coverage = (len(selected_tickers) / len(validation_cohort_tickers)) if validation_cohort_tickers else 0.0
    improved_rate = ticker_improvement_rate_from_mask(prepared, validation_mask, horizon=ref_horizon)
    rejection: list[str] = []
    if int(train_ref["count"]) < min_train_obs:
        rejection.append("insufficient_train_obs")
    if int(validation_all_ref["count"]) < min_validation_obs:
        rejection.append("insufficient_validation_obs")
    if int(validation_ref["count"]) < min_selected_validation:
        rejection.append("insufficient_selected_validation")
    if int(validation_ref["unique_tickers"]) < min_unique_tickers:
        rejection.append("insufficient_unique_tickers")
    if selected_coverage < min_selected_ticker_coverage:
        rejection.append("insufficient_selected_ticker_coverage")
    if improved_rate is None or improved_rate < min_improved_selected_ticker_rate:
        rejection.append("insufficient_improved_selected_ticker_rate")
    if (to_float(validation_ref.get("median")) or 0.0) <= 0:
        rejection.append("nonpositive_validation_median_excess")
    if (to_float(train_ref.get("median")) or 0.0) > 0 and (to_float(validation_ref.get("median")) or 0.0) < 0:
        rejection.append("train_validation_sign_flip")
    rejection.extend(
        validation_lcb_rejections(
            metrics_validation,
            required_horizons=required_positive_lcb_horizons,
            min_lcb_excess=min_validation_lcb_excess,
        )
    )
    objective = score_objective(metrics_validation, objective_weights)
    base = {
        "calibration_cohort": prepared.cohort,
        "parameter_set_id": parameter_id(
            prepared.cohort,
            raw_score_min,
            cohort_percentile_min,
            fundamental_quality_min,
            fda_product_min,
            reimbursement_min,
            valuation_min,
            technical_entry_min,
            value_trap_max,
        ),
        "raw_score_min": raw_score_min,
        "cohort_percentile_min": cohort_percentile_min,
        "fundamental_quality_min": fundamental_quality_min,
        "fda_product_min": fda_product_min,
        "reimbursement_min": reimbursement_min,
        "valuation_min": valuation_min,
        "technical_entry_min": technical_entry_min,
        "value_trap_max": value_trap_max,
        "train_end_asof": train_end_asof,
        "effective_train_end_asof": effective_train_end_asof,
        "embargo_days": embargo_days,
        "validation_start_asof": validation_start_asof,
        "validation_end_asof": validation_end_asof,
        "objective_score": f"{objective:.6f}",
        "pass_fail": "fail" if rejection else "pass",
        "rejection_reason": ";".join(dict.fromkeys(rejection)),
        "validation_cohort_obs_120d": int(validation_all_ref["count"]),
        "validation_cohort_unique_tickers_120d": len(validation_cohort_tickers),
        "validation_selected_ticker_coverage_120d": f"{selected_coverage:.4f}",
        "validation_improved_selected_ticker_rate_120d": "" if improved_rate is None else f"{improved_rate:.4f}",
        "selected_tickers_validation": ";".join(sorted(unique_tickers_from_mask(prepared, validation_mask)))[:500],
    }
    for horizon in horizons:
        for prefix, payload in (("train", metrics_train[horizon]), ("validation", metrics_validation[horizon])):
            for key, value in payload.items():
                base[f"{prefix}_{key}_{horizon}d"] = value
    return base


def output_fields(horizons: list[int]) -> list[str]:
    fields = list(BASE_FIELDS)
    for horizon in horizons:
        for prefix in ("train", "validation"):
            for key in ("count", "unique_tickers", "mean", "median", "hit_rate", "loss_rate", "lcb", "worst_loss", "sortino", "profit_factor"):
                fields.append(f"{prefix}_{key}_{horizon}d")
    return fields


def main() -> None:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    input_csv = (
        args.input_csv.expanduser().resolve()
        if args.input_csv
        else resolve_path(cfg_get(config, "calibration.cohort_neutral_backtest_csv"), base_dir=base_dir)
    )
    output_csv = (
        args.output_csv.expanduser().resolve()
        if args.output_csv
        else resolve_path(cfg_get(config, "calibration.gate_grid_results_csv"), base_dir=base_dir)
    )
    rows = read_csv(input_csv)
    horizons = parse_int_list(cfg_get(config, "calibration.horizons", "30,60,120"), "30,60,120")
    if not rows:
        raise RuntimeError(f"Gate optimization input is empty: {input_csv}")
    missing_horizons = [horizon for horizon in horizons if f"cohort_excess_return_{horizon}d" not in rows[0]]
    if missing_horizons:
        raise RuntimeError(
            f"Gate optimization input {input_csv} is missing cohort_excess_return columns for horizons: "
            + ",".join(str(item) for item in missing_horizons)
        )
    train_end_asof, validation_start_asof, validation_end_asof = resolve_calibration_dates(config, rows)
    embargo_days = int(cfg_get(config, "calibration.embargo_days", 120))
    effective_train_end_asof = effective_train_end(train_end_asof, validation_start_asof, embargo_days)
    min_train_obs = int(cfg_get(config, "calibration.min_train_obs", 100))
    min_validation_obs = int(cfg_get(config, "calibration.min_validation_obs", 40))
    min_unique_tickers = int(cfg_get(config, "calibration.min_unique_tickers", 5))
    min_selected_validation = int(cfg_get(config, "calibration.min_selected_validation", 20))
    min_selected_ticker_coverage = float(cfg_get(config, "calibration.min_selected_ticker_coverage", 0.60))
    min_improved_selected_ticker_rate = float(cfg_get(config, "calibration.min_improved_selected_ticker_rate", 0.60))
    min_validation_lcb_excess = float(cfg_get(config, "calibration.min_validation_lcb_excess", 0.0))
    required_positive_lcb_horizons = [
        horizon
        for horizon in parse_int_list(
            cfg_get(config, "calibration.require_positive_lcb_horizons", str(max(horizons))),
            str(max(horizons)),
        )
        if horizon in horizons
    ]
    if not required_positive_lcb_horizons:
        required_positive_lcb_horizons = [max(horizons)]
    objective_weights = cfg_get(config, "calibration.objective", {}) or {}

    raw_mins = parse_float_list(cfg_get(config, "calibration.candidate_raw_score_min"), "55,60,65,70")
    cohort_percentile_mins = parse_float_list(cfg_get(config, "calibration.candidate_cohort_percentile_min"), "60,70,80,90")
    fundamental_mins = parse_float_list(cfg_get(config, "calibration.candidate_fundamental_quality_min"), "0,60,70")
    fda_mins = parse_float_list(cfg_get(config, "calibration.candidate_fda_product_min"), "0,50,60")
    reimbursement_mins = parse_float_list(cfg_get(config, "calibration.candidate_reimbursement_min"), "0,45,55")
    valuation_mins = parse_float_list(cfg_get(config, "calibration.candidate_valuation_min"), "0,55,60")
    technical_mins = parse_float_list(cfg_get(config, "calibration.candidate_technical_entry_min"), "0,45,55")
    value_trap_maxes = parse_float_list(cfg_get(config, "calibration.candidate_value_trap_max"), "20,25,30,40")

    cohorts = sorted({str(row.get("calibration_cohort") or "") for row in rows if str(row.get("calibration_cohort") or "")})
    all_results: list[dict[str, Any]] = []
    for cohort in cohorts:
        prepared = prepare_cohort(
            rows,
            cohort=cohort,
            horizons=horizons,
            effective_train_end_asof=effective_train_end_asof,
            validation_start_asof=validation_start_asof,
            validation_end_asof=validation_end_asof,
            raw_mins=raw_mins,
            cohort_percentile_mins=cohort_percentile_mins,
            fundamental_mins=fundamental_mins,
            fda_mins=fda_mins,
            reimbursement_mins=reimbursement_mins,
            valuation_mins=valuation_mins,
            technical_mins=technical_mins,
            value_trap_maxes=value_trap_maxes,
        )
        cohort_results: list[dict[str, Any]] = []
        for raw_min, pct_min, fund_min, fda_min, reimb_min, val_min, tech_min, trap_max in itertools.product(
            raw_mins,
            cohort_percentile_mins,
            fundamental_mins,
            fda_mins,
            reimbursement_mins,
            valuation_mins,
            technical_mins,
            value_trap_maxes,
        ):
            cohort_results.append(
                evaluate_prepared_parameter_set(
                    prepared,
                    horizons=horizons,
                    train_end_asof=train_end_asof,
                    effective_train_end_asof=effective_train_end_asof,
                    embargo_days=embargo_days,
                    validation_start_asof=validation_start_asof,
                    validation_end_asof=validation_end_asof,
                    min_train_obs=min_train_obs,
                    min_validation_obs=min_validation_obs,
                    min_unique_tickers=min_unique_tickers,
                    min_selected_validation=min_selected_validation,
                    min_selected_ticker_coverage=min_selected_ticker_coverage,
                    min_improved_selected_ticker_rate=min_improved_selected_ticker_rate,
                    min_validation_lcb_excess=min_validation_lcb_excess,
                    required_positive_lcb_horizons=required_positive_lcb_horizons,
                    objective_weights=objective_weights,
                    raw_score_min=raw_min,
                    cohort_percentile_min=pct_min,
                    fundamental_quality_min=fund_min,
                    fda_product_min=fda_min,
                    reimbursement_min=reimb_min,
                    valuation_min=val_min,
                    technical_entry_min=tech_min,
                    value_trap_max=trap_max,
                )
            )
        cohort_results.sort(key=lambda item: (item["pass_fail"] == "pass", to_float(item["objective_score"]) or -999.0), reverse=True)
        if args.max_rows_per_cohort > 0:
            all_results.extend(cohort_results[: args.max_rows_per_cohort])
        else:
            all_results.extend(cohort_results)
    write_csv(output_csv, all_results, output_fields(horizons))
    print(f"gate_grid_results_csv={output_csv} rows={len(all_results)}")


if __name__ == "__main__":
    raise SystemExit(main())
