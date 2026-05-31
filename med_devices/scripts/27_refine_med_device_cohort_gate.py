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
from med_devices.core.fda_states import MANUAL_FDA_REVIEW_STATES as MANUAL_FDA_STATES  # noqa: E402
from med_devices.core.logging_utils import configure_utc_logging  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
REVIEW_CLASSIFICATIONS = {"manual_review_regulatory_risk", "avoid_confirmed_regulatory_risk", "data_review_required"}
GRID_FIELDS = [
    "calibration_cohort",
    "parameter_set_id",
    "raw_score_min",
    "cohort_percentile_min",
    "value_trap_max",
    "min_avg_dollar_volume_60d",
    "data_completeness_min",
    "entry_status_policy",
    "fda_review_policy",
    "reimbursement_policy",
    "effective_train_end_asof",
    "validation_start_asof",
    "validation_end_asof",
    "objective_score",
    "pass_fail",
    "rejection_reason",
    "validation_cohort_unique_tickers_120d",
    "validation_selected_ticker_coverage_120d",
    "validation_improved_selected_ticker_rate_120d",
    "selected_tickers_validation",
]
METRIC_KEYS = ("count", "unique_tickers", "mean", "median", "hit_rate", "lcb", "sortino", "profit_factor")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Refine a promoted med-device cohort gate with risk-control filters.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--cohort", type=str, default="")
    parser.add_argument("--input-csv", type=Path, default=None)
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--recommendation-csv", type=Path, default=None)
    parser.add_argument("--comparison-csv", type=Path, default=None)
    return parser.parse_args()


def to_float(raw: object) -> float | None:
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def int_flag(raw: object) -> int:
    return 1 if str(raw or "").strip().lower() in {"1", "true", "yes", "y", "on"} or raw == 1 else 0


def parse_float_list(raw: object, default: str) -> list[float]:
    return [float(item.strip()) for item in str(raw or default).split(",") if item.strip()]


def parse_str_list(raw: object, default: str) -> list[str]:
    return [item.strip() for item in str(raw or default).split(",") if item.strip()]


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
    return min(train_end, validation_start - timedelta(days=embargo_days)).strftime("%Y-%m-%d")


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
        return {key: "" for key in METRIC_KEYS} | {"count": 0, "unique_tickers": 0}
    avg = mean(values)
    if len(values) == 1:
        lcb = values[0]
    else:
        variance = sum((value - avg) ** 2 for value in values) / (len(values) - 1)
        lcb = avg - 1.64 * math.sqrt(variance) / math.sqrt(len(values))
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
        "lcb": f"{lcb:.6f}",
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
        if to_float(row.get(field)) is not None and str(row.get("ticker") or ""):
            out.add(str(row["ticker"]))
    return out


def selected_ticker_improvement_rate(rows: list[dict[str, str]], *, horizon: int) -> float | None:
    grouped: dict[str, list[float]] = {}
    field = f"cohort_excess_return_{horizon}d"
    for row in rows:
        value = to_float(row.get(field))
        ticker = str(row.get("ticker") or "")
        if value is not None and ticker:
            grouped.setdefault(ticker, []).append(value)
    if not grouped:
        return None
    return sum(1 for values in grouped.values() if median(values) > 0) / len(grouped)


def reimbursement_live(row: dict[str, str]) -> bool:
    status = str(row.get("reimbursement_status") or "").strip().lower()
    if int_flag(row.get("unknown_reimbursement_flag")) or status in {"", "unknown", "cms_data_not_loaded"}:
        return False
    return True


def has_reimbursement_evidence(row: dict[str, str]) -> bool:
    return any(
        int_flag(row.get(field))
        for field in (
            "direct_code_evidence",
            "payment_rate_evidence",
            "coverage_policy_evidence",
            "procedure_bundled_flag",
            "capital_equipment_flag",
            "diagnostics_lab_flag",
        )
    )


