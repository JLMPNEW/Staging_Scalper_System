#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
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
from med_devices.core.logging_utils import configure_utc_logging  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
DEFAULT_COHORTS = (
    "capital_equipment_procedure_platforms",
    "diagnostics_clinical_tests",
    "elective_vision_dental_aesthetic_devices",
    "emerging_single_product_therapeutic_platforms",
    "healthcare_services_cro_lab_services",
    "home_chronic_care_devices_dme_drug_delivery",
    "hospital_supplies_surgical_consumables_oem",
    "implantable_interventional_devices_direct_payment",
    "life_science_tools_research_instruments",
    "orthopedics_spine_sports_implants",
)
COMPONENT_FIELDS = [
    "raw_composite_score",
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
]
INVERSE_COMPONENT_FIELDS = {"value_trap_score"}
OUTPUT_FIELDS = [
    "calibration_cohort",
    "split",
    "horizon_days",
    "component",
    "count",
    "unique_tickers",
    "unique_asof_dates",
    "spearman_ic_excess",
    "pearson_ic_excess",
    "top_decile_count",
    "top_decile_unique_tickers",
    "top_decile_mean_excess",
    "top_decile_median_excess",
    "top_decile_lcb_excess",
    "top_decile_hit_rate_excess",
    "top_quintile_count",
    "top_quintile_median_excess",
    "bottom_quintile_count",
    "bottom_quintile_median_excess",
    "top_minus_bottom_median_excess",
    "inverse_top_decile_count",
    "inverse_top_decile_unique_tickers",
    "inverse_top_decile_mean_excess",
    "inverse_top_decile_median_excess",
    "inverse_top_decile_lcb_excess",
    "inverse_top_decile_hit_rate_excess",
    "inverse_top_minus_bottom_median_excess",
    "best_direction",
    "component_action",
    "diagnostic_reason",
]
SUMMARY_FIELDS = [
    "calibration_cohort",
    "horizon_days",
    "best_positive_component",
    "best_positive_median_excess",
    "best_positive_lcb_excess",
    "best_inverse_component",
    "best_inverse_median_excess",
    "best_inverse_lcb_excess",
    "components_to_keep",
    "components_to_invert_or_pullback",
    "components_to_neutralize",
    "components_as_risk_gate_only",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Diagnose component sleeves for weak med-device calibration cohorts.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--input-csv", type=Path, default=None)
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--summary-csv", type=Path, default=None)
    parser.add_argument("--cohorts", type=str, default="")
    return parser.parse_args()


def parse_csv_list(raw: object) -> list[str]:
    return [item.strip() for item in str(raw or "").split(",") if item.strip()]


def to_float(raw: object) -> float | None:
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def fmt(value: float | None) -> str:
    return "" if value is None else f"{value:.6f}"


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def return_horizons(rows: list[dict[str, str]]) -> list[int]:
    if not rows:
        return []
    horizons: list[int] = []
    for key in rows[0]:
        if key.startswith("cohort_excess_return_") and key.endswith("d"):
            text = key[len("cohort_excess_return_") : -1]
            if text.isdigit():
                horizons.append(int(text))
    return sorted(horizons)


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
    if len(xs) < 5 or len(xs) != len(ys):
        return None
    mx = mean(xs)
    my = mean(ys)
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if sx <= 1e-12 or sy <= 1e-12:
        return 0.0
    return max(-1.0, min(1.0, cov / (sx * sy)))


def spearman(xs: list[float], ys: list[float]) -> float | None:
    return correlation(fractional_rank(xs), fractional_rank(ys))


def lcb(values: list[float], z: float = 1.64) -> float | None:
    if not values:
        return None
    if len(values) < 2:
        return values[0]
    avg = mean(values)
    variance = sum((value - avg) ** 2 for value in values) / (len(values) - 1)
    return avg - z * math.sqrt(variance / len(values))


def metrics(values: list[tuple[str, str, float]]) -> dict[str, float | int | None]:
    returns = [item[2] for item in values]
    tickers = {item[0] for item in values}
    return {
        "count": len(values),
        "unique_tickers": len(tickers),
        "mean_excess": mean(returns) if returns else None,
        "median_excess": median(returns) if returns else None,
        "lcb_excess": lcb(returns),
        "hit_rate": (sum(1 for value in returns if value > 0) / len(returns)) if returns else None,
    }


def component_percentile_rows(
    rows: list[dict[str, Any]],
    *,
    cohort: str,
    split: str,
    horizon: int,
    component: str,
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    higher_is_better = component not in INVERSE_COMPONENT_FIELDS
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if str(row.get("calibration_cohort") or "") != cohort:
            continue
        if split != "all" and split_for_row(row, config) != split:
            continue
        component_value = to_float(row.get(component))
        excess = to_float(row.get(f"cohort_excess_return_{horizon}d"))
        if component_value is None or excess is None:
            continue
        grouped[str(row.get("asof_date") or "")[:10]].append(row)
    out: list[dict[str, Any]] = []
    for asof, items in grouped.items():
        sortable = sorted(
            items,
            key=lambda item: (
                to_float(item.get(component)) if to_float(item.get(component)) is not None else -math.inf,
                str(item.get("ticker") or ""),
            ),
            reverse=not higher_is_better,
        )
        if len(sortable) == 1:
            item = sortable[0]
            out.append(
                {
                    "ticker": str(item.get("ticker") or ""),
                    "asof_date": asof,
                    "component_value": to_float(item.get(component)),
                    "excess": to_float(item.get(f"cohort_excess_return_{horizon}d")),
                    "percentile": 50.0,
                }
            )
            continue
        denom = len(sortable) - 1
        for pos, item in enumerate(sortable):
            out.append(
                {
                    "ticker": str(item.get("ticker") or ""),
                    "asof_date": asof,
                    "component_value": to_float(item.get(component)),
                    "excess": to_float(item.get(f"cohort_excess_return_{horizon}d")),
                    "percentile": round(100.0 * (pos / denom), 2),
                }
            )
    return out


def action_for_component(
    *,
    component: str,
    count: int,
    unique_tickers: int,
    ic: float | None,
    top_spread: float | None,
    inverse_spread: float | None,
    top_lcb: float | None,
    inverse_lcb: float | None,
) -> tuple[str, str, str]:
    if count < 50 or unique_tickers < 3:
        return "neutral", "insufficient_observations", "insufficient row or ticker coverage"
    top_score = (top_spread or 0.0) + 0.5 * (top_lcb or 0.0)
    inverse_score = (inverse_spread or 0.0) + 0.5 * (inverse_lcb or 0.0)
    if component == "value_trap_score":
        if ic is not None and ic < -0.05 and inverse_lcb is not None and inverse_lcb > 0:
            return "inverse", "risk_gate_or_inverse", "lower value-trap scores are associated with better excess returns"
        return "neutral", "risk_gate_only", "value-trap signal should remain a gate unless inverse evidence is strong"
    if top_score > 0.03 and (ic is None or ic > -0.03):
        return "positive", "keep_or_increase_weight", "top-ranked component bucket has positive spread and LCB support"
    if inverse_score > 0.03 and (ic is None or ic < 0.03):
        return "inverse", "invert_or_pullback_test", "low-ranked component bucket is stronger than high-ranked bucket"
    if top_score < -0.03 and inverse_score < -0.03:
        return "neutral", "neutralize_or_gate_only", "both positive and inverse buckets have weak forward excess"
    return "neutral", "neutralize_until_stronger", "component evidence is weak, mixed, or unstable"


def analyze_component(
    rows: list[dict[str, Any]],
    *,
    cohort: str,
    split: str,
    horizon: int,
    component: str,
    config: dict[str, Any],
) -> dict[str, Any]:
    ranked = component_percentile_rows(
        rows,
        cohort=cohort,
        split=split,
        horizon=horizon,
        component=component,
        config=config,
    )
    xs = [float(item["component_value"]) for item in ranked if item["component_value"] is not None]
    ys = [float(item["excess"]) for item in ranked if item["excess"] is not None]
    top_decile = [(item["ticker"], item["asof_date"], float(item["excess"])) for item in ranked if item["percentile"] >= 90]
    top_quintile = [(item["ticker"], item["asof_date"], float(item["excess"])) for item in ranked if item["percentile"] >= 80]
    bottom_quintile = [(item["ticker"], item["asof_date"], float(item["excess"])) for item in ranked if item["percentile"] <= 20]
    inverse_top_decile = [(item["ticker"], item["asof_date"], float(item["excess"])) for item in ranked if item["percentile"] <= 10]
    top_decile_metrics = metrics(top_decile)
    inverse_top_decile_metrics = metrics(inverse_top_decile)
    top_quintile_metrics = metrics(top_quintile)
    bottom_quintile_metrics = metrics(bottom_quintile)
    top_spread = (
        float(top_quintile_metrics["median_excess"]) - float(bottom_quintile_metrics["median_excess"])
        if top_quintile_metrics["median_excess"] is not None and bottom_quintile_metrics["median_excess"] is not None
        else None
    )
    inverse_spread = -top_spread if top_spread is not None else None
    ic = spearman(xs, ys)
    direction, action, reason = action_for_component(
        component=component,
        count=len(ranked),
        unique_tickers=len({item["ticker"] for item in ranked}),
        ic=ic,
        top_spread=top_spread,
        inverse_spread=inverse_spread,
        top_lcb=to_float(top_decile_metrics["lcb_excess"]),
        inverse_lcb=to_float(inverse_top_decile_metrics["lcb_excess"]),
    )
    return {
        "calibration_cohort": cohort,
        "split": split,
        "horizon_days": horizon,
        "component": component,
        "count": len(ranked),
        "unique_tickers": len({item["ticker"] for item in ranked}),
        "unique_asof_dates": len({item["asof_date"] for item in ranked}),
        "spearman_ic_excess": fmt(ic),
        "pearson_ic_excess": fmt(correlation(xs, ys)),
        "top_decile_count": top_decile_metrics["count"],
        "top_decile_unique_tickers": top_decile_metrics["unique_tickers"],
        "top_decile_mean_excess": fmt(to_float(top_decile_metrics["mean_excess"])),
        "top_decile_median_excess": fmt(to_float(top_decile_metrics["median_excess"])),
        "top_decile_lcb_excess": fmt(to_float(top_decile_metrics["lcb_excess"])),
        "top_decile_hit_rate_excess": fmt(to_float(top_decile_metrics["hit_rate"])),
        "top_quintile_count": top_quintile_metrics["count"],
        "top_quintile_median_excess": fmt(to_float(top_quintile_metrics["median_excess"])),
        "bottom_quintile_count": bottom_quintile_metrics["count"],
        "bottom_quintile_median_excess": fmt(to_float(bottom_quintile_metrics["median_excess"])),
        "top_minus_bottom_median_excess": fmt(top_spread),
        "inverse_top_decile_count": inverse_top_decile_metrics["count"],
        "inverse_top_decile_unique_tickers": inverse_top_decile_metrics["unique_tickers"],
        "inverse_top_decile_mean_excess": fmt(to_float(inverse_top_decile_metrics["mean_excess"])),
        "inverse_top_decile_median_excess": fmt(to_float(inverse_top_decile_metrics["median_excess"])),
        "inverse_top_decile_lcb_excess": fmt(to_float(inverse_top_decile_metrics["lcb_excess"])),
        "inverse_top_decile_hit_rate_excess": fmt(to_float(inverse_top_decile_metrics["hit_rate"])),
        "inverse_top_minus_bottom_median_excess": fmt(inverse_spread),
        "best_direction": direction,
        "component_action": action,
        "diagnostic_reason": reason,
    }


def build_summary(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row["split"] == "validation":
            grouped[(row["calibration_cohort"], int(row["horizon_days"]))].append(row)
    out: list[dict[str, Any]] = []
    for (cohort, horizon), items in sorted(grouped.items()):
        positive = [row for row in items if row["best_direction"] == "positive"]
        inverse = [row for row in items if row["best_direction"] == "inverse"]
        positive.sort(
            key=lambda row: (
                to_float(row.get("top_decile_lcb_excess")) or -999.0,
                to_float(row.get("top_decile_median_excess")) or -999.0,
            ),
            reverse=True,
        )
        inverse.sort(
            key=lambda row: (
                to_float(row.get("inverse_top_decile_lcb_excess")) or -999.0,
                to_float(row.get("inverse_top_decile_median_excess")) or -999.0,
            ),
            reverse=True,
        )
        best_positive = positive[0] if positive else {}
        best_inverse = inverse[0] if inverse else {}
        out.append(
            {
                "calibration_cohort": cohort,
                "horizon_days": horizon,
                "best_positive_component": best_positive.get("component", ""),
                "best_positive_median_excess": best_positive.get("top_decile_median_excess", ""),
                "best_positive_lcb_excess": best_positive.get("top_decile_lcb_excess", ""),
                "best_inverse_component": best_inverse.get("component", ""),
                "best_inverse_median_excess": best_inverse.get("inverse_top_decile_median_excess", ""),
                "best_inverse_lcb_excess": best_inverse.get("inverse_top_decile_lcb_excess", ""),
                "components_to_keep": ";".join(row["component"] for row in items if row["component_action"] == "keep_or_increase_weight"),
                "components_to_invert_or_pullback": ";".join(row["component"] for row in items if row["component_action"] == "invert_or_pullback_test"),
                "components_to_neutralize": ";".join(
                    row["component"]
                    for row in items
                    if row["component_action"] in {"neutralize_until_stronger", "neutralize_or_gate_only"}
                ),
                "components_as_risk_gate_only": ";".join(row["component"] for row in items if row["component_action"] == "risk_gate_only"),
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
                "calibration.bad_cohort_component_diagnostics.output_csv",
                "../output/med_devices_reports/calibration/med_device_bad_cohort_component_diagnostics.csv",
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
                "calibration.bad_cohort_component_diagnostics.summary_csv",
                "../output/med_devices_reports/calibration/med_device_bad_cohort_component_diagnostic_summary.csv",
            ),
            base_dir=base_dir,
        )
    )
    rows = read_csv(input_csv)
    cohorts = parse_csv_list(args.cohorts) or parse_csv_list(
        cfg_get(config, "calibration.bad_cohort_component_diagnostics.cohorts", ",".join(DEFAULT_COHORTS))
    )
    horizons = return_horizons(rows)
    output_rows = [
        analyze_component(rows, cohort=cohort, split=split, horizon=horizon, component=component, config=config)
        for cohort in cohorts
        for split in ("all", "train", "validation")
        for horizon in horizons
        for component in COMPONENT_FIELDS
    ]
    summary_rows = build_summary(output_rows)
    write_csv(output_csv, output_rows, OUTPUT_FIELDS)
    write_csv(summary_csv, summary_rows, SUMMARY_FIELDS)
    print(f"bad_cohort_component_diagnostics_csv={output_csv} rows={len(output_rows)}")
    print(f"bad_cohort_component_diagnostic_summary_csv={summary_csv} rows={len(summary_rows)}")


if __name__ == "__main__":
    main()
