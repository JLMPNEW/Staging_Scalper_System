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
EXCLUDED_COMPONENTS = {
    "raw_composite_score",
    "cohort_percentile",
    "data_completeness_score",
    "liquidity_score",
}
RESULT_FIELDS = [
    "calibration_cohort",
    "candidate_id",
    "horizon_days",
    "fold_id",
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
    "component_spec",
]
SUMMARY_FIELDS = [
    "calibration_cohort",
    "candidate_id",
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
    "mean_validation_excess_hit_rate",
    "mean_selected_ticker_coverage",
    "mean_improved_selected_ticker_rate",
    "objective_score",
    "candidate_status",
    "candidate_reason",
    "component_spec",
]
RECOMMENDATION_FIELDS = [
    "calibration_cohort",
    "recommended_candidate_id",
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
    "objective_score",
    "promotion_reason",
    "component_spec",
]


@dataclass(frozen=True)
class Fold:
    fold_id: str
    train_start: date
    train_end: date
    validation_start: date
    validation_end: date


@dataclass(frozen=True)
class ComponentPolicy:
    component: str
    direction: str
    horizon: int
    support_folds: int
    coverage: float
    ic: float
    spread: float


@dataclass(frozen=True)
class Candidate:
    cohort: str
    candidate_id: str
    horizon: int
    components: tuple[tuple[str, str, float], ...]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Optimize med-device component weights from stable component policies.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--input-csv", type=Path, default=None)
    parser.add_argument("--policy-csv", type=Path, default=None)
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--summary-csv", type=Path, default=None)
    parser.add_argument("--recommendation-csv", type=Path, default=None)
    parser.add_argument("--rejected-csv", type=Path, default=None)
    parser.add_argument("--horizons", type=str, default="")
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


def score_or(raw: object, default: float = 50.0) -> float:
    value = to_float(raw)
    return default if value is None else max(0.0, min(100.0, value))


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


def parse_str_set(raw: object) -> set[str]:
    return {item.strip() for item in str(raw or "").split(",") if item.strip()}


def cfg_bool(config: dict[str, Any], path: str, default: bool) -> bool:
    return str(cfg_get(config, path, default)).strip().lower() not in {"0", "false", "no", "off"}


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


def build_folds(rows: list[dict[str, str]], config: dict[str, Any]) -> list[Fold]:
    asof_dates = sorted({parse_date(row.get("asof_date")) for row in rows if row.get("asof_date")})
    if not asof_dates:
        return []
    min_date = asof_dates[0]
    max_date = asof_dates[-1]
    train_months = int(cfg_get(config, "calibration.component_weight_optimizer.train_months", cfg_get(config, "calibration.template_walk_forward.train_months", 12)))
    validation_months = int(cfg_get(config, "calibration.component_weight_optimizer.validation_months", cfg_get(config, "calibration.template_walk_forward.validation_months", 3)))
    step_months = int(cfg_get(config, "calibration.component_weight_optimizer.step_months", cfg_get(config, "calibration.template_walk_forward.step_months", validation_months)))
    embargo_days = int(cfg_get(config, "calibration.component_weight_optimizer.embargo_days", cfg_get(config, "calibration.template_walk_forward.embargo_days", 120)))
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


def component_score(row: dict[str, Any], field: str, direction: str) -> float:
    value = score_or(row.get(field), 50.0)
    if direction == "positive":
        return value
    if direction == "inverse":
        return 100.0 - value
    raise ValueError(f"Unknown component direction: {direction}")


def candidate_score(row: dict[str, Any], candidate: Candidate) -> float:
    total = sum(weight for _, _, weight in candidate.components)
    if total <= 0:
        return 50.0
    value = sum(
        component_score(row, field, direction) * weight
        for field, direction, weight in candidate.components
    ) / total
    return round(max(0.0, min(100.0, value)), 6)


def rank_bucket_for_group(rows: list[dict[str, Any]], *, score_field: str) -> None:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("asof_date") or "")].append(row)
    for items in grouped.values():
        sortable = sorted(items, key=lambda row: (-float(row[score_field]), str(row.get("ticker") or "")))
        if len(sortable) == 1:
            sortable[0]["sim_cohort_percentile"] = 50.0
            sortable[0]["sim_cohort_rank_bucket"] = "cohort_middle"
            continue
        denom = len(sortable) - 1
        for pos, row in enumerate(sortable):
            percentile = round(100.0 * (1.0 - pos / denom), 2)
            row["sim_cohort_percentile"] = percentile
            if percentile >= 90.0:
                bucket = "cohort_top_decile"
            elif percentile >= 80.0:
                bucket = "cohort_top_quintile_ex_decile"
            elif percentile <= 20.0:
                bucket = "cohort_bottom_quintile"
            else:
                bucket = "cohort_middle"
            row["sim_cohort_rank_bucket"] = bucket


