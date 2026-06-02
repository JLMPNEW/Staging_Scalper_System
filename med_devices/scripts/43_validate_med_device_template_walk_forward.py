#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import importlib.util
import math
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import mean, median
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from med_devices.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from med_devices.core.logging_utils import configure_utc_logging  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
RESULT_FIELDS = [
    "calibration_cohort",
    "template_id",
    "horizon_days",
    "fold_id",
    "split",
    "train_start",
    "train_end",
    "validation_start",
    "validation_end",
    "count",
    "unique_tickers",
    "selected_ticker_coverage",
    "mean_return",
    "median_return",
    "hit_rate",
    "mean_excess",
    "median_excess",
    "excess_hit_rate",
    "lcb_excess",
    "delta_mean_excess_vs_baseline",
    "delta_median_excess_vs_baseline",
    "delta_excess_hit_rate_vs_baseline",
    "delta_lcb_excess_vs_baseline",
    "improved_selected_ticker_rate",
    "fold_status",
    "fold_reason",
    "weights_spec",
]
SUMMARY_FIELDS = [
    "calibration_cohort",
    "template_id",
    "horizon_days",
    "fold_count",
    "pass_fold_count",
    "pass_fold_rate",
    "validation_count",
    "validation_unique_tickers",
    "mean_validation_median_excess",
    "min_validation_median_excess",
    "mean_validation_lcb_excess",
    "min_validation_lcb_excess",
    "mean_validation_hit_rate",
    "mean_validation_excess_hit_rate",
    "mean_selected_ticker_coverage",
    "mean_improved_selected_ticker_rate",
    "sign_flip_fold_count",
    "recommendation_status",
    "recommendation_reason",
    "weights_spec",
]
RECOMMENDATION_FIELDS = [
    "calibration_cohort",
    "recommended_template_id",
    "promotion_status",
    "horizon_days",
    "pass_fold_count",
    "pass_fold_rate",
    "validation_unique_tickers",
    "mean_validation_median_excess",
    "min_validation_lcb_excess",
    "mean_validation_excess_hit_rate",
    "mean_selected_ticker_coverage",
    "mean_improved_selected_ticker_rate",
    "promotion_reason",
    "weights_spec",
]


@dataclass(frozen=True)
class Fold:
    fold_id: str
    train_start: date
    train_end: date
    validation_start: date
    validation_end: date


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run rolling walk-forward validation for med-device cohort templates.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--input-csv", type=Path, default=None)
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--summary-csv", type=Path, default=None)
    parser.add_argument("--recommendation-csv", type=Path, default=None)
    parser.add_argument("--horizons", type=str, default="")
    return parser.parse_args()


