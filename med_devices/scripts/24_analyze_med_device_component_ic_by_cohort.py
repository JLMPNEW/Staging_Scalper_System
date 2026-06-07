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
    "ic_tilted_composite_score",
    "cohort_percentile",
    "safe_core_score",
    "safe_core_percentile",
    "safe_core_cohort_percentile",
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
    "fda_product_score_legacy",
    "fda_alpha_score",
    "fda_safety_score",
    "fda_clearance_velocity_raw",
    "fda_clearance_velocity_score",
    "fda_clearance_acceleration_raw",
    "fda_clearance_acceleration_score",
    "fda_evidence_quality_score",
    "fda_event_risk_score",
    "quality_value_interaction_score",
    "fda_technical_interaction_score",
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
]
OUTPUT_FIELDS = [
    "calibration_cohort",
    "horizon_days",
    "component",
    "count",
    "unique_tickers",
    "spearman_ic_excess",
    "spearman_ic_excess_t_stat",
    "spearman_ic_excess_p_value",
    "spearman_ic_excess_bh_q_value",
    "spearman_ic_excess_bh_accepted",
    "net_spearman_ic_excess",
    "net_spearman_ic_excess_t_stat",
    "net_spearman_ic_excess_p_value",
    "net_spearman_ic_excess_bh_q_value",
    "net_spearman_ic_excess_bh_accepted",
    "factor_neutral_spearman_ic_excess",
    "factor_neutral_spearman_ic_excess_t_stat",
    "factor_neutral_spearman_ic_excess_p_value",
    "factor_neutral_spearman_ic_excess_bh_q_value",
    "factor_neutral_spearman_ic_excess_bh_accepted",
    "factor_neutralization_obs",
    "factor_neutralization_factors",
    "pearson_ic_excess",
    "spearman_ic_raw",
    "top_quintile_count",
    "bottom_quintile_count",
    "top_quintile_median_excess",
    "bottom_quintile_median_excess",
    "top_minus_bottom_median_excess",
    "net_top_minus_bottom_median_excess",
    "factor_neutral_top_minus_bottom_median_excess",
    "top_quintile_hit_rate_excess",
    "bottom_quintile_hit_rate_excess",
    "recommendation",
    "net_recommendation",
    "factor_neutral_recommendation",
    "production_recommendation",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compute component IC by med-device calibration cohort.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--input-csv", type=Path, default=None)
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--technical-output-csv", type=Path, default=None)
    parser.add_argument("--min-obs", type=int, default=0)
    parser.add_argument("--min-ic-t-stat", type=float, default=None)
    parser.add_argument("--bh-fdr", type=float, default=None)
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


def matrix_solve(matrix: list[list[float]], rhs: list[float]) -> list[float] | None:
    n = len(rhs)
    if n == 0 or any(len(row) != n for row in matrix):
        return None
    augmented = [list(matrix[row]) + [rhs[row]] for row in range(n)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda row: abs(augmented[row][col]))
        if abs(augmented[pivot][col]) < 1e-10:
            return None
        if pivot != col:
            augmented[col], augmented[pivot] = augmented[pivot], augmented[col]
        scale = augmented[col][col]
        augmented[col] = [value / scale for value in augmented[col]]
        for row in range(n):
            if row == col:
                continue
            factor = augmented[row][col]
            if abs(factor) <= 1e-12:
                continue
            augmented[row] = [cur - factor * base for cur, base in zip(augmented[row], augmented[col])]
    return [augmented[row][-1] for row in range(n)]


def factor_value(row: dict[str, str], factor: str) -> float | None:
    if factor == "log_market_cap":
        value = to_float(row.get("market_cap"))
        return math.log1p(value) if value is not None and value > 0 else None
    return to_float(row.get(factor))


def residualize_scores(
    records: list[tuple[str, dict[str, str], float, float, float | None]],
    *,
    component: str,
    factors: list[str],
    min_group_n: int,
    ridge: float,
) -> list[tuple[str, float, float, float | None]]:
    active_factors = [factor for factor in factors if factor != component]
    if not active_factors:
        return []
    grouped: dict[str, list[tuple[str, dict[str, str], float, float, float | None, list[float]]]] = {}
    for ticker, row, score, excess, net_excess in records:
        xs = [factor_value(row, factor) for factor in active_factors]
        if any(value is None for value in xs):
            continue
        grouped.setdefault(str(row.get("asof_date") or ""), []).append(
            (ticker, row, score, excess, net_excess, [float(value) for value in xs if value is not None])
        )
    out: list[tuple[str, float, float, float | None]] = []
    for items in grouped.values():
        feature_count = len(active_factors)
        if len(items) < max(min_group_n, feature_count + 3):
            continue
        columns = [[item[5][col] for item in items] for col in range(feature_count)]
        means = [mean(column) for column in columns]
        stds: list[float] = []
        for column, avg in zip(columns, means):
            variance = sum((value - avg) ** 2 for value in column) / max(1, len(column) - 1)
            stds.append(math.sqrt(variance) if variance > 1e-12 else 1.0)
        x_rows = [[1.0] + [(value - avg) / std for value, avg, std in zip(item[5], means, stds)] for item in items]
        y_values = [item[2] for item in items]
        k = feature_count + 1
        xtx = [[0.0 for _ in range(k)] for _ in range(k)]
        xty = [0.0 for _ in range(k)]
        for x_row, y_value in zip(x_rows, y_values):
            for i in range(k):
                xty[i] += x_row[i] * y_value
                for j in range(k):
                    xtx[i][j] += x_row[i] * x_row[j]
        for i in range(1, k):
            xtx[i][i] += ridge
        beta = matrix_solve(xtx, xty)
        if beta is None:
            continue
        for item, x_row in zip(items, x_rows):
            prediction = sum(beta_i * x_i for beta_i, x_i in zip(beta, x_row))
            out.append((item[0], item[2] - prediction, item[3], item[4]))
    return out


def fmt(value: float | None, digits: int = 6) -> str:
    return "" if value is None else f"{value:.{digits}f}"


def two_sided_t_p_value(t_stat: float | None, df: int) -> float | None:
    if t_stat is None or not math.isfinite(t_stat) or df < 1:
        return None
    try:
        from scipy import stats  # type: ignore

        return float(2.0 * stats.t.sf(abs(t_stat), df=df))
    except Exception:
        return math.erfc(abs(t_stat) / math.sqrt(2.0))


def correlation_t_stat(corr: float | None, n: int) -> float | None:
    if corr is None or n < 3:
        return None
    if abs(corr) >= 1.0:
        return math.copysign(float("inf"), corr)
    denom = max(1e-12, 1.0 - corr * corr)
    return corr * math.sqrt((n - 2) / denom)


def hit_rate(values: list[float]) -> str:
    return "" if not values else f"{sum(1 for value in values if value > 0) / len(values):.4f}"


def as_bool_text(value: bool) -> str:
    return "1" if value else "0"


def recommendation(
    *,
    count: int,
    ic: float | None,
    spread: float | None,
    t_stat: float | None,
    bh_accepted: bool,
    min_obs: int,
    min_abs_ic: float,
    min_t_stat: float,
) -> str:
    if count < min_obs or ic is None or spread is None or t_stat is None:
        return "insufficient_observations"
    if abs(ic) < min_abs_ic:
        return "weak_or_unstable_factor"
    if abs(t_stat) < min_t_stat:
        return "insufficient_ic_t_stat"
    if not bh_accepted:
        return "not_significant_after_bh_fdr"
    if ic > 0 and spread > 0:
        return "positive_candidate_factor"
    if ic < 0 and spread < 0:
        return "negative_or_inverse_factor"
    return "weak_or_unstable_factor"


def quintile_spread(pairs: list[tuple[float, float]]) -> tuple[int, int, float | None, float | None, float | None]:
    sorted_pairs = sorted(pairs, key=lambda item: item[0])
    quintile_n = max(1, len(sorted_pairs) // 5) if sorted_pairs else 0
    bottom = [item[1] for item in sorted_pairs[:quintile_n]]
    top = [item[1] for item in sorted_pairs[-quintile_n:]]
    top_med = median(top) if top else None
    bottom_med = median(bottom) if bottom else None
    spread = (top_med - bottom_med) if top_med is not None and bottom_med is not None else None
    return len(top), len(bottom), top_med, bottom_med, spread


def analyze_component(
    rows: list[dict[str, str]],
    *,
    cohort: str,
    horizon: int,
    component: str,
    factors: list[str],
    factor_neutral_min_group_n: int,
    factor_neutral_ridge: float,
) -> dict[str, Any]:
    pairs: list[tuple[str, dict[str, str], float, float, float, float | None]] = []
    for row in rows:
        if str(row.get("calibration_cohort") or "") != cohort:
            continue
        component_value = to_float(row.get(component))
        excess = to_float(row.get(f"cohort_excess_return_{horizon}d"))
        raw_return = to_float(row.get(f"forward_return_{horizon}d"))
        net_excess = to_float(row.get(f"net_cohort_excess_return_{horizon}d"))
        if component_value is None or excess is None or raw_return is None:
            continue
        pairs.append((str(row.get("ticker") or ""), row, component_value, excess, raw_return, net_excess))
    xs = [item[2] for item in pairs]
    excess_values = [item[3] for item in pairs]
    raw_values = [item[4] for item in pairs]
    top_count, bottom_count, top_med, bottom_med, spread = quintile_spread([(item[2], item[3]) for item in pairs])
    ic = spearman(xs, excess_values)
    t_stat = correlation_t_stat(ic, len(pairs))
    p_value = two_sided_t_p_value(t_stat, max(1, len(pairs) - 2))
    net_pairs = [(item[2], float(item[5])) for item in pairs if item[5] is not None]
    net_ic = spearman([item[0] for item in net_pairs], [item[1] for item in net_pairs])
    net_t_stat = correlation_t_stat(net_ic, len(net_pairs))
    net_p_value = two_sided_t_p_value(net_t_stat, max(1, len(net_pairs) - 2))
    _, _, _, _, net_spread = quintile_spread(net_pairs)
    residualized = residualize_scores(
        [(ticker, row, score, excess, net_excess) for ticker, row, score, excess, _, net_excess in pairs],
        component=component,
        factors=factors,
        min_group_n=factor_neutral_min_group_n,
        ridge=factor_neutral_ridge,
    )
    factor_xs = [item[1] for item in residualized]
    factor_excess_values = [item[2] for item in residualized]
    factor_ic = spearman(factor_xs, factor_excess_values)
    factor_t_stat = correlation_t_stat(factor_ic, len(residualized))
    factor_p_value = two_sided_t_p_value(factor_t_stat, max(1, len(residualized) - 2))
    _, _, _, _, factor_spread = quintile_spread([(item[1], item[2]) for item in residualized])
    return {
        "calibration_cohort": cohort,
        "horizon_days": horizon,
        "component": component,
        "count": len(pairs),
        "unique_tickers": len({item[0] for item in pairs}),
        "spearman_ic_excess": fmt(ic),
        "spearman_ic_excess_t_stat": fmt(t_stat, 4),
        "spearman_ic_excess_p_value": fmt(p_value, 8),
        "spearman_ic_excess_bh_q_value": "",
        "spearman_ic_excess_bh_accepted": "0",
        "net_spearman_ic_excess": fmt(net_ic),
        "net_spearman_ic_excess_t_stat": fmt(net_t_stat, 4),
        "net_spearman_ic_excess_p_value": fmt(net_p_value, 8),
        "net_spearman_ic_excess_bh_q_value": "",
        "net_spearman_ic_excess_bh_accepted": "0",
        "factor_neutral_spearman_ic_excess": fmt(factor_ic),
        "factor_neutral_spearman_ic_excess_t_stat": fmt(factor_t_stat, 4),
        "factor_neutral_spearman_ic_excess_p_value": fmt(factor_p_value, 8),
        "factor_neutral_spearman_ic_excess_bh_q_value": "",
        "factor_neutral_spearman_ic_excess_bh_accepted": "0",
        "factor_neutralization_obs": len(residualized),
        "factor_neutralization_factors": ",".join(factor for factor in factors if factor != component),
        "pearson_ic_excess": fmt(correlation(xs, excess_values)),
        "spearman_ic_raw": fmt(spearman(xs, raw_values)),
        "top_quintile_count": top_count,
        "bottom_quintile_count": bottom_count,
        "top_quintile_median_excess": fmt(top_med),
        "bottom_quintile_median_excess": fmt(bottom_med),
        "top_minus_bottom_median_excess": fmt(spread),
        "net_top_minus_bottom_median_excess": fmt(net_spread),
        "factor_neutral_top_minus_bottom_median_excess": fmt(factor_spread),
        "top_quintile_hit_rate_excess": hit_rate([item[3] for item in sorted(pairs, key=lambda item: item[2])[-top_count:]]),
        "bottom_quintile_hit_rate_excess": hit_rate([item[3] for item in sorted(pairs, key=lambda item: item[2])[:bottom_count]]),
        "recommendation": "",
        "net_recommendation": "",
        "factor_neutral_recommendation": "",
        "production_recommendation": "",
    }


def apply_bh_correction(
    rows: list[dict[str, Any]],
    *,
    fdr: float,
    scope: str,
    p_field: str,
    q_field: str,
    accepted_field: str,
) -> None:
    grouped: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for row in rows:
        if scope == "global":
            key = ("global",)
        elif scope == "cohort":
            key = (str(row.get("calibration_cohort") or ""),)
        elif scope == "horizon":
            key = (str(row.get("horizon_days") or ""),)
        else:
            key = (str(row.get("calibration_cohort") or ""), str(row.get("horizon_days") or ""))
        grouped.setdefault(key, []).append(row)
    for items in grouped.values():
        valid = [(idx, to_float(row.get(p_field))) for idx, row in enumerate(items)]
        valid = [(idx, p_value) for idx, p_value in valid if p_value is not None]
        if not valid:
            continue
        ordered = sorted(valid, key=lambda item: item[1])
        m = len(ordered)
        q_by_idx: dict[int, float] = {}
        running_min = 1.0
        for rank, (idx, p_value) in reversed(list(enumerate(ordered, start=1))):
            adjusted = min(running_min, p_value * m / rank)
            running_min = adjusted
            q_by_idx[idx] = min(1.0, adjusted)
        for idx, _ in valid:
            q_value = q_by_idx[idx]
            items[idx][q_field] = fmt(q_value, 8)
            items[idx][accepted_field] = as_bool_text(q_value <= fdr)


def apply_recommendations(
    rows: list[dict[str, Any]],
    *,
    min_obs: int,
    min_abs_ic: float,
    min_t_stat: float,
) -> None:
    for row in rows:
        row["recommendation"] = recommendation(
            count=int(row.get("count") or 0),
            ic=to_float(row.get("spearman_ic_excess")),
            spread=to_float(row.get("top_minus_bottom_median_excess")),
            t_stat=to_float(row.get("spearman_ic_excess_t_stat")),
            bh_accepted=str(row.get("spearman_ic_excess_bh_accepted") or "") == "1",
            min_obs=min_obs,
            min_abs_ic=min_abs_ic,
            min_t_stat=min_t_stat,
        )
        row["net_recommendation"] = recommendation(
            count=int(row.get("count") or 0),
            ic=to_float(row.get("net_spearman_ic_excess")),
            spread=to_float(row.get("net_top_minus_bottom_median_excess")),
            t_stat=to_float(row.get("net_spearman_ic_excess_t_stat")),
            bh_accepted=str(row.get("net_spearman_ic_excess_bh_accepted") or "") == "1",
            min_obs=min_obs,
            min_abs_ic=min_abs_ic,
            min_t_stat=min_t_stat,
        )
        row["factor_neutral_recommendation"] = recommendation(
            count=int(row.get("factor_neutralization_obs") or 0),
            ic=to_float(row.get("factor_neutral_spearman_ic_excess")),
            spread=to_float(row.get("factor_neutral_top_minus_bottom_median_excess")),
            t_stat=to_float(row.get("factor_neutral_spearman_ic_excess_t_stat")),
            bh_accepted=str(row.get("factor_neutral_spearman_ic_excess_bh_accepted") or "") == "1",
            min_obs=min_obs,
            min_abs_ic=min_abs_ic,
            min_t_stat=min_t_stat,
        )
        aligned = {
            row["recommendation"],
            row["net_recommendation"],
            row["factor_neutral_recommendation"],
        }
        if aligned == {"positive_candidate_factor"}:
            row["production_recommendation"] = "positive_candidate_factor"
        elif aligned == {"negative_or_inverse_factor"}:
            row["production_recommendation"] = "negative_or_inverse_factor"
        elif row["recommendation"] in {"positive_candidate_factor", "negative_or_inverse_factor"}:
            row["production_recommendation"] = "research_only_net_or_factor_validation_failed"
        else:
            row["production_recommendation"] = row["recommendation"]


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
    min_obs = (
        int(args.min_obs)
        if args.min_obs > 0
        else int(cfg_get(config, "calibration.component_ic.min_obs", 50))
    )
    min_abs_ic = float(cfg_get(config, "calibration.component_ic.min_abs_spearman_ic", 0.05))
    min_t_stat = (
        float(args.min_ic_t_stat)
        if args.min_ic_t_stat is not None
        else float(cfg_get(config, "calibration.component_ic.min_ic_t_stat", 2.0))
    )
    bh_fdr = (
        float(args.bh_fdr)
        if args.bh_fdr is not None
        else float(cfg_get(config, "calibration.component_ic.bh_fdr", 0.05))
    )
    bh_scope = str(cfg_get(config, "calibration.component_ic.bh_scope", "cohort_horizon") or "cohort_horizon").strip().lower()
    factor_text = str(
        cfg_get(
            config,
            "calibration.component_ic.factor_neutralization_factors",
            "log_market_cap,valuation_score,technical_relative_strength_score,technical_volatility_risk_score",
        )
        or ""
    )
    factors = [item.strip() for item in factor_text.split(",") if item.strip()]
    factor_neutral_min_group_n = int(cfg_get(config, "calibration.component_ic.factor_neutralization_min_group_n", 10))
    factor_neutral_ridge = float(cfg_get(config, "calibration.component_ic.factor_neutralization_ridge", 1.0))
    rows = read_csv(input_csv)
    horizons = return_horizons(rows)
    cohorts = sorted({str(row.get("calibration_cohort") or "") for row in rows if str(row.get("calibration_cohort") or "")})
    out = [
        analyze_component(
            rows,
            cohort=cohort,
            horizon=horizon,
            component=component,
            factors=factors,
            factor_neutral_min_group_n=factor_neutral_min_group_n,
            factor_neutral_ridge=factor_neutral_ridge,
        )
        for cohort in cohorts
        for horizon in horizons
        for component in COMPONENT_FIELDS
    ]
    apply_bh_correction(
        out,
        fdr=bh_fdr,
        scope=bh_scope,
        p_field="spearman_ic_excess_p_value",
        q_field="spearman_ic_excess_bh_q_value",
        accepted_field="spearman_ic_excess_bh_accepted",
    )
    apply_bh_correction(
        out,
        fdr=bh_fdr,
        scope=bh_scope,
        p_field="net_spearman_ic_excess_p_value",
        q_field="net_spearman_ic_excess_bh_q_value",
        accepted_field="net_spearman_ic_excess_bh_accepted",
    )
    apply_bh_correction(
        out,
        fdr=bh_fdr,
        scope=bh_scope,
        p_field="factor_neutral_spearman_ic_excess_p_value",
        q_field="factor_neutral_spearman_ic_excess_bh_q_value",
        accepted_field="factor_neutral_spearman_ic_excess_bh_accepted",
    )
    apply_recommendations(out, min_obs=min_obs, min_abs_ic=min_abs_ic, min_t_stat=min_t_stat)
    write_csv(output_csv, out)
    technical_out = [row for row in out if str(row.get("component") or "").startswith("technical_")]
    write_csv(technical_output_csv, technical_out)
    print(f"component_ic_csv={output_csv} rows={len(out)}")
    print(f"technical_component_ic_csv={technical_output_csv} rows={len(technical_out)}")


if __name__ == "__main__":
    main()