def passes_policy(row: dict[str, str], *, entry_policy: str, fda_policy: str, reimbursement_policy: str) -> bool:
    if str(row.get("classification") or "") in REVIEW_CLASSIFICATIONS:
        return False
    entry = str(row.get("entry_status") or "")
    if entry_policy == "entry_eligible_only" and entry != "entry_eligible":
        return False
    if entry_policy == "entry_eligible_or_setup" and entry not in {"entry_eligible", "watch_for_setup"}:
        return False

    fda_state = str(row.get("fda_review_state") or "").strip().lower()
    if fda_policy == "clean_or_cleared_only" and fda_state not in {"", "clean", "cleared", "low_materiality"}:
        return False
    if fda_policy == "exclude_manual_hard_red" and (fda_state in MANUAL_FDA_STATES or int_flag(row.get("hard_red_flag"))):
        return False

    if reimbursement_policy == "all_known" and not reimbursement_live(row):
        return False
    if reimbursement_policy == "live_evidence_only" and (not reimbursement_live(row) or not has_reimbursement_evidence(row)):
        return False
    if reimbursement_policy == "direct_or_bundled_or_capital" and not any(
        int_flag(row.get(field))
        for field in ("direct_code_evidence", "payment_rate_evidence", "procedure_bundled_flag", "capital_equipment_flag")
    ):
        return False
    return True


def passes_gates(
    row: dict[str, str],
    *,
    raw_score_min: float,
    cohort_percentile_min: float,
    value_trap_max: float,
    min_avg_dollar_volume_60d: float,
    data_completeness_min: float,
    entry_status_policy: str,
    fda_review_policy: str,
    reimbursement_policy: str,
) -> bool:
    checks = [
        ("raw_composite_score", raw_score_min),
        ("cohort_percentile", cohort_percentile_min),
        ("avg_dollar_volume_60d", min_avg_dollar_volume_60d),
        ("data_completeness_score", data_completeness_min),
    ]
    for field, threshold in checks:
        value = to_float(row.get(field))
        if field == "avg_dollar_volume_60d" and threshold <= 0 and value is None:
            continue
        if value is None or value < threshold:
            return False
    value_trap = to_float(row.get("value_trap_score"))
    if value_trap is not None and value_trap > value_trap_max:
        return False
    return passes_policy(
        row,
        entry_policy=entry_status_policy,
        fda_policy=fda_review_policy,
        reimbursement_policy=reimbursement_policy,
    )


def score_objective(metrics_by_horizon: dict[int, dict[str, Any]], weights: dict[str, float]) -> float:
    total = 0.0
    used = 0
    for payload in metrics_by_horizon.values():
        median_value = to_float(payload.get("median")) or 0.0
        lcb_value = to_float(payload.get("lcb")) or 0.0
        mean_value = to_float(payload.get("mean")) or 0.0
        sortino = min(3.0, max(-3.0, to_float(payload.get("sortino")) or 0.0))
        profit = min(3.0, max(0.0, to_float(payload.get("profit_factor")) or 0.0))
        total += (
            float(weights.get("median_excess_return", 0.35)) * median_value * 100.0
            + float(weights.get("lower_confidence_bound", 0.25)) * lcb_value * 100.0
            + float(weights.get("sortino", 0.15)) * sortino
            + float(weights.get("profit_factor", 0.15)) * (profit - 1.0)
            + float(weights.get("mean_excess_return", 0.10)) * mean_value * 100.0
        )
        used += 1
    return total / used if used else -999.0


def parameter_id(*values: object) -> str:
    raw = "|".join(str(value) for value in values)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def output_fields(horizons: list[int]) -> list[str]:
    fields = list(GRID_FIELDS)
    for horizon in horizons:
        for prefix in ("train", "validation"):
            for key in METRIC_KEYS:
                fields.append(f"{prefix}_{key}_{horizon}d")
    return fields