def load_template_module() -> Any:
    path = PACKAGE_ROOT / "scripts" / "41_test_med_device_restricted_cohort_templates.py"
    spec = importlib.util.spec_from_file_location("restricted_cohort_templates", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to import restricted template module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def to_float(raw: object) -> float | None:
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def fmt(value: object) -> str:
    number = to_float(value)
    return "" if number is None else f"{number:.6f}"


def parse_int_list(raw: object) -> list[int]:
    out: list[int] = []
    for item in str(raw or "").split(","):
        text = item.strip()
        if text.isdigit():
            out.append(int(text))
    return out


def cfg_bool(config: dict[str, Any], path: str, default: bool) -> bool:
    value = cfg_get(config, path, default)
    return str(value).strip().lower() not in {"0", "false", "no", "off"}


def parse_date(raw: object) -> date:
    return datetime.strptime(str(raw)[:10], "%Y-%m-%d").date()


def add_months(value: date, months: int) -> date:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    days_in_month = [
        31,
        29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28,
        31,
        30,
        31,
        30,
        31,
        31,
        30,
        31,
        30,
        31,
    ][month - 1]
    return date(year, month, min(value.day, days_in_month))


def available_horizons(rows: list[dict[str, str]]) -> list[int]:
    if not rows:
        return []
    out: list[int] = []
    for key in rows[0]:
        if key.startswith("cohort_excess_return_") and key.endswith("d"):
            text = key[len("cohort_excess_return_") : -1]
            if text.isdigit():
                out.append(int(text))
    return sorted(out)


def build_folds(rows: list[dict[str, str]], config: dict[str, Any]) -> list[Fold]:
    asof_dates = sorted({parse_date(row.get("asof_date")) for row in rows if row.get("asof_date")})
    if not asof_dates:
        return []
    min_date = asof_dates[0]
    max_date = asof_dates[-1]
    train_months = int(cfg_get(config, "calibration.template_walk_forward.train_months", 12))
    validation_months = int(cfg_get(config, "calibration.template_walk_forward.validation_months", 3))
    step_months = int(cfg_get(config, "calibration.template_walk_forward.step_months", validation_months))
    embargo_days = int(cfg_get(config, "calibration.template_walk_forward.embargo_days", 120))
    validation_start = add_months(min_date, train_months) + timedelta(days=embargo_days)
    folds: list[Fold] = []
    fold_no = 1
    while validation_start <= max_date:
        validation_end = min(add_months(validation_start, validation_months) - timedelta(days=1), max_date)
        train_end = validation_start - timedelta(days=embargo_days + 1)
        train_start = add_months(train_end, -train_months) + timedelta(days=1)
        if train_start >= min_date and train_start <= train_end and validation_start <= validation_end:
            folds.append(
                Fold(
                    fold_id=f"wf_{fold_no:02d}",
                    train_start=train_start,
                    train_end=train_end,
                    validation_start=validation_start,
                    validation_end=validation_end,
                )
            )
            fold_no += 1
        validation_start = add_months(validation_start, step_months)
    return folds


def rows_in_range(rows: list[dict[str, Any]], start: date, end: date) -> list[dict[str, Any]]:
    return [
        row for row in rows
        if row.get("asof_date") and start <= parse_date(row.get("asof_date")) <= end
    ]


def lcb(values: list[float], z: float = 1.64) -> float:
    if not values:
        return 0.0
    if len(values) < 2:
        return values[0]
    avg = mean(values)
    variance = sum((value - avg) ** 2 for value in values) / (len(values) - 1)
    return avg - z * math.sqrt(variance / len(values))


def selected_rows(rows: list[dict[str, Any]], *, horizon: int) -> list[dict[str, Any]]:
    return [
        row for row in rows
        if row.get("sim_cohort_rank_bucket") == "cohort_top_decile"
        and to_float(row.get(f"forward_return_{horizon}d")) is not None
        and to_float(row.get(f"cohort_excess_return_{horizon}d")) is not None
    ]


def metrics(rows: list[dict[str, Any]], *, horizon: int, full_rows: list[dict[str, Any]]) -> dict[str, Any]:
    selected = selected_rows(rows, horizon=horizon)
    returns = [float(row[f"forward_return_{horizon}d"]) for row in selected]
    excess = [float(row[f"cohort_excess_return_{horizon}d"]) for row in selected]
    tickers = {str(row.get("ticker") or "") for row in selected}
    full_tickers = {str(row.get("ticker") or "") for row in full_rows}
    if not selected:
        return {
            "count": 0,
            "unique_tickers": 0,
            "selected_ticker_coverage": 0.0,
            "mean_return": 0.0,
            "median_return": 0.0,
            "hit_rate": 0.0,
            "mean_excess": 0.0,
            "median_excess": 0.0,
            "excess_hit_rate": 0.0,
            "lcb_excess": 0.0,
        }
    return {
        "count": len(selected),
        "unique_tickers": len(tickers),
        "selected_ticker_coverage": len(tickers) / len(full_tickers) if full_tickers else 0.0,
        "mean_return": mean(returns),
        "median_return": median(returns),
        "hit_rate": sum(1 for value in returns if value > 0) / len(returns),
        "mean_excess": mean(excess),
        "median_excess": median(excess),
        "excess_hit_rate": sum(1 for value in excess if value > 0) / len(excess),
        "lcb_excess": lcb(excess),
    }


def selected_ticker_means(rows: list[dict[str, Any]], *, horizon: int) -> dict[str, float]:
    grouped: dict[str, list[float]] = defaultdict(list)
    for row in selected_rows(rows, horizon=horizon):
        value = to_float(row.get(f"cohort_excess_return_{horizon}d"))
        if value is not None:
            grouped[str(row.get("ticker") or "")].append(value)
    return {ticker: mean(values) for ticker, values in grouped.items() if values}


def weights_spec(template: Any) -> str:
    return ";".join(f"{field}:{direction}:{weight:.2f}" for field, direction, weight in template.weights)


def evaluate_fold_row(
    *,
    cohort: str,
    template_id: str,
    horizon: int,
    fold: Fold,
    split: str,
    candidate_rows: list[dict[str, Any]],
    baseline_rows: list[dict[str, Any]],
    full_rows: list[dict[str, Any]],
    config: dict[str, Any],
    weights: str,
) -> dict[str, Any]:
    row = {
        "calibration_cohort": cohort,
        "template_id": template_id,
        "horizon_days": horizon,
        "fold_id": fold.fold_id,
        "split": split,
        "train_start": fold.train_start.isoformat(),
        "train_end": fold.train_end.isoformat(),
        "validation_start": fold.validation_start.isoformat(),
        "validation_end": fold.validation_end.isoformat(),
        "weights_spec": weights,
    }
    candidate = metrics(candidate_rows, horizon=horizon, full_rows=full_rows)
    baseline = metrics(baseline_rows, horizon=horizon, full_rows=full_rows)
    row.update(candidate)
    for field in ("mean_excess", "median_excess", "excess_hit_rate", "lcb_excess"):
        row[f"delta_{field}_vs_baseline"] = candidate[field] - baseline[field]
    base_means = selected_ticker_means(baseline_rows, horizon=horizon)
    candidate_means = selected_ticker_means(candidate_rows, horizon=horizon)
    comparable = [ticker for ticker in candidate_means if ticker in base_means]
    improved = [ticker for ticker in comparable if candidate_means[ticker] > base_means[ticker]]
    improved_rate = len(improved) / len(comparable) if comparable else 0.0
    row["improved_selected_ticker_rate"] = improved_rate
    if split != "validation" or template_id == "baseline_existing":
        row["fold_status"] = "baseline" if template_id == "baseline_existing" else "not_scored_split"
        row["fold_reason"] = "baseline_reference" if template_id == "baseline_existing" else "fold checks use validation split"
        return row

    min_selected = int(cfg_get(config, "calibration.template_walk_forward.min_validation_selected", 10))
    min_unique = int(cfg_get(config, "calibration.template_walk_forward.min_validation_unique_tickers", 3))
    min_coverage = float(cfg_get(config, "calibration.template_walk_forward.min_selected_ticker_coverage", 0.10))
    min_hit = float(cfg_get(config, "calibration.template_walk_forward.min_excess_hit_rate", 0.52))
    min_improved = float(cfg_get(config, "calibration.template_walk_forward.min_improved_selected_ticker_rate", 0.50))
    require_positive_median = cfg_bool(config, "calibration.template_walk_forward.require_positive_median_excess", True)
    require_positive_lcb = cfg_bool(config, "calibration.template_walk_forward.require_positive_lcb_excess", True)
    reasons: list[str] = []
    if candidate["count"] < min_selected:
        reasons.append("insufficient_selected_obs")
    if candidate["unique_tickers"] < min_unique:
        reasons.append("insufficient_unique_tickers")
    if candidate["selected_ticker_coverage"] < min_coverage:
        reasons.append("insufficient_ticker_coverage")
    if require_positive_median and candidate["median_excess"] <= 0:
        reasons.append("median_excess_not_positive")
    if require_positive_lcb and candidate["lcb_excess"] <= 0:
        reasons.append("lcb_excess_not_positive")
    if candidate["excess_hit_rate"] < min_hit:
        reasons.append("excess_hit_rate_below_min")
    if row["delta_median_excess_vs_baseline"] <= 0:
        reasons.append("median_excess_not_improved")
    if row["delta_lcb_excess_vs_baseline"] <= 0:
        reasons.append("lcb_excess_not_improved")
    if improved_rate < min_improved:
        reasons.append("improved_selected_ticker_rate_below_min")
    row["fold_status"] = "pass" if not reasons else "fail"
    row["fold_reason"] = "passes_walk_forward_fold_checks" if not reasons else ";".join(reasons)
    return row


def summarize(rows: list[dict[str, Any]], config: dict[str, Any]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, int], list[dict[str, Any]]] = defaultdict(list)
    train_by_key: dict[tuple[str, str, int, str], dict[str, Any]] = {}
    for row in rows:
        if row["template_id"] == "baseline_existing":
            continue
        key = (row["calibration_cohort"], row["template_id"], int(row["horizon_days"]))
        if row["split"] == "train":
            train_by_key[(row["calibration_cohort"], row["template_id"], int(row["horizon_days"]), row["fold_id"])] = row
        elif row["split"] == "validation":
            grouped[key].append(row)

    min_pass_folds = int(cfg_get(config, "calibration.template_walk_forward.min_pass_folds", 2))
    min_pass_rate = float(cfg_get(config, "calibration.template_walk_forward.min_pass_fold_rate", 0.60))
    min_unique = int(cfg_get(config, "calibration.template_walk_forward.min_summary_unique_tickers", 3))
    summary_rows: list[dict[str, Any]] = []
    for (cohort, template_id, horizon), items in sorted(grouped.items()):
        pass_count = sum(1 for row in items if row["fold_status"] == "pass")
        pass_rate = pass_count / len(items) if items else 0.0
        validation_count = sum(int(row["count"]) for row in items)
        unique_tickers = max((int(row["unique_tickers"]) for row in items), default=0)
        sign_flip_count = 0
        for row in items:
            train = train_by_key.get((cohort, template_id, horizon, row["fold_id"]))
            if train and float(train["median_excess"]) * float(row["median_excess"]) < 0:
                sign_flip_count += 1
        median_values = [float(row["median_excess"]) for row in items]
        lcb_values = [float(row["lcb_excess"]) for row in items]
        hit_values = [float(row["hit_rate"]) for row in items]
        excess_hit_values = [float(row["excess_hit_rate"]) for row in items]
        coverage_values = [float(row["selected_ticker_coverage"]) for row in items]
        improved_values = [float(row["improved_selected_ticker_rate"]) for row in items]
        reasons: list[str] = []
        if pass_count < min_pass_folds:
            reasons.append("insufficient_pass_folds")
        if pass_rate < min_pass_rate:
            reasons.append("pass_fold_rate_below_min")
        if unique_tickers < min_unique:
            reasons.append("insufficient_unique_tickers")
        if not lcb_values or min(lcb_values) <= 0:
            reasons.append("nonpositive_min_lcb")
        status = "candidate" if not reasons else "reject"
        summary_rows.append(
            {
                "calibration_cohort": cohort,
                "template_id": template_id,
                "horizon_days": horizon,
                "fold_count": len(items),
                "pass_fold_count": pass_count,
                "pass_fold_rate": pass_rate,
                "validation_count": validation_count,
                "validation_unique_tickers": unique_tickers,
                "mean_validation_median_excess": mean(median_values) if median_values else 0.0,
                "min_validation_median_excess": min(median_values) if median_values else 0.0,
                "mean_validation_lcb_excess": mean(lcb_values) if lcb_values else 0.0,
                "min_validation_lcb_excess": min(lcb_values) if lcb_values else 0.0,
                "mean_validation_hit_rate": mean(hit_values) if hit_values else 0.0,
                "mean_validation_excess_hit_rate": mean(excess_hit_values) if excess_hit_values else 0.0,
                "mean_selected_ticker_coverage": mean(coverage_values) if coverage_values else 0.0,
                "mean_improved_selected_ticker_rate": mean(improved_values) if improved_values else 0.0,
                "sign_flip_fold_count": sign_flip_count,
                "recommendation_status": status,
                "recommendation_reason": "passes_walk_forward_summary_checks" if not reasons else ";".join(reasons),
                "weights_spec": str(items[0].get("weights_spec") or "") if items else "",
            }
        )
    return summary_rows


