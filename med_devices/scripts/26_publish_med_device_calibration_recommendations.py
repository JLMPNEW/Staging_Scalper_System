#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
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
        rec = str(row.get("recommendation") or "")
        if rec == "positive_candidate_factor":
            positive.append(component)
        elif rec in {"negative_or_inverse_factor", "weak_or_unstable_factor"}:
            weak_or_negative.append(component)
    return ";".join(positive[:5]), ";".join(weak_or_negative[:5])


def choose_recommendations(grid_rows: list[dict[str, str]], ic_rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    cohorts = sorted({str(row.get("calibration_cohort") or "") for row in grid_rows if str(row.get("calibration_cohort") or "")})
    out: list[dict[str, Any]] = []
    for cohort in cohorts:
        rows = [row for row in grid_rows if str(row.get("calibration_cohort") or "") == cohort]
        passing = [row for row in rows if str(row.get("pass_fail") or "") == "pass"]
        selected = passing[0] if passing else (rows[0] if rows else {})
        positive, weak = best_components(ic_rows, cohort)
        if passing:
            action = "review_for_manual_promotion"
            notes = "Gate set passed validation constraints; review sample names before production config change."
        else:
            action = "do_not_promote_collect_more_data"
            notes = "No gate set passed constraints; use current global gates and continue data collection."
        out.append(
            {
                "calibration_cohort": cohort,
                "recommended_action": action,
                "parameter_set_id": selected.get("parameter_set_id", ""),
                "objective_score": selected.get("objective_score", ""),
                "pass_fail": selected.get("pass_fail", ""),
                "rejection_reason": selected.get("rejection_reason", ""),
                "raw_score_min": selected.get("raw_score_min", ""),
                "cohort_percentile_min": selected.get("cohort_percentile_min", ""),
                "fundamental_quality_min": selected.get("fundamental_quality_min", ""),
                "fda_product_min": selected.get("fda_product_min", ""),
                "reimbursement_min": selected.get("reimbursement_min", ""),
                "valuation_min": selected.get("valuation_min", ""),
                "technical_entry_min": selected.get("technical_entry_min", ""),
                "value_trap_max": selected.get("value_trap_max", ""),
                "validation_count_120d": selected.get("validation_count_120d", ""),
                "validation_unique_tickers_120d": selected.get("validation_unique_tickers_120d", ""),
                "validation_cohort_unique_tickers_120d": selected.get("validation_cohort_unique_tickers_120d", ""),
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
                f"      parameter_set_id: {row['parameter_set_id']}",
                "      gates:",
                f"        raw_composite_min: {row['raw_score_min']}",
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
    recommendations = choose_recommendations(read_csv(grid_csv), read_csv(ic_csv))
    write_csv(output_csv, recommendations)
    write_yaml_fragment(output_yaml, recommendations)
    print(f"recommendations_csv={output_csv} rows={len(recommendations)}")
    print(f"recommended_config_yaml={output_yaml}")


if __name__ == "__main__":
    main()
