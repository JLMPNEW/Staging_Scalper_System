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
POSITIVE = "positive_candidate_factor"
INVERSE = "negative_or_inverse_factor"
PROMOTABLE = {POSITIVE, INVERSE}
DEFAULT_EXCLUDED_COMPONENTS = {
    "raw_composite_score",
    "cohort_percentile",
    "composite_percentile",
    "safe_core_score",
    "safe_core_percentile",
    "safe_core_cohort_percentile",
}
DETAIL_FIELDS = [
    "calibration_cohort",
    "component",
    "horizon_days",
    "direction",
    "review_action",
    "review_reason",
    "production_recommendation",
    "gross_recommendation",
    "net_recommendation",
    "factor_neutral_recommendation",
    "count",
    "unique_tickers",
    "eligible_cohort_tickers",
    "ticker_coverage_pct",
    "min_unique_tickers_required",
    "tier1_unique_tickers_required",
    "max_single_ticker_share",
    "paired_horizon",
    "paired_horizon_recommendation",
    "paired_horizon_direction",
    "persistent_60_120_flag",
    "spearman_ic_excess",
    "net_spearman_ic_excess",
    "factor_neutral_spearman_ic_excess",
    "top_minus_bottom_median_excess",
    "net_top_minus_bottom_median_excess",
    "factor_neutral_top_minus_bottom_median_excess",
    "spearman_ic_excess_bh_q_value",
    "net_spearman_ic_excess_bh_q_value",
    "factor_neutral_spearman_ic_excess_bh_q_value",
]
SUMMARY_FIELDS = [
    "review_action",
    "rows",
    "cohorts",
    "components",
    "positive_rows",
    "inverse_rows",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build support-gated component promotion review for med-device scoring.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--component-ic-csv", type=Path, default=None)
    parser.add_argument("--cohort-neutral-csv", type=Path, default=None)
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


def to_int(raw: object, default: int = 0) -> int:
    try:
        return int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return default


def to_float(raw: object) -> float | None:
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def pct(value: float | None) -> str:
    return "" if value is None else f"{100.0 * value:.2f}"


def direction_from_recommendation(recommendation: str) -> str:
    if recommendation == POSITIVE:
        return "positive"
    if recommendation == INVERSE:
        return "inverse"
    return ""


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


def support_stats(
    cohort_rows: list[dict[str, str]],
    *,
    cohort: str,
    horizon: int,
    component: str,
) -> tuple[int, float]:
    counts: Counter[str] = Counter()
    target = f"cohort_excess_return_{horizon}d"
    for row in cohort_rows:
        if str(row.get("calibration_cohort") or "") != cohort:
            continue
        if to_float(row.get(component)) is None or to_float(row.get(target)) is None:
            continue
        ticker = str(row.get("ticker") or "")
        if ticker:
            counts[ticker] += 1
    total = sum(counts.values())
    max_share = max(counts.values()) / total if total else 0.0
    return len(counts), max_share


def eligible_tickers_by_cohort_horizon(rows: list[dict[str, str]], horizons: list[int]) -> dict[tuple[str, int], int]:
    tickers: dict[tuple[str, int], set[str]] = defaultdict(set)
    for row in rows:
        cohort = str(row.get("calibration_cohort") or "")
        ticker = str(row.get("ticker") or "")
        if not cohort or not ticker:
            continue
        for horizon in horizons:
            if to_float(row.get(f"cohort_excess_return_{horizon}d")) is not None:
                tickers[(cohort, horizon)].add(ticker)
    return {key: len(value) for key, value in tickers.items()}


def load_component_set(raw: object) -> set[str]:
    if raw is None:
        return set(DEFAULT_EXCLUDED_COMPONENTS)
    text = str(raw).strip()
    if not text:
        return set()
    return {item.strip() for item in text.split(",") if item.strip()}


def build_review_rows(
    *,
    ic_rows: list[dict[str, str]],
    cohort_rows: list[dict[str, str]],
    eligible_tickers: dict[tuple[str, int], int],
    excluded_components: set[str],
    min_unique_tickers: int,
    min_cohort_coverage_pct: float,
    tier1_min_unique_tickers: int,
    tier1_min_cohort_coverage_pct: float,
    min_validation_obs: int,
    max_single_ticker_share: float,
    require_60_120_persistence: bool,
) -> list[dict[str, Any]]:
    by_key = {
        (str(row.get("calibration_cohort") or ""), str(row.get("component") or ""), to_int(row.get("horizon_days"))): row
        for row in ic_rows
    }
    out: list[dict[str, Any]] = []
    for row in ic_rows:
        cohort = str(row.get("calibration_cohort") or "")
        component = str(row.get("component") or "")
        horizon = to_int(row.get("horizon_days"))
        production_recommendation = str(row.get("production_recommendation") or "")
        direction = direction_from_recommendation(production_recommendation)
        eligible_count = eligible_tickers.get((cohort, horizon), 0)
        component_unique, single_ticker_share = support_stats(
            cohort_rows,
            cohort=cohort,
            horizon=horizon,
            component=component,
        )
        ticker_coverage = component_unique / eligible_count if eligible_count else 0.0
        required_unique = max(min_unique_tickers, math.ceil(min_cohort_coverage_pct * eligible_count))
        tier1_required_unique = max(tier1_min_unique_tickers, math.ceil(tier1_min_cohort_coverage_pct * eligible_count))
        paired_horizon = 60 if horizon == 120 else 120 if horizon == 60 else 0
        paired = by_key.get((cohort, component, paired_horizon), {})
        paired_recommendation = str(paired.get("production_recommendation") or "")
        paired_direction = direction_from_recommendation(paired_recommendation)
        persistent = bool(direction and paired_direction == direction)
        reasons: list[str] = []
        action = "reject"
        if component in excluded_components:
            action = "exclude_meta_component"
            reasons.append("excluded_meta_or_composite_component")
        elif production_recommendation not in PROMOTABLE:
            action = "research_only"
            reasons.append(production_recommendation or "not_promotable")
        else:
            if to_int(row.get("count")) < min_validation_obs:
                reasons.append("insufficient_validation_obs")
            if component_unique < required_unique:
                reasons.append(f"unique_tickers_below_{required_unique}")
            if ticker_coverage < min_cohort_coverage_pct:
                reasons.append(f"ticker_coverage_below_{100.0 * min_cohort_coverage_pct:.0f}pct")
            if single_ticker_share > max_single_ticker_share:
                reasons.append(f"single_ticker_share_above_{100.0 * max_single_ticker_share:.0f}pct")
            if horizon == 30:
                reasons.append("short_horizon_only")
            if require_60_120_persistence and horizon in {60, 120} and not persistent:
                reasons.append("missing_same_direction_60_120_persistence")
            tier1_ready = (
                not reasons
                and horizon in {60, 120}
                and component_unique >= tier1_required_unique
                and ticker_coverage >= tier1_min_cohort_coverage_pct
            )
            if tier1_ready:
                action = "promote_to_cohort_policy_review"
            elif not reasons:
                action = "promote_research_only_support_gap"
                reasons.append("below_tier1_support_threshold")
            else:
                action = "research_only"
        out.append(
            {
                "calibration_cohort": cohort,
                "component": component,
                "horizon_days": horizon,
                "direction": direction,
                "review_action": action,
                "review_reason": ";".join(dict.fromkeys(reasons)),
                "production_recommendation": production_recommendation,
                "gross_recommendation": row.get("recommendation") or "",
                "net_recommendation": row.get("net_recommendation") or "",
                "factor_neutral_recommendation": row.get("factor_neutral_recommendation") or "",
                "count": row.get("count") or "0",
                "unique_tickers": component_unique,
                "eligible_cohort_tickers": eligible_count,
                "ticker_coverage_pct": pct(ticker_coverage),
                "min_unique_tickers_required": required_unique,
                "tier1_unique_tickers_required": tier1_required_unique,
                "max_single_ticker_share": f"{single_ticker_share:.4f}",
                "paired_horizon": paired_horizon or "",
                "paired_horizon_recommendation": paired_recommendation,
                "paired_horizon_direction": paired_direction,
                "persistent_60_120_flag": "1" if persistent else "0",
                "spearman_ic_excess": row.get("spearman_ic_excess") or "",
                "net_spearman_ic_excess": row.get("net_spearman_ic_excess") or "",
                "factor_neutral_spearman_ic_excess": row.get("factor_neutral_spearman_ic_excess") or "",
                "top_minus_bottom_median_excess": row.get("top_minus_bottom_median_excess") or "",
                "net_top_minus_bottom_median_excess": row.get("net_top_minus_bottom_median_excess") or "",
                "factor_neutral_top_minus_bottom_median_excess": row.get("factor_neutral_top_minus_bottom_median_excess") or "",
                "spearman_ic_excess_bh_q_value": row.get("spearman_ic_excess_bh_q_value") or "",
                "net_spearman_ic_excess_bh_q_value": row.get("net_spearman_ic_excess_bh_q_value") or "",
                "factor_neutral_spearman_ic_excess_bh_q_value": row.get("factor_neutral_spearman_ic_excess_bh_q_value") or "",
            }
        )
    return out


def summarize(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("review_action") or "")].append(row)
    out: list[dict[str, Any]] = []
    for action, items in sorted(grouped.items()):
        out.append(
            {
                "review_action": action,
                "rows": len(items),
                "cohorts": len({str(item.get("calibration_cohort") or "") for item in items}),
                "components": len({str(item.get("component") or "") for item in items}),
                "positive_rows": sum(1 for item in items if item.get("direction") == "positive"),
                "inverse_rows": sum(1 for item in items if item.get("direction") == "inverse"),
            }
        )
    return out


