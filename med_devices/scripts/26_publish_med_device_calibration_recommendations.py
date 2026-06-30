#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from med_devices.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from med_devices.core.logging_utils import configure_utc_logging  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
OUTPUT_FIELDS = [
    "calibration_cohort",
    "recommended_action",
    "parameter_set_id",
    "objective_score",
    "pass_fail",
    "selected_row_type",
    "production_candidate",
    "rejection_reason",
    "raw_score_min",
    "cohort_percentile_min",
    "fundamental_quality_min",
    "fda_product_min",
    "reimbursement_min",
    "valuation_min",
    "technical_entry_min",
    "value_trap_max",
    "validation_count_120d",
    "validation_unique_tickers_120d",
    "validation_cohort_unique_tickers_120d",
    "validation_selected_ticker_coverage_120d",
    "validation_improved_selected_ticker_rate_120d",
    "validation_median_excess_120d",
    "validation_hit_rate_120d",
    "validation_lcb_120d",
    "best_positive_components_120d",
    "weak_or_negative_components_120d",
    "notes",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Publish med-device calibration recommendations by cohort.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--grid-csv", type=Path, default=None)
    parser.add_argument("--ic-csv", type=Path, default=None)
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--output-yaml", type=Path, default=None)
    return parser.parse_args()


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def validate_grid_contract(rows: list[dict[str, str]], *, path: Path) -> None:
    if not rows:
        raise RuntimeError(f"Calibration recommendation grid is empty: {path}")
    required = {
        "calibration_cohort",
        "parameter_set_id",
        "pass_fail",
        "objective_score",
        "validation_unique_tickers_120d",
        "validation_cohort_unique_tickers_120d",
        "validation_lcb_120d",
    }
    missing = sorted(required.difference(rows[0]))
    if "validation_count_120d" not in rows[0] and "validation_cohort_obs_120d" not in rows[0]:
        missing.append("validation_count_120d|validation_cohort_obs_120d")
    if missing:
        raise RuntimeError(
            f"Calibration grid CSV {path} is missing required script-25 output columns: {','.join(missing)}. "
            "Regenerate the gate grid before publishing recommendations."
        )


def parse_csv_set(raw: object) -> set[str]:
    return {item.strip() for item in str(raw or "").split(",") if item.strip()}


def parse_int_list(raw: object, default: str) -> list[int]:
    text = str(raw if raw is not None else default)
    return [int(item.strip()) for item in text.split(",") if item.strip()]


def to_float(raw: object) -> float | None:
    try:
        text = str(raw).strip()
        if not text:
            return None
        return float(text)
    except (TypeError, ValueError):
        return None


def to_int(raw: object) -> int:
    value = to_float(raw)
    return int(value) if value is not None else 0


def row_value(row: dict[str, str], *keys: str) -> str:
    for key in keys:
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return ""


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_FIELDS, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def best_components(ic_rows: list[dict[str, str]], cohort: str) -> tuple[str, str]:
    positive: list[str] = []
    weak_or_negative: list[str] = []
    for row in ic_rows:
        if str(row.get("calibration_cohort") or "") != cohort or str(row.get("horizon_days") or "") != "120":
            continue
        component = str(row.get("component") or "")
        rec = str(row.get("production_recommendation") or "")
        if rec == "positive_candidate_factor":
            positive.append(component)
        elif rec in {"negative_or_inverse_factor", "weak_or_unstable_factor"}:
            weak_or_negative.append(component)
    return ";".join(positive[:5]), ";".join(weak_or_negative[:5])


def production_guardrail_reasons(
    selected: dict[str, str],
    *,
    cohort: str,
    min_selected_validation: int,
    min_unique_tickers: int,
    concentration_override_cohorts: set[str],
    min_validation_lcb_excess: float,
    required_positive_lcb_horizons: list[int],
) -> tuple[list[str], bool]:
    reasons: list[str] = []
    # validation_count_120d is selected-subset support; validation_cohort_obs_120d is only
    # a compatibility fallback for older/alternate grid files and is not semantically identical.
    validation_count = to_int(row_value(selected, "validation_count_120d", "validation_cohort_obs_120d"))
    unique_tickers = to_int(row_value(selected, "validation_unique_tickers_120d", "validation_cohort_unique_tickers_120d"))
    concentration_override = cohort in concentration_override_cohorts
    concentration_override_used = False
    if validation_count < min_selected_validation:
        reasons.append("insufficient_selected_validation_for_production")
    if unique_tickers < min_unique_tickers:
        if concentration_override:
            concentration_override_used = True
        else:
            reasons.append("insufficient_unique_tickers_for_production")
    for horizon in required_positive_lcb_horizons:
        lcb = to_float(selected.get(f"validation_lcb_{horizon}d"))
        if lcb is None or lcb < min_validation_lcb_excess:
            reasons.append(f"{horizon}d_lcb_below_min_for_production")
    return list(dict.fromkeys(reasons)), concentration_override_used


def choose_recommendations(
    grid_rows: list[dict[str, str]],
    ic_rows: list[dict[str, str]],
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    min_selected_validation_for_production = int(
        cfg_get(
            config,
            "calibration.min_selected_validation_for_production",
            cfg_get(config, "calibration.min_selected_validation", 20),
        )
    )
    min_unique_tickers_for_production = int(
        cfg_get(
            config,
            "calibration.min_unique_tickers_for_production",
            cfg_get(config, "calibration.min_unique_tickers", 5),
        )
    )
    concentration_override_cohorts = parse_csv_set(
        cfg_get(config, "calibration.production_candidate_override_cohorts", "")
    )
    production_seed_cohorts = parse_csv_set(
        cfg_get(config, "calibration.calibrated_baseline.production_seed_cohorts", "")
    )
    min_validation_lcb_excess = float(cfg_get(config, "calibration.min_validation_lcb_excess", 0.0))
    required_positive_lcb_horizons = parse_int_list(
        cfg_get(config, "calibration.require_positive_lcb_horizons", "120"),
        "120",
    )
    cohorts = sorted({str(row.get("calibration_cohort") or "") for row in grid_rows if str(row.get("calibration_cohort") or "")})
    out: list[dict[str, Any]] = []
    for cohort in cohorts:
        rows = [row for row in grid_rows if str(row.get("calibration_cohort") or "") == cohort]
        passing = [row for row in rows if str(row.get("pass_fail") or "") == "pass"]
        selected = passing[0] if passing else (rows[0] if rows else {})
        positive, weak = best_components(ic_rows, cohort)
        if passing:
            guardrail_reasons, concentration_override_used = production_guardrail_reasons(
                selected,
                cohort=cohort,
                min_selected_validation=min_selected_validation_for_production,
                min_unique_tickers=min_unique_tickers_for_production,
                concentration_override_cohorts=concentration_override_cohorts,
                min_validation_lcb_excess=min_validation_lcb_excess,
                required_positive_lcb_horizons=required_positive_lcb_horizons,
            )
            if guardrail_reasons:
                action = "review_for_manual_promotion"
                selected_row_type = "passed_validation_watchlist"
                production_candidate = 0
                notes = (
                    "Gate set passed diagnostic constraints but did not clear production support guardrails: "
                    + ";".join(guardrail_reasons)
                )
            elif production_seed_cohorts and cohort not in production_seed_cohorts:
                action = "review_for_manual_promotion"
                selected_row_type = "passed_validation_watchlist"
                production_candidate = 0
                notes = "Gate set passed production guardrails but is not in the explicit production seed cohort list."
            else:
                action = "promote_to_calibrated_baseline"
                selected_row_type = "passed_validation_production_candidate"
                production_candidate = 1
                notes = "Gate set passed validation and production support guardrails."
                if concentration_override_used:
                    notes += " Manual concentration override accepted for this cohort."
        else:
            action = "do_not_promote_collect_more_data"
            selected_row_type = "best_failed_diagnostic"
            production_candidate = 0
            notes = (
                "No gate set passed constraints; shown parameter set is the best failed diagnostic row, "
                "not a calibrated production candidate."
            )
        out.append(
            {
                "calibration_cohort": cohort,
                "recommended_action": action,
                "parameter_set_id": selected.get("parameter_set_id", ""),
                "objective_score": selected.get("objective_score", ""),
                "pass_fail": selected.get("pass_fail", ""),
                "selected_row_type": selected_row_type,
                "production_candidate": production_candidate,
                "rejection_reason": selected.get("rejection_reason", ""),
                "raw_score_min": selected.get("raw_score_min", ""),
                "cohort_percentile_min": selected.get("cohort_percentile_min", ""),
                "fundamental_quality_min": selected.get("fundamental_quality_min", ""),
                "fda_product_min": selected.get("fda_product_min", ""),
                "reimbursement_min": selected.get("reimbursement_min", ""),
                "valuation_min": selected.get("valuation_min", ""),
                "technical_entry_min": selected.get("technical_entry_min", ""),
                "value_trap_max": selected.get("value_trap_max", ""),
                "validation_count_120d": row_value(selected, "validation_count_120d", "validation_cohort_obs_120d"),
                "validation_unique_tickers_120d": row_value(
                    selected,
                    "validation_unique_tickers_120d",
                    "validation_cohort_unique_tickers_120d",
                ),
                "validation_cohort_unique_tickers_120d": row_value(
                    selected,
                    "validation_cohort_unique_tickers_120d",
                    "validation_unique_tickers_120d",
                ),
                "validation_selected_ticker_coverage_120d": selected.get("validation_selected_ticker_coverage_120d", ""),
                "validation_improved_selected_ticker_rate_120d": selected.get("validation_improved_selected_ticker_rate_120d", ""),
                "validation_median_excess_120d": selected.get("validation_median_120d", ""),
                "validation_hit_rate_120d": selected.get("validation_hit_rate_120d", ""),
                "validation_lcb_120d": selected.get("validation_lcb_120d", ""),
                "best_positive_components_120d": positive,
                "weak_or_negative_components_120d": weak,
                "notes": notes,
            }
        )
    return out


def write_yaml_fragment(path: Path, recommendations: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Generated calibration recommendation fragment.",
        "# Review manually before copying any values into med_devices/config.yaml.",
        "scoring:",
        "  cohort_profiles:",
    ]
    for row in recommendations:
        cohort = str(row["calibration_cohort"])
        lines.extend(
            [
                f"    {cohort}:",
                f"      recommended_action: {row['recommended_action']}",
                f"      selected_row_type: {row['selected_row_type']}",
                f"      production_candidate: {str(bool(row['production_candidate'])).lower()}",
                f"      parameter_set_id: {row['parameter_set_id']}",
                f"      note: {json.dumps(str(row['notes']), ensure_ascii=True)}",
                "      gates:",
                f"        composite_min: {row['raw_score_min']}",
                f"        cohort_percentile_min: {row['cohort_percentile_min']}",
                f"        fundamental_quality_min: {row['fundamental_quality_min']}",
                f"        fda_product_min: {row['fda_product_min']}",
                f"        reimbursement_min: {row['reimbursement_min']}",
                f"        valuation_min: {row['valuation_min']}",
                f"        technical_entry_min: {row['technical_entry_min']}",
                f"        value_trap_max: {row['value_trap_max']}",
            ]
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    grid_csv = (
        args.grid_csv.expanduser().resolve()
        if args.grid_csv
        else resolve_path(cfg_get(config, "calibration.gate_grid_results_csv"), base_dir=base_dir)
    )
    ic_csv = (
        args.ic_csv.expanduser().resolve()
        if args.ic_csv
        else resolve_path(cfg_get(config, "calibration.component_ic_csv"), base_dir=base_dir)
    )
    output_csv = (
        args.output_csv.expanduser().resolve()
        if args.output_csv
        else resolve_path(cfg_get(config, "calibration.recommendations_csv"), base_dir=base_dir)
    )
    output_yaml = (
        args.output_yaml.expanduser().resolve()
        if args.output_yaml
        else resolve_path(cfg_get(config, "calibration.recommended_config_yaml"), base_dir=base_dir)
    )
    grid_rows = read_csv(grid_csv)
    validate_grid_contract(grid_rows, path=grid_csv)
    recommendations = choose_recommendations(grid_rows, read_csv(ic_csv), config)
    write_csv(output_csv, recommendations)
    write_yaml_fragment(output_yaml, recommendations)
    print(f"recommendations_csv={output_csv} rows={len(recommendations)}")
    print(f"recommended_config_yaml={output_yaml}")


if __name__ == "__main__":
    raise SystemExit(main())
