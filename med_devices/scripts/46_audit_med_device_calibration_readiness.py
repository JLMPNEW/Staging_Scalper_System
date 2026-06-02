#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import math
import sys
from collections import Counter, defaultdict
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
    "production_status",
    "ticker_count",
    "asof_count",
    "total_rows",
    "fold_count",
    "label_coverage_30d",
    "label_coverage_60d",
    "label_coverage_120d",
    "latest_labeled_asof_30d",
    "latest_labeled_asof_60d",
    "latest_labeled_asof_120d",
    "top_decile_unique_tickers_30d",
    "top_decile_unique_tickers_60d",
    "top_decile_unique_tickers_120d",
    "top_decile_max_ticker_share_30d",
    "top_decile_max_ticker_share_60d",
    "top_decile_max_ticker_share_120d",
    "baseline_median_excess_30d",
    "baseline_median_excess_60d",
    "baseline_median_excess_120d",
    "baseline_lcb_excess_30d",
    "baseline_lcb_excess_60d",
    "baseline_lcb_excess_120d",
    "best_template_id",
    "best_template_horizon_days",
    "best_template_pass_fold_count",
    "best_template_pass_fold_rate",
    "best_template_min_lcb_excess",
    "best_template_reason",
    "best_optimizer_id",
    "best_optimizer_horizon_days",
    "best_optimizer_pass_fold_count",
    "best_optimizer_pass_fold_rate",
    "best_optimizer_min_lcb_excess",
    "best_optimizer_reason",
    "feature_positive_alpha_count",
    "feature_inverse_alpha_count",
    "feature_risk_gate_count",
    "feature_neutralize_count",
    "feature_repair_data_count",
    "blocker_flags",
    "recommended_next_action",
    "recommendation_reason",
]
ACTION_FIELDS = [
    "calibration_cohort",
    "blocker",
    "severity",
    "recommended_action",
    "details",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit med-device calibration readiness by cohort.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--input-csv", type=Path, default=None)
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--action-plan-csv", type=Path, default=None)
    return parser.parse_args()


def to_float(raw: object) -> float | None:
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def fmt(value: object) -> str:
    number = to_float(value)
    return "" if number is None else f"{number:.6f}"


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def parse_int_list(raw: object) -> list[int]:
    out: list[int] = []
    for item in str(raw or "").split(","):
        text = item.strip()
        if text.isdigit():
            out.append(int(text))
    return out


def lcb(values: list[float], z: float = 1.64) -> float:
    if not values:
        return 0.0
    if len(values) < 2:
        return values[0]
    avg = mean(values)
    variance = sum((value - avg) ** 2 for value in values) / (len(values) - 1)
    return avg - z * math.sqrt(variance / len(values))


def production_status(cohort: str, config: dict[str, Any]) -> str:
    profiles = cfg_get(config, "scoring.cohort_profiles", {}) or {}
    profile = profiles.get(cohort, {}) if isinstance(profiles, dict) else {}
    if not isinstance(profile, dict):
        return "default"
    status = str(profile.get("calibration_status") or "").strip()
    if status:
        return status
    if "score_template" in profile:
        return "production_template"
    if "gates" in profile:
        return "custom_gates"
    return "default"


def path_from_config(config: dict[str, Any], key: str, default: str, *, base_dir: Path) -> Path:
    return resolve_path(cfg_get(config, key, default), base_dir=base_dir)


def horizon_stats(rows: list[dict[str, str]], horizon: int) -> dict[str, Any]:
    return_key = f"forward_return_{horizon}d"
    excess_key = f"cohort_excess_return_{horizon}d"
    labeled = [
        row for row in rows
        if to_float(row.get(return_key)) is not None and to_float(row.get(excess_key)) is not None
    ]
    top = [row for row in labeled if row.get("cohort_rank_bucket") == "cohort_top_decile"]
    top_excess = [float(row[excess_key]) for row in top]
    top_tickers = Counter(str(row.get("ticker") or "") for row in top)
    max_share = max(top_tickers.values()) / len(top) if top else 0.0
    return {
        "label_coverage": len(labeled) / len(rows) if rows else 0.0,
        "latest_labeled_asof": max((str(row.get("asof_date") or "")[:10] for row in labeled), default=""),
        "top_decile_unique_tickers": len(top_tickers),
        "top_decile_obs": len(top),
        "top_decile_max_ticker_share": max_share,
        "baseline_median_excess": median(top_excess) if top_excess else 0.0,
        "baseline_lcb_excess": lcb(top_excess),
    }


def best_template(rows: list[dict[str, str]], cohort: str) -> dict[str, Any]:
    items = [row for row in rows if row.get("calibration_cohort") == cohort]
    if not items:
        return {}
    items.sort(
        key=lambda row: (
            to_float(row.get("pass_fold_count")) or 0.0,
            to_float(row.get("pass_fold_rate")) or 0.0,
            to_float(row.get("min_validation_lcb_excess")) or -999.0,
            to_float(row.get("mean_validation_median_excess")) or -999.0,
        ),
        reverse=True,
    )
    return items[0]


def best_optimizer(rows: list[dict[str, str]], cohort: str) -> dict[str, Any]:
    items = [row for row in rows if row.get("calibration_cohort") == cohort]
    if not items:
        return {}
    items.sort(
        key=lambda row: (
            to_float(row.get("pass_fold_count")) or 0.0,
            to_float(row.get("pass_fold_rate")) or 0.0,
            to_float(row.get("min_validation_lcb_excess")) or -999.0,
            to_float(row.get("objective_score")) or -999.0,
        ),
        reverse=True,
    )
    return items[0]


def feature_counts(rows: list[dict[str, str]], cohort: str) -> Counter[str]:
    return Counter(
        str(row.get("recommended_action") or "")
        for row in rows
        if row.get("calibration_cohort") == cohort
    )


def blockers(
    *,
    stats: dict[str, Any],
    config: dict[str, Any],
) -> list[str]:
    min_tickers = int(cfg_get(config, "calibration.calibration_readiness.min_tickers", 8))
    min_top_unique = int(cfg_get(config, "calibration.calibration_readiness.min_top_decile_unique_tickers", 3))
    max_top_share = float(cfg_get(config, "calibration.calibration_readiness.max_top_decile_ticker_share", 0.50))
    min_coverage_30 = float(cfg_get(config, "calibration.calibration_readiness.min_label_coverage_30d", 0.80))
    min_coverage_60 = float(cfg_get(config, "calibration.calibration_readiness.min_label_coverage_60d", 0.80))
    min_coverage_120 = float(cfg_get(config, "calibration.calibration_readiness.min_label_coverage_120d", 0.60))
    min_pass_rate = float(cfg_get(config, "calibration.calibration_readiness.min_pass_fold_rate", 0.60))
    out: list[str] = []
    if stats["ticker_count"] < min_tickers:
        out.append("too_few_tickers")
    if stats["label_coverage_30d"] < min_coverage_30:
        out.append("low_30d_label_coverage")
    if stats["label_coverage_60d"] < min_coverage_60:
        out.append("low_60d_label_coverage")
    if stats["label_coverage_120d"] < min_coverage_120:
        out.append("low_120d_label_coverage")
    for horizon in (30, 60):
        if stats[f"top_decile_unique_tickers_{horizon}d"] < min_top_unique:
            out.append(f"one_ticker_or_too_narrow_{horizon}d")
        if stats[f"top_decile_max_ticker_share_{horizon}d"] > max_top_share:
            out.append(f"top_decile_concentration_{horizon}d")
    if stats.get("best_template_pass_fold_rate", 0.0) < min_pass_rate:
        out.append("template_walk_forward_not_stable")
    if stats.get("best_optimizer_pass_fold_rate", 0.0) < min_pass_rate:
        out.append("optimizer_walk_forward_not_stable")
    if stats.get("best_template_min_lcb_excess", 0.0) <= 0 and stats.get("best_optimizer_min_lcb_excess", 0.0) <= 0:
        out.append("nonpositive_walk_forward_lcb")
    if stats["feature_repair_data_count"] > max(stats["feature_positive_alpha_count"], stats["feature_inverse_alpha_count"]):
        out.append("feature_repair_needed")
    return out


def recommended_action(status: str, flags: list[str]) -> tuple[str, str]:
    flag_set = set(flags)
    if status in {"restricted_research_only", "excluded_from_tier1"}:
        return "keep_restricted", "cohort is already blocked from Tier 1 while calibration evidence is weak"
    if "too_few_tickers" in flag_set:
        return "merge_or_exclude_from_calibration", "ticker count is too small for robust cohort-level calibration"
    if "low_30d_label_coverage" in flag_set or "low_60d_label_coverage" in flag_set:
        return "collect_more_labels", "near-horizon forward-return coverage is below readiness threshold"
    if "feature_repair_needed" in flag_set:
        return "repair_feature_sleeves", "feature diagnostics show repair-data issues dominate alpha-ready features"
    if "template_walk_forward_not_stable" in flag_set and "optimizer_walk_forward_not_stable" in flag_set:
        if status in {"production_eligible", "production_template", "custom_gates"}:
            return "keep_current_production_monitor_only", "current promoted profile should not be expanded until walk-forward evidence improves"
        return "hold_default_no_promotion", "no template or optimizer candidate is stable enough for promotion"
    if "one_ticker_or_too_narrow_30d" in flag_set or "one_ticker_or_too_narrow_60d" in flag_set:
        return "broaden_selection_or_merge", "top-decile selections are too concentrated for robust calibration"
    return "ready_for_constrained_optimization", "cohort meets basic readiness checks"


def action_rows(cohort: str, flags: list[str], stats: dict[str, Any]) -> list[dict[str, Any]]:
    severity = {
        "too_few_tickers": "high",
        "low_30d_label_coverage": "high",
        "low_60d_label_coverage": "high",
        "low_120d_label_coverage": "medium",
        "template_walk_forward_not_stable": "high",
        "optimizer_walk_forward_not_stable": "high",
        "nonpositive_walk_forward_lcb": "high",
        "feature_repair_needed": "medium",
    }
    action = {
        "too_few_tickers": "merge cohort or exclude from cohort-specific calibration",
        "low_30d_label_coverage": "refresh backtest labels or wait for more forward history",
        "low_60d_label_coverage": "refresh backtest labels or wait for more forward history",
        "low_120d_label_coverage": "use 30d/60d for optimization and keep 120d diagnostic-only",
        "template_walk_forward_not_stable": "do not promote hand-built templates",
        "optimizer_walk_forward_not_stable": "do not promote optimized weights",
        "nonpositive_walk_forward_lcb": "require more folds/history or repair features before promotion",
        "feature_repair_needed": "inspect components marked repair_data in feature-stability output",
    }
    rows: list[dict[str, Any]] = []
    for flag in flags:
        details = ""
        if flag == "too_few_tickers":
            details = f"ticker_count={stats['ticker_count']}"
        elif flag.startswith("low_"):
            details = (
                f"coverage_30d={stats['label_coverage_30d']:.2f};"
                f"coverage_60d={stats['label_coverage_60d']:.2f};"
                f"coverage_120d={stats['label_coverage_120d']:.2f}"
            )
        elif flag.startswith("one_ticker") or flag.startswith("top_decile_concentration"):
            details = (
                f"top_unique_30d={stats['top_decile_unique_tickers_30d']};"
                f"top_unique_60d={stats['top_decile_unique_tickers_60d']};"
                f"max_share_30d={stats['top_decile_max_ticker_share_30d']:.2f};"
                f"max_share_60d={stats['top_decile_max_ticker_share_60d']:.2f}"
            )
        elif flag.endswith("not_stable"):
            details = (
                f"template_pass_rate={stats['best_template_pass_fold_rate']:.2f};"
                f"optimizer_pass_rate={stats['best_optimizer_pass_fold_rate']:.2f}"
            )
        elif flag == "nonpositive_walk_forward_lcb":
            details = (
                f"template_min_lcb={stats['best_template_min_lcb_excess']:.4f};"
                f"optimizer_min_lcb={stats['best_optimizer_min_lcb_excess']:.4f}"
            )
        rows.append(
            {
                "calibration_cohort": cohort,
                "blocker": flag,
                "severity": severity.get(flag, "medium"),
                "recommended_action": action.get(flag, "review manually"),
                "details": details,
            }
        )
    return rows


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
        else path_from_config(
            config,
            "calibration.calibration_readiness.output_csv",
            "../output/med_devices_reports/calibration/med_device_calibration_readiness_audit.csv",
            base_dir=base_dir,
        )
    )
    action_plan_csv = (
        args.action_plan_csv.expanduser().resolve()
        if args.action_plan_csv
        else path_from_config(
            config,
            "calibration.calibration_readiness.action_plan_csv",
            "../output/med_devices_reports/calibration/med_device_calibration_readiness_action_plan.csv",
            base_dir=base_dir,
        )
    )
    template_summary = read_csv(
        path_from_config(
            config,
            "calibration.template_walk_forward.summary_csv",
            "../output/med_devices_reports/calibration/med_device_template_walk_forward_summary.csv",
            base_dir=base_dir,
        )
    )
    optimizer_summary = read_csv(
        path_from_config(
            config,
            "calibration.component_weight_optimizer.summary_csv",
            "../output/med_devices_reports/calibration/med_device_component_weight_optimizer_summary.csv",
            base_dir=base_dir,
        )
    )
    feature_recs = read_csv(
        path_from_config(
            config,
            "calibration.feature_stability.recommendation_csv",
            "../output/med_devices_reports/calibration/med_device_feature_stability_recommendations.csv",
            base_dir=base_dir,
        )
    )
    rows = read_csv(input_csv)
    horizons = parse_int_list(cfg_get(config, "calibration.calibration_readiness.horizons", "30,60,120"))
    cohorts = sorted({str(row.get("calibration_cohort") or "") for row in rows if row.get("calibration_cohort")})
    by_cohort: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        cohort = str(row.get("calibration_cohort") or "")
        if cohort:
            by_cohort[cohort].append(row)
    output_rows: list[dict[str, Any]] = []
    action_plan_rows: list[dict[str, Any]] = []
    fold_count = len({row.get("fold_id") for row in template_summary if row.get("fold_id")}) or int(
        max((to_float(row.get("fold_count")) or 0.0 for row in template_summary), default=0.0)
    )
    for cohort in cohorts:
        cohort_rows = by_cohort[cohort]
        stats: dict[str, Any] = {
            "calibration_cohort": cohort,
            "production_status": production_status(cohort, config),
            "ticker_count": len({str(row.get("ticker") or "") for row in cohort_rows}),
            "asof_count": len({str(row.get("asof_date") or "")[:10] for row in cohort_rows}),
            "total_rows": len(cohort_rows),
            "fold_count": fold_count,
        }
        horizon_values = {horizon: horizon_stats(cohort_rows, horizon) for horizon in horizons}
        for horizon in (30, 60, 120):
            values = horizon_values.get(horizon, {})
            stats[f"label_coverage_{horizon}d"] = values.get("label_coverage", 0.0)
            stats[f"latest_labeled_asof_{horizon}d"] = values.get("latest_labeled_asof", "")
            stats[f"top_decile_unique_tickers_{horizon}d"] = values.get("top_decile_unique_tickers", 0)
            stats[f"top_decile_max_ticker_share_{horizon}d"] = values.get("top_decile_max_ticker_share", 0.0)
            stats[f"baseline_median_excess_{horizon}d"] = values.get("baseline_median_excess", 0.0)
            stats[f"baseline_lcb_excess_{horizon}d"] = values.get("baseline_lcb_excess", 0.0)
        template = best_template(template_summary, cohort)
        optimizer = best_optimizer(optimizer_summary, cohort)
        stats.update(
            {
                "best_template_id": template.get("template_id", ""),
                "best_template_horizon_days": to_float(template.get("horizon_days")) or "",
                "best_template_pass_fold_count": to_float(template.get("pass_fold_count")) or 0.0,
                "best_template_pass_fold_rate": to_float(template.get("pass_fold_rate")) or 0.0,
                "best_template_min_lcb_excess": to_float(template.get("min_validation_lcb_excess")) or 0.0,
                "best_template_reason": template.get("recommendation_reason", ""),
                "best_optimizer_id": optimizer.get("candidate_id", ""),
                "best_optimizer_horizon_days": to_float(optimizer.get("horizon_days")) or "",
                "best_optimizer_pass_fold_count": to_float(optimizer.get("pass_fold_count")) or 0.0,
                "best_optimizer_pass_fold_rate": to_float(optimizer.get("pass_fold_rate")) or 0.0,
                "best_optimizer_min_lcb_excess": to_float(optimizer.get("min_validation_lcb_excess")) or 0.0,
                "best_optimizer_reason": optimizer.get("candidate_reason", ""),
            }
        )
        counts = feature_counts(feature_recs, cohort)
        stats["feature_positive_alpha_count"] = counts["use_as_positive_alpha"]
        stats["feature_inverse_alpha_count"] = counts["test_inverse_alpha"]
        stats["feature_risk_gate_count"] = counts["risk_gate_only"]
        stats["feature_neutralize_count"] = counts["neutralize"]
        stats["feature_repair_data_count"] = counts["repair_data"]
        flag_values = blockers(stats=stats, config=config)
        action, reason = recommended_action(str(stats["production_status"]), flag_values)
        stats["blocker_flags"] = ";".join(flag_values)
        stats["recommended_next_action"] = action
        stats["recommendation_reason"] = reason
        output_rows.append(stats)
        action_plan_rows.extend(action_rows(cohort, flag_values, stats))

    for row in output_rows:
        for field in OUTPUT_FIELDS:
            if field in {
                "calibration_cohort",
                "production_status",
                "latest_labeled_asof_30d",
                "latest_labeled_asof_60d",
                "latest_labeled_asof_120d",
                "best_template_id",
                "best_template_reason",
                "best_optimizer_id",
                "best_optimizer_reason",
                "blocker_flags",
                "recommended_next_action",
                "recommendation_reason",
            }:
                continue
            row[field] = fmt(row.get(field))
    write_csv(output_csv, output_rows, OUTPUT_FIELDS)
    write_csv(action_plan_csv, action_plan_rows, ACTION_FIELDS)
    print(f"calibration_readiness_csv={output_csv} rows={len(output_rows)}")
    print(f"calibration_readiness_action_plan_csv={action_plan_csv} rows={len(action_plan_rows)}")


if __name__ == "__main__":
    main()
