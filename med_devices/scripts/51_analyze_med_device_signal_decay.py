#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import math
import sys
from pathlib import Path
from statistics import median
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from med_devices.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from med_devices.core.logging_utils import configure_utc_logging  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
DEFAULT_COMPONENTS = (
    "ic_tilted_composite_score",
    "fundamental_quality_score",
    "durable_growth_score",
    "durable_growth_score_legacy",
    "durable_growth_alpha_score",
    "fda_alpha_score",
    "fda_safety_score",
    "fda_clearance_velocity_score",
    "fda_clearance_acceleration_score",
    "quality_value_interaction_score",
    "fda_technical_interaction_score",
    "reimbursement_score",
    "valuation_score",
    "technical_entry_score",
    "technical_liquidity_score",
    "technical_alpha_score",
    "technical_pullback_score",
    "borrow_availability_score",
    "borrow_fee_score",
    "borrow_squeeze_risk_score",
    "borrow_pressure_score",
    "short_interest_score",
    "short_pressure_score",
    "short_squeeze_score",
    "short_volume_score",
    "short_interest_velocity_score",
    "days_to_cover_score",
    "institutional_accumulation_score",
    "institutional_crowding_score",
    "institutional_breadth_score",
    "insider_net_buy_score",
    "insider_cluster_buy_score",
    "insider_selling_pressure_score",
    "insider_activity_score",
    "sentiment_catalyst_score",
    "value_trap_score",
)
OUTPUT_FIELDS = [
    "calibration_cohort",
    "component",
    "count_30d",
    "unique_tickers_30d",
    "spearman_ic_30d",
    "ic_t_stat_30d",
    "top_minus_bottom_median_excess_30d",
    "count_60d",
    "unique_tickers_60d",
    "spearman_ic_60d",
    "ic_t_stat_60d",
    "top_minus_bottom_median_excess_60d",
    "count_120d",
    "unique_tickers_120d",
    "spearman_ic_120d",
    "ic_t_stat_120d",
    "top_minus_bottom_median_excess_120d",
    "best_horizon_days",
    "decay_profile",
    "suggested_refresh_cadence",
    "diagnostic_reason",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze med-device component signal decay across horizons.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--input-csv", type=Path, default=None)
    parser.add_argument("--output-csv", type=Path, default=None)
    return parser.parse_args()


def to_float(raw: object) -> float | None:
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def fmt(value: float | None, digits: int = 6) -> str:
    return "" if value is None else f"{value:.{digits}f}"


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_FIELDS, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def parse_int_list(raw: object, default: str) -> list[int]:
    out: list[int] = []
    for item in str(raw or default).split(","):
        text = item.strip()
        if text.isdigit():
            out.append(int(text))
    return sorted(set(out))


def parse_component_list(raw: object) -> list[str]:
    text = str(raw or "").strip()
    if not text:
        return list(DEFAULT_COMPONENTS)
    return [item.strip() for item in text.split(",") if item.strip()]


def ranks(values: list[float]) -> list[float]:
    indexed = sorted(enumerate(values), key=lambda item: item[1])
    out = [0.0] * len(values)
    pos = 0
    while pos < len(indexed):
        end = pos + 1
        while end < len(indexed) and indexed[end][1] == indexed[pos][1]:
            end += 1
        avg_rank = (pos + end - 1) / 2.0
        for original_idx, _ in indexed[pos:end]:
            out[original_idx] = avg_rank
        pos = end
    return out


def correlation(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 3 or len(xs) != len(ys):
        return None
    x_mean = sum(xs) / len(xs)
    y_mean = sum(ys) / len(ys)
    numerator = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys))
    x_denom = math.sqrt(sum((x - x_mean) ** 2 for x in xs))
    y_denom = math.sqrt(sum((y - y_mean) ** 2 for y in ys))
    if x_denom <= 0 or y_denom <= 0:
        return None
    return numerator / (x_denom * y_denom)