def evaluate(
    rows: list[dict[str, str]],
    *,
    cohort: str,
    horizons: list[int],
    train_end_asof: str,
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
    value_trap_max: float,
    min_avg_dollar_volume_60d: float,
    data_completeness_min: float,
    entry_status_policy: str,
    fda_review_policy: str,
    reimbursement_policy: str,
) -> dict[str, Any]:
    selected = [
        row
        for row in rows
        if str(row.get("calibration_cohort") or "") == cohort
        and passes_gates(
            row,
            raw_score_min=raw_score_min,
            cohort_percentile_min=cohort_percentile_min,
            value_trap_max=value_trap_max,
            min_avg_dollar_volume_60d=min_avg_dollar_volume_60d,
            data_completeness_min=data_completeness_min,
            entry_status_policy=entry_status_policy,
            fda_review_policy=fda_review_policy,
            reimbursement_policy=reimbursement_policy,
        )
    ]
    train = [row for row in selected if str(row.get("asof_date") or "") <= train_end_asof]
    validation = [row for row in selected if validation_start_asof <= str(row.get("asof_date") or "") <= validation_end_asof]
    validation_all = [
        row
        for row in rows
        if str(row.get("calibration_cohort") or "") == cohort
        and validation_start_asof <= str(row.get("asof_date") or "") <= validation_end_asof
    ]
    metrics_train: dict[int, dict[str, Any]] = {}
    metrics_validation: dict[int, dict[str, Any]] = {}
    for horizon in horizons:
        values, tickers = selected_values(train, horizon=horizon)
        metrics_train[horizon] = metrics(values, tickers)
        values, tickers = selected_values(validation, horizon=horizon)
        metrics_validation[horizon] = metrics(values, tickers)
    ref_horizon = max(horizons)
    validation_ref = metrics_validation[ref_horizon]
    train_ref = metrics_train[ref_horizon]
    validation_cohort_tickers = unique_tickers_with_returns(validation_all, horizon=ref_horizon)
    selected_tickers = unique_tickers_with_returns(validation, horizon=ref_horizon)
    selected_coverage = len(selected_tickers) / len(validation_cohort_tickers) if validation_cohort_tickers else 0.0
    improved_rate = selected_ticker_improvement_rate(validation, horizon=ref_horizon)
    rejection: list[str] = []
    if int(train_ref["count"]) < min_train_obs:
        rejection.append("insufficient_train_obs")
    if int(validation_ref["count"]) < min_validation_obs:
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
    item: dict[str, Any] = {
        "calibration_cohort": cohort,
        "parameter_set_id": parameter_id(
            cohort,
            raw_score_min,
            cohort_percentile_min,
            value_trap_max,
            min_avg_dollar_volume_60d,
            data_completeness_min,
            entry_status_policy,
            fda_review_policy,
            reimbursement_policy,
        ),
        "raw_score_min": raw_score_min,
        "cohort_percentile_min": cohort_percentile_min,
        "value_trap_max": value_trap_max,
        "min_avg_dollar_volume_60d": min_avg_dollar_volume_60d,
        "data_completeness_min": data_completeness_min,
        "entry_status_policy": entry_status_policy,
        "fda_review_policy": fda_review_policy,
        "reimbursement_policy": reimbursement_policy,
        "effective_train_end_asof": train_end_asof,
        "validation_start_asof": validation_start_asof,
        "validation_end_asof": validation_end_asof,
        "objective_score": f"{score_objective(metrics_validation, objective_weights):.6f}",
        "pass_fail": "fail" if rejection else "pass",
        "rejection_reason": ";".join(rejection),
        "validation_cohort_unique_tickers_120d": len(validation_cohort_tickers),
        "validation_selected_ticker_coverage_120d": f"{selected_coverage:.4f}",
        "validation_improved_selected_ticker_rate_120d": "" if improved_rate is None else f"{improved_rate:.4f}",
        "selected_tickers_validation": ";".join(sorted(selected_tickers))[:500],
    }
    for horizon in horizons:
        for prefix, payload in (("train", metrics_train[horizon]), ("validation", metrics_validation[horizon])):
            for key, value in payload.items():
                item[f"{prefix}_{key}_{horizon}d"] = value
    return item


def comparison_row(name: str, rows: list[dict[str, str]], *, horizons: list[int]) -> dict[str, Any]:
    item: dict[str, Any] = {"selection_name": name, "selected_tickers": ""}
    tickers = unique_tickers_with_returns(rows, horizon=max(horizons))
    item["selected_tickers"] = ";".join(sorted(tickers))
    for horizon in horizons:
        values, ticker_values = selected_values(rows, horizon=horizon)
        payload = metrics(values, ticker_values)
        for key, value in payload.items():
            item[f"{key}_{horizon}d"] = value
    return item