def recommendations(summary_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_cohort: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in summary_rows:
        if row["recommendation_status"] == "candidate":
            by_cohort[str(row["calibration_cohort"])].append(row)
    out: list[dict[str, Any]] = []
    all_cohorts = sorted({str(row["calibration_cohort"]) for row in summary_rows})
    for cohort in all_cohorts:
        candidates = by_cohort.get(cohort, [])
        candidates.sort(
            key=lambda row: (
                int(row["pass_fold_count"]),
                float(row["pass_fold_rate"]),
                float(row["min_validation_lcb_excess"]),
                float(row["mean_validation_median_excess"]),
                float(row["mean_validation_excess_hit_rate"]),
            ),
            reverse=True,
        )
        best = candidates[0] if candidates else None
        out.append(
            {
                "calibration_cohort": cohort,
                "recommended_template_id": best["template_id"] if best else "",
                "promotion_status": "walk_forward_candidate" if best else "no_walk_forward_candidate",
                "horizon_days": best["horizon_days"] if best else "",
                "pass_fold_count": best["pass_fold_count"] if best else "",
                "pass_fold_rate": best["pass_fold_rate"] if best else "",
                "validation_unique_tickers": best["validation_unique_tickers"] if best else "",
                "mean_validation_median_excess": best["mean_validation_median_excess"] if best else "",
                "min_validation_lcb_excess": best["min_validation_lcb_excess"] if best else "",
                "mean_validation_excess_hit_rate": best["mean_validation_excess_hit_rate"] if best else "",
                "mean_selected_ticker_coverage": best["mean_selected_ticker_coverage"] if best else "",
                "mean_improved_selected_ticker_rate": best["mean_improved_selected_ticker_rate"] if best else "",
                "promotion_reason": best["recommendation_reason"] if best else "no template passed walk-forward summary checks",
                "weights_spec": best["weights_spec"] if best else "",
            }
        )
    return out


def main() -> None:
    configure_utc_logging()
    args = parse_args()
    template_module = load_template_module()
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
        else resolve_path(
            cfg_get(
                config,
                "calibration.template_walk_forward.output_csv",
                "../output/med_devices_reports/calibration/med_device_template_walk_forward_results.csv",
            ),
            base_dir=base_dir,
        )
    )
    summary_csv = (
        args.summary_csv.expanduser().resolve()
        if args.summary_csv
        else resolve_path(
            cfg_get(
                config,
                "calibration.template_walk_forward.summary_csv",
                "../output/med_devices_reports/calibration/med_device_template_walk_forward_summary.csv",
            ),
            base_dir=base_dir,
        )
    )
    recommendation_csv = (
        args.recommendation_csv.expanduser().resolve()
        if args.recommendation_csv
        else resolve_path(
            cfg_get(
                config,
                "calibration.template_walk_forward.recommendation_csv",
                "../output/med_devices_reports/calibration/med_device_template_walk_forward_recommendations.csv",
            ),
            base_dir=base_dir,
        )
    )
    rows = read_csv(input_csv)
    horizons = parse_int_list(args.horizons) or parse_int_list(
        cfg_get(config, "calibration.template_walk_forward.horizons", "30,60,120")
    )
    horizons = [horizon for horizon in horizons if horizon in available_horizons(rows)]
    folds = build_folds(rows, config)
    by_cohort: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_cohort[str(row.get("calibration_cohort") or "")].append(row)

    result_rows: list[dict[str, Any]] = []
    for cohort in sorted({template.cohort for template in template_module.templates()}):
        cohort_rows = by_cohort.get(cohort, [])
        if not cohort_rows:
            continue
        baseline_all = template_module.baseline_rows(cohort_rows)
        simulated_by_template = {
            template.template_id: template_module.simulated_rows(cohort_rows, template)
            for template in template_module.templates()
            if template.cohort == cohort
        }
        full_tickers_rows = cohort_rows
        for fold in folds:
            baseline_train = rows_in_range(baseline_all, fold.train_start, fold.train_end)
            baseline_validation = rows_in_range(baseline_all, fold.validation_start, fold.validation_end)
            for horizon in horizons:
                for split, split_rows in (("train", baseline_train), ("validation", baseline_validation)):
                    result_rows.append(
                        evaluate_fold_row(
                            cohort=cohort,
                            template_id="baseline_existing",
                            horizon=horizon,
                            fold=fold,
                            split=split,
                            candidate_rows=split_rows,
                            baseline_rows=split_rows,
                            full_rows=full_tickers_rows,
                            config=config,
                            weights="",
                        )
                    )
                for template in [template for template in template_module.templates() if template.cohort == cohort]:
                    simulated_all = simulated_by_template[template.template_id]
                    candidate_train = rows_in_range(simulated_all, fold.train_start, fold.train_end)
                    candidate_validation = rows_in_range(simulated_all, fold.validation_start, fold.validation_end)
                    for split, candidate_rows, base_rows in (
                        ("train", candidate_train, baseline_train),
                        ("validation", candidate_validation, baseline_validation),
                    ):
                        result_rows.append(
                            evaluate_fold_row(
                                cohort=cohort,
                                template_id=template.template_id,
                                horizon=horizon,
                                fold=fold,
                                split=split,
                                candidate_rows=candidate_rows,
                                baseline_rows=base_rows,
                                full_rows=full_tickers_rows,
                                config=config,
                                weights=weights_spec(template),
                            )
                        )

    summary_rows = summarize(result_rows, config)
    recommendation_rows = recommendations(summary_rows)
    for row in result_rows:
        for field in RESULT_FIELDS:
            if field in {
                "calibration_cohort",
                "template_id",
                "fold_id",
                "split",
                "train_start",
                "train_end",
                "validation_start",
                "validation_end",
                "fold_status",
                "fold_reason",
                "weights_spec",
            }:
                continue
            row[field] = fmt(row.get(field))
    for row in summary_rows:
        for field in SUMMARY_FIELDS:
            if field in {"calibration_cohort", "template_id", "recommendation_status", "recommendation_reason", "weights_spec"}:
                continue
            row[field] = fmt(row.get(field))
    for row in recommendation_rows:
        for field in RECOMMENDATION_FIELDS:
            if field in {"calibration_cohort", "recommended_template_id", "promotion_status", "promotion_reason", "weights_spec"}:
                continue
            row[field] = fmt(row.get(field))
    write_csv(output_csv, result_rows, RESULT_FIELDS)
    write_csv(summary_csv, summary_rows, SUMMARY_FIELDS)
    write_csv(recommendation_csv, recommendation_rows, RECOMMENDATION_FIELDS)
    print(f"template_walk_forward_results_csv={output_csv} rows={len(result_rows)} folds={len(folds)}")
    print(f"template_walk_forward_summary_csv={summary_csv} rows={len(summary_rows)}")
    print(f"template_walk_forward_recommendations_csv={recommendation_csv} rows={len(recommendation_rows)}")


if __name__ == "__main__":
    main()
