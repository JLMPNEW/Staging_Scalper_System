#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import math
import sys
from collections import defaultdict
from dataclasses import dataclass
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
OUTPUT_FIELDS = [
    "calibration_cohort",
    "template_id",
    "split",
    "count",
    "unique_tickers",
    "selected_ticker_coverage",
    "mean_return_120d",
    "median_return_120d",
    "hit_rate_120d",
    "mean_excess_120d",
    "median_excess_120d",
    "excess_hit_rate_120d",
    "lcb_excess_120d",
    "delta_mean_excess_vs_baseline",
    "delta_median_excess_vs_baseline",
    "delta_excess_hit_rate_vs_baseline",
    "delta_lcb_excess_vs_baseline",
    "improved_selected_ticker_rate",
    "promotion_status",
    "promotion_reason",
    "weights_spec",
]
RECOMMENDATION_FIELDS = [
    "calibration_cohort",
    "recommended_template_id",
    "promotion_status",
    "validation_mean_excess_120d",
    "validation_median_excess_120d",
    "validation_excess_hit_rate_120d",
    "validation_lcb_excess_120d",
    "validation_unique_tickers",
    "validation_selected_ticker_coverage",
    "improved_selected_ticker_rate",
    "promotion_reason",
]


@dataclass(frozen=True)
class Template:
    cohort: str
    template_id: str
    weights: tuple[tuple[str, str, float], ...]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Test targeted templates for restricted med-device cohorts.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--input-csv", type=Path, default=None)
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--recommendation-csv", type=Path, default=None)
    return parser.parse_args()


