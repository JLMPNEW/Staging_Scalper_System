#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import logging
import math
import sys
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
LOGGER = logging.getLogger("build_med_device_calibrated_baseline")
PRODUCTION_BASELINE_SEED = "production_baseline_seed"
WATCHLIST_BASELINE_SEED = "watchlist_baseline_seed"
DIAGNOSTIC_ONLY = "diagnostic_only"
EXCLUDED_EVENT_DRIVEN = "excluded_event_driven"
HARD_EXCLUDED_CLASSIFICATIONS = {
    "manual_review_regulatory_risk",
    "avoid",
    "avoid_confirmed_regulatory_risk",
    "data_review_required",
}
GATE_PAIRS = (
    ("raw_composite_score", "raw_score_min"),
    ("cohort_percentile", "cohort_percentile_min"),
    ("fundamental_quality_score", "fundamental_quality_min"),
    ("fda_product_score", "fda_product_min"),
    ("reimbursement_score", "reimbursement_min"),
    ("valuation_score", "valuation_min"),
    ("technical_entry_score", "technical_entry_min"),
)
COMPARISON_FIELDS = [
    "calibration_cohort",
    "horizon_days",
    "baseline_seed_status",
    "support_tier",
    "status_reason",
    "production_candidate",
    "selected_row_type",
    "candidate_count",
    "candidate_unique_tickers",
    "candidate_ticker_coverage",
    "candidate_observation_coverage",
    "candidate_median_excess",
    "candidate_hit_rate",
    "candidate_lcb_excess",
    "production_tier1_count",
    "production_tier1_unique_tickers",
    "production_tier1_median_excess",
    "production_tier1_hit_rate",
    "production_tier1_lcb_excess",
    "production_broad_count",
    "production_broad_unique_tickers",
    "production_broad_median_excess",
    "production_broad_hit_rate",
    "production_broad_lcb_excess",
    "cohort_top_decile_count",
    "cohort_top_decile_unique_tickers",
    "cohort_top_decile_median_excess",
    "cohort_top_decile_hit_rate",
    "cohort_top_decile_lcb_excess",
    "full_cohort_count",
    "full_cohort_unique_tickers",
    "full_cohort_median_excess",
    "full_cohort_hit_rate",
    "full_cohort_lcb_excess",
    "candidate_lcb_delta_vs_production_broad",
    "candidate_lcb_delta_vs_cohort_top_decile",
    "candidate_lcb_delta_vs_full_cohort",
    "raw_score_min",
    "cohort_percentile_min",
    "fundamental_quality_min",
    "fda_product_min",
    "reimbursement_min",
    "valuation_min",
    "technical_entry_min",
    "value_trap_max",
    "selected_tickers",
]
CONSTITUENT_FIELDS = [
    "calibration_cohort",
    "asof_date",
    "ticker",
    "company_name",
    "horizon_days",
    "raw_composite_score",
    "cohort_percentile",
    "fundamental_quality_score",
    "fda_product_score",
    "reimbursement_score",
    "valuation_score",
    "technical_entry_score",
    "value_trap_score",
    "cohort_excess_return",
    "forward_return",
    "classification",
    "decision_bucket",
    "fda_review_state",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a calibrated baseline seed for future med-device calibration comparisons.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--panel-csv", type=Path, default=None)
    parser.add_argument("--recommendations-csv", type=Path, default=None)
    parser.add_argument("--comparison-csv", type=Path, default=None)
    parser.add_argument("--constituents-csv", type=Path, default=None)
    parser.add_argument("--config-fragment-yaml", type=Path, default=None)
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


def to_bool(raw: object) -> bool:
    return str(raw or "").strip().lower() in {"1", "true", "yes", "y"}


def parse_csv_set(raw: object) -> set[str]:
    return {item.strip() for item in str(raw or "").split(",") if item.strip()}


def fmt_float(value: float | None, digits: int = 6) -> str:
    return "" if value is None else f"{value:.{digits}f}"


def lcb(values: list[float]) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    avg = mean(values)
    variance = sum((value - avg) ** 2 for value in values) / (len(values) - 1)
    return avg - 1.64 * math.sqrt(variance) / math.sqrt(len(values))


def metrics(rows: list[dict[str, str]], *, horizon: int) -> dict[str, Any]:
    values: list[float] = []
    tickers: set[str] = set()
    for row in rows:
        value = to_float(row.get(f"cohort_excess_return_{horizon}d"))
        if value is None:
            continue
        values.append(value)
        ticker = str(row.get("ticker") or "").strip().upper()
        if ticker:
            tickers.add(ticker)
    if not values:
        return {
            "count": 0,
            "unique_tickers": 0,
            "median_excess": None,
            "hit_rate": None,
            "lcb_excess": None,
        }
    return {
        "count": len(values),
        "unique_tickers": len(tickers),
        "median_excess": median(values),
        "hit_rate": sum(1 for value in values if value > 0) / len(values),
        "lcb_excess": lcb(values),
    }


def prefixed_metrics(prefix: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        f"{prefix}_count": payload["count"],
        f"{prefix}_unique_tickers": payload["unique_tickers"],
        f"{prefix}_median_excess": fmt_float(payload["median_excess"]),
        f"{prefix}_hit_rate": fmt_float(payload["hit_rate"], 4),
        f"{prefix}_lcb_excess": fmt_float(payload["lcb_excess"]),
    }


def coverage_ratio(numerator: int | float | None, denominator: int | float | None) -> float | None:
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return float(numerator) / float(denominator)


def delta(candidate: dict[str, Any], baseline: dict[str, Any]) -> str:
    if candidate["lcb_excess"] is None or baseline["lcb_excess"] is None:
        return ""
    return fmt_float(float(candidate["lcb_excess"]) - float(baseline["lcb_excess"]))


def passes_static_exclusions(row: dict[str, str]) -> bool:
    if str(row.get("classification") or "") in HARD_EXCLUDED_CLASSIFICATIONS:
        return False
    if str(row.get("fda_review_state") or "") in MANUAL_FDA_REVIEW_STATES:
        return False
    return True


def passes_candidate_gates(row: dict[str, str], rec: dict[str, str]) -> bool:
    if not passes_static_exclusions(row):
        return False
    for field, threshold_field in GATE_PAIRS:
        threshold = to_float(rec.get(threshold_field))
        value = to_float(row.get(field))
        if threshold is not None and (value is None or value < threshold):
            return False
    value_trap_max = to_float(rec.get("value_trap_max"))
    value_trap = to_float(row.get("value_trap_score"))
    if value_trap_max is not None and value_trap is not None and value_trap > value_trap_max:
        return False
    return True


def selected_tickers(rows: list[dict[str, str]], *, horizon: int) -> str:
    tickers = sorted(
        {
            str(row.get("ticker") or "").strip().upper()
            for row in rows
            if to_float(row.get(f"cohort_excess_return_{horizon}d")) is not None and str(row.get("ticker") or "").strip()
        }
    )
    return ";".join(tickers)


def status_for_candidate(
    cohort: str,
    rec: dict[str, str],
    metrics_by_horizon: dict[int, dict[str, Any]],
    full_metrics_by_horizon: dict[int, dict[str, Any]],
    *,
    reference_horizon: int,
    min_selected_obs: int,
    min_unique_tickers: int,
    preferred_min_unique_tickers: int,
    min_selected_ticker_coverage: float,
    preferred_min_selected_ticker_coverage: float,
    min_hit_rate: float,
    min_lcb_excess: float,
    require_60_120_persistence: bool,
    production_seed_cohorts: set[str],
    watchlist_seed_cohorts: set[str],
    event_driven_excluded_cohorts: set[str],
) -> tuple[str, str, str]:
    reasons: list[str] = []
    if cohort in event_driven_excluded_cohorts:
        return EXCLUDED_EVENT_DRIVEN, "excluded_event_driven", "event_driven_or_binary_outcome_dominates_validation"
    if str(rec.get("production_candidate") or "") != "1":
        return DIAGNOSTIC_ONLY, "not_baseline", rec.get("rejection_reason") or "not_a_production_candidate"
    ref = metrics_by_horizon[reference_horizon]
    full_ref = full_metrics_by_horizon[reference_horizon]
    ref_ticker_coverage = coverage_ratio(ref["unique_tickers"], full_ref["unique_tickers"])
    support_reasons: list[str] = []
    performance_reasons: list[str] = []
    if ref["count"] < min_selected_obs:
        support_reasons.append("insufficient_selected_obs")
    if ref["unique_tickers"] < min_unique_tickers:
        support_reasons.append("insufficient_unique_tickers")
    if ref_ticker_coverage is None:
        support_reasons.append("missing_full_cohort_support")
    elif ref_ticker_coverage < min_selected_ticker_coverage:
        support_reasons.append("insufficient_selected_ticker_coverage")
    if ref["hit_rate"] is None or ref["hit_rate"] < min_hit_rate:
        performance_reasons.append("hit_rate_below_min")
    if ref["lcb_excess"] is None or ref["lcb_excess"] < min_lcb_excess:
        performance_reasons.append("lcb_below_min")
    if require_60_120_persistence:
        for horizon in (60, 120):
            if horizon not in metrics_by_horizon:
                continue
            item = metrics_by_horizon[horizon]
            if item["median_excess"] is None or item["median_excess"] <= 0:
                performance_reasons.append(f"nonpositive_{horizon}d_median")
            if item["lcb_excess"] is None or item["lcb_excess"] < min_lcb_excess:
                performance_reasons.append(f"{horizon}d_lcb_below_min")
    reasons = support_reasons + performance_reasons
    deduped_reasons = ";".join(dict.fromkeys(reasons))
    if reasons:
        if ref["count"] > 0 and support_reasons and not performance_reasons:
            return WATCHLIST_BASELINE_SEED, "thin_support", deduped_reasons
        if cohort in watchlist_seed_cohorts and ref["count"] > 0:
            return WATCHLIST_BASELINE_SEED, "watchlist", deduped_reasons
        return DIAGNOSTIC_ONLY, "not_baseline", deduped_reasons
    if cohort in production_seed_cohorts:
        preferred_ticker_count = ref["unique_tickers"] >= preferred_min_unique_tickers
        preferred_coverage = (
            ref_ticker_coverage is not None and ref_ticker_coverage >= preferred_min_selected_ticker_coverage
        )
        support_tier = "preferred_support" if preferred_ticker_count and preferred_coverage else "acceptable_support"
        return PRODUCTION_BASELINE_SEED, support_tier, "passes_production_baseline_guardrails"
    if cohort in watchlist_seed_cohorts or ref["unique_tickers"] < preferred_min_unique_tickers:
        return WATCHLIST_BASELINE_SEED, "watchlist", "passes_minimums_but_not_promoted_to_production_baseline"
    return WATCHLIST_BASELINE_SEED, "watchlist", "passes_minimums_but_not_promoted_to_production_baseline"


def yaml_number(raw: object) -> str:
    value = to_float(raw)
    return str(raw or "") if value is None else f"{value:g}"


def write_config_fragment(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    eligible = [row for row in rows if row["horizon_days"] == 120 and row["baseline_seed_status"] == PRODUCTION_BASELINE_SEED]
    lines = [
        "# Generated calibrated-baseline seed fragment.",
        "# Review manually before copying into med_devices/config.yaml.",
        "# This fragment includes only production_baseline_seed cohorts.",
        "scoring:",
        "  cohort_profiles:",
    ]
    for row in eligible:
        cohort = str(row["calibration_cohort"])
        lines.extend(
            [
                f"    {cohort}:",
                f"      calibration_status: production_eligible",
                f"      calibration_status_reason: production_baseline_seed_2026_06_05;support_tier_{row['support_tier']}",
                "      gates:",
                f"        composite_min: {yaml_number(row['raw_score_min'])}",
                f"        cohort_percentile_min: {yaml_number(row['cohort_percentile_min'])}",
                f"        fundamental_quality_min: {yaml_number(row['fundamental_quality_min'])}",
                f"        fda_product_min: {yaml_number(row['fda_product_min'])}",
                f"        reimbursement_min: {yaml_number(row['reimbursement_min'])}",
                f"        valuation_min: {yaml_number(row['valuation_min'])}",
                f"        technical_entry_min: {yaml_number(row['technical_entry_min'])}",
                f"        value_trap_max: {yaml_number(row['value_trap_max'])}",
            ]
        )
    if not eligible:
        lines.append("    {}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_constituents(
    cohort: str,
    rows: list[dict[str, str]],
    *,
    horizons: list[int],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        for horizon in horizons:
            if to_float(row.get(f"cohort_excess_return_{horizon}d")) is None:
                continue
            out.append(
                {
                    "calibration_cohort": cohort,
                    "asof_date": row.get("asof_date", ""),
                    "ticker": row.get("ticker", ""),
                    "company_name": row.get("company_name", ""),
                    "horizon_days": horizon,
                    "raw_composite_score": row.get("raw_composite_score", ""),
                    "cohort_percentile": row.get("cohort_percentile", ""),
                    "fundamental_quality_score": row.get("fundamental_quality_score", ""),
                    "fda_product_score": row.get("fda_product_score", ""),
                    "reimbursement_score": row.get("reimbursement_score", ""),
                    "valuation_score": row.get("valuation_score", ""),
                    "technical_entry_score": row.get("technical_entry_score", ""),
                    "value_trap_score": row.get("value_trap_score", ""),
                    "cohort_excess_return": row.get(f"cohort_excess_return_{horizon}d", ""),
                    "forward_return": row.get(f"forward_return_{horizon}d", ""),
                    "classification": row.get("classification", ""),
                    "decision_bucket": row.get("decision_bucket", ""),
                    "fda_review_state": row.get("fda_review_state", ""),
                }
            )
    return out


def main() -> None:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    panel_csv = (
        args.panel_csv.expanduser().resolve()
        if args.panel_csv
        else resolve_path(cfg_get(config, "calibration.cohort_neutral_backtest_csv"), base_dir=base_dir)
    )
    recommendations_csv = (
        args.recommendations_csv.expanduser().resolve()
        if args.recommendations_csv
        else resolve_path(cfg_get(config, "calibration.recommendations_csv"), base_dir=base_dir)
    )
    comparison_csv = (
        args.comparison_csv.expanduser().resolve()
        if args.comparison_csv
        else resolve_path(cfg_get(config, "calibration.calibrated_baseline.comparison_csv"), base_dir=base_dir)
    )
    constituents_csv = (
        args.constituents_csv.expanduser().resolve()
        if args.constituents_csv
        else resolve_path(cfg_get(config, "calibration.calibrated_baseline.constituents_csv"), base_dir=base_dir)
    )
    fragment_yaml = (
        args.config_fragment_yaml.expanduser().resolve()
        if args.config_fragment_yaml
        else resolve_path(cfg_get(config, "calibration.calibrated_baseline.config_fragment_yaml"), base_dir=base_dir)
    )
    validation_start = str(cfg_get(config, "calibration.validation_start_asof", ""))
    validation_end = str(cfg_get(config, "calibration.validation_end_asof", ""))
    horizons = [int(item.strip()) for item in str(cfg_get(config, "calibration.horizons", "30,60,120")).split(",") if item.strip()]
    reference_horizon = max(horizons)
    min_selected_obs = int(cfg_get(config, "calibration.calibrated_baseline.min_selected_obs", 10))
    min_unique_tickers = int(cfg_get(config, "calibration.calibrated_baseline.min_unique_tickers", 2))
    preferred_min_unique_tickers = int(cfg_get(config, "calibration.calibrated_baseline.preferred_min_unique_tickers", 3))
    min_selected_ticker_coverage = float(
        cfg_get(config, "calibration.calibrated_baseline.min_selected_ticker_coverage", 0.0)
    )
    preferred_min_selected_ticker_coverage = float(
        cfg_get(config, "calibration.calibrated_baseline.preferred_min_selected_ticker_coverage", 0.0)
    )
    min_hit_rate = float(cfg_get(config, "calibration.calibrated_baseline.min_hit_rate", 0.50))
    min_lcb_excess = float(cfg_get(config, "calibration.calibrated_baseline.min_lcb_excess_return", 0.0))
    require_60_120_persistence = to_bool(cfg_get(config, "calibration.calibrated_baseline.require_60_120_persistence", True))
    production_seed_cohorts = parse_csv_set(cfg_get(config, "calibration.calibrated_baseline.production_seed_cohorts", ""))
    watchlist_seed_cohorts = parse_csv_set(cfg_get(config, "calibration.calibrated_baseline.watchlist_seed_cohorts", ""))
    event_driven_excluded_cohorts = parse_csv_set(cfg_get(config, "calibration.calibrated_baseline.event_driven_excluded_cohorts", ""))

    rows = [
        row
        for row in read_csv(panel_csv)
        if validation_start <= str(row.get("asof_date") or "")[:10] <= validation_end
    ]
    recommendations = {row["calibration_cohort"]: row for row in read_csv(recommendations_csv)}
    comparison_rows: list[dict[str, Any]] = []
    constituent_rows: list[dict[str, Any]] = []
    for cohort in sorted({str(row.get("calibration_cohort") or "") for row in rows if str(row.get("calibration_cohort") or "")}):
        cohort_rows = [row for row in rows if str(row.get("calibration_cohort") or "") == cohort]
        rec = recommendations.get(cohort, {})
        candidate_rows = [row for row in cohort_rows if rec and passes_candidate_gates(row, rec)]
        constituent_rows.extend(build_constituents(cohort, candidate_rows, horizons=horizons))
        candidate_metrics_by_horizon = {horizon: metrics(candidate_rows, horizon=horizon) for horizon in horizons}
        full_metrics_by_horizon = {horizon: metrics(cohort_rows, horizon=horizon) for horizon in horizons}
        status, support_tier, status_reason = status_for_candidate(
            cohort,
            rec,
            candidate_metrics_by_horizon,
            full_metrics_by_horizon,
            reference_horizon=reference_horizon,
            min_selected_obs=min_selected_obs,
            min_unique_tickers=min_unique_tickers,
            preferred_min_unique_tickers=preferred_min_unique_tickers,
            min_selected_ticker_coverage=min_selected_ticker_coverage,
            preferred_min_selected_ticker_coverage=preferred_min_selected_ticker_coverage,
            min_hit_rate=min_hit_rate,
            min_lcb_excess=min_lcb_excess,
            require_60_120_persistence=require_60_120_persistence,
            production_seed_cohorts=production_seed_cohorts,
            watchlist_seed_cohorts=watchlist_seed_cohorts,
            event_driven_excluded_cohorts=event_driven_excluded_cohorts,
        )
        for horizon in horizons:
            candidate = candidate_metrics_by_horizon[horizon]
            tier1 = metrics([row for row in cohort_rows if str(row.get("classification") or "") == "tier_1_long_candidate"], horizon=horizon)
            broad = metrics([row for row in cohort_rows if str(row.get("final_investability_gate") or "") == "1"], horizon=horizon)
            top_decile = metrics([row for row in cohort_rows if (to_float(row.get("cohort_percentile")) or -1.0) >= 90.0], horizon=horizon)
            full = metrics(cohort_rows, horizon=horizon)
            candidate_ticker_coverage = coverage_ratio(candidate["unique_tickers"], full["unique_tickers"])
            candidate_observation_coverage = coverage_ratio(candidate["count"], full["count"])
            item: dict[str, Any] = {
                "calibration_cohort": cohort,
                "horizon_days": horizon,
                "baseline_seed_status": status,
                "support_tier": support_tier,
                "status_reason": status_reason,
                "production_candidate": rec.get("production_candidate", ""),
                "selected_row_type": rec.get("selected_row_type", ""),
                "candidate_lcb_delta_vs_production_broad": delta(candidate, broad),
                "candidate_lcb_delta_vs_cohort_top_decile": delta(candidate, top_decile),
                "candidate_lcb_delta_vs_full_cohort": delta(candidate, full),
                "candidate_ticker_coverage": fmt_float(candidate_ticker_coverage, 4),
                "candidate_observation_coverage": fmt_float(candidate_observation_coverage, 4),
                "selected_tickers": selected_tickers(candidate_rows, horizon=horizon),
            }
            item.update(prefixed_metrics("candidate", candidate))
            item.update(prefixed_metrics("production_tier1", tier1))
            item.update(prefixed_metrics("production_broad", broad))
            item.update(prefixed_metrics("cohort_top_decile", top_decile))
            item.update(prefixed_metrics("full_cohort", full))
            for _, threshold_field in GATE_PAIRS:
                item[threshold_field] = rec.get(threshold_field, "")
            item["value_trap_max"] = rec.get("value_trap_max", "")
            comparison_rows.append(item)

    write_csv(comparison_csv, comparison_rows, COMPARISON_FIELDS)
    write_csv(constituents_csv, constituent_rows, CONSTITUENT_FIELDS)
    write_config_fragment(fragment_yaml, comparison_rows)
    seed_count = sum(
        1
        for row in comparison_rows
        if row["horizon_days"] == reference_horizon and row["baseline_seed_status"] == PRODUCTION_BASELINE_SEED
    )
    LOGGER.info(
        "Calibrated baseline built: comparison=%s rows=%d constituents=%s rows=%d baseline_seeds=%d",
        comparison_csv,
        len(comparison_rows),
        constituents_csv,
        len(constituent_rows),
        seed_count,
    )
    print(f"calibrated_baseline_comparison_csv={comparison_csv} rows={len(comparison_rows)}")
    print(f"calibrated_baseline_constituents_csv={constituents_csv} rows={len(constituent_rows)}")
    print(f"calibrated_baseline_config_fragment_yaml={fragment_yaml} baseline_seeds={seed_count}")


if __name__ == "__main__":
    main()
