#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
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
from med_devices.core.logging_utils import configure_utc_logging  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
COMPONENT_FIELDS = [
    "raw_composite_score",
    "cohort_percentile",
    "fundamental_quality_score",
    "durable_growth_score",
    "fda_product_score",
    "fda_product_score_legacy",
    "fda_alpha_score",
    "fda_safety_score",
    "fda_clearance_velocity_score",
    "fda_evidence_quality_score",
    "fda_event_risk_score",
    "reimbursement_score",
    "valuation_score",
    "technical_entry_score",
    "technical_trend_quality_score",
    "technical_relative_strength_score",
    "technical_liquidity_score",
    "technical_volume_breakout_score",
    "technical_volatility_risk_score",
    "technical_setup_score",
    "technical_core_score",
    "technical_alpha_score",
    "technical_pullback_score",
    "technical_overextension_score",
    "sentiment_catalyst_score",
    "value_trap_score",
]
OUTPUT_FIELDS = [
    "calibration_cohort",
    "horizon_days",
    "component",
    "count",
    "unique_tickers",
    "spearman_ic_excess",
    "pearson_ic_excess",
    "spearman_ic_raw",
    "top_quintile_count",
    "bottom_quintile_count",
    "top_quintile_median_excess",
    "bottom_quintile_median_excess",
    "top_minus_bottom_median_excess",
    "top_quintile_hit_rate_excess",
    "bottom_quintile_hit_rate_excess",
    "recommendation",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compute component IC by med-device calibration cohort.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--input-csv", type=Path, default=None)
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--technical-output-csv", type=Path, default=None)
    return parser.parse_args()


def to_float(raw: object) -> float | None:
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_FIELDS, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def return_horizons(rows: list[dict[str, str]]) -> list[int]:
    if not rows:
        return []
    out: list[int] = []
    for key in rows[0]:
        if key.startswith("cohort_excess_return_") and key.endswith("d"):
            text = key[len("cohort_excess_return_") : -1]
            if text.isdigit():
                out.append(int(text))
    return sorted(out)


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


def fmt(value: float | None, digits: int = 6) -> str:
    return "" if value is None else f"{value:.{digits}f}"


def hit_rate(values: list[float]) -> str:
    return "" if not values else f"{sum(1 for value in values if value > 0) / len(values):.4f}"


def recommendation(count: int, ic: float | None, spread: float | None) -> str:
    if count < 50 or ic is None or spread is None:
        return "insufficient_observations"
    if ic > 0.05 and spread > 0:
        return "positive_candidate_factor"
    if ic < -0.05 and spread < 0:
        return "negative_or_inverse_factor"
    return "weak_or_unstable_factor"


def analyze_component(rows: list[dict[str, str]], *, cohort: str, horizon: int, component: str) -> dict[str, Any]:
    pairs: list[tuple[str, float, float, float]] = []
    for row in rows:
        if str(row.get("calibration_cohort") or "") != cohort:
            continue
        component_value = to_float(row.get(component))
        excess = to_float(row.get(f"cohort_excess_return_{horizon}d"))
        raw_return = to_float(row.get(f"forward_return_{horizon}d"))
        if component_value is None or excess is None or raw_return is None:
            continue
        pairs.append((str(row.get("ticker") or ""), component_value, excess, raw_return))
    xs = [item[1] for item in pairs]
    excess_values = [item[2] for item in pairs]
    raw_values = [item[3] for item in pairs]
    sorted_pairs = sorted(pairs, key=lambda item: item[1])
    quintile_n = max(1, len(sorted_pairs) // 5) if sorted_pairs else 0
    bottom = [item[2] for item in sorted_pairs[:quintile_n]]
    top = [item[2] for item in sorted_pairs[-quintile_n:]]
    top_med = median(top) if top else None
    bottom_med = median(bottom) if bottom else None
    spread = (top_med - bottom_med) if top_med is not None and bottom_med is not None else None
    ic = spearman(xs, excess_values)
    return {
        "calibration_cohort": cohort,
        "horizon_days": horizon,
        "component": component,
        "count": len(pairs),
        "unique_tickers": len({item[0] for item in pairs}),
        "spearman_ic_excess": fmt(ic),
        "pearson_ic_excess": fmt(correlation(xs, excess_values)),
        "spearman_ic_raw": fmt(spearman(xs, raw_values)),
        "top_quintile_count": len(top),
        "bottom_quintile_count": len(bottom),
        "top_quintile_median_excess": fmt(top_med),
        "bottom_quintile_median_excess": fmt(bottom_med),
        "top_minus_bottom_median_excess": fmt(spread),
        "top_quintile_hit_rate_excess": hit_rate(top),
        "bottom_quintile_hit_rate_excess": hit_rate(bottom),
        "recommendation": recommendation(len(pairs), ic, spread),
    }


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
        else resolve_path(cfg_get(config, "calibration.component_ic_csv"), base_dir=base_dir)
    )
    technical_output_csv = (
        args.technical_output_csv.expanduser().resolve()
        if args.technical_output_csv
        else resolve_path(
            cfg_get(
                config,
                "calibration.technical_component_ic_csv",
                "../output/med_devices_reports/calibration/med_device_technical_component_ic_by_cohort.csv",
            ),
            base_dir=base_dir,
        )
    )
    rows = read_csv(input_csv)
    horizons = return_horizons(rows)
    cohorts = sorted({str(row.get("calibration_cohort") or "") for row in rows if str(row.get("calibration_cohort") or "")})
    out = [
        analyze_component(rows, cohort=cohort, horizon=horizon, component=component)
        for cohort in cohorts
        for horizon in horizons
        for component in COMPONENT_FIELDS
    ]
    write_csv(output_csv, out)
    technical_out = [row for row in out if str(row.get("component") or "").startswith("technical_")]
    write_csv(technical_output_csv, technical_out)
    print(f"component_ic_csv={output_csv} rows={len(out)}")
    print(f"technical_component_ic_csv={technical_output_csv} rows={len(technical_out)}")


if __name__ == "__main__":
    main()
