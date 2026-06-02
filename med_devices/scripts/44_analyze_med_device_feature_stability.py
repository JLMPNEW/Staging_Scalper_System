#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
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
DEFAULT_COMPONENTS = [
    "raw_composite_score",
    "cohort_percentile",
    "fundamental_quality_score",
    "durable_growth_score",
    "fda_product_score",
    "reimbursement_score",
    "valuation_score",
    "technical_entry_score",
    "technical_setup_score",
    "technical_core_score",
    "technical_alpha_score",
    "technical_pullback_score",
    "sentiment_catalyst_score",
    "value_trap_score",
    "data_completeness_score",
    "liquidity_score",
]
DETAIL_FIELDS = [
    "calibration_cohort",
    "component",
    "horizon_days",
    "fold_id",
    "split",
    "window_start",
    "window_end",
    "eligible_count",
    "count",
    "missing_count",
    "coverage_pct",
    "unique_tickers",
    "unique_asof_dates",
    "mean_score",
    "median_score",
    "std_score",
    "pooled_spearman_ic",
    "pooled_pearson_ic",
    "cross_sectional_ic_count",
    "mean_cross_sectional_spearman_ic",
    "median_cross_sectional_spearman_ic",
    "lcb_cross_sectional_spearman_ic",
    "positive_ic_date_rate",
    "negative_ic_date_rate",
    "top_bucket_count",
    "top_bucket_unique_tickers",
    "top_bucket_median_excess",
    "top_bucket_lcb_excess",
    "top_bucket_hit_rate",
    "bottom_bucket_count",
    "bottom_bucket_unique_tickers",
    "bottom_bucket_median_excess",
    "bottom_bucket_lcb_excess",
    "bottom_bucket_hit_rate",
    "top_minus_bottom_median_excess",
    "fold_action",
    "fold_reason",
]
SUMMARY_FIELDS = [
    "calibration_cohort",
    "component",
    "horizon_days",
    "validation_fold_count",
    "mean_coverage_pct",
    "min_coverage_pct",
    "mean_unique_tickers",
    "mean_cross_sectional_ic",
    "median_cross_sectional_ic",
    "min_lcb_cross_sectional_ic",
    "mean_top_minus_bottom_median_excess",
    "positive_alpha_fold_count",
    "inverse_alpha_fold_count",
    "repair_data_fold_count",
    "neutralize_fold_count",
    "risk_gate_fold_count",
    "recommended_action",
    "recommendation_reason",
]
RECOMMENDATION_FIELDS = [
    "calibration_cohort",
    "component",
    "recommended_action",
    "best_horizon_days",
    "support_fold_count",
    "validation_fold_count",
    "mean_coverage_pct",
    "mean_cross_sectional_ic",
    "min_lcb_cross_sectional_ic",
    "mean_top_minus_bottom_median_excess",
    "recommendation_reason",
]


@dataclass(frozen=True)
class Fold:
    fold_id: str
    train_start: date
    train_end: date
    validation_start: date
    validation_end: date


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze med-device feature stability by cohort and walk-forward fold.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--input-csv", type=Path, default=None)
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--summary-csv", type=Path, default=None)
    parser.add_argument("--recommendation-csv", type=Path, default=None)
    parser.add_argument("--horizons", type=str, default="")
    parser.add_argument("--components", type=str, default="")
    return parser.parse_args()


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


def parse_str_list(raw: object) -> list[str]:
    return [item.strip() for item in str(raw or "").split(",") if item.strip()]


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
    train_months = int(cfg_get(config, "calibration.feature_stability.train_months", cfg_get(config, "calibration.template_walk_forward.train_months", 12)))
    validation_months = int(cfg_get(config, "calibration.feature_stability.validation_months", cfg_get(config, "calibration.template_walk_forward.validation_months", 3)))
    step_months = int(cfg_get(config, "calibration.feature_stability.step_months", cfg_get(config, "calibration.template_walk_forward.step_months", validation_months)))
    embargo_days = int(cfg_get(config, "calibration.feature_stability.embargo_days", cfg_get(config, "calibration.template_walk_forward.embargo_days", 120)))
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


def rows_in_range(rows: list[dict[str, str]], start: date, end: date) -> list[dict[str, str]]:
    return [
        row for row in rows
        if row.get("asof_date") and start <= parse_date(row.get("asof_date")) <= end
    ]


