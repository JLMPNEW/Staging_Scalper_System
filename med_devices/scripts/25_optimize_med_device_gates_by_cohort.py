#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import itertools
import math
import sys
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
            "lcb": "",
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
        "lcb": f"{lcb_value:.6f}",
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
    if value_trap is not None and value_trap > value_trap_max:
        return False
    return True


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
        total += (
            weights.get("median_excess_return", 0.35) * median_value * 100.0
            + weights.get("lower_confidence_bound", 0.25) * lcb_value * 100.0
            + weights.get("sortino", 0.15) * sortino
            + weights.get("profit_factor", 0.15) * (profit - 1.0)
            + weights.get("mean_excess_return", 0.10) * mean_value * 100.0
        )
        used += 1
    return total / used if used else -999.0


def parameter_id(*values: object) -> str:
    raw = "|".join(str(value) for value in values)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


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
        "rejection_reason": ";".join(rejection),
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


def output_fields(horizons: list[int]) -> list[str]:
    fields = list(BASE_FIELDS)
    for horizon in horizons:
        for prefix in ("train", "validation"):
            for key in ("count", "unique_tickers", "mean", "median", "hit_rate", "lcb", "sortino", "profit_factor"):
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
    train_end_asof = str(cfg_get(config, "calibration.train_end_asof", "2025-05-30"))
    validation_start_asof = str(cfg_get(config, "calibration.validation_start_asof", "2025-06-06"))
    validation_end_asof = str(cfg_get(config, "calibration.validation_end_asof", "2025-11-28"))
    embargo_days = int(cfg_get(config, "calibration.embargo_days", 120))
    effective_train_end_asof = effective_train_end(train_end_asof, validation_start_asof, embargo_days)
    min_train_obs = int(cfg_get(config, "calibration.min_train_obs", 100))
    min_validation_obs = int(cfg_get(config, "calibration.min_validation_obs", 40))
    min_unique_tickers = int(cfg_get(config, "calibration.min_unique_tickers", 5))
    min_selected_validation = int(cfg_get(config, "calibration.min_selected_validation", 20))
    min_selected_ticker_coverage = float(cfg_get(config, "calibration.min_selected_ticker_coverage", 0.60))
    min_improved_selected_ticker_rate = float(cfg_get(config, "calibration.min_improved_selected_ticker_rate", 0.60))
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
                evaluate_parameter_set(
                    rows,
                    cohort=cohort,
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
    main()
