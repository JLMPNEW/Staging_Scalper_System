#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import math
import os
import sys
from collections import Counter, defaultdict
from datetime import date, datetime
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
    "fda_event_risk_score",
    "fda_event_risk_breadth_adjusted_score",
    "borrow_squeeze_risk_score",
    "borrow_pressure_score",
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
    "ic_generated_asof",
    "input_scoring_model_version",
    "support_window_start",
    "support_window_end",
    "input_lockbox_violation",
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
    tmp_path = path.with_name(path.name + ".tmp")
    with tmp_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(tmp_path, path)


def parse_iso_date(raw: object) -> date | None:
    text = str(raw or "").strip()[:10]
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def validate_ic_contract(rows: list[dict[str, str]], *, path: Path) -> None:
    if not rows:
        raise RuntimeError(
            f"Component IC CSV is empty: {path}. Regenerate component ICs before building the promotion review."
        )
    required = {"calibration_cohort", "component", "horizon_days", "production_recommendation", "generated_asof"}
    missing = sorted(required.difference(rows[0]))
    if missing:
        raise RuntimeError(
            f"Component IC CSV {path} is missing required columns: {','.join(missing)}. "
            "Regenerate the component IC file (with provenance columns) before building the promotion review."
        )


def validate_scoring_model_version(
    cohort_rows: list[dict[str, str]],
    *,
    path: Path,
    expected: str,
    allowed_extra: set[str],
) -> str:
    if "scoring_model_version" not in cohort_rows[0]:
        raise RuntimeError(
            f"Cohort-neutral backtest CSV {path} has no scoring_model_version column; "
            "regenerate it with the current scoring pipeline before building the promotion review."
        )
    versions = sorted({str(row.get("scoring_model_version") or "").strip() for row in cohort_rows})
    allowed = {expected} | allowed_extra
    allowed.discard("")
    unexpected = [version for version in versions if version not in allowed]
    if unexpected:
        raise RuntimeError(
            f"Cohort-neutral backtest CSV {path} contains scoring_model_version values "
            f"{','.join(repr(v) for v in unexpected)} that do not match config scoring.model_version "
            f"{expected!r}. Regenerate the backtest with the current model, or add the version(s) to "
            "calibration.component_promotion_review.allowed_scoring_model_versions for an explicit "
            "calibration run."
        )
    return ";".join(version for version in versions if version)


def filter_to_dev_window(
    cohort_rows: list[dict[str, str]],
    *,
    window_start: date,
    window_end: date,
) -> tuple[list[dict[str, str]], date | None, date | None, int]:
    kept: list[dict[str, str]] = []
    input_min: date | None = None
    input_max: date | None = None
    dropped = 0
    for row in cohort_rows:
        asof = parse_iso_date(row.get("asof_date"))
        if asof is None:
            dropped += 1
            continue
        input_min = asof if input_min is None or asof < input_min else input_min
        input_max = asof if input_max is None or asof > input_max else input_max
        if window_start <= asof <= window_end:
            kept.append(row)
        else:
            dropped += 1
    return kept, input_min, input_max, dropped


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
    defaults = set(DEFAULT_EXCLUDED_COMPONENTS)
    if raw is None:
        return defaults
    text = str(raw).strip()
    if not text:
        return defaults
    return defaults | {item.strip() for item in text.split(",") if item.strip()}