def comparison_fields(horizons: list[int]) -> list[str]:
    fields = ["selection_name", "selected_tickers"]
    for horizon in horizons:
        for key in METRIC_KEYS:
            fields.append(f"{key}_{horizon}d")
    return fields


def current_promoted_gate(config: dict[str, Any], cohort: str) -> dict[str, float]:
    profiles = cfg_get(config, "scoring.cohort_profiles", {}) or {}
    profile = profiles.get(cohort, {}) if isinstance(profiles, dict) else {}
    gates = profile.get("gates", {}) if isinstance(profile, dict) else {}
    if not isinstance(gates, dict):
        gates = {}
    return {
        "raw_score_min": float(gates.get("composite_min", gates.get("raw_composite_min", 55.0))),
        "cohort_percentile_min": float(gates.get("cohort_percentile_min", 60.0)),
        "value_trap_max": float(gates.get("value_trap_max", 40.0)),
        "min_avg_dollar_volume_60d": float(cfg_get(config, "scoring.gates.min_avg_dollar_volume_60d", 1_000_000.0)),
        "data_completeness_min": float(cfg_get(config, "scoring.gates.data_completeness_min", 90.0)),
    }


def main() -> None:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    cohort = args.cohort.strip() or str(cfg_get(config, "calibration.refinement.default_cohort", "hospital_supplies_consumables_dme"))
    input_csv = (
        args.input_csv.expanduser().resolve()
        if args.input_csv
        else resolve_path(cfg_get(config, "calibration.cohort_neutral_backtest_csv"), base_dir=base_dir)
    )
    output_csv = (
        args.output_csv.expanduser().resolve()
        if args.output_csv
        else resolve_path(cfg_get(config, "calibration.refinement.output_csv"), base_dir=base_dir)
    )
    recommendation_csv = (
        args.recommendation_csv.expanduser().resolve()
        if args.recommendation_csv
        else resolve_path(cfg_get(config, "calibration.refinement.recommendation_csv"), base_dir=base_dir)
    )
    comparison_csv = (
        args.comparison_csv.expanduser().resolve()
        if args.comparison_csv
        else resolve_path(cfg_get(config, "calibration.refinement.comparison_csv"), base_dir=base_dir)
    )
    rows = read_csv(input_csv)
    horizons = [int(value) for value in parse_float_list(cfg_get(config, "calibration.horizons", "30,60,120"), "30,60,120")]
    validation_start = str(cfg_get(config, "calibration.validation_start_asof", "2025-06-06"))
    validation_end = str(cfg_get(config, "calibration.validation_end_asof", "2025-11-28"))
    effective_train = effective_train_end(
        str(cfg_get(config, "calibration.train_end_asof", "2025-05-30")),
        validation_start,
        int(cfg_get(config, "calibration.embargo_days", 120)),
    )
    objective_weights = cfg_get(config, "calibration.objective", {}) or {}
    min_train_obs = int(cfg_get(config, "calibration.min_train_obs", 100))
    min_validation_obs = int(cfg_get(config, "calibration.min_validation_obs", 40))
    min_unique_tickers = int(cfg_get(config, "calibration.min_unique_tickers", 5))
    min_selected_validation = int(cfg_get(config, "calibration.min_selected_validation", 20))
    min_selected_ticker_coverage = float(cfg_get(config, "calibration.min_selected_ticker_coverage", 0.60))
    min_improved_selected_ticker_rate = float(cfg_get(config, "calibration.min_improved_selected_ticker_rate", 0.60))

    all_results: list[dict[str, Any]] = []
    for raw_min, pct_min, trap_max, liquidity_min, data_min, entry_policy, fda_policy, reimbursement_policy in itertools.product(
        parse_float_list(cfg_get(config, "calibration.refinement.raw_score_min"), "52.5,55,57.5,60"),
        parse_float_list(cfg_get(config, "calibration.refinement.cohort_percentile_min"), "55,60,65,70"),
        parse_float_list(cfg_get(config, "calibration.refinement.value_trap_max"), "30,35,40"),
        parse_float_list(cfg_get(config, "calibration.refinement.min_avg_dollar_volume_60d"), "0,1000000,2000000,5000000,10000000"),
        parse_float_list(cfg_get(config, "calibration.refinement.data_completeness_min"), "85,90,100"),
        parse_str_list(cfg_get(config, "calibration.refinement.entry_status_policy"), "entry_eligible_or_setup,entry_eligible_only"),
        parse_str_list(cfg_get(config, "calibration.refinement.fda_review_policy"), "exclude_manual_hard_red,clean_or_cleared_only"),
        parse_str_list(
            cfg_get(config, "calibration.refinement.reimbursement_policy"),
            "all_known,live_evidence_only,direct_or_bundled_or_capital",
        ),
    ):
        all_results.append(
            evaluate(
                rows,
                cohort=cohort,
                horizons=horizons,
                train_end_asof=effective_train,
                validation_start_asof=validation_start,
                validation_end_asof=validation_end,
                min_train_obs=min_train_obs,
                min_validation_obs=min_validation_obs,
                min_unique_tickers=min_unique_tickers,
                min_selected_validation=min_selected_validation,
                min_selected_ticker_coverage=min_selected_ticker_coverage,
                min_improved_selected_ticker_rate=min_improved_selected_ticker_rate,
                objective_weights=objective_weights,
                raw_score_min=raw_min,
                cohort_percentile_min=pct_min,
                value_trap_max=trap_max,
                min_avg_dollar_volume_60d=liquidity_min,
                data_completeness_min=data_min,
                entry_status_policy=entry_policy,
                fda_review_policy=fda_policy,
                reimbursement_policy=reimbursement_policy,
            )
        )
    all_results.sort(key=lambda item: (item["pass_fail"] == "pass", to_float(item["objective_score"]) or -999.0), reverse=True)
    write_csv(output_csv, all_results, output_fields(horizons))
    write_csv(recommendation_csv, all_results[:1], output_fields(horizons))

    validation_rows = [
        row
        for row in rows
        if str(row.get("calibration_cohort") or "") == cohort and validation_start <= str(row.get("asof_date") or "") <= validation_end
    ]
    promoted = current_promoted_gate(config, cohort)
    current_rows = [
        row
        for row in validation_rows
        if passes_gates(
            row,
            raw_score_min=promoted["raw_score_min"],
            cohort_percentile_min=promoted["cohort_percentile_min"],
            value_trap_max=promoted["value_trap_max"],
            min_avg_dollar_volume_60d=promoted["min_avg_dollar_volume_60d"],
            data_completeness_min=promoted["data_completeness_min"],
            entry_status_policy="entry_eligible_or_setup",
            fda_review_policy="exclude_manual_hard_red",
            reimbursement_policy="all_known",
        )
    ]
    best = all_results[0]
    refined_rows = [
        row
        for row in validation_rows
        if passes_gates(
            row,
            raw_score_min=float(best["raw_score_min"]),
            cohort_percentile_min=float(best["cohort_percentile_min"]),
            value_trap_max=float(best["value_trap_max"]),
            min_avg_dollar_volume_60d=float(best["min_avg_dollar_volume_60d"]),
            data_completeness_min=float(best["data_completeness_min"]),
            entry_status_policy=str(best["entry_status_policy"]),
            fda_review_policy=str(best["fda_review_policy"]),
            reimbursement_policy=str(best["reimbursement_policy"]),
        )
    ]
    write_csv(
        comparison_csv,
        [
            comparison_row("full_validation_cohort", validation_rows, horizons=horizons),
            comparison_row("current_promoted_gate", current_rows, horizons=horizons),
            comparison_row("refined_best_gate", refined_rows, horizons=horizons),
        ],
        comparison_fields(horizons),
    )
    print(f"refined_gate_grid_csv={output_csv} rows={len(all_results)}")
    print(f"refined_gate_recommendation_csv={recommendation_csv} pass_fail={best['pass_fail']}")
    print(f"refined_gate_comparison_csv={comparison_csv}")


if __name__ == "__main__":
    main()