def main() -> None:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    ic_csv = (
        args.component_ic_csv.expanduser().resolve()
        if args.component_ic_csv
        else resolve_path(cfg_get(config, "calibration.component_ic_csv"), base_dir=base_dir)
    )
    cohort_csv = (
        args.cohort_neutral_csv.expanduser().resolve()
        if args.cohort_neutral_csv
        else resolve_path(cfg_get(config, "calibration.cohort_neutral_backtest_csv"), base_dir=base_dir)
    )
    output_csv = (
        args.output_csv.expanduser().resolve()
        if args.output_csv
        else resolve_path(
            cfg_get(
                config,
                "calibration.component_promotion_review.output_csv",
                "../output/med_devices_reports/calibration/med_device_component_promotion_review.csv",
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
                "calibration.component_promotion_review.summary_csv",
                "../output/med_devices_reports/calibration/med_device_component_promotion_review_summary.csv",
            ),
            base_dir=base_dir,
        )
    )
    min_unique = int(cfg_get(config, "calibration.component_promotion_review.min_unique_tickers", 3))
    min_coverage = float(cfg_get(config, "calibration.component_promotion_review.min_cohort_coverage_pct", 0.20))
    tier1_min_unique = int(cfg_get(config, "calibration.component_promotion_review.tier1_min_unique_tickers", 4))
    tier1_min_coverage = float(cfg_get(config, "calibration.component_promotion_review.tier1_min_cohort_coverage_pct", 0.25))
    min_validation_obs = int(cfg_get(config, "calibration.component_promotion_review.min_validation_obs", 20))
    max_share = float(cfg_get(config, "calibration.component_promotion_review.max_single_ticker_share", 0.35))
    require_persistence = bool(cfg_get(config, "calibration.component_promotion_review.require_60_120_persistence", True))
    excluded_components = load_component_set(
        cfg_get(config, "calibration.component_promotion_review.excluded_components", "")
    )

    ic_rows = read_csv(ic_csv)
    cohort_rows = read_csv(cohort_csv)
    horizons = return_horizons(cohort_rows)
    eligible = eligible_tickers_by_cohort_horizon(cohort_rows, horizons)
    review_rows = build_review_rows(
        ic_rows=ic_rows,
        cohort_rows=cohort_rows,
        eligible_tickers=eligible,
        excluded_components=excluded_components,
        min_unique_tickers=min_unique,
        min_cohort_coverage_pct=min_coverage,
        tier1_min_unique_tickers=tier1_min_unique,
        tier1_min_cohort_coverage_pct=tier1_min_coverage,
        min_validation_obs=min_validation_obs,
        max_single_ticker_share=max_share,
        require_60_120_persistence=require_persistence,
    )
    write_csv(output_csv, review_rows, DETAIL_FIELDS)
    summary_rows = summarize(review_rows)
    write_csv(summary_csv, summary_rows, SUMMARY_FIELDS)
    print(f"component_promotion_review={output_csv} rows={len(review_rows)}")
    print(f"component_promotion_review_summary={summary_csv} rows={len(summary_rows)}")


if __name__ == "__main__":
    main()