def baseline_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = [dict(row) for row in rows]
    for row in out:
        row["sim_score"] = score_or(row.get("raw_composite_score"), score_or(row.get("composite_score"), 50.0))
        row["sim_cohort_rank_bucket"] = row.get("cohort_rank_bucket") or ""
        row["sim_cohort_percentile"] = row.get("cohort_percentile") or ""
    return out


def simulated_rows(rows: list[dict[str, Any]], candidate: Candidate) -> list[dict[str, Any]]:
    out = [dict(row) for row in rows]
    for row in out:
        row["sim_score"] = candidate_score(row, candidate)
    rank_bucket_for_group(out, score_field="sim_score")
    return out


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


def load_policies(path: Path, config: dict[str, Any], horizons: set[int]) -> dict[tuple[str, int], list[ComponentPolicy]]:
    min_support = int(cfg_get(config, "calibration.component_weight_optimizer.min_policy_support_folds", 3))
    min_coverage = float(cfg_get(config, "calibration.component_weight_optimizer.min_policy_coverage_pct", 0.60))
    excluded = set(EXCLUDED_COMPONENTS)
    excluded.update(parse_str_set(cfg_get(config, "calibration.component_weight_optimizer.excluded_components", "")))
    out: dict[tuple[str, int], list[ComponentPolicy]] = defaultdict(list)
    for row in read_csv(path):
        action = str(row.get("recommended_action") or "").strip()
        if action not in {"use_as_positive_alpha", "test_inverse_alpha"}:
            continue
        component = str(row.get("component") or "").strip()
        if not component or component in excluded:
            continue
        if component == "value_trap_score" and action != "test_inverse_alpha":
            continue
        horizon_value = to_float(row.get("horizon_days"))
        if horizon_value is None:
            continue
        horizon = int(horizon_value)
        if horizon not in horizons:
            continue
        support = int(to_float(row.get("positive_alpha_fold_count" if action == "use_as_positive_alpha" else "inverse_alpha_fold_count")) or 0)
        coverage = to_float(row.get("mean_coverage_pct")) or 0.0
        if support < min_support or coverage < min_coverage:
            continue
        direction = "positive" if action == "use_as_positive_alpha" else "inverse"
        out[(str(row.get("calibration_cohort") or ""), horizon)].append(
            ComponentPolicy(
                component=component,
                direction=direction,
                horizon=horizon,
                support_folds=support,
                coverage=coverage,
                ic=abs(to_float(row.get("mean_cross_sectional_ic")) or 0.0),
                spread=abs(to_float(row.get("mean_top_minus_bottom_median_excess")) or 0.0),
            )
        )
    for key, values in out.items():
        values.sort(key=policy_quality, reverse=True)
    return out


def policy_quality(policy: ComponentPolicy) -> tuple[float, float, float, float]:
    return (float(policy.support_folds), policy.coverage, policy.ic, policy.spread)


def normalize_weights(items: list[tuple[str, str, float]]) -> tuple[tuple[str, str, float], ...]:
    total = sum(weight for _, _, weight in items)
    if total <= 0:
        return tuple()
    return tuple((field, direction, round(weight / total, 6)) for field, direction, weight in items)


def generate_candidates(policies: dict[tuple[str, int], list[ComponentPolicy]], config: dict[str, Any]) -> list[Candidate]:
    max_components = int(cfg_get(config, "calibration.component_weight_optimizer.max_components_per_candidate", 5))
    max_policies = int(cfg_get(config, "calibration.component_weight_optimizer.max_policy_components", 7))
    candidates: list[Candidate] = []
    seen: set[tuple[str, int, tuple[tuple[str, str, float], ...]]] = set()
    for (cohort, horizon), raw_policies in sorted(policies.items()):
        usable = raw_policies[:max_policies]
        if len(usable) < 2:
            continue
        candidate_no = 1
        for k in range(2, min(max_components, len(usable)) + 1):
            selected = usable[:k]
            specs: list[tuple[tuple[str, str, float], ...]] = []
            specs.append(normalize_weights([(item.component, item.direction, 1.0) for item in selected]))
            front = [(selected[0].component, selected[0].direction, 0.45)]
            front.extend((item.component, item.direction, 0.55 / (k - 1)) for item in selected[1:])
            specs.append(normalize_weights(front))
            quality = [
                (item.component, item.direction, max(0.01, item.ic + abs(item.spread) + 0.05 * item.support_folds))
                for item in selected
            ]
            specs.append(normalize_weights(quality))
            for components in specs:
                key = (cohort, horizon, components)
                if not components or key in seen:
                    continue
                seen.add(key)
                candidates.append(
                    Candidate(
                        cohort=cohort,
                        candidate_id=f"{cohort}_{horizon}d_w{candidate_no:03d}",
                        horizon=horizon,
                        components=components,
                    )
                )
                candidate_no += 1
    return candidates


