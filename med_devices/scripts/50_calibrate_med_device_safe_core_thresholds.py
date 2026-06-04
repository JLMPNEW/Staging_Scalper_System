#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import itertools
import math
import sys
from collections import defaultdict
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
RESTRICTED_CALIBRATION_STATUSES = {"restricted_research_only", "excluded_from_tier1"}
TUNABLE_TIER1_REASONS = {
    "fundamental_below_tier1_safety_min",
    "valuation_below_tier1_safety_min",
    "durable_growth_below_tier1_safety_min",
    "value_trap_above_tier1_safety_max",
    "fda_event_risk_above_tier1_safety_max",
    "market_cap_missing_or_below_tier1_min",
    "liquidity_missing_or_below_tier1_min",
}
HARD_SAFE_CORE_REASONS = {
    "fda_manual_review_required",
    "hard_red_flag",
    "value_trap_hard_gate",
    "confirmed_hard_red",
}
RESULT_FIELDS = [
    "calibration_cohort",
    "parameter_set_id",
    "horizon_days",
    "validation_start_asof",
    "validation_end_asof",
    "min_safe_core_score",
    "min_safe_core_percentile",
    "min_safe_core_cohort_percentile",
    "min_fundamental_quality",
    "min_valuation",
    "min_durable_growth",
    "min_data_completeness",
    "min_market_cap",
    "min_avg_dollar_volume_60d",
    "max_value_trap",
    "max_fda_event_risk",
    "selected_count",
    "unique_tickers",
    "selected_tickers",
    "mean_excess_return",
    "median_excess_return",
    "hit_rate",
    "loss_rate",
    "lcb_excess_return",
    "worst_loss",
    "baseline_count",
    "baseline_unique_tickers",
    "baseline_lcb_excess_return",
    "delta_lcb_excess_return",
    "objective_score",
    "pass_fail",
    "rejection_reason",
]
RECOMMENDATION_FIELDS = [
    "calibration_cohort",
    "recommendation_status",
    "recommended_parameter_set_id",
    "horizon_days",
    "selected_count",
    "unique_tickers",
    "lcb_excess_return",
    "median_excess_return",
    "hit_rate",
    "loss_rate",
    "worst_loss",
    "delta_lcb_excess_return",
    "objective_score",
    "min_safe_core_score",
    "min_safe_core_percentile",
    "min_safe_core_cohort_percentile",
    "min_fundamental_quality",
    "min_valuation",
    "min_durable_growth",
    "min_data_completeness",
    "min_market_cap",
    "min_avg_dollar_volume_60d",
    "max_value_trap",
    "max_fda_event_risk",
    "recommendation_reason",
]
NEAR_MISS_FIELDS = [
    "asof_date",
    "ticker",
    "company_name",
    "calibration_cohort",
    "classification",
    "classification_reason",
    "safe_core_score",
    "safe_core_percentile",
    "safe_core_cohort_percentile",
    "safe_core_status",
    "safe_core_reason",
    "tier1_safety_reason",
    "legacy_gate_misses",
    "hard_blockers",
    "tunable_blockers",
    "fundamental_quality_score",
    "valuation_score",
    "durable_growth_score",
    "data_completeness_score",
    "market_cap",
    "avg_dollar_volume_60d",
    "value_trap_score",
    "fda_event_risk_score",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Calibrate guarded safe-core Tier 1 threshold variants.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--input-csv", type=Path, default=None)
    parser.add_argument("--current-scores-csv", type=Path, default=None)
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--recommendation-csv", type=Path, default=None)
    parser.add_argument("--near-miss-csv", type=Path, default=None)
    parser.add_argument("--horizons", type=str, default="")
    return parser.parse_args()


def to_float(raw: object) -> float | None:
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def to_int(raw: object) -> int:
    value = to_float(raw)
    return int(value) if value is not None else 0


def parse_float_list(raw: object, default: str) -> list[float]:
    text = str(raw if raw is not None else default)
    values = [float(item.strip()) for item in text.split(",") if item.strip()]
    return sorted(dict.fromkeys(values))


def parse_int_list(raw: object, default: str) -> list[int]:
    text = str(raw if raw is not None else default)
    values = [int(item.strip()) for item in text.split(",") if item.strip()]
    return sorted(dict.fromkeys(values))


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def split_tokens(raw: object) -> set[str]:
    return {item.strip() for item in str(raw or "").split(";") if item.strip()}


def parameter_id(*values: object) -> str:
    raw = "|".join(str(value) for value in values)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def lcb(values: list[float], z: float = 1.64) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    avg = mean(values)
    variance = sum((value - avg) ** 2 for value in values) / (len(values) - 1)
    return avg - z * math.sqrt(variance / len(values))


def metrics(rows: list[dict[str, str]], *, horizon: int) -> dict[str, Any]:
    field = f"cohort_excess_return_{horizon}d"
    values: list[float] = []
    tickers: set[str] = set()
    for row in rows:
        value = to_float(row.get(field))
        if value is None:
            continue
        values.append(value)
        ticker = str(row.get("ticker") or "").strip()
        if ticker:
            tickers.add(ticker)
    if not values:
        return {
            "count": 0,
            "unique_tickers": 0,
            "mean": 0.0,
            "median": 0.0,
            "hit_rate": 0.0,
            "loss_rate": 0.0,
            "lcb": 0.0,
            "worst_loss": 0.0,
            "tickers": "",
        }
    return {
        "count": len(values),
        "unique_tickers": len(tickers),
        "mean": mean(values),
        "median": median(values),
        "hit_rate": sum(1 for value in values if value > 0) / len(values),
        "loss_rate": sum(1 for value in values if value < 0) / len(values),
        "lcb": lcb(values),
        "worst_loss": min(values),
        "tickers": ";".join(sorted(tickers)),
    }


def fmt_float(value: object, digits: int = 6) -> str:
    number = to_float(value)
    return "" if number is None else f"{number:.{digits}f}"


def hard_blockers(row: dict[str, str], *, value_trap_hard_max: float) -> set[str]:
    blockers = split_tokens(row.get("tier1_safety_reason")) - TUNABLE_TIER1_REASONS
    blockers.update(split_tokens(row.get("safe_core_reason")) & HARD_SAFE_CORE_REASONS)
    calibration_status = str(row.get("calibration_status") or "").strip().lower()
    if calibration_status in RESTRICTED_CALIBRATION_STATUSES:
        blockers.add(calibration_status)
    if str(row.get("fda_review_state") or "").strip() in MANUAL_FDA_REVIEW_STATES:
        blockers.add("fda_manual_review_required")
    if to_int(row.get("hard_red_flag")):
        blockers.add("hard_red_flag")
    if to_int(row.get("technical_breakdown_flag")):
        blockers.add("technical_breakdown")
    value_trap = to_float(row.get("value_trap_score"))
    if value_trap is not None and value_trap >= value_trap_hard_max:
        blockers.add("value_trap_hard_gate")
    return blockers


def hard_controls_pass(row: dict[str, str], *, value_trap_hard_max: float) -> bool:
    return not hard_blockers(row, value_trap_hard_max=value_trap_hard_max)


def numeric_at_least(row: dict[str, str], field: str, threshold: float) -> bool:
    value = to_float(row.get(field))
    return value is not None and value >= threshold


def numeric_at_most(row: dict[str, str], field: str, threshold: float) -> bool:
    value = to_float(row.get(field))
    return value is not None and value <= threshold


def passes_variant(row: dict[str, str], *, params: dict[str, float], value_trap_hard_max: float) -> bool:
    if not hard_controls_pass(row, value_trap_hard_max=value_trap_hard_max):
        return False
    checks = [
        ("safe_core_score", params["min_safe_core_score"]),
        ("safe_core_percentile", params["min_safe_core_percentile"]),
        ("safe_core_cohort_percentile", params["min_safe_core_cohort_percentile"]),
        ("fundamental_quality_score", params["min_fundamental_quality"]),
        ("valuation_score", params["min_valuation"]),
        ("data_completeness_score", params["min_data_completeness"]),
        ("market_cap", params["min_market_cap"]),
        ("avg_dollar_volume_60d", params["min_avg_dollar_volume_60d"]),
    ]
    if any(not numeric_at_least(row, field, threshold) for field, threshold in checks):
        return False
    if params["min_durable_growth"] > 0 and not numeric_at_least(
        row, "durable_growth_score", params["min_durable_growth"]
    ):
        return False
    if not numeric_at_most(row, "value_trap_score", params["max_value_trap"]):
        return False
    if not numeric_at_most(row, "fda_event_risk_score", params["max_fda_event_risk"]):
        return False
    return True


def baseline_pass(row: dict[str, str]) -> bool:
    return to_int(row.get("legacy_all_gates_gate")) == 1 and to_int(row.get("passed_tier1_safety_gate")) == 1


def validation_rows(
    rows: list[dict[str, str]],
    *,
    cohort: str,
    validation_start: str,
    validation_end: str,
    horizon: int,
) -> list[dict[str, str]]:
    field = f"cohort_excess_return_{horizon}d"
    return [
        row
        for row in rows
        if str(row.get("calibration_cohort") or "") == cohort
        and validation_start <= str(row.get("asof_date") or "")[:10] <= validation_end
        and to_float(row.get(field)) is not None
    ]


def objective_score(metric: dict[str, Any], baseline_metric: dict[str, Any]) -> float:
    return (
        float(metric["lcb"]) * 100.0 * 0.40
        + float(metric["median"]) * 100.0 * 0.25
        + float(metric["mean"]) * 100.0 * 0.15
        + (float(metric["hit_rate"]) - 0.50) * 20.0 * 0.10
        + (float(metric["lcb"]) - float(baseline_metric["lcb"])) * 100.0 * 0.10
    )


def evaluate_variant(
    *,
    cohort: str,
    horizon: int,
    cohort_validation: list[dict[str, str]],
    validation_start: str,
    validation_end: str,
    params: dict[str, float],
    value_trap_hard_max: float,
    min_validation_obs: int,
    min_unique_tickers: int,
    min_lcb_excess: float,
    max_loss_rate: float,
    require_lcb_delta_nonnegative: bool,
) -> dict[str, Any]:
    selected = [row for row in cohort_validation if passes_variant(row, params=params, value_trap_hard_max=value_trap_hard_max)]
    baseline = [row for row in cohort_validation if baseline_pass(row)]
    selected_metrics = metrics(selected, horizon=horizon)
    baseline_metrics = metrics(baseline, horizon=horizon)
    delta_lcb = float(selected_metrics["lcb"]) - float(baseline_metrics["lcb"])
    reasons: list[str] = []
    if int(selected_metrics["count"]) < min_validation_obs:
        reasons.append("insufficient_validation_obs")
    if int(selected_metrics["unique_tickers"]) < min_unique_tickers:
        reasons.append("insufficient_unique_tickers")
    if float(selected_metrics["lcb"]) < min_lcb_excess:
        reasons.append("lcb_below_min")
    if float(selected_metrics["loss_rate"]) > max_loss_rate:
        reasons.append("loss_rate_above_max")
    if require_lcb_delta_nonnegative and baseline_metrics["count"] and delta_lcb < 0:
        reasons.append("lcb_delta_negative_vs_legacy")
    values = [
        cohort,
        horizon,
        params["min_safe_core_score"],
        params["min_safe_core_percentile"],
        params["min_safe_core_cohort_percentile"],
        params["min_fundamental_quality"],
        params["min_valuation"],
        params["min_durable_growth"],
        params["min_data_completeness"],
        params["min_market_cap"],
        params["min_avg_dollar_volume_60d"],
        params["max_value_trap"],
        params["max_fda_event_risk"],
    ]
    return {
        "calibration_cohort": cohort,
        "parameter_set_id": parameter_id(*values),
        "horizon_days": horizon,
        "validation_start_asof": validation_start,
        "validation_end_asof": validation_end,
        **params,
        "selected_count": selected_metrics["count"],
        "unique_tickers": selected_metrics["unique_tickers"],
        "selected_tickers": selected_metrics["tickers"],
        "mean_excess_return": fmt_float(selected_metrics["mean"]),
        "median_excess_return": fmt_float(selected_metrics["median"]),
        "hit_rate": fmt_float(selected_metrics["hit_rate"], 4),
        "loss_rate": fmt_float(selected_metrics["loss_rate"], 4),
        "lcb_excess_return": fmt_float(selected_metrics["lcb"]),
        "worst_loss": fmt_float(selected_metrics["worst_loss"]),
        "baseline_count": baseline_metrics["count"],
        "baseline_unique_tickers": baseline_metrics["unique_tickers"],
        "baseline_lcb_excess_return": fmt_float(baseline_metrics["lcb"]),
        "delta_lcb_excess_return": fmt_float(delta_lcb),
        "objective_score": fmt_float(objective_score(selected_metrics, baseline_metrics), 4),
        "pass_fail": "pass" if not reasons else "fail",
        "rejection_reason": ";".join(reasons),
    }


def grid_params(config: dict[str, Any]) -> list[dict[str, float]]:
    prefix = "calibration.safe_core_threshold_sensitivity"
    grids = {
        "min_safe_core_score": parse_float_list(
            cfg_get(config, f"{prefix}.candidate_min_safe_core_score", None), "60,62,65"
        ),
        "min_safe_core_percentile": parse_float_list(
            cfg_get(config, f"{prefix}.candidate_min_safe_core_percentile", None), "78,82,86"
        ),
        "min_safe_core_cohort_percentile": parse_float_list(
            cfg_get(config, f"{prefix}.candidate_min_safe_core_cohort_percentile", None), "50,60"
        ),
        "min_fundamental_quality": parse_float_list(
            cfg_get(config, f"{prefix}.candidate_min_fundamental_quality", None), "55,60,65"
        ),
        "min_valuation": parse_float_list(cfg_get(config, f"{prefix}.candidate_min_valuation", None), "45,50,55"),
        "min_durable_growth": parse_float_list(
            cfg_get(config, f"{prefix}.candidate_min_durable_growth", None), "0,40,45"
        ),
        "min_data_completeness": parse_float_list(
            cfg_get(config, f"{prefix}.candidate_min_data_completeness", None), "80,85"
        ),
        "min_market_cap": parse_float_list(
            cfg_get(config, f"{prefix}.candidate_min_market_cap", None), "250000000,500000000"
        ),
        "min_avg_dollar_volume_60d": parse_float_list(
            cfg_get(config, f"{prefix}.candidate_min_avg_dollar_volume_60d", None), "1000000,2000000"
        ),
        "max_value_trap": parse_float_list(cfg_get(config, f"{prefix}.candidate_max_value_trap", None), "30,35"),
        "max_fda_event_risk": parse_float_list(
            cfg_get(config, f"{prefix}.candidate_max_fda_event_risk", None), "35,40"
        ),
    }
    keys = list(grids)
    return [dict(zip(keys, values)) for values in itertools.product(*(grids[key] for key in keys))]


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


def recommendations(rows: list[dict[str, Any]], *, cohorts: list[str], horizon: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    by_cohort: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if int(row["horizon_days"]) == horizon:
            by_cohort[str(row["calibration_cohort"])].append(row)
    for cohort in cohorts:
        passed = [row for row in by_cohort.get(cohort, []) if row.get("pass_fail") == "pass"]
        if not passed:
            best = max(
                by_cohort.get(cohort, []),
                key=lambda item: to_float(item.get("objective_score")) or -999999.0,
                default=None,
            )
            if best is None:
                out.append({"calibration_cohort": cohort, "recommendation_status": "no_data"})
                continue
            out.append(
                {
                    "calibration_cohort": cohort,
                    "recommendation_status": "research_only",
                    "recommended_parameter_set_id": best.get("parameter_set_id"),
                    "horizon_days": horizon,
                    "selected_count": best.get("selected_count"),
                    "unique_tickers": best.get("unique_tickers"),
                    "lcb_excess_return": best.get("lcb_excess_return"),
                    "median_excess_return": best.get("median_excess_return"),
                    "hit_rate": best.get("hit_rate"),
                    "loss_rate": best.get("loss_rate"),
                    "worst_loss": best.get("worst_loss"),
                    "delta_lcb_excess_return": best.get("delta_lcb_excess_return"),
                    "objective_score": best.get("objective_score"),
                    "recommendation_reason": best.get("rejection_reason") or "no_passing_threshold_variant",
                    **{key: best.get(key, "") for key in RECOMMENDATION_FIELDS if key.startswith("min_") or key.startswith("max_")},
                }
            )
            continue
        best = max(passed, key=lambda item: to_float(item.get("objective_score")) or -999999.0)
        out.append(
            {
                "calibration_cohort": cohort,
                "recommendation_status": "promotion_candidate",
                "recommended_parameter_set_id": best.get("parameter_set_id"),
                "horizon_days": horizon,
                "selected_count": best.get("selected_count"),
                "unique_tickers": best.get("unique_tickers"),
                "lcb_excess_return": best.get("lcb_excess_return"),
                "median_excess_return": best.get("median_excess_return"),
                "hit_rate": best.get("hit_rate"),
                "loss_rate": best.get("loss_rate"),
                "worst_loss": best.get("worst_loss"),
                "delta_lcb_excess_return": best.get("delta_lcb_excess_return"),
                "objective_score": best.get("objective_score"),
                "recommendation_reason": "passes_loss_aware_threshold_sensitivity",
                **{key: best.get(key, "") for key in RECOMMENDATION_FIELDS if key.startswith("min_") or key.startswith("max_")},
            }
        )
    return out


def current_near_misses(
    rows: list[dict[str, str]],
    *,
    min_score: float,
    value_trap_hard_max: float,
) -> list[dict[str, Any]]:
    if not rows:
        return []
    latest_asof = max(str(row.get("asof_date") or "")[:10] for row in rows)
    out: list[dict[str, Any]] = []
    for row in rows:
        if str(row.get("asof_date") or "")[:10] != latest_asof:
            continue
        score = to_float(row.get("safe_core_score"))
        if score is None or score < min_score:
            continue
        hard = hard_blockers(row, value_trap_hard_max=value_trap_hard_max)
        tunable = (split_tokens(row.get("tier1_safety_reason")) & TUNABLE_TIER1_REASONS) | (
            split_tokens(row.get("safe_core_reason"))
            & {
                "safe_core_score_below_min",
                "safe_core_percentile_below_min",
                "safe_core_cohort_percentile_below_min",
                "data_quality_below_gate",
                "liquidity_below_gate",
                "tier1_safety_failed",
            }
        )
        out.append(
            {
                **{field: row.get(field, "") for field in NEAR_MISS_FIELDS if field not in {"hard_blockers", "tunable_blockers"}},
                "hard_blockers": ";".join(sorted(hard)),
                "tunable_blockers": ";".join(sorted(tunable)),
            }
        )
    return sorted(out, key=lambda item: -(to_float(item.get("safe_core_score")) or 0.0))


def main() -> None:
    args = parse_args()
    configure_utc_logging()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    prefix = "calibration.safe_core_threshold_sensitivity"
    input_csv = (
        args.input_csv.expanduser().resolve()
        if args.input_csv is not None
        else resolve_path(
            cfg_get(
                config,
                f"{prefix}.input_csv",
                cfg_get(config, "calibration.cohort_neutral_backtest_csv", "../output/med_devices_reports/calibration/med_device_cohort_neutral_backtest.csv"),
            ),
            base_dir=base_dir,
        )
    )
    current_scores_csv = (
        args.current_scores_csv.expanduser().resolve()
        if args.current_scores_csv is not None
        else resolve_path(
            cfg_get(config, f"{prefix}.current_scores_csv", cfg_get(config, "scoring.output_csv")),
            base_dir=base_dir,
        )
    )
    output_csv = (
        args.output_csv.expanduser().resolve()
        if args.output_csv is not None
        else resolve_path(
            cfg_get(
                config,
                f"{prefix}.output_csv",
                "../output/med_devices_reports/calibration/med_device_safe_core_threshold_sensitivity.csv",
            ),
            base_dir=base_dir,
        )
    )
    recommendation_csv = (
        args.recommendation_csv.expanduser().resolve()
        if args.recommendation_csv is not None
        else resolve_path(
            cfg_get(
                config,
                f"{prefix}.recommendation_csv",
                "../output/med_devices_reports/calibration/med_device_safe_core_threshold_recommendations.csv",
            ),
            base_dir=base_dir,
        )
    )
    near_miss_csv = (
        args.near_miss_csv.expanduser().resolve()
        if args.near_miss_csv is not None
        else resolve_path(
            cfg_get(
                config,
                f"{prefix}.current_near_miss_csv",
                "../output/med_devices_reports/calibration/med_device_safe_core_current_near_misses.csv",
            ),
            base_dir=base_dir,
        )
    )
    rows = read_csv(input_csv)
    current_rows = read_csv(current_scores_csv) if current_scores_csv.exists() else []
    horizons = (
        parse_int_list(args.horizons, args.horizons)
        if args.horizons
        else parse_int_list(cfg_get(config, f"{prefix}.horizons", None), "120")
    )
    if not horizons:
        horizons = available_horizons(rows)
    validation_start = str(
        cfg_get(config, f"{prefix}.validation_start_asof", cfg_get(config, "calibration.validation_start_asof", ""))
        or ""
    )[:10]
    validation_end = str(
        cfg_get(config, f"{prefix}.validation_end_asof", cfg_get(config, "calibration.validation_end_asof", ""))
        or ""
    )[:10]
    if not validation_start or not validation_end:
        raise ValueError("validation_start_asof and validation_end_asof are required")
    value_trap_hard_max = float(cfg_get(config, "scoring.gates.value_trap_hard_max", 85.0))
    params_grid = grid_params(config)
    min_validation_obs = int(cfg_get(config, f"{prefix}.min_validation_obs", 20))
    min_unique_tickers = int(cfg_get(config, f"{prefix}.min_unique_tickers", 3))
    min_lcb_excess = float(cfg_get(config, f"{prefix}.min_lcb_excess", 0.0))
    max_loss_rate = float(cfg_get(config, f"{prefix}.max_loss_rate", 0.45))
    require_lcb_delta_nonnegative = str(
        cfg_get(config, f"{prefix}.require_lcb_delta_nonnegative", True)
    ).strip().lower() not in {"0", "false", "no", "off"}
    cohorts = sorted({str(row.get("calibration_cohort") or "") for row in rows if row.get("calibration_cohort")})
    result_rows: list[dict[str, Any]] = []
    for cohort in cohorts:
        for horizon in horizons:
            cohort_validation = validation_rows(
                rows,
                cohort=cohort,
                validation_start=validation_start,
                validation_end=validation_end,
                horizon=horizon,
            )
            if not cohort_validation:
                continue
            for params in params_grid:
                result_rows.append(
                    evaluate_variant(
                        cohort=cohort,
                        horizon=horizon,
                        cohort_validation=cohort_validation,
                        validation_start=validation_start,
                        validation_end=validation_end,
                        params=params,
                        value_trap_hard_max=value_trap_hard_max,
                        min_validation_obs=min_validation_obs,
                        min_unique_tickers=min_unique_tickers,
                        min_lcb_excess=min_lcb_excess,
                        max_loss_rate=max_loss_rate,
                        require_lcb_delta_nonnegative=require_lcb_delta_nonnegative,
                    )
                )
    recs = recommendations(result_rows, cohorts=cohorts, horizon=max(horizons))
    near_misses = current_near_misses(
        current_rows,
        min_score=float(cfg_get(config, f"{prefix}.current_near_miss_min_safe_core_score", 60.0)),
        value_trap_hard_max=value_trap_hard_max,
    )
    write_csv(output_csv, result_rows, RESULT_FIELDS)
    write_csv(recommendation_csv, recs, RECOMMENDATION_FIELDS)
    write_csv(near_miss_csv, near_misses, NEAR_MISS_FIELDS)
    print(
        f"safe_core_threshold_sensitivity rows={len(result_rows)} recommendations={len(recs)} "
        f"near_misses={len(near_misses)} output={output_csv}"
    )


if __name__ == "__main__":
    main()