def templates() -> list[Template]:
    return [
        Template(
            cohort="implantable_interventional_devices_procedure_bundled",
            template_id="procedure_bundled_pullback_fda_risk_only",
            weights=(
                ("technical_pullback_score", "positive", 0.45),
                ("valuation_score", "inverse", 0.20),
                ("reimbursement_score", "inverse", 0.15),
                ("sentiment_catalyst_score", "inverse", 0.10),
                ("fundamental_quality_score", "inverse", 0.10),
            ),
        ),
        Template(
            cohort="implantable_interventional_devices_procedure_bundled",
            template_id="procedure_bundled_pullback_simple",
            weights=(
                ("technical_pullback_score", "positive", 0.60),
                ("valuation_score", "inverse", 0.20),
                ("reimbursement_score", "inverse", 0.20),
            ),
        ),
        Template(
            cohort="implantable_interventional_devices_direct_payment",
            template_id="direct_payment_pullback_reimbursement_quality",
            weights=(
                ("technical_pullback_score", "positive", 0.35),
                ("reimbursement_score", "positive", 0.25),
                ("fda_product_score", "positive", 0.20),
                ("fundamental_quality_score", "positive", 0.10),
                ("value_trap_score", "inverse", 0.10),
            ),
        ),
        Template(
            cohort="implantable_interventional_devices_direct_payment",
            template_id="direct_payment_pullback_fda_risk_only",
            weights=(
                ("technical_pullback_score", "positive", 0.45),
                ("fda_product_score", "positive", 0.25),
                ("reimbursement_score", "positive", 0.20),
                ("value_trap_score", "inverse", 0.10),
            ),
        ),
        Template(
            cohort="implantable_interventional_devices_direct_payment",
            template_id="direct_payment_quality_value_pullback",
            weights=(
                ("fundamental_quality_score", "positive", 0.30),
                ("valuation_score", "positive", 0.25),
                ("technical_pullback_score", "positive", 0.25),
                ("reimbursement_score", "positive", 0.20),
            ),
        ),
        Template(
            cohort="implantable_interventional_devices_direct_payment",
            template_id="direct_payment_technical_neutral_fda_reimbursement",
            weights=(
                ("fda_product_score", "positive", 0.35),
                ("reimbursement_score", "positive", 0.35),
                ("fundamental_quality_score", "positive", 0.20),
                ("value_trap_score", "inverse", 0.10),
            ),
        ),
        Template(
            cohort="orthopedics_spine_dental",
            template_id="ortho_quality_reimbursement_pullback",
            weights=(
                ("fundamental_quality_score", "positive", 0.35),
                ("reimbursement_score", "positive", 0.30),
                ("technical_pullback_score", "positive", 0.20),
                ("value_trap_score", "inverse", 0.10),
                ("valuation_score", "inverse", 0.05),
            ),
        ),
        Template(
            cohort="orthopedics_spine_dental",
            template_id="ortho_quality_reimbursement_only",
            weights=(
                ("fundamental_quality_score", "positive", 0.50),
                ("reimbursement_score", "positive", 0.35),
                ("value_trap_score", "inverse", 0.15),
            ),
        ),
        Template(
            cohort="capital_equipment_imaging_monitoring",
            template_id="capital_growth_sentiment_pullback",
            weights=(
                ("durable_growth_score", "positive", 0.40),
                ("technical_pullback_score", "positive", 0.30),
                ("sentiment_catalyst_score", "positive", 0.20),
                ("valuation_score", "inverse", 0.10),
            ),
        ),
        Template(
            cohort="capital_equipment_imaging_monitoring",
            template_id="capital_growth_pullback_simple",
            weights=(
                ("durable_growth_score", "positive", 0.55),
                ("technical_pullback_score", "positive", 0.35),
                ("sentiment_catalyst_score", "positive", 0.10),
            ),
        ),
        Template(
            cohort="diagnostics_clinical_tests",
            template_id="diagnostics_growth_reimbursement_momentum",
            weights=(
                ("durable_growth_score", "positive", 0.30),
                ("reimbursement_score", "positive", 0.25),
                ("technical_alpha_score", "positive", 0.20),
                ("sentiment_catalyst_score", "positive", 0.15),
                ("valuation_score", "positive", 0.10),
            ),
        ),
        Template(
            cohort="diagnostics_clinical_tests",
            template_id="diagnostics_reimbursement_quality_value",
            weights=(
                ("reimbursement_score", "positive", 0.35),
                ("fundamental_quality_score", "positive", 0.25),
                ("valuation_score", "positive", 0.20),
                ("value_trap_score", "inverse", 0.10),
                ("sentiment_catalyst_score", "positive", 0.10),
            ),
        ),
        Template(
            cohort="diagnostics_clinical_tests",
            template_id="diagnostics_technical_alpha_growth",
            weights=(
                ("technical_alpha_score", "positive", 0.35),
                ("durable_growth_score", "positive", 0.30),
                ("sentiment_catalyst_score", "positive", 0.20),
                ("valuation_score", "positive", 0.15),
            ),
        ),
        Template(
            cohort="diagnostics_clinical_tests",
            template_id="diagnostics_quality_value_alpha_v2",
            weights=(
                ("fundamental_quality_score", "positive", 0.35),
                ("durable_growth_score", "positive", 0.25),
                ("valuation_score", "positive", 0.20),
                ("technical_alpha_score", "positive", 0.10),
                ("value_trap_score", "inverse", 0.10),
            ),
        ),
        Template(
            cohort="diagnostics_clinical_tests",
            template_id="diagnostics_quality_value_pullback_v2",
            weights=(
                ("fundamental_quality_score", "positive", 0.35),
                ("valuation_score", "positive", 0.25),
                ("technical_pullback_score", "positive", 0.20),
                ("durable_growth_score", "positive", 0.10),
                ("value_trap_score", "inverse", 0.10),
            ),
        ),
        Template(
            cohort="diagnostics_clinical_tests",
            template_id="diagnostics_quality_value_only_v2",
            weights=(
                ("fundamental_quality_score", "positive", 0.45),
                ("valuation_score", "positive", 0.30),
                ("durable_growth_score", "positive", 0.15),
                ("value_trap_score", "inverse", 0.10),
            ),
        ),
        Template(
            cohort="life_science_tools_research_instruments",
            template_id="life_science_quality_value_technical",
            weights=(
                ("fundamental_quality_score", "positive", 0.30),
                ("valuation_score", "positive", 0.25),
                ("technical_alpha_score", "positive", 0.20),
                ("durable_growth_score", "positive", 0.15),
                ("sentiment_catalyst_score", "positive", 0.10),
            ),
        ),
        Template(
            cohort="life_science_tools_research_instruments",
            template_id="life_science_technical_sentiment_growth",
            weights=(
                ("technical_alpha_score", "positive", 0.30),
                ("sentiment_catalyst_score", "positive", 0.25),
                ("durable_growth_score", "positive", 0.25),
                ("valuation_score", "positive", 0.20),
            ),
        ),
        Template(
            cohort="life_science_tools_research_instruments",
            template_id="life_science_quality_value_only",
            weights=(
                ("fundamental_quality_score", "positive", 0.40),
                ("valuation_score", "positive", 0.35),
                ("value_trap_score", "inverse", 0.15),
                ("sentiment_catalyst_score", "positive", 0.10),
            ),
        ),
        Template(
            cohort="life_science_tools_research_instruments",
            template_id="life_science_reimbursement_value_v2",
            weights=(
                ("reimbursement_score", "positive", 0.55),
                ("valuation_score", "positive", 0.30),
                ("value_trap_score", "inverse", 0.15),
            ),
        ),
        Template(
            cohort="life_science_tools_research_instruments",
            template_id="life_science_reimbursement_only_v2",
            weights=(
                ("reimbursement_score", "positive", 0.75),
                ("value_trap_score", "inverse", 0.25),
            ),
        ),
        Template(
            cohort="life_science_tools_research_instruments",
            template_id="life_science_value_reimbursement_v2",
            weights=(
                ("valuation_score", "positive", 0.50),
                ("reimbursement_score", "positive", 0.35),
                ("value_trap_score", "inverse", 0.15),
            ),
        ),
        Template(
            cohort="healthcare_services_cro_other",
            template_id="services_quality_value_growth",
            weights=(
                ("fundamental_quality_score", "positive", 0.35),
                ("valuation_score", "positive", 0.30),
                ("durable_growth_score", "positive", 0.20),
                ("sentiment_catalyst_score", "positive", 0.15),
            ),
        ),
        Template(
            cohort="healthcare_services_cro_other",
            template_id="services_growth_sentiment_value",
            weights=(
                ("durable_growth_score", "positive", 0.35),
                ("sentiment_catalyst_score", "positive", 0.25),
                ("valuation_score", "positive", 0.25),
                ("value_trap_score", "inverse", 0.15),
            ),
        ),
        Template(
            cohort="healthcare_services_cro_other",
            template_id="services_pullback_fundamental_sentiment_v2",
            weights=(
                ("technical_pullback_score", "positive", 0.40),
                ("fundamental_quality_score", "positive", 0.30),
                ("sentiment_catalyst_score", "positive", 0.20),
                ("value_trap_score", "inverse", 0.10),
            ),
        ),
        Template(
            cohort="healthcare_services_cro_other",
            template_id="services_fundamental_sentiment_fda_inverse_v2",
            weights=(
                ("fundamental_quality_score", "positive", 0.35),
                ("sentiment_catalyst_score", "positive", 0.25),
                ("fda_product_score", "inverse", 0.25),
                ("value_trap_score", "inverse", 0.15),
            ),
        ),
        Template(
            cohort="healthcare_services_cro_other",
            template_id="services_technical_core_inverse_v2",
            weights=(
                ("technical_core_score", "inverse", 0.40),
                ("fundamental_quality_score", "positive", 0.25),
                ("sentiment_catalyst_score", "positive", 0.20),
                ("value_trap_score", "inverse", 0.15),
            ),
        ),
        Template(
            cohort="diabetes_wearables_drug_delivery",
            template_id="diabetes_growth_reimbursement_technical",
            weights=(
                ("durable_growth_score", "positive", 0.30),
                ("reimbursement_score", "positive", 0.25),
                ("technical_alpha_score", "positive", 0.25),
                ("fda_product_score", "positive", 0.10),
                ("valuation_score", "positive", 0.10),
            ),
        ),
        Template(
            cohort="diabetes_wearables_drug_delivery",
            template_id="diabetes_reimbursement_quality_growth",
            weights=(
                ("reimbursement_score", "positive", 0.35),
                ("fundamental_quality_score", "positive", 0.25),
                ("durable_growth_score", "positive", 0.20),
                ("technical_alpha_score", "positive", 0.10),
                ("value_trap_score", "inverse", 0.10),
            ),
        ),
        Template(
            cohort="diabetes_wearables_drug_delivery",
            template_id="diabetes_growth_fda_reimbursement_v2",
            weights=(
                ("durable_growth_score", "positive", 0.35),
                ("fda_product_score", "positive", 0.25),
                ("reimbursement_score", "positive", 0.20),
                ("valuation_score", "positive", 0.15),
                ("value_trap_score", "inverse", 0.05),
            ),
        ),
        Template(
            cohort="diabetes_wearables_drug_delivery",
            template_id="diabetes_raw_signal_trimmed_v2",
            weights=(
                ("durable_growth_score", "positive", 0.30),
                ("reimbursement_score", "positive", 0.25),
                ("valuation_score", "positive", 0.20),
                ("technical_alpha_score", "positive", 0.15),
                ("fda_product_score", "positive", 0.10),
            ),
        ),
        Template(
            cohort="surgical_robotics_platforms",
            template_id="robotics_growth_sentiment_technical",
            weights=(
                ("durable_growth_score", "positive", 0.35),
                ("sentiment_catalyst_score", "positive", 0.25),
                ("technical_alpha_score", "positive", 0.20),
                ("valuation_score", "positive", 0.20),
            ),
        ),
        Template(
            cohort="surgical_robotics_platforms",
            template_id="robotics_quality_value_growth",
            weights=(
                ("fundamental_quality_score", "positive", 0.35),
                ("valuation_score", "positive", 0.30),
                ("durable_growth_score", "positive", 0.20),
                ("value_trap_score", "inverse", 0.15),
            ),
        ),
        Template(
            cohort="surgical_robotics_platforms",
            template_id="robotics_quality_value_inverse_growth_v2",
            weights=(
                ("fundamental_quality_score", "positive", 0.30),
                ("valuation_score", "positive", 0.25),
                ("durable_growth_score", "inverse", 0.20),
                ("technical_alpha_score", "inverse", 0.15),
                ("value_trap_score", "inverse", 0.10),
            ),
        ),
        Template(
            cohort="surgical_robotics_platforms",
            template_id="robotics_fda_reimbursement_quality_v2",
            weights=(
                ("fda_product_score", "positive", 0.30),
                ("reimbursement_score", "positive", 0.30),
                ("fundamental_quality_score", "positive", 0.25),
                ("value_trap_score", "inverse", 0.15),
            ),
        ),
    ]