def component_spec(candidate: Candidate) -> str:
    return ";".join(f"{field}:{direction}:{weight:.4f}" for field, direction, weight in candidate.components)


def evaluate_fold(
    *,
    candidate: Candidate,
    fold: Fold,
    candidate_rows: list[dict[str, Any]],
    baseline_rows_for_fold: list[dict[str, Any]],
    full_rows: list[dict[str, Any]],
    config: dict[str, Any],
) -> dict[str, Any]:
    candidate_metrics = metrics(candidate_rows, horizon=candidate.horizon, full_rows=full_rows)
    baseline_metrics = metrics(baseline_rows_for_fold, horizon=candidate.horizon, full_rows=full_rows)
    row: dict[str, Any] = {
        "calibration_cohort": candidate.cohort,
        "candidate_id": candidate.candidate_id,
        "horizon_days": candidate.horizon,
        "fold_id": fold.fold_id,
        "validation_start": fold.validation_start.isoformat(),
        "validation_end": fold.validation_end.isoformat(),
        "component_spec": component_spec(candidate),
    }
    row.update(candidate_metrics)
    for field in ("mean_excess", "median_excess", "excess_hit_rate", "lcb_excess"):
        row[f"delta_{field}_vs_baseline"] = candidate_metrics[field] - baseline_metrics[field]
    candidate_means = selected_ticker_means(candidate_rows, horizon=candidate.horizon)
    baseline_means = selected_ticker_means(baseline_rows_for_fold, horizon=candidate.horizon)
    comparable = [ticker for ticker in candidate_means if ticker in baseline_means]
    improved = [ticker for ticker in comparable if candidate_means[ticker] > baseline_means[ticker]]
    improved_rate = len(improved) / len(comparable) if comparable else 0.0
    row["improved_selected_ticker_rate"] = improved_rate
    row["fold_status"], row["fold_reason"] = fold_status(row, config)
    return row


def fold_status(row: dict[str, Any], config: dict[str, Any]) -> tuple[str, str]:
    min_selected = int(cfg_get(config, "calibration.component_weight_optimizer.min_validation_selected", 10))
    min_unique = int(cfg_get(config, "calibration.component_weight_optimizer.min_validation_unique_tickers", 3))
    min_coverage = float(cfg_get(config, "calibration.component_weight_optimizer.min_selected_ticker_coverage", 0.10))
    min_hit = float(cfg_get(config, "calibration.component_weight_optimizer.min_excess_hit_rate", 0.52))
    min_improved = float(cfg_get(config, "calibration.component_weight_optimizer.min_improved_selected_ticker_rate", 0.50))
    reasons: list[str] = []
    if int(row["count"]) < min_selected:
        reasons.append("insufficient_selected_obs")
    if int(row["unique_tickers"]) < min_unique:
        reasons.append("insufficient_unique_tickers")
    if float(row["selected_ticker_coverage"]) < min_coverage:
        reasons.append("insufficient_ticker_coverage")
    if float(row["median_excess"]) <= 0:
        reasons.append("median_excess_not_positive")
    if float(row["lcb_excess"]) <= 0:
        reasons.append("lcb_excess_not_positive")
    if float(row["excess_hit_rate"]) < min_hit:
        reasons.append("excess_hit_rate_below_min")
    if float(row["delta_median_excess_vs_baseline"]) <= 0:
        reasons.append("median_excess_not_improved")
    if float(row["delta_lcb_excess_vs_baseline"]) <= 0:
        reasons.append("lcb_excess_not_improved")
    if float(row["improved_selected_ticker_rate"]) < min_improved:
        reasons.append("improved_selected_ticker_rate_below_min")
    return ("pass", "passes_component_weight_fold_checks") if not reasons else ("fail", ";".join(reasons))


