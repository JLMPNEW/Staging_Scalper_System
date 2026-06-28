#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from med_devices.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from med_devices.core.logging_utils import configure_utc_logging  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
PROMOTE_ACTION = "promote_to_cohort_policy_review"
RECOMMENDATION_FIELDS = [
    "calibration_cohort",
    "recommendation_rank",
    "recommended_action",
    "sleeve",
    "direction",
    "primary_component",
    "supporting_components",
    "supporting_component_count",
    "implementation_target",
    "implementation_note",
    "risk_level",
    "evidence_score",
    "min_ticker_coverage_pct",
    "max_single_ticker_share",
    "eligible_cohort_tickers",
    "primary_60d_count",
    "primary_60d_unique_tickers",
    "primary_60d_gross_ic",
    "primary_60d_net_ic",
    "primary_60d_factor_neutral_ic",
    "primary_60d_net_spread",
    "primary_120d_count",
    "primary_120d_unique_tickers",
    "primary_120d_gross_ic",
    "primary_120d_net_ic",
    "primary_120d_factor_neutral_ic",
    "primary_120d_net_spread",
    "recommendation_reason",
]
SUMMARY_FIELDS = [
    "calibration_cohort",
    "production_config_candidates",
    "guarded_candidates",
    "deferred_candidates",
    "top_sleeves",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build cohort-level policy recommendations from component review rows.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--input-csv", type=Path, default=None)
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--summary-csv", type=Path, default=None)
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


def to_int(raw: object, default: int = 0) -> int:
    try:
        return int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return default


def fmt(value: float | None, digits: int = 6) -> str:
    return "" if value is None else f"{value:.{digits}f}"


def pct_value(raw: object) -> float:
    value = to_float(raw)
    return 0.0 if value is None else value / 100.0


def component_sleeve(component: str) -> str:
    if component.endswith("_interaction_score"):
        return "interaction_shadow"
    if component.startswith("technical_"):
        return "technical"
    if component.startswith("durable_growth"):
        return "durable_growth"
    if component.startswith("fda_"):
        return "fda_regulatory"
    if component.startswith("reimbursement"):
        return "reimbursement"
    if component.startswith("sentiment"):
        return "sentiment"
    if component.startswith("valuation"):
        return "valuation"
    if component.startswith("fundamental"):
        return "fundamental_quality"
    if component.startswith("value_trap"):
        return "value_trap"
    return "other"


def implementation_target(sleeve: str, direction: str) -> tuple[str, str, str]:
    if sleeve == "technical":
        return (
            "cohort_profiles.<cohort>.technical_policy_or_score_template",
            "Use as cohort-specific technical overlay; avoid hard Tier 1 gate until walk-forward confirms.",
            "medium",
        )
    if sleeve == "valuation":
        return (
            "cohort_profiles.<cohort>.composite_weights.valuation",
            "Consider modest valuation weight increase or template term; keep value-trap cap active.",
            "low",
        )
    if sleeve == "fundamental_quality":
        return (
            "cohort_profiles.<cohort>.composite_weights.fundamental_quality",
            "Consider modest fundamental-quality weight increase.",
            "low",
        )
    if sleeve == "fda_regulatory":
        return (
            "cohort_profiles.<cohort>.fda_policy_or_score_template",
            "Use regulatory sleeve as risk/quality overlay; inverse event-risk signals should remain veto-aware.",
            "medium",
        )
    if sleeve == "reimbursement":
        qualifier = "inverse" if direction == "inverse" else "positive"
        return (
            "cohort_profiles.<cohort>.reimbursement_policy_or_score_template",
            f"Review {qualifier} reimbursement signal manually before production because payer evidence is cohort-specific.",
            "high" if direction == "inverse" else "medium",
        )
    if sleeve == "durable_growth":
        return (
            "cohort_profiles.<cohort>.durable_growth_policy",
            "Guarded only: use legacy champion or research-only challenger controls; do not promote nonlegacy v2 globally.",
            "high",
        )
    if sleeve == "sentiment":
        return (
            "cohort_profiles.<cohort>.sentiment_or_score_template",
            "Use only as small overlay; sentiment direction should not override quality/liquidity gates.",
            "medium",
        )
    return (
        "cohort_profiles.<cohort>.score_template",
        "Manual review required before config translation.",
        "high",
    )


def evidence_score(row_120: dict[str, str]) -> float:
    gross_ic = abs(to_float(row_120.get("spearman_ic_excess")) or 0.0)
    net_ic = abs(to_float(row_120.get("net_spearman_ic_excess")) or 0.0)
    neutral_ic = abs(to_float(row_120.get("factor_neutral_spearman_ic_excess")) or 0.0)
    net_spread = abs(to_float(row_120.get("net_top_minus_bottom_median_excess")) or 0.0)
    coverage = pct_value(row_120.get("ticker_coverage_pct"))
    concentration_penalty = to_float(row_120.get("max_single_ticker_share")) or 0.0
    return (
        0.40 * neutral_ic
        + 0.30 * net_ic
        + 0.20 * gross_ic
        + 0.10 * min(0.25, net_spread)
        + 0.02 * coverage
        - 0.02 * concentration_penalty
    )


def paired_component_rows(rows: list[dict[str, str]]) -> dict[tuple[str, str, str], dict[int, dict[str, str]]]:
    grouped: dict[tuple[str, str, str], dict[int, dict[str, str]]] = defaultdict(dict)
    for row in rows:
        if str(row.get("review_action") or "") != PROMOTE_ACTION:
            continue
        direction = str(row.get("direction") or "")
        horizon = to_int(row.get("horizon_days"))
        if horizon not in {60, 120} or direction not in {"positive", "inverse"}:
            continue
        key = (
            str(row.get("calibration_cohort") or ""),
            str(row.get("component") or ""),
            direction,
        )
        grouped[key][horizon] = row
    return {key: value for key, value in grouped.items() if 60 in value and 120 in value}


def build_recommendations(
    rows: list[dict[str, str]],
    *,
    max_recommendations_per_cohort: int,
    max_high_risk_recommendations_per_cohort: int,
    min_supporting_components: int,
) -> list[dict[str, Any]]:
    paired = paired_component_rows(rows)
    sleeve_groups: dict[tuple[str, str, str], list[tuple[str, dict[int, dict[str, str]]]]] = defaultdict(list)
    for (cohort, component, direction), horizons in paired.items():
        sleeve_groups[(cohort, component_sleeve(component), direction)].append((component, horizons))

    candidates: list[dict[str, Any]] = []
    for (cohort, sleeve, direction), items in sleeve_groups.items():
        if len(items) < min_supporting_components:
            continue
        ranked_items = sorted(items, key=lambda item: evidence_score(item[1][120]), reverse=True)
        primary_component, primary_horizons = ranked_items[0]
        row_60 = primary_horizons[60]
        row_120 = primary_horizons[120]
        min_coverage = min(pct_value(row_60.get("ticker_coverage_pct")), pct_value(row_120.get("ticker_coverage_pct")))
        max_share = max(to_float(row_60.get("max_single_ticker_share")) or 0.0, to_float(row_120.get("max_single_ticker_share")) or 0.0)
        target, note, risk = implementation_target(sleeve, direction)
        reasons = [
            "gross_net_factor_neutral_pass",
            "same_direction_60d_120d",
            f"{len(items)}_paired_component_support",
            f"{100.0 * min_coverage:.0f}pct_min_ticker_coverage",
        ]
        action = "production_config_candidate"
        if risk == "high":
            action = "guarded_policy_candidate"
        candidates.append(
            {
                "calibration_cohort": cohort,
                "recommendation_rank": 0,
                "recommended_action": action,
                "sleeve": sleeve,
                "direction": direction,
                "primary_component": primary_component,
                "supporting_components": ",".join(component for component, _ in ranked_items),
                "supporting_component_count": len(items),
                "implementation_target": target,
                "implementation_note": note,
                "risk_level": risk,
                "evidence_score": fmt(evidence_score(row_120), 8),
                "min_ticker_coverage_pct": fmt(100.0 * min_coverage, 2),
                "max_single_ticker_share": fmt(max_share, 4),
                "eligible_cohort_tickers": row_120.get("eligible_cohort_tickers") or "",
                "primary_60d_count": row_60.get("count") or "",
                "primary_60d_unique_tickers": row_60.get("unique_tickers") or "",
                "primary_60d_gross_ic": row_60.get("spearman_ic_excess") or "",
                "primary_60d_net_ic": row_60.get("net_spearman_ic_excess") or "",
                "primary_60d_factor_neutral_ic": row_60.get("factor_neutral_spearman_ic_excess") or "",
                "primary_60d_net_spread": row_60.get("net_top_minus_bottom_median_excess") or "",
                "primary_120d_count": row_120.get("count") or "",
                "primary_120d_unique_tickers": row_120.get("unique_tickers") or "",
                "primary_120d_gross_ic": row_120.get("spearman_ic_excess") or "",
                "primary_120d_net_ic": row_120.get("net_spearman_ic_excess") or "",
                "primary_120d_factor_neutral_ic": row_120.get("factor_neutral_spearman_ic_excess") or "",
                "primary_120d_net_spread": row_120.get("net_top_minus_bottom_median_excess") or "",
                "recommendation_reason": ";".join(reasons),
            }
        )

    by_cohort: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for candidate in candidates:
        by_cohort[str(candidate["calibration_cohort"])].append(candidate)
    out: list[dict[str, Any]] = []
    for cohort, items in sorted(by_cohort.items()):
        sorted_items = sorted(
            items,
            key=lambda item: (
                1 if item["recommended_action"] == "production_config_candidate" else 0,
                float(item["evidence_score"] or 0.0),
            ),
            reverse=True,
        )
        high_risk_used = 0
        selected = 0
        for item in sorted_items:
            action = str(item["recommended_action"])
            if selected >= max_recommendations_per_cohort:
                item["recommended_action"] = "defer_lower_priority"
                item["recommendation_reason"] = f"{item['recommendation_reason']};outside_top_{max_recommendations_per_cohort}_cohort_limit"
            elif action == "guarded_policy_candidate" and high_risk_used >= max_high_risk_recommendations_per_cohort:
                item["recommended_action"] = "defer_lower_priority"
                item["recommendation_reason"] = (
                    f"{item['recommendation_reason']};outside_high_risk_limit_{max_high_risk_recommendations_per_cohort}"
                )
            else:
                selected += 1
                if action == "guarded_policy_candidate":
                    high_risk_used += 1
            item["recommendation_rank"] = selected if item["recommended_action"] != "defer_lower_priority" else -1
            out.append(item)
    return out


def summarize(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("calibration_cohort") or "")].append(row)
    out: list[dict[str, Any]] = []
    for cohort, items in sorted(grouped.items()):
        selected = [
            item
            for item in items
            if item.get("recommended_action") in {"production_config_candidate", "guarded_policy_candidate"}
        ]
        counts = Counter(str(item.get("recommended_action") or "") for item in selected)
        top_sleeves = ",".join(
            f"{item.get('sleeve')}:{item.get('direction')}:{item.get('primary_component')}" for item in selected
        )
        out.append(
            {
                "calibration_cohort": cohort,
                "production_config_candidates": counts.get("production_config_candidate", 0),
                "guarded_candidates": counts.get("guarded_policy_candidate", 0),
                "deferred_candidates": sum(1 for item in items if item.get("recommended_action") == "defer_lower_priority"),
                "top_sleeves": top_sleeves,
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
        else resolve_path(
            cfg_get(
                config,
                "calibration.cohort_policy_recommendations.input_csv",
                "../output/med_devices_reports/calibration/med_device_component_promotion_review.csv",
            ),
            base_dir=base_dir,
        )
    )
    output_csv = (
        args.output_csv.expanduser().resolve()
        if args.output_csv
        else resolve_path(
            cfg_get(
                config,
                "calibration.cohort_policy_recommendations.output_csv",
                "../output/med_devices_reports/calibration/med_device_cohort_policy_recommendations.csv",
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
                "calibration.cohort_policy_recommendations.summary_csv",
                "../output/med_devices_reports/calibration/med_device_cohort_policy_recommendation_summary.csv",
            ),
            base_dir=base_dir,
        )
    )
    rows = read_csv(input_csv)
    recommendations = build_recommendations(
        rows,
        max_recommendations_per_cohort=int(
            cfg_get(config, "calibration.cohort_policy_recommendations.max_recommendations_per_cohort", 3)
        ),
        max_high_risk_recommendations_per_cohort=int(
            cfg_get(config, "calibration.cohort_policy_recommendations.max_high_risk_recommendations_per_cohort", 1)
        ),
        min_supporting_components=int(
            cfg_get(config, "calibration.cohort_policy_recommendations.min_supporting_components", 2)
        ),
    )
    summary_rows = summarize(recommendations)
    write_csv(output_csv, recommendations, RECOMMENDATION_FIELDS)
    write_csv(summary_csv, summary_rows, SUMMARY_FIELDS)
    print(f"cohort_policy_recommendations={output_csv} rows={len(recommendations)}")
    print(f"cohort_policy_recommendation_summary={summary_csv} rows={len(summary_rows)}")


if __name__ == "__main__":
    raise SystemExit(main())