def fdr_gate_passed(row: dict[str, str], *, max_q_value: float) -> bool:
    fields = (
        "spearman_ic_excess_bh_q_value",
        "net_spearman_ic_excess_bh_q_value",
        "factor_neutral_spearman_ic_excess_bh_q_value",
    )
    for field in fields:
        q_value = to_float(row.get(field))
        if q_value is None or q_value > max_q_value:
            return False
    return True


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
    max_bh_q_value: float,
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
            if not fdr_gate_passed(row, max_q_value=max_bh_q_value):
                reasons.append(f"bh_q_value_above_{max_bh_q_value:.3f}")
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
                "ic_generated_asof": row.get("generated_asof") or "",
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
    max_bh_q_value = float(
        cfg_get(
            config,
            "calibration.component_promotion_review.bh_fdr_alpha",
            cfg_get(config, "calibration.component_ic.bh_fdr", 0.05),
        )
    )
    excluded_components = load_component_set(
        cfg_get(config, "calibration.component_promotion_review.excluded_components", None)
    )

    ic_rows = read_csv(ic_csv)
    validate_ic_contract(ic_rows, path=ic_csv)
    cohort_rows = read_csv(cohort_csv)
    if not cohort_rows:
        raise RuntimeError(f"Cohort-neutral backtest CSV is empty: {cohort_csv}")
    expected_model_version = str(cfg_get(config, "scoring.model_version", "")).strip()
    allowed_extra_versions = {
        item.strip()
        for item in str(
            cfg_get(config, "calibration.component_promotion_review.allowed_scoring_model_versions", "")
        ).split(",")
        if item.strip()
    }
    input_model_version = validate_scoring_model_version(
        cohort_rows,
        path=cohort_csv,
        expected=expected_model_version,
        allowed_extra=allowed_extra_versions,
    )
    window_start_raw = str(cfg_get(config, "calibration.dev_window_start", "2024-01-02")).strip()
    window_end_raw = str(cfg_get(config, "calibration.dev_window_end", "2025-12-31")).strip()
    window_start = parse_iso_date(window_start_raw)
    window_end = parse_iso_date(window_end_raw)
    if window_start is None or window_end is None or window_start > window_end:
        raise RuntimeError(
            f"Invalid calibration dev window: start={window_start_raw!r} end={window_end_raw!r}"
        )
    on_lockbox_violation = str(
        cfg_get(config, "calibration.component_promotion_review.on_lockbox_violation", "fail")
    ).strip().lower()
    if on_lockbox_violation not in {"fail", "downgrade"}:
        raise RuntimeError(
            "calibration.component_promotion_review.on_lockbox_violation must be 'fail' or 'downgrade', "
            f"got {on_lockbox_violation!r}"
        )
    cohort_rows, input_asof_min, input_asof_max, dropped_rows = filter_to_dev_window(
        cohort_rows,
        window_start=window_start,
        window_end=window_end,
    )
    if input_asof_min is None or input_asof_max is None or not cohort_rows:
        raise RuntimeError(
            f"Cohort-neutral backtest CSV {cohort_csv} has no parseable asof_date rows inside the dev window "
            f"{window_start.isoformat()}..{window_end.isoformat()}"
        )
    lockbox_violation = input_asof_min < window_start or input_asof_max > window_end
    if lockbox_violation and on_lockbox_violation == "fail":
        raise RuntimeError(
            f"Lockbox violation: cohort-neutral backtest {cohort_csv} spans asof "
            f"{input_asof_min.isoformat()}..{input_asof_max.isoformat()}, outside the sealed dev window "
            f"{window_start.isoformat()}..{window_end.isoformat()}. Regenerate the backtest inside the dev "
            "window, or set calibration.component_promotion_review.on_lockbox_violation to 'downgrade' to "
            "publish a flagged, non-promotable review."
        )
    horizons = return_horizons(cohort_rows)
    if not horizons:
        raise RuntimeError(f"No cohort_excess_return_<horizon>d columns found in {cohort_csv}")
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
        max_bh_q_value=max_bh_q_value,
    )
    for review_row in review_rows:
        review_row["input_scoring_model_version"] = input_model_version
        review_row["support_window_start"] = window_start.isoformat()
        review_row["support_window_end"] = window_end.isoformat()
        review_row["input_lockbox_violation"] = "1" if lockbox_violation else "0"
    if lockbox_violation:
        # Downgrade mode: IC statistics upstream are not dev-window constrained, so no
        # promotion may leave this script while the input extends past the seal.
        for review_row in review_rows:
            if review_row["review_action"] == "promote_to_cohort_policy_review":
                review_row["review_action"] = "research_only"
                reasons = [item for item in str(review_row["review_reason"]).split(";") if item]
                reasons.append("input_window_exceeds_lockbox_seal")
                review_row["review_reason"] = ";".join(dict.fromkeys(reasons))
        print(
            "warning: lockbox violation - input asof range exceeds the sealed dev window; "
            "all promote actions downgraded to research_only"
        )
    write_csv(output_csv, review_rows, DETAIL_FIELDS)
    summary_rows = summarize(review_rows)
    write_csv(summary_csv, summary_rows, SUMMARY_FIELDS)
    print(f"component_promotion_review={output_csv} rows={len(review_rows)}")
    print(f"component_promotion_review_summary={summary_csv} rows={len(summary_rows)}")
    print(
        f"support_window={window_start.isoformat()}..{window_end.isoformat()} "
        f"input_asof_range={input_asof_min.isoformat()}..{input_asof_max.isoformat()} "
        f"rows_in_window={len(cohort_rows)} rows_dropped={dropped_rows} "
        f"lockbox_violation={1 if lockbox_violation else 0}"
    )
    print(f"input_scoring_model_version={input_model_version}")


if __name__ == "__main__":
    raise SystemExit(main())