def summarize(results: list[dict[str, Any]], config: dict[str, Any]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in results:
        grouped[str(row["candidate_id"])].append(row)
    min_pass_folds = int(cfg_get(config, "calibration.component_weight_optimizer.min_pass_folds", 2))
    min_pass_rate = float(cfg_get(config, "calibration.component_weight_optimizer.min_pass_fold_rate", 0.60))
    min_unique = int(cfg_get(config, "calibration.component_weight_optimizer.min_summary_unique_tickers", 3))
    summary_rows: list[dict[str, Any]] = []
    for candidate_id, items in sorted(grouped.items()):
        first = items[0]
        pass_count = sum(1 for row in items if row["fold_status"] == "pass")
        pass_rate = pass_count / len(items) if items else 0.0
        medians = [float(row["median_excess"]) for row in items]
        lcbs = [float(row["lcb_excess"]) for row in items]
        hits = [float(row["excess_hit_rate"]) for row in items]
        coverages = [float(row["selected_ticker_coverage"]) for row in items]
        improved_rates = [float(row["improved_selected_ticker_rate"]) for row in items]
        unique_tickers = max(int(row["unique_tickers"]) for row in items)
        validation_count = sum(int(row["count"]) for row in items)
        objective = (
            mean(medians)
            + 0.75 * mean(lcbs)
            + 0.20 * (mean(hits) - 0.50)
            + 0.10 * mean(improved_rates)
            - 0.01 * len(str(first["component_spec"]).split(";"))
        )
        reasons: list[str] = []
        if pass_count < min_pass_folds:
            reasons.append("insufficient_pass_folds")
        if pass_rate < min_pass_rate:
            reasons.append("pass_fold_rate_below_min")
        if unique_tickers < min_unique:
            reasons.append("insufficient_unique_tickers")
        if min(lcbs) <= 0:
            reasons.append("nonpositive_min_lcb")
        status = "candidate" if not reasons else "reject"
        summary_rows.append(
            {
                "calibration_cohort": first["calibration_cohort"],
                "candidate_id": candidate_id,
                "horizon_days": first["horizon_days"],
                "fold_count": len(items),
                "pass_fold_count": pass_count,
                "pass_fold_rate": pass_rate,
                "validation_count": validation_count,
                "validation_unique_tickers": unique_tickers,
                "mean_validation_median_excess": mean(medians),
                "min_validation_median_excess": min(medians),
                "mean_validation_lcb_excess": mean(lcbs),
                "min_validation_lcb_excess": min(lcbs),
                "mean_validation_excess_hit_rate": mean(hits),
                "mean_selected_ticker_coverage": mean(coverages),
                "mean_improved_selected_ticker_rate": mean(improved_rates),
                "objective_score": objective,
                "candidate_status": status,
                "candidate_reason": "passes_component_weight_summary_checks" if not reasons else ";".join(reasons),
                "component_spec": first["component_spec"],
            }
        )
    return summary_rows


def recommendations(summary_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_cohort: dict[str, list[dict[str, Any]]] = defaultdict(list)
    all_cohorts = sorted({str(row["calibration_cohort"]) for row in summary_rows})
    for row in summary_rows:
        if row["candidate_status"] == "candidate":
            by_cohort[str(row["calibration_cohort"])].append(row)
    out: list[dict[str, Any]] = []
    for cohort in all_cohorts:
        candidates = by_cohort.get(cohort, [])
        candidates.sort(
            key=lambda row: (
                float(row["objective_score"]),
                int(row["pass_fold_count"]),
                float(row["min_validation_lcb_excess"]),
            ),
            reverse=True,
        )
        best = candidates[0] if candidates else None
        out.append(
            {
                "calibration_cohort": cohort,
                "recommended_candidate_id": best["candidate_id"] if best else "",
                "promotion_status": "component_weight_candidate" if best else "no_component_weight_candidate",
                "horizon_days": best["horizon_days"] if best else "",
                "pass_fold_count": best["pass_fold_count"] if best else "",
                "pass_fold_rate": best["pass_fold_rate"] if best else "",
                "validation_unique_tickers": best["validation_unique_tickers"] if best else "",
                "mean_validation_median_excess": best["mean_validation_median_excess"] if best else "",
                "min_validation_lcb_excess": best["min_validation_lcb_excess"] if best else "",
                "mean_validation_excess_hit_rate": best["mean_validation_excess_hit_rate"] if best else "",
                "mean_selected_ticker_coverage": best["mean_selected_ticker_coverage"] if best else "",
                "mean_improved_selected_ticker_rate": best["mean_improved_selected_ticker_rate"] if best else "",
                "objective_score": best["objective_score"] if best else "",
                "promotion_reason": best["candidate_reason"] if best else "no candidate passed component weight checks",
                "component_spec": best["component_spec"] if best else "",
            }
        )
    return out


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
    policy_csv = (
        args.policy_csv.expanduser().resolve()
        if args.policy_csv
        else resolve_path(
            cfg_get(config, "calibration.feature_stability.summary_csv"),
            base_dir=base_dir,
        )
    )
    output_csv = (
        args.output_csv.expanduser().resolve()
        if args.output_csv
        else resolve_path(
            cfg_get(
                config,
                "calibration.component_weight_optimizer.output_csv",
                "../output/med_devices_reports/calibration/med_device_component_weight_optimizer_results.csv",
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
                "calibration.component_weight_optimizer.summary_csv",
                "../output/med_devices_reports/calibration/med_device_component_weight_optimizer_summary.csv",
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
                "calibration.component_weight_optimizer.recommendation_csv",
                "../output/med_devices_reports/calibration/med_device_component_weight_optimizer_recommendations.csv",
            ),
            base_dir=base_dir,
        )
    )
    rejected_csv = (
        args.rejected_csv.expanduser().resolve()
        if args.rejected_csv
        else resolve_path(
            cfg_get(
                config,
                "calibration.component_weight_optimizer.rejected_csv",
                "../output/med_devices_reports/calibration/med_device_component_weight_optimizer_rejected.csv",
            ),
            base_dir=base_dir,
        )
    )
    rows = read_csv(input_csv)
    horizons = parse_int_list(args.horizons) or parse_int_list(
        cfg_get(config, "calibration.component_weight_optimizer.horizons", "30,60")
    )
    horizons = [horizon for horizon in horizons if horizon in available_horizons(rows)]
    policies = load_policies(policy_csv, config, set(horizons))
    candidates = generate_candidates(policies, config)
    folds = build_folds(rows, config)
    by_cohort: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_cohort[str(row.get("calibration_cohort") or "")].append(row)

    result_rows: list[dict[str, Any]] = []
    for candidate in candidates:
        cohort_rows = by_cohort.get(candidate.cohort, [])
        if not cohort_rows:
            continue
        baseline_all = baseline_rows(cohort_rows)
        simulated_all = simulated_rows(cohort_rows, candidate)
        for fold in folds:
            result_rows.append(
                evaluate_fold(
                    candidate=candidate,
                    fold=fold,
                    candidate_rows=rows_in_range(simulated_all, fold.validation_start, fold.validation_end),
                    baseline_rows_for_fold=rows_in_range(baseline_all, fold.validation_start, fold.validation_end),
                    full_rows=cohort_rows,
                    config=config,
                )
            )
    summary_rows = summarize(result_rows, config)
    recommendation_rows = recommendations(summary_rows)
    rejected_rows = [row for row in summary_rows if row["candidate_status"] != "candidate"]

    for row in result_rows:
        for field in RESULT_FIELDS:
            if field in {
                "calibration_cohort",
                "candidate_id",
                "fold_id",
                "validation_start",
                "validation_end",
                "fold_status",
                "fold_reason",
                "component_spec",
            }:
                continue
            row[field] = fmt(row.get(field))
    for row in summary_rows:
        for field in SUMMARY_FIELDS:
            if field in {"calibration_cohort", "candidate_id", "candidate_status", "candidate_reason", "component_spec"}:
                continue
            row[field] = fmt(row.get(field))
    for row in recommendation_rows:
        for field in RECOMMENDATION_FIELDS:
            if field in {"calibration_cohort", "recommended_candidate_id", "promotion_status", "promotion_reason", "component_spec"}:
                continue
            row[field] = fmt(row.get(field))
    for row in rejected_rows:
        for field in SUMMARY_FIELDS:
            if field in {"calibration_cohort", "candidate_id", "candidate_status", "candidate_reason", "component_spec"}:
                continue
            row[field] = fmt(row.get(field))

    write_csv(output_csv, result_rows, RESULT_FIELDS)
    write_csv(summary_csv, summary_rows, SUMMARY_FIELDS)
    write_csv(recommendation_csv, recommendation_rows, RECOMMENDATION_FIELDS)
    write_csv(rejected_csv, rejected_rows, SUMMARY_FIELDS)
    print(f"component_weight_optimizer_results_csv={output_csv} rows={len(result_rows)} candidates={len(candidates)}")
    print(f"component_weight_optimizer_summary_csv={summary_csv} rows={len(summary_rows)}")
    print(f"component_weight_optimizer_recommendations_csv={recommendation_csv} rows={len(recommendation_rows)}")
    print(f"component_weight_optimizer_rejected_csv={rejected_csv} rows={len(rejected_rows)}")


if __name__ == "__main__":
    main()
