#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import math
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from med_devices.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from med_devices.core.logging_utils import configure_utc_logging  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
DEFAULT_SCORING_COMPONENTS = [
    "fundamental_quality_score",
    "durable_growth_score",
    "durable_growth_score_legacy",
    "durable_growth_alpha_score",
    "durable_growth_growth_score",
    "durable_growth_quality_score",
    "durable_growth_efficiency_score",
    "durable_growth_capital_discipline_score",
    "durable_growth_evidence_quality_score",
    "fda_product_score",
    "reimbursement_score",
    "valuation_score",
    "technical_alpha_score",
    "technical_pullback_score",
    "sentiment_catalyst_score",
    "value_trap_score",
]
TECHNICAL_DIAGNOSTIC_COMPONENTS = {
    "technical_entry_score",
    "technical_setup_score",
    "technical_core_score",
}
CONTROL_COMPONENTS = {
    "raw_composite_score",
    "cohort_percentile",
    "data_completeness_score",
    "liquidity_score",
}
OUTPUT_FIELDS = [
    "priority_rank",
    "component",
    "component_category",
    "cohort_count",
    "positive_alpha_cohorts",
    "inverse_alpha_cohorts",
    "neutralize_cohorts",
    "repair_data_cohorts",
    "risk_gate_cohorts",
    "bad_or_unusable_rate",
    "directional_conflict_rate",
    "low_coverage_rate",
    "mean_coverage_pct",
    "mean_cross_sectional_ic",
    "min_lcb_cross_sectional_ic",
    "mean_top_minus_bottom_median_excess",
    "mean_abs_top_minus_bottom_median_excess",
    "negative_spread_cohorts",
    "positive_spread_cohorts",
    "all_horizon_negative_spread_count",
    "all_horizon_positive_spread_count",
    "all_horizon_repair_count",
    "redesign_priority_score",
    "recommended_redesign_action",
    "rationale",
]
STRING_OUTPUT_FIELDS = {
    "component",
    "component_category",
    "recommended_redesign_action",
    "rationale",
}
INT_OUTPUT_FIELDS = {
    "priority_rank",
    "cohort_count",
    "positive_alpha_cohorts",
    "inverse_alpha_cohorts",
    "neutralize_cohorts",
    "repair_data_cohorts",
    "risk_gate_cohorts",
    "negative_spread_cohorts",
    "positive_spread_cohorts",
    "all_horizon_negative_spread_count",
    "all_horizon_positive_spread_count",
    "all_horizon_repair_count",
}
DETAIL_FIELDS = [
    "component",
    "calibration_cohort",
    "recommended_action",
    "best_horizon_days",
    "support_fold_count",
    "validation_fold_count",
    "mean_coverage_pct",
    "mean_cross_sectional_ic",
    "min_lcb_cross_sectional_ic",
    "mean_top_minus_bottom_median_excess",
    "recommendation_reason",
    "component_category",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Rank med-device scoring components by redesign priority.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--recommendation-csv", type=Path, default=None)
    parser.add_argument("--summary-csv", type=Path, default=None)
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--detail-csv", type=Path, default=None)
    parser.add_argument("--include-control-components", action="store_true")
    return parser.parse_args()


def read_csv(path: Path, *, label: str, required: bool = True) -> list[dict[str, str]]:
    if not path.exists():
        if required:
            raise RuntimeError(
                f"{label} CSV not found: {path}. Run the feature-stability analysis (script 44) "
                "before ranking component redesign priority."
            )
        print(f"warning: optional {label} CSV missing: {path}; all_horizon_* columns will be zero")
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = [dict(row) for row in csv.DictReader(handle)]
    if not rows:
        if required:
            raise RuntimeError(
                f"{label} CSV has no data rows: {path}. Refusing to publish an empty redesign-priority "
                "artifact over the previous output."
            )
        print(f"warning: optional {label} CSV is empty: {path}; all_horizon_* columns will be zero")
    return rows


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    with tmp_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(tmp_path, path)


def to_float(raw: object) -> float | None:
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def parse_str_list(raw: object) -> list[str]:
    return [item.strip() for item in str(raw or "").split(",") if item.strip()]


def path_from_config(config: dict[str, Any], key: str, default: str, *, base_dir: Path) -> Path:
    return resolve_path(cfg_get(config, key, default), base_dir=base_dir)


def component_category(component: str, scoring_components: set[str]) -> str:
    if component in CONTROL_COMPONENTS:
        return "control_metric"
    if component in TECHNICAL_DIAGNOSTIC_COMPONENTS:
        return "technical_diagnostic"
    if component in scoring_components:
        return "scoring_component"
    return "diagnostic_component"


def average(values: list[float]) -> float:
    return mean(values) if values else 0.0


def redesign_action(component: str, counts: Counter[str], category: str, cohort_count: int) -> str:
    if category == "control_metric":
        return "do_not_optimize_control_metric"
    if counts["repair_data"] >= max(3, math.ceil(cohort_count * 0.35)):
        return "repair_data_generation_and_coverage_first"
    if counts["neutralize"] >= max(4, math.ceil(cohort_count * 0.40)):
        return "neutralize_until_redesigned"
    if counts["test_inverse_alpha"] >= max(3, math.ceil(cohort_count * 0.30)):
        return "redesign_as_directional_cohort_specific_signal"
    if counts["use_as_positive_alpha"] and counts["test_inverse_alpha"]:
        return "split_by_cohort_direction"
    if component == "value_trap_score":
        return "keep_as_risk_gate_not_positive_alpha"
    if counts["use_as_positive_alpha"] >= max(4, math.ceil(cohort_count * 0.40)):
        return "keep_but_use_cohort_specific_weighting"
    return "monitor_or_low_priority_refinement"


def rationale_for(row: dict[str, Any]) -> str:
    parts = [
        f"{int(row['repair_data_cohorts'])} repair-data",
        f"{int(row['neutralize_cohorts'])} neutralize",
        f"{int(row['inverse_alpha_cohorts'])} inverse",
        f"{int(row['positive_alpha_cohorts'])} positive",
    ]
    if row["low_coverage_rate"] >= 0.30:
        parts.append("coverage is a material blocker")
    if row["directional_conflict_rate"] >= 0.30:
        parts.append("direction changes by cohort")
    if row["mean_abs_top_minus_bottom_median_excess"] >= 0.05:
        parts.append("spread magnitude is large enough to matter")
    return "; ".join(parts)


def priority_score(
    *,
    counts: Counter[str],
    cohort_count: int,
    low_coverage_count: int,
    mean_abs_spread: float,
    category: str,
) -> float:
    if cohort_count <= 0:
        return 0.0
    bad_rate = (counts["repair_data"] + counts["neutralize"]) / cohort_count
    inverse_rate = counts["test_inverse_alpha"] / cohort_count
    conflict_rate = min(counts["use_as_positive_alpha"], counts["test_inverse_alpha"]) * 2.0 / cohort_count
    low_coverage_rate = low_coverage_count / cohort_count
    magnitude = min(abs(mean_abs_spread), 0.25) / 0.25
    score = 100.0 * (
        0.30 * bad_rate
        + 0.25 * inverse_rate
        + 0.20 * low_coverage_rate
        + 0.15 * conflict_rate
        + 0.10 * magnitude
    )
    if category == "control_metric":
        score *= 0.25
    return score


def main() -> None:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    scoring_components = set(
        parse_str_list(
            cfg_get(
                config,
                "calibration.component_redesign_priority.scoring_components",
                ",".join(DEFAULT_SCORING_COMPONENTS),
            )
        )
    )
    include_control = args.include_control_components or str(
        cfg_get(config, "calibration.component_redesign_priority.include_control_components", False)
    ).strip().lower() in {"1", "true", "yes", "on"}
    recommendation_csv = (
        args.recommendation_csv.expanduser().resolve()
        if args.recommendation_csv
        else path_from_config(
            config,
            "calibration.feature_stability.recommendation_csv",
            "../output/med_devices_reports/calibration/med_device_feature_stability_recommendations.csv",
            base_dir=base_dir,
        )
    )
    summary_csv = (
        args.summary_csv.expanduser().resolve()
        if args.summary_csv
        else path_from_config(
            config,
            "calibration.feature_stability.summary_csv",
            "../output/med_devices_reports/calibration/med_device_feature_stability_summary.csv",
            base_dir=base_dir,
        )
    )
    output_csv = (
        args.output_csv.expanduser().resolve()
        if args.output_csv
        else path_from_config(
            config,
            "calibration.component_redesign_priority.output_csv",
            "../output/med_devices_reports/calibration/med_device_component_redesign_priority.csv",
            base_dir=base_dir,
        )
    )
    detail_csv = (
        args.detail_csv.expanduser().resolve()
        if args.detail_csv
        else path_from_config(
            config,
            "calibration.component_redesign_priority.detail_csv",
            "../output/med_devices_reports/calibration/med_device_component_redesign_priority_detail.csv",
            base_dir=base_dir,
        )
    )
    require_summary = str(
        cfg_get(config, "calibration.component_redesign_priority.require_summary_csv", True)
    ).strip().lower() in {"1", "true", "yes", "on"}
    recommendations = read_csv(recommendation_csv, label="feature-stability recommendations")
    summaries = read_csv(summary_csv, label="feature-stability summary", required=require_summary)
    print(f"recommendation_csv={recommendation_csv} rows={len(recommendations)}")
    print(f"summary_csv={summary_csv} rows={len(summaries)}")

    components = sorted({str(row.get("component") or "") for row in recommendations if row.get("component")})
    if not components:
        raise RuntimeError(
            f"No component values found in {recommendation_csv}; refusing to publish an empty "
            "redesign-priority artifact."
        )
    detail_rows: list[dict[str, Any]] = []
    output_rows: list[dict[str, Any]] = []
    summary_by_component: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in summaries:
        component = str(row.get("component") or "")
        if component:
            summary_by_component[component].append(row)

    for component in components:
        category = component_category(component, scoring_components)
        if category == "control_metric" and not include_control:
            continue
        rows = [row for row in recommendations if row.get("component") == component]
        cohort_count = len(rows)
        counts = Counter(str(row.get("recommended_action") or "") for row in rows)
        coverage_values = [value for value in (to_float(row.get("mean_coverage_pct")) for row in rows) if value is not None]
        ic_values = [value for value in (to_float(row.get("mean_cross_sectional_ic")) for row in rows) if value is not None]
        lcb_values = [value for value in (to_float(row.get("min_lcb_cross_sectional_ic")) for row in rows) if value is not None]
        spread_values = [
            value for value in (to_float(row.get("mean_top_minus_bottom_median_excess")) for row in rows) if value is not None
        ]
        low_coverage_count = sum(1 for value in coverage_values if value < 0.80)
        all_horizon_rows = summary_by_component.get(component, [])
        all_horizon_negative = sum(
            1 for row in all_horizon_rows
            if (to_float(row.get("mean_top_minus_bottom_median_excess")) or 0.0) < 0.0
        )
        all_horizon_positive = sum(
            1 for row in all_horizon_rows
            if (to_float(row.get("mean_top_minus_bottom_median_excess")) or 0.0) > 0.0
        )
        all_horizon_repair = sum(1 for row in all_horizon_rows if row.get("recommended_action") == "repair_data")
        row: dict[str, Any] = {
            "priority_rank": 0,
            "component": component,
            "component_category": category,
            "cohort_count": cohort_count,
            "positive_alpha_cohorts": counts["use_as_positive_alpha"],
            "inverse_alpha_cohorts": counts["test_inverse_alpha"],
            "neutralize_cohorts": counts["neutralize"],
            "repair_data_cohorts": counts["repair_data"],
            "risk_gate_cohorts": counts["risk_gate_only"],
            "bad_or_unusable_rate": (
                (counts["repair_data"] + counts["neutralize"]) / cohort_count if cohort_count else 0.0
            ),
            "directional_conflict_rate": (
                min(counts["use_as_positive_alpha"], counts["test_inverse_alpha"]) * 2.0 / cohort_count
                if cohort_count else 0.0
            ),
            "low_coverage_rate": low_coverage_count / cohort_count if cohort_count else 0.0,
            "mean_coverage_pct": average(coverage_values),
            "mean_cross_sectional_ic": average(ic_values),
            "min_lcb_cross_sectional_ic": min(lcb_values) if lcb_values else 0.0,
            "mean_top_minus_bottom_median_excess": average(spread_values),
            "mean_abs_top_minus_bottom_median_excess": average([abs(value) for value in spread_values]),
            "negative_spread_cohorts": sum(1 for value in spread_values if value < 0.0),
            "positive_spread_cohorts": sum(1 for value in spread_values if value > 0.0),
            "all_horizon_negative_spread_count": all_horizon_negative,
            "all_horizon_positive_spread_count": all_horizon_positive,
            "all_horizon_repair_count": all_horizon_repair,
        }
        row["redesign_priority_score"] = priority_score(
            counts=counts,
            cohort_count=cohort_count,
            low_coverage_count=low_coverage_count,
            mean_abs_spread=row["mean_abs_top_minus_bottom_median_excess"],
            category=category,
        )
        row["recommended_redesign_action"] = redesign_action(component, counts, category, cohort_count)
        row["rationale"] = rationale_for(row)
        output_rows.append(row)
        for detail in rows:
            detail_row = dict(detail)
            detail_row["component_category"] = category
            detail_rows.append(detail_row)

    output_rows.sort(
        key=lambda item: (
            item["redesign_priority_score"],
            item["mean_abs_top_minus_bottom_median_excess"],
            item["cohort_count"],
        ),
        reverse=True,
    )
    for rank, row in enumerate(output_rows, start=1):
        row["priority_rank"] = rank
    for row in output_rows:
        for field in OUTPUT_FIELDS:
            if field in STRING_OUTPUT_FIELDS:
                continue
            number = to_float(row.get(field))
            if number is None:
                row[field] = ""
            elif field in INT_OUTPUT_FIELDS:
                row[field] = str(int(number))
            else:
                row[field] = f"{number:.6f}"
    write_csv(output_csv, output_rows, OUTPUT_FIELDS)
    write_csv(detail_csv, detail_rows, DETAIL_FIELDS)
    print(f"component_redesign_priority_csv={output_csv} rows={len(output_rows)}")
    print(f"component_redesign_priority_detail_csv={detail_csv} rows={len(detail_rows)}")


if __name__ == "__main__":
    raise SystemExit(main())