def spearman(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 3 or len(xs) != len(ys):
        return None
    return correlation(ranks(xs), ranks(ys))


def correlation_t_stat(corr: float | None, n: int) -> float | None:
    if corr is None or n < 3:
        return None
    if abs(corr) >= 1.0:
        return math.copysign(float("inf"), corr)
    denom = max(1e-12, 1.0 - corr * corr)
    return corr * math.sqrt((n - 2) / denom)


def quintile_spread(pairs: list[tuple[float, float]]) -> float | None:
    if not pairs:
        return None
    sorted_pairs = sorted(pairs, key=lambda item: item[0])
    bucket_n = max(1, len(sorted_pairs) // 5)
    bottom = [item[1] for item in sorted_pairs[:bucket_n]]
    top = [item[1] for item in sorted_pairs[-bucket_n:]]
    return median(top) - median(bottom) if top and bottom else None


def horizon_metric(rows: list[dict[str, str]], *, component: str, horizon: int) -> dict[str, Any]:
    target = f"cohort_excess_return_{horizon}d"
    pairs: list[tuple[str, float, float]] = []
    for row in rows:
        score = to_float(row.get(component))
        excess = to_float(row.get(target))
        ticker = str(row.get("ticker") or "")
        if score is None or excess is None or not ticker:
            continue
        pairs.append((ticker, score, excess))
    scores = [item[1] for item in pairs]
    returns = [item[2] for item in pairs]
    ic = spearman(scores, returns)
    return {
        "count": len(pairs),
        "unique_tickers": len({item[0] for item in pairs}),
        "ic": ic,
        "t_stat": correlation_t_stat(ic, len(pairs)),
        "spread": quintile_spread([(item[1], item[2]) for item in pairs]),
    }


def classify_decay(metrics: dict[int, dict[str, Any]], *, min_obs: int, min_abs_ic: float, min_t: float, fast_ratio: float) -> tuple[str, str, str, str]:
    eligible = {
        horizon: metric
        for horizon, metric in metrics.items()
        if metric["count"] >= min_obs
        and metric["ic"] is not None
        and abs(float(metric["ic"])) >= min_abs_ic
        and metric["t_stat"] is not None
        and abs(float(metric["t_stat"])) >= min_t
    }
    if not eligible:
        return "", "insufficient_signal", "diagnostic_only", "no horizon clears obs/ic/t-stat thresholds"
    best_horizon, best_metric = max(eligible.items(), key=lambda item: abs(float(item[1]["ic"])))
    abs_30 = abs(float(metrics.get(30, {}).get("ic") or 0.0))
    abs_60 = abs(float(metrics.get(60, {}).get("ic") or 0.0))
    abs_120 = abs(float(metrics.get(120, {}).get("ic") or 0.0))
    if best_horizon == 30 and abs_30 >= fast_ratio * max(abs_60, abs_120, 1e-9):
        return "30", "fast_decay", "weekly", "30d IC dominates longer horizons"
    if best_horizon == 60:
        return "60", "medium_decay", "monthly", "60d IC is strongest eligible horizon"
    if best_horizon == 120:
        return "120", "slow_decay", "quarterly", "120d IC is strongest eligible horizon"
    return str(best_horizon), "mixed_decay", "monthly", "eligible horizons are mixed; monthly review is the conservative default"


def build_rows(
    rows: list[dict[str, str]],
    *,
    components: list[str],
    horizons: list[int],
    min_obs: int,
    min_abs_ic: float,
    min_t: float,
    fast_ratio: float,
) -> list[dict[str, Any]]:
    cohorts = sorted({str(row.get("calibration_cohort") or "") for row in rows if row.get("calibration_cohort")})
    out: list[dict[str, Any]] = []
    for cohort in cohorts:
        cohort_rows = [row for row in rows if str(row.get("calibration_cohort") or "") == cohort]
        for component in components:
            if not any(component in row for row in cohort_rows):
                continue
            metrics = {horizon: horizon_metric(cohort_rows, component=component, horizon=horizon) for horizon in horizons}
            best, profile, cadence, reason = classify_decay(
                metrics,
                min_obs=min_obs,
                min_abs_ic=min_abs_ic,
                min_t=min_t,
                fast_ratio=fast_ratio,
            )
            item: dict[str, Any] = {
                "calibration_cohort": cohort,
                "component": component,
                "best_horizon_days": best,
                "decay_profile": profile,
                "suggested_refresh_cadence": cadence,
                "diagnostic_reason": reason,
            }
            for horizon in (30, 60, 120):
                metric = metrics.get(horizon, {"count": 0, "unique_tickers": 0, "ic": None, "t_stat": None, "spread": None})
                item[f"count_{horizon}d"] = metric["count"]
                item[f"unique_tickers_{horizon}d"] = metric["unique_tickers"]
                item[f"spearman_ic_{horizon}d"] = fmt(metric["ic"])
                item[f"ic_t_stat_{horizon}d"] = fmt(metric["t_stat"], 4)
                item[f"top_minus_bottom_median_excess_{horizon}d"] = fmt(metric["spread"])
            out.append(item)
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
            cfg_get(config, "calibration.signal_decay.output_csv", "../output/med_devices_reports/calibration/med_device_signal_decay_analysis.csv"),
            base_dir=base_dir,
        )
    )
    components = parse_component_list(
        cfg_get(config, "calibration.signal_decay.components", cfg_get(config, "calibration.feature_stability.components", ""))
    )
    horizons = parse_int_list(cfg_get(config, "calibration.signal_decay.horizons", "30,60,120"), "30,60,120")
    rows = build_rows(
        read_csv(input_csv),
        components=components,
        horizons=horizons,
        min_obs=int(cfg_get(config, "calibration.signal_decay.min_obs", 50)),
        min_abs_ic=float(cfg_get(config, "calibration.signal_decay.min_abs_ic", 0.05)),
        min_t=float(cfg_get(config, "calibration.signal_decay.min_ic_t_stat", 2.0)),
        fast_ratio=float(cfg_get(config, "calibration.signal_decay.fast_decay_ratio", 1.25)),
    )
    write_csv(output_csv, rows)
    print(f"signal_decay_csv={output_csv} rows={len(rows)}")


if __name__ == "__main__":
    main()