def fractional_rank(values: list[float]) -> list[float]:
    indexed = sorted(enumerate(values), key=lambda item: item[1])
    ranks = [0.0] * len(values)
    idx = 0
    while idx < len(indexed):
        end = idx
        while end + 1 < len(indexed) and indexed[end + 1][1] == indexed[idx][1]:
            end += 1
        avg_rank = (idx + end) / 2.0 + 1.0
        for pos in range(idx, end + 1):
            ranks[indexed[pos][0]] = avg_rank
        idx = end + 1
    return ranks


def correlation(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 3 or len(xs) != len(ys):
        return None
    mx = mean(xs)
    my = mean(ys)
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if sx <= 1e-12 or sy <= 1e-12:
        return 0.0
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return max(-1.0, min(1.0, cov / (sx * sy)))


def spearman(xs: list[float], ys: list[float]) -> float | None:
    return correlation(fractional_rank(xs), fractional_rank(ys))


def stddev(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    avg = mean(values)
    return math.sqrt(sum((value - avg) ** 2 for value in values) / (len(values) - 1))


def lcb(values: list[float], z: float = 1.64) -> float | None:
    if not values:
        return None
    if len(values) < 2:
        return values[0]
    deviation = stddev(values)
    if deviation is None:
        return values[0]
    return mean(values) - z * deviation / math.sqrt(len(values))


def hit_rate(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(1 for value in values if value > 0) / len(values)


def scored_pairs(rows: list[dict[str, str]], *, component: str, horizon: int) -> tuple[int, list[tuple[dict[str, str], float, float]]]:
    eligible = [
        row for row in rows
        if to_float(row.get(f"cohort_excess_return_{horizon}d")) is not None
    ]
    pairs: list[tuple[dict[str, str], float, float]] = []
    for row in eligible:
        score = to_float(row.get(component))
        excess = to_float(row.get(f"cohort_excess_return_{horizon}d"))
        if score is not None and excess is not None:
            pairs.append((row, score, excess))
    return len(eligible), pairs


def cross_sectional_ics(
    pairs: list[tuple[dict[str, str], float, float]],
    *,
    min_obs: int,
) -> list[float]:
    by_date: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for row, score, excess in pairs:
        by_date[str(row.get("asof_date") or "")[:10]].append((score, excess))
    out: list[float] = []
    for items in by_date.values():
        if len(items) < min_obs:
            continue
        ic = spearman([item[0] for item in items], [item[1] for item in items])
        if ic is not None:
            out.append(ic)
    return out


def bucket(values: list[tuple[dict[str, str], float, float]], *, top: bool, pct: float) -> list[tuple[dict[str, str], float, float]]:
    if not values:
        return []
    count = max(1, int(math.ceil(len(values) * pct)))
    sorted_values = sorted(values, key=lambda item: item[1], reverse=top)
    return sorted_values[:count]


def fold_action(
    *,
    component: str,
    eligible_count: int,
    count: int,
    unique_tickers: int,
    coverage: float,
    mean_cs_ic: float | None,
    ic_lcb: float | None,
    spread: float | None,
    config: dict[str, Any],
) -> tuple[str, str]:
    min_count = int(cfg_get(config, "calibration.feature_stability.min_obs", 50))
    min_unique = int(cfg_get(config, "calibration.feature_stability.min_unique_tickers", 3))
    min_coverage = float(cfg_get(config, "calibration.feature_stability.min_coverage_pct", 0.80))
    min_abs_ic = float(cfg_get(config, "calibration.feature_stability.min_abs_cross_sectional_ic", 0.05))
    reasons: list[str] = []
    if eligible_count == 0:
        return "repair_data", "no_return_observations"
    if coverage < min_coverage:
        reasons.append("low_coverage")
    if count < min_count:
        reasons.append("insufficient_observations")
    if unique_tickers < min_unique:
        reasons.append("insufficient_unique_tickers")
    if reasons:
        return "repair_data", ";".join(reasons)
    if mean_cs_ic is None or ic_lcb is None or spread is None:
        return "neutralize", "insufficient_cross_sectional_ic"
    if mean_cs_ic >= min_abs_ic and spread > 0 and ic_lcb > 0:
        return "positive_alpha", "positive_ic_positive_spread_positive_lcb"
    if mean_cs_ic <= -min_abs_ic and spread < 0 and ic_lcb < 0:
        return "inverse_alpha", "negative_ic_negative_spread_negative_lcb"
    if component == "value_trap_score":
        return "risk_gate_only", "value_trap_component_not_alpha_stable"
    return "neutralize", "weak_or_unstable_signal"


def analyze_fold_component(
    rows: list[dict[str, str]],
    *,
    cohort: str,
    component: str,
    horizon: int,
    fold_id: str,
    split: str,
    window_start: date,
    window_end: date,
    config: dict[str, Any],
) -> dict[str, Any]:
    eligible_count, pairs = scored_pairs(rows, component=component, horizon=horizon)
    scores = [score for _, score, _ in pairs]
    excess_values = [excess for _, _, excess in pairs]
    unique_tickers = len({str(row.get("ticker") or "") for row, _, _ in pairs})
    unique_asofs = len({str(row.get("asof_date") or "")[:10] for row, _, _ in pairs})
    coverage = len(pairs) / eligible_count if eligible_count else 0.0
    min_cs_obs = int(cfg_get(config, "calibration.feature_stability.min_cross_sectional_obs", 5))
    cs_ics = cross_sectional_ics(pairs, min_obs=min_cs_obs)
    top_values = bucket(pairs, top=True, pct=float(cfg_get(config, "calibration.feature_stability.bucket_pct", 0.20)))
    bottom_values = bucket(pairs, top=False, pct=float(cfg_get(config, "calibration.feature_stability.bucket_pct", 0.20)))
    top_excess = [item[2] for item in top_values]
    bottom_excess = [item[2] for item in bottom_values]
    top_med = median(top_excess) if top_excess else None
    bottom_med = median(bottom_excess) if bottom_excess else None
    spread = top_med - bottom_med if top_med is not None and bottom_med is not None else None
    mean_cs_ic = mean(cs_ics) if cs_ics else None
    ic_lcb = lcb(cs_ics)
    action, reason = fold_action(
        component=component,
        eligible_count=eligible_count,
        count=len(pairs),
        unique_tickers=unique_tickers,
        coverage=coverage,
        mean_cs_ic=mean_cs_ic,
        ic_lcb=ic_lcb,
        spread=spread,
        config=config,
    )
    return {
        "calibration_cohort": cohort,
        "component": component,
        "horizon_days": horizon,
        "fold_id": fold_id,
        "split": split,
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "eligible_count": eligible_count,
        "count": len(pairs),
        "missing_count": max(0, eligible_count - len(pairs)),
        "coverage_pct": coverage,
        "unique_tickers": unique_tickers,
        "unique_asof_dates": unique_asofs,
        "mean_score": mean(scores) if scores else None,
        "median_score": median(scores) if scores else None,
        "std_score": stddev(scores),
        "pooled_spearman_ic": spearman(scores, excess_values),
        "pooled_pearson_ic": correlation(scores, excess_values),
        "cross_sectional_ic_count": len(cs_ics),
        "mean_cross_sectional_spearman_ic": mean_cs_ic,
        "median_cross_sectional_spearman_ic": median(cs_ics) if cs_ics else None,
        "lcb_cross_sectional_spearman_ic": ic_lcb,
        "positive_ic_date_rate": hit_rate(cs_ics),
        "negative_ic_date_rate": sum(1 for value in cs_ics if value < 0) / len(cs_ics) if cs_ics else None,
        "top_bucket_count": len(top_values),
        "top_bucket_unique_tickers": len({str(row.get("ticker") or "") for row, _, _ in top_values}),
        "top_bucket_median_excess": top_med,
        "top_bucket_lcb_excess": lcb(top_excess),
        "top_bucket_hit_rate": hit_rate(top_excess),
        "bottom_bucket_count": len(bottom_values),
        "bottom_bucket_unique_tickers": len({str(row.get("ticker") or "") for row, _, _ in bottom_values}),
        "bottom_bucket_median_excess": bottom_med,
        "bottom_bucket_lcb_excess": lcb(bottom_excess),
        "bottom_bucket_hit_rate": hit_rate(bottom_excess),
        "top_minus_bottom_median_excess": spread,
        "fold_action": action,
        "fold_reason": reason,
    }


def summarize(detail_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, int], list[dict[str, Any]]] = defaultdict(list)
    for row in detail_rows:
        if row["split"] == "validation":
            grouped[(str(row["calibration_cohort"]), str(row["component"]), int(row["horizon_days"]))].append(row)
    out: list[dict[str, Any]] = []
    for (cohort, component, horizon), items in sorted(grouped.items()):
        coverage = [float(row["coverage_pct"]) for row in items]
        unique_tickers = [float(row["unique_tickers"]) for row in items]
        cs_ic = [float(row["mean_cross_sectional_spearman_ic"]) for row in items if row["mean_cross_sectional_spearman_ic"] is not None]
        ic_lcb = [float(row["lcb_cross_sectional_spearman_ic"]) for row in items if row["lcb_cross_sectional_spearman_ic"] is not None]
        spreads = [float(row["top_minus_bottom_median_excess"]) for row in items if row["top_minus_bottom_median_excess"] is not None]
        action_counts = defaultdict(int)
        for row in items:
            action_counts[str(row["fold_action"])] += 1
        recommended_action, reason = summary_action(action_counts, items)
        out.append(
            {
                "calibration_cohort": cohort,
                "component": component,
                "horizon_days": horizon,
                "validation_fold_count": len(items),
                "mean_coverage_pct": mean(coverage) if coverage else 0.0,
                "min_coverage_pct": min(coverage) if coverage else 0.0,
                "mean_unique_tickers": mean(unique_tickers) if unique_tickers else 0.0,
                "mean_cross_sectional_ic": mean(cs_ic) if cs_ic else None,
                "median_cross_sectional_ic": median(cs_ic) if cs_ic else None,
                "min_lcb_cross_sectional_ic": min(ic_lcb) if ic_lcb else None,
                "mean_top_minus_bottom_median_excess": mean(spreads) if spreads else None,
                "positive_alpha_fold_count": action_counts["positive_alpha"],
                "inverse_alpha_fold_count": action_counts["inverse_alpha"],
                "repair_data_fold_count": action_counts["repair_data"],
                "neutralize_fold_count": action_counts["neutralize"],
                "risk_gate_fold_count": action_counts["risk_gate_only"],
                "recommended_action": recommended_action,
                "recommendation_reason": reason,
            }
        )
    return out


def summary_action(action_counts: dict[str, int], rows: list[dict[str, Any]]) -> tuple[str, str]:
    fold_count = len(rows)
    if not rows:
        return "neutralize", "no_validation_folds"
    if action_counts.get("repair_data", 0) >= math.ceil(fold_count / 2):
        return "repair_data", "data_coverage_or_observation_failures_dominate"
    if action_counts.get("positive_alpha", 0) >= math.ceil(fold_count * 0.6):
        return "use_as_positive_alpha", "positive_alpha_stable_across_folds"
    if action_counts.get("inverse_alpha", 0) >= math.ceil(fold_count * 0.6):
        return "test_inverse_alpha", "inverse_alpha_stable_across_folds"
    if action_counts.get("risk_gate_only", 0) >= math.ceil(fold_count / 2):
        return "risk_gate_only", "risk_gate_behavior_more_stable_than_alpha"
    return "neutralize", "unstable_or_weak_across_folds"


def recommendations(summary_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in summary_rows:
        grouped[(str(row["calibration_cohort"]), str(row["component"]))].append(row)
    out: list[dict[str, Any]] = []
    for (cohort, component), items in sorted(grouped.items()):
        priority = {
            "use_as_positive_alpha": 4,
            "test_inverse_alpha": 3,
            "risk_gate_only": 2,
            "repair_data": 1,
            "neutralize": 0,
        }
        items.sort(
            key=lambda row: (
                priority.get(str(row["recommended_action"]), 0),
                abs(float(row["mean_cross_sectional_ic"] or 0.0)),
                abs(float(row["mean_top_minus_bottom_median_excess"] or 0.0)),
            ),
            reverse=True,
        )
        best = items[0]
        support_field = {
            "use_as_positive_alpha": "positive_alpha_fold_count",
            "test_inverse_alpha": "inverse_alpha_fold_count",
            "risk_gate_only": "risk_gate_fold_count",
            "repair_data": "repair_data_fold_count",
            "neutralize": "neutralize_fold_count",
        }.get(str(best["recommended_action"]), "neutralize_fold_count")
        out.append(
            {
                "calibration_cohort": cohort,
                "component": component,
                "recommended_action": best["recommended_action"],
                "best_horizon_days": best["horizon_days"],
                "support_fold_count": best[support_field],
                "validation_fold_count": best["validation_fold_count"],
                "mean_coverage_pct": best["mean_coverage_pct"],
                "mean_cross_sectional_ic": best["mean_cross_sectional_ic"],
                "min_lcb_cross_sectional_ic": best["min_lcb_cross_sectional_ic"],
                "mean_top_minus_bottom_median_excess": best["mean_top_minus_bottom_median_excess"],
                "recommendation_reason": best["recommendation_reason"],
            }
        )
    return out


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
        else resolve_path(
            cfg_get(
                config,
                "calibration.feature_stability.output_csv",
                "../output/med_devices_reports/calibration/med_device_feature_stability_by_fold.csv",
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
                "calibration.feature_stability.summary_csv",
                "../output/med_devices_reports/calibration/med_device_feature_stability_summary.csv",
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
                "calibration.feature_stability.recommendation_csv",
                "../output/med_devices_reports/calibration/med_device_feature_stability_recommendations.csv",
            ),
            base_dir=base_dir,
        )
    )
    rows = read_csv(input_csv)
    horizons = parse_int_list(args.horizons) or parse_int_list(
        cfg_get(config, "calibration.feature_stability.horizons", "30,60,120")
    )
    horizons = [horizon for horizon in horizons if horizon in available_horizons(rows)]
    components = parse_str_list(args.components) or parse_str_list(
        cfg_get(config, "calibration.feature_stability.components", ",".join(DEFAULT_COMPONENTS))
    )
    if rows:
        available = set(rows[0])
        components = [component for component in components if component in available]
    folds = build_folds(rows, config)
    cohorts = sorted({str(row.get("calibration_cohort") or "") for row in rows if str(row.get("calibration_cohort") or "")})
    detail_rows: list[dict[str, Any]] = []
    for cohort in cohorts:
        cohort_rows = [row for row in rows if str(row.get("calibration_cohort") or "") == cohort]
        for fold in folds:
            split_rows = {
                "train": rows_in_range(cohort_rows, fold.train_start, fold.train_end),
                "validation": rows_in_range(cohort_rows, fold.validation_start, fold.validation_end),
            }
            split_windows = {
                "train": (fold.train_start, fold.train_end),
                "validation": (fold.validation_start, fold.validation_end),
            }
            for split, items in split_rows.items():
                window_start, window_end = split_windows[split]
                for horizon in horizons:
                    for component in components:
                        detail_rows.append(
                            analyze_fold_component(
                                items,
                                cohort=cohort,
                                component=component,
                                horizon=horizon,
                                fold_id=fold.fold_id,
                                split=split,
                                window_start=window_start,
                                window_end=window_end,
                                config=config,
                            )
                        )
    summary_rows = summarize(detail_rows)
    recommendation_rows = recommendations(summary_rows)
    for row in detail_rows:
        for field in DETAIL_FIELDS:
            if field in {
                "calibration_cohort",
                "component",
                "fold_id",
                "split",
                "window_start",
                "window_end",
                "fold_action",
                "fold_reason",
            }:
                continue
            row[field] = fmt(row.get(field))
    for row in summary_rows:
        for field in SUMMARY_FIELDS:
            if field in {"calibration_cohort", "component", "recommended_action", "recommendation_reason"}:
                continue
            row[field] = fmt(row.get(field))
    for row in recommendation_rows:
        for field in RECOMMENDATION_FIELDS:
            if field in {"calibration_cohort", "component", "recommended_action", "recommendation_reason"}:
                continue
            row[field] = fmt(row.get(field))
    write_csv(output_csv, detail_rows, DETAIL_FIELDS)
    write_csv(summary_csv, summary_rows, SUMMARY_FIELDS)
    write_csv(recommendation_csv, recommendation_rows, RECOMMENDATION_FIELDS)
    print(f"feature_stability_by_fold_csv={output_csv} rows={len(detail_rows)} folds={len(folds)}")
    print(f"feature_stability_summary_csv={summary_csv} rows={len(summary_rows)}")
    print(f"feature_stability_recommendation_csv={recommendation_csv} rows={len(recommendation_rows)}")


if __name__ == "__main__":
    main()