def to_float(raw: object) -> float | None:
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def score_or(raw: object, default: float = 50.0) -> float:
    value = to_float(raw)
    return default if value is None else max(0.0, min(100.0, value))


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def split_for_row(row: dict[str, Any], config: dict[str, Any]) -> str:
    asof = str(row.get("asof_date") or "")[:10]
    train_end = str(cfg_get(config, "calibration.train_end_asof", "2025-05-30"))
    validation_start = str(cfg_get(config, "calibration.validation_start_asof", "2025-06-06"))
    validation_end = str(cfg_get(config, "calibration.validation_end_asof", "2025-11-28"))
    if asof <= train_end:
        return "train"
    if validation_start <= asof <= validation_end:
        return "validation"
    return "holdout_or_incomplete"


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
    if direction == "neutral":
        return 50.0
    raise ValueError(f"Unknown component direction {direction!r}")


def template_score(row: dict[str, Any], template: Template) -> float:
    total = sum(weight for _, _, weight in template.weights)
    if total <= 0:
        return 50.0
    score = sum(component_score(row, field, direction) * weight for field, direction, weight in template.weights) / total
    return round(max(0.0, min(100.0, score)), 6)


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
            percentile = round(100.0 * (1.0 - (pos / denom)), 2)
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


def baseline_rows(cohort_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = [dict(row) for row in cohort_rows]
    for row in out:
        row["sim_score"] = score_or(row.get("raw_composite_score"), score_or(row.get("composite_score"), 50.0))
        row["sim_cohort_rank_bucket"] = row.get("cohort_rank_bucket") or ""
        row["sim_cohort_percentile"] = row.get("cohort_percentile") or ""
    return out


def simulated_rows(cohort_rows: list[dict[str, Any]], template: Template) -> list[dict[str, Any]]:
    out = [dict(row) for row in cohort_rows]
    for row in out:
        row["sim_score"] = template_score(row, template)
    rank_bucket_for_group(out, score_field="sim_score")
    return out


def selected_rows(rows: list[dict[str, Any]], *, split: str, config: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        row for row in rows
        if row.get("sim_cohort_rank_bucket") == "cohort_top_decile"
        and (split == "all" or split_for_row(row, config) == split)
        and to_float(row.get("forward_return_120d")) is not None
        and to_float(row.get("cohort_excess_return_120d")) is not None
    ]


def metrics(rows: list[dict[str, Any]], *, split: str, config: dict[str, Any], full_rows: list[dict[str, Any]]) -> dict[str, Any]:
    selected = selected_rows(rows, split=split, config=config)
    returns = [float(row["forward_return_120d"]) for row in selected]
    excess = [float(row["cohort_excess_return_120d"]) for row in selected]
    tickers = {str(row.get("ticker") or "") for row in selected}
    full_tickers = {str(row.get("ticker") or "") for row in full_rows}
    if not selected:
        return {
            "count": 0,
            "unique_tickers": 0,
            "selected_ticker_coverage": 0.0,
            "mean_return_120d": 0.0,
            "median_return_120d": 0.0,
            "hit_rate_120d": 0.0,
            "mean_excess_120d": 0.0,
            "median_excess_120d": 0.0,
            "excess_hit_rate_120d": 0.0,
            "lcb_excess_120d": 0.0,
        }
    return {
        "count": len(selected),
        "unique_tickers": len(tickers),
        "selected_ticker_coverage": len(tickers) / len(full_tickers) if full_tickers else 0.0,
        "mean_return_120d": mean(returns),
        "median_return_120d": median(returns),
        "hit_rate_120d": sum(1 for value in returns if value > 0) / len(returns),
        "mean_excess_120d": mean(excess),
        "median_excess_120d": median(excess),
        "excess_hit_rate_120d": sum(1 for value in excess if value > 0) / len(excess),
        "lcb_excess_120d": lcb(excess),
    }


def selected_ticker_means(rows: list[dict[str, Any]], *, config: dict[str, Any]) -> dict[str, float]:
    grouped: dict[str, list[float]] = defaultdict(list)
    for row in selected_rows(rows, split="validation", config=config):
        value = to_float(row.get("cohort_excess_return_120d"))
        if value is not None:
            grouped[str(row.get("ticker") or "")].append(value)
    return {ticker: mean(values) for ticker, values in grouped.items() if values}


def weights_spec(template: Template) -> str:
    return ";".join(f"{field}:{direction}:{weight:.2f}" for field, direction, weight in template.weights)


def fmt(value: object) -> str:
    number = to_float(value)
    return "" if number is None else f"{number:.6f}"


def add_status(rows: list[dict[str, Any]], *, baseline_by_cohort_split: dict[tuple[str, str], dict[str, Any]], baseline_ticker_means: dict[str, dict[str, float]], config: dict[str, Any]) -> None:
    min_improved = float(cfg_get(config, "calibration.restricted_cohort_templates.min_improved_selected_ticker_rate", 0.50))
    for row in rows:
        baseline = baseline_by_cohort_split.get((row["calibration_cohort"], row["split"]))
        for field in ("mean_excess_120d", "median_excess_120d", "excess_hit_rate_120d", "lcb_excess_120d"):
            row[f"delta_{field.replace('_120d', '')}_vs_baseline"] = (
                row[field] - baseline[field] if baseline else 0.0
            )
        if row["template_id"] == "baseline_existing":
            row["improved_selected_ticker_rate"] = 0.0
            row["promotion_status"] = "baseline"
            row["promotion_reason"] = "baseline_reference"
            continue
        if row["split"] != "validation":
            row["improved_selected_ticker_rate"] = 0.0
            row["promotion_status"] = "not_evaluated_split"
            row["promotion_reason"] = "promotion checks use validation split"
            continue
        selected_means = row.pop("_selected_ticker_means", {})
        base_means = baseline_ticker_means.get(row["calibration_cohort"], {})
        comparable = [ticker for ticker in selected_means if ticker in base_means]
        improved = [ticker for ticker in comparable if selected_means[ticker] > base_means[ticker]]
        improved_rate = len(improved) / len(comparable) if comparable else 0.0
        row["improved_selected_ticker_rate"] = improved_rate
        reasons: list[str] = []
        if not baseline:
            reasons.append("missing_baseline")
        else:
            if row["median_excess_120d"] <= baseline["median_excess_120d"]:
                reasons.append("median_excess_not_improved")
            if row["lcb_excess_120d"] <= baseline["lcb_excess_120d"]:
                reasons.append("lcb_excess_not_improved")
            if row["excess_hit_rate_120d"] < baseline["excess_hit_rate_120d"]:
                reasons.append("excess_hit_rate_below_baseline")
            if row["unique_tickers"] < baseline["unique_tickers"]:
                reasons.append("selected_ticker_count_below_baseline")
        if improved_rate < min_improved:
            reasons.append("improved_selected_ticker_rate_below_min")
        row["promotion_status"] = "candidate" if not reasons else "reject"
        row["promotion_reason"] = ";".join(reasons) if reasons else "passes_restricted_cohort_template_checks"


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
                "calibration.restricted_cohort_templates.output_csv",
                "../output/med_devices_reports/calibration/med_device_restricted_cohort_template_results.csv",
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
                "calibration.restricted_cohort_templates.recommendation_csv",
                "../output/med_devices_reports/calibration/med_device_restricted_cohort_template_recommendations.csv",
            ),
            base_dir=base_dir,
        )
    )
    rows = read_csv(input_csv)
    by_cohort: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_cohort[str(row.get("calibration_cohort") or "")].append(row)
    output_rows: list[dict[str, Any]] = []
    baseline_by_cohort_split: dict[tuple[str, str], dict[str, Any]] = {}
    baseline_ticker_means: dict[str, dict[str, float]] = {}
    for cohort in sorted({template.cohort for template in templates()}):
        cohort_rows = by_cohort.get(cohort, [])
        if not cohort_rows:
            continue
        baseline = baseline_rows(cohort_rows)
        baseline_ticker_means[cohort] = selected_ticker_means(baseline, config=config)
        for split in ("all", "train", "validation"):
            item = {
                "calibration_cohort": cohort,
                "template_id": "baseline_existing",
                "split": split,
                "weights_spec": "",
            }
            item.update(metrics(baseline, split=split, config=config, full_rows=cohort_rows))
            baseline_by_cohort_split[(cohort, split)] = item
            output_rows.append(item)
        for template in [template for template in templates() if template.cohort == cohort]:
            simulated = simulated_rows(cohort_rows, template)
            ticker_means = selected_ticker_means(simulated, config=config)
            for split in ("all", "train", "validation"):
                item = {
                    "calibration_cohort": cohort,
                    "template_id": template.template_id,
                    "split": split,
                    "weights_spec": weights_spec(template),
                    "_selected_ticker_means": ticker_means if split == "validation" else {},
                }
                item.update(metrics(simulated, split=split, config=config, full_rows=cohort_rows))
                output_rows.append(item)
    add_status(
        output_rows,
        baseline_by_cohort_split=baseline_by_cohort_split,
        baseline_ticker_means=baseline_ticker_means,
        config=config,
    )
    recommendations: list[dict[str, Any]] = []
    for cohort in sorted({row["calibration_cohort"] for row in output_rows}):
        candidates = [
            row for row in output_rows
            if row["calibration_cohort"] == cohort
            and row["split"] == "validation"
            and row["promotion_status"] == "candidate"
        ]
        candidates.sort(
            key=lambda row: (
                row["lcb_excess_120d"],
                row["median_excess_120d"],
                row["mean_excess_120d"],
                row["excess_hit_rate_120d"],
            ),
            reverse=True,
        )
        best = candidates[0] if candidates else None
        recommendations.append(
            {
                "calibration_cohort": cohort,
                "recommended_template_id": best["template_id"] if best else "",
                "promotion_status": "candidate" if best else "no_candidate",
                "validation_mean_excess_120d": best["mean_excess_120d"] if best else "",
                "validation_median_excess_120d": best["median_excess_120d"] if best else "",
                "validation_excess_hit_rate_120d": best["excess_hit_rate_120d"] if best else "",
                "validation_lcb_excess_120d": best["lcb_excess_120d"] if best else "",
                "validation_unique_tickers": best["unique_tickers"] if best else "",
                "validation_selected_ticker_coverage": best["selected_ticker_coverage"] if best else "",
                "improved_selected_ticker_rate": best["improved_selected_ticker_rate"] if best else "",
                "promotion_reason": best["promotion_reason"] if best else "no tested template passed restricted cohort checks",
            }
        )
    for row in output_rows:
        row.pop("_selected_ticker_means", None)
        for field in OUTPUT_FIELDS:
            if field in {"calibration_cohort", "template_id", "split", "promotion_status", "promotion_reason", "weights_spec"}:
                continue
            row[field] = fmt(row.get(field))
    write_csv(output_csv, output_rows, OUTPUT_FIELDS)
    write_csv(recommendation_csv, recommendations, RECOMMENDATION_FIELDS)
    print(f"restricted_cohort_template_results_csv={output_csv} rows={len(output_rows)}")
    print(f"restricted_cohort_template_recommendations_csv={recommendation_csv} rows={len(recommendations)}")


if __name__ == "__main__":
    main()
