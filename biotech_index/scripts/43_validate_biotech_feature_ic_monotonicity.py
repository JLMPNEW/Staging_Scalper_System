#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import importlib.util
import math
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path
from statistics import mean
from typing import Any, Iterable


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from biotech_index.core.config import cfg_get, load_yaml, normalize_string_list, resolve_path  # noqa: E402
from biotech_index.core.db import connect  # noqa: E402
from biotech_index.core.market_policy import calibration_market_sources  # noqa: E402
from biotech_index.core.text_norm import normalize_ticker  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "output" / "biotech_index_reports" / "feature_ic_monitor"
CALIBRATION_MODULE_PATH = PACKAGE_ROOT / "scripts" / "28_calibrate_biotech_opportunity.py"


# Factors that measure data availability or history depth rather than fundamental signal.
# These can show spurious IC (survivorship/liquidity bias) and must not auto-promote to production.
DATA_AVAILABILITY_PROXY_FACTORS: frozenset[str] = frozenset({
    "borrow_fee_data_available_flag",
    "shortable_data_available_flag",
    "borrow_fee_history_count_30d",
    "borrow_fee_history_count_90d",
    "borrow_fee_staleness_days",
    "shortable_staleness_days",
    "borrow_fee_stale_flag",
    "float_shares_proxy_flag",
    "float_shares_staleness_days",
    "float_shares_measurement_staleness_days",
    "short_interest_pct_float_available_flag",
    "short_interest_signal_max_possible_score",
})

CANDIDATE_FACTORS = [
    "short_interest_signal_score",
    "short_interest_pct_float",
    "short_interest_pct_float_available_flag",
    "short_interest_pct_score",
    "short_interest_days_to_cover_score",
    "short_interest_signal_max_possible_score",
    "float_shares_proxy_flag",
    "float_shares_staleness_days",
    "float_shares_measurement_staleness_days",
    "borrow_pressure_score",
    "borrow_rate_current",
    "borrow_fee_data_available_flag",
    "shortable_data_available_flag",
    "borrow_fee_staleness_days",
    "shortable_staleness_days",
    "borrow_fee_history_count_30d",
    "borrow_fee_history_count_90d",
    "borrow_rate_spike_flag",
    "hard_to_borrow_flag",
    "institutional_accumulation_score",
    "institutional_ownership_delta_pct",
    "new_institutional_buyer_count",
    "exiting_institutional_holder_count",
    "net_institutional_buyer_count",
    "forward_catalyst_score",
    "forward_catalyst_unfiltered_score",
    "ctgov_forward_catalyst_score",
    "open_market_buy_count_90d",
    "planned_10b5_1_buy_count",
    "insider_accumulation_score",
    "adcom_score",
    "adcom_nearest_days",
    "adcom_within_60d_flag",
    "adcom_within_120d_flag",
    "adcom_committee_oncology_flag",
    "breakthrough_therapy_count",
    "orphan_drug_count",
    "fast_track_count",
    "rmat_count",
    "priority_review_flag",
    "fda_designation_tier",
    "fda_designation_score",
]
CATALYST_FACTORS = {
    "forward_catalyst_score",
    "forward_catalyst_unfiltered_score",
    "ctgov_forward_catalyst_score",
    "adcom_score",
    "adcom_within_60d_flag",
    "adcom_within_120d_flag",
    "priority_review_flag",
    "fda_designation_score",
    "fda_designation_tier",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Per-feature IC / monotonicity monitor for biotech shadow and candidate factors. "
            "This is report-only and does not change scoring."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--start-asof", type=str, default="")
    parser.add_argument("--end-asof", type=str, default="")
    parser.add_argument("--horizons", type=str, default="20,60,120")
    parser.add_argument("--market-sources", type=str, default="")
    parser.add_argument("--max-snapshots", type=int, default=0)
    parser.add_argument("--include-non-fridays", action="store_true")
    parser.add_argument("--strict-feature-lag", action="store_true")
    parser.add_argument("--no-strict-feature-lag", dest="strict_feature_lag", action="store_false")
    parser.set_defaults(strict_feature_lag=None)
    parser.add_argument("--next-bar-entry", action="store_true")
    parser.add_argument("--same-bar-entry", dest="next_bar_entry", action="store_false")
    parser.set_defaults(next_bar_entry=None)
    parser.add_argument("--factors", type=str, default="", help="Optional comma-separated factor subset.")
    parser.add_argument("--min-observations", type=int, default=40)
    parser.add_argument("--min-quintile-observations", type=int, default=8)
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Accepted for clean-sequence compatibility; the IC monitor recomputes report outputs.",
    )
    return parser.parse_args()


def load_calibration_module() -> Any:
    spec = importlib.util.spec_from_file_location("biotech_tier1_calibration", CALIBRATION_MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load calibration module from {CALIBRATION_MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def parse_optional_date(raw: str) -> date | None:
    clean = str(raw or "").strip()
    if not clean:
        return None
    if len(clean) == 8 and clean.isdigit():
        clean = f"{clean[:4]}-{clean[4:6]}-{clean[6:]}"
    return date.fromisoformat(clean)


def parse_horizons(raw: str) -> list[int]:
    out: list[int] = []
    for item in str(raw or "").split(","):
        clean = item.strip()
        if not clean:
            continue
        horizon = int(clean)
        if horizon <= 0:
            raise ValueError(f"Invalid horizon: {item!r}")
        out.append(horizon)
    if not out:
        raise ValueError("At least one horizon is required.")
    return sorted(set(out))


def to_float(raw: object, default: float | None = None) -> float | None:
    try:
        value = float(str(raw).strip()) if raw is not None and str(raw).strip() != "" else None
    except (TypeError, ValueError):
        return default
    if value is None or not math.isfinite(value):
        return default
    return value


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fieldnames is None:
        fieldnames = []
        seen: set[str] = set()
        for row in rows:
            for key in row:
                if key not in seen:
                    seen.add(key)
                    fieldnames.append(key)
        if not fieldnames:
            fieldnames = ["message"]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def percentile_lcb(values: list[float], *, z: float = 1.0) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    avg = mean(values)
    variance = sum((value - avg) ** 2 for value in values) / (len(values) - 1)
    return avg - z * math.sqrt(variance) / math.sqrt(len(values))


def rank_values(values: list[float]) -> list[float]:
    indexed = sorted(enumerate(values), key=lambda item: item[1])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(indexed):
        j = i + 1
        while j < len(indexed) and indexed[j][1] == indexed[i][1]:
            j += 1
        avg_rank = (i + 1 + j) / 2.0
        for k in range(i, j):
            ranks[indexed[k][0]] = avg_rank
        i = j
    return ranks


def pearson(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) != len(ys) or len(xs) < 3:
        return None
    x_mean = mean(xs)
    y_mean = mean(ys)
    x_var = sum((value - x_mean) ** 2 for value in xs)
    y_var = sum((value - y_mean) ** 2 for value in ys)
    if x_var <= 0.0 or y_var <= 0.0:
        return None
    cov = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys))
    return cov / math.sqrt(x_var * y_var)


def spearman(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) != len(ys) or len(xs) < 3:
        return None
    return pearson(rank_values(xs), rank_values(ys))


def source_group_for(row: dict[str, Any], factor: str) -> str:
    if factor.startswith("short_interest_") or factor in {"days_to_cover", "float_shares"}:
        float_source = str(row.get("float_shares_source") or "").strip().lower()
        proxy_flag = to_float(row.get("float_shares_proxy_flag"), 0.0) or 0.0
        if float_source:
            return f"float_source:{float_source}" + (":proxy" if proxy_flag > 0.0 else "")
        basis = str(row.get("short_interest_signal_basis") or "").strip().lower()
        if basis:
            return basis
        available = to_float(row.get("short_interest_pct_float_available_flag"), 0.0) or 0.0
        return "pct_float_available" if available > 0.0 else "days_to_cover_only_or_missing_float"
    if factor.startswith("float_shares_") or factor.startswith("public_float_"):
        float_source = str(row.get("float_shares_source") or "").strip().lower()
        return f"float_source:{float_source}" if float_source else "no_float_source"
    if factor.startswith("borrow_") or factor.startswith("shortable_") or factor == "hard_to_borrow_flag":
        fee_available = to_float(row.get("borrow_fee_data_available_flag"), 0.0) or 0.0
        shortable_available = to_float(row.get("shortable_data_available_flag"), 0.0) or 0.0
        if fee_available > 0.0 and shortable_available > 0.0:
            return "ibkr_fee_and_shortable_available"
        if fee_available > 0.0:
            return "ibkr_fee_only"
        if shortable_available > 0.0:
            return "ibkr_shortable_only"
        return "ibkr_unavailable"
    if factor not in CATALYST_FACTORS:
        return "ALL"
    source = str(row.get("forward_catalyst_source") or "").strip().lower()
    ctgov_score = to_float(row.get("ctgov_forward_catalyst_score"), 0.0) or 0.0
    forward_score = to_float(row.get("forward_catalyst_score"), 0.0) or 0.0
    if "ctgov" in source or ctgov_score > 0.0:
        return "ctgov"
    if "manual" in source or "override" in source:
        return "manual"
    if "sec" in source or "filing" in source or forward_score > 0.0:
        return "sec_or_curated"
    return "none"


def cohort_for(row: dict[str, Any]) -> str:
    return str(row.get("biotech_primary_cohort") or row.get("primary_cohort") or "unknown").strip() or "unknown"


def completed_rows(rows: Iterable[dict[str, Any]], factor: str, ret_key: str) -> list[tuple[dict[str, Any], float, float]]:
    out: list[tuple[dict[str, Any], float, float]] = []
    for row in rows:
        value = to_float(row.get(factor))
        ret = to_float(row.get(ret_key))
        if value is None or ret is None:
            continue
        out.append((row, value, ret))
    return out


def split_quintiles(values: list[tuple[dict[str, Any], float, float]]) -> dict[int, list[tuple[dict[str, Any], float, float]]]:
    if not values:
        return {}
    if len({item[1] for item in values}) < 2:
        return {3: list(values)}
    ordered = sorted(values, key=lambda item: item[1])
    quintiles: dict[int, list[tuple[dict[str, Any], float, float]]] = defaultdict(list)
    n = len(ordered)
    for idx, item in enumerate(ordered):
        quintile = min(5, int(idx * 5 / n) + 1)
        quintiles[quintile].append(item)
    return dict(quintiles)


def metrics_for_values(
    values: list[tuple[dict[str, Any], float, float]],
    *,
    lcb_z: float,
) -> dict[str, Any]:
    returns = [item[2] for item in values]
    factor_values = [item[1] for item in values]
    tickers = {normalize_ticker(item[0].get("ticker")) for item in values if normalize_ticker(item[0].get("ticker"))}
    asofs = {str(item[0].get("asof_date") or "") for item in values if str(item[0].get("asof_date") or "")}
    lcb = percentile_lcb(returns, z=lcb_z)
    return {
        "n": len(values),
        "unique_tickers": len(tickers),
        "asof_count": len(asofs),
        "factor_mean": "" if not factor_values else round(mean(factor_values), 6),
        "factor_min": "" if not factor_values else round(min(factor_values), 6),
        "factor_max": "" if not factor_values else round(max(factor_values), 6),
        "mean_return_pct": "" if not returns else round(100.0 * mean(returns), 4),
        "hit_rate_pct": "" if not returns else round(100.0 * sum(1 for ret in returns if ret > 0.0) / len(returns), 4),
        "lcb_pct": "" if lcb is None else round(100.0 * lcb, 4),
        "loss20_rate_pct": "" if not returns else round(100.0 * sum(1 for ret in returns if ret <= -0.20) / len(returns), 4),
        "spearman_ic": "" if (ic := spearman(factor_values, returns)) is None else round(ic, 6),
    }


def summary_for_group(
    *,
    factor: str,
    horizon: int,
    cohort: str,
    source_group: str,
    values: list[tuple[dict[str, Any], float, float]],
    lcb_z: float,
    min_observations: int,
    min_quintile_observations: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    base = metrics_for_values(values, lcb_z=lcb_z)
    quintile_rows: list[dict[str, Any]] = []
    quintiles = split_quintiles(values)
    q_metrics: dict[int, dict[str, Any]] = {}
    for quintile in range(1, 6):
        q_values = quintiles.get(quintile, [])
        metrics = metrics_for_values(q_values, lcb_z=lcb_z)
        q_metrics[quintile] = metrics
        quintile_rows.append(
            {
                "factor": factor,
                "horizon": horizon,
                "cohort": cohort,
                "source_group": source_group,
                "quintile": quintile,
                **metrics,
            }
        )

    q1 = q_metrics.get(1, {})
    q5 = q_metrics.get(5, {})
    q_means = [to_float(q_metrics.get(q, {}).get("mean_return_pct")) for q in range(1, 6)]
    mean_spread = (
        (to_float(q5.get("mean_return_pct")) or 0.0) - (to_float(q1.get("mean_return_pct")) or 0.0)
        if to_float(q5.get("mean_return_pct")) is not None and to_float(q1.get("mean_return_pct")) is not None
        else None
    )
    lcb_spread = (
        (to_float(q5.get("lcb_pct")) or 0.0) - (to_float(q1.get("lcb_pct")) or 0.0)
        if to_float(q5.get("lcb_pct")) is not None and to_float(q1.get("lcb_pct")) is not None
        else None
    )
    monotonicity = pearson(
        [float(idx) for idx, value in enumerate(q_means, start=1) if value is not None],
        [float(value) for value in q_means if value is not None],
    )
    min_q_n = min((int(to_float(q_metrics.get(q, {}).get("n"), 0.0) or 0.0) for q in range(1, 6)), default=0)
    factor_unique_values = len({item[1] for item in values})
    spearman_ic = to_float(base.get("spearman_ic"))
    classification = classify_factor_group(
        n=int(base["n"]),
        min_q_n=min_q_n,
        factor_unique_values=factor_unique_values,
        spearman_ic=spearman_ic,
        mean_spread=mean_spread,
        lcb_spread=lcb_spread,
        monotonicity=monotonicity,
        cohort=cohort,
        source_group=source_group,
        min_observations=min_observations,
        min_quintile_observations=min_quintile_observations,
        factor=factor,
    )
    summary = {
        "factor": factor,
        "horizon": horizon,
        "cohort": cohort,
        "source_group": source_group,
        **base,
        "q1_mean_return_pct": q1.get("mean_return_pct", ""),
        "q5_mean_return_pct": q5.get("mean_return_pct", ""),
        "quintile_mean_spread_pct": "" if mean_spread is None else round(mean_spread, 4),
        "q1_lcb_pct": q1.get("lcb_pct", ""),
        "q5_lcb_pct": q5.get("lcb_pct", ""),
        "quintile_lcb_spread_pct": "" if lcb_spread is None else round(lcb_spread, 4),
        "monotonicity_score": "" if monotonicity is None else round(monotonicity, 6),
        "min_quintile_n": min_q_n,
        "factor_unique_values": factor_unique_values,
        "classification": classification,
    }
    return summary, quintile_rows


def classify_factor_group(
    *,
    n: int,
    min_q_n: int,
    factor_unique_values: int,
    spearman_ic: float | None,
    mean_spread: float | None,
    lcb_spread: float | None,
    monotonicity: float | None,
    cohort: str,
    source_group: str,
    min_observations: int,
    min_quintile_observations: int,
    factor: str = "",
) -> str:
    if n < min_observations or factor_unique_values < 2 or min_q_n < min_quintile_observations:
        return "insufficient_data"
    # Availability/history-depth proxies can show spurious IC via survivorship bias.
    # Cap their classification at diagnostic_only regardless of IC strength.
    if factor in DATA_AVAILABILITY_PROXY_FACTORS:
        return "diagnostic_only"
    ic = spearman_ic or 0.0
    spread = mean_spread or 0.0
    lcb = lcb_spread or 0.0
    mono = monotonicity or 0.0
    strongly_positive = ic >= 0.04 and spread > 0.75 and lcb >= -0.25 and mono >= 0.25
    strongly_negative = ic <= -0.04 and spread < -0.75 and lcb <= 0.25 and mono <= -0.25
    if strongly_negative:
        return "invert_or_redesign"
    if strongly_positive and (cohort != "ALL" or source_group not in {"ALL", "none"}):
        return "cohort_specific_only"
    if strongly_positive:
        return "promote_candidate"
    return "diagnostic_only"


def rows_by_groups(
    values: list[tuple[dict[str, Any], float, float]],
    *,
    factor: str,
) -> dict[tuple[str, str], list[tuple[dict[str, Any], float, float]]]:
    grouped: dict[tuple[str, str], list[tuple[dict[str, Any], float, float]]] = defaultdict(list)
    for item in values:
        row = item[0]
        source_group = source_group_for(row, factor)
        cohort = cohort_for(row)
        grouped[("ALL", source_group)].append(item)
        grouped[(cohort, source_group)].append(item)
        grouped[("ALL", "ALL")].append(item)
        grouped[(cohort, "ALL")].append(item)
    return grouped


def dedupe_group_values(values: list[tuple[dict[str, Any], float, float]]) -> list[tuple[dict[str, Any], float, float]]:
    seen: set[tuple[str, str, float, float]] = set()
    out: list[tuple[dict[str, Any], float, float]] = []
    for row, factor_value, ret in values:
        key = (str(row.get("asof_date") or ""), str(row.get("ticker") or ""), factor_value, ret)
        if key in seen:
            continue
        seen.add(key)
        out.append((row, factor_value, ret))
    return out


def selected_factor_list(raw: str) -> list[str]:
    if not str(raw or "").strip():
        return list(CANDIDATE_FACTORS)
    selected = [item.strip() for item in str(raw).split(",") if item.strip()]
    unknown = sorted(set(selected) - set(CANDIDATE_FACTORS))
    if unknown:
        raise ValueError(f"Unsupported factor(s): {', '.join(unknown)}")
    return selected


def main() -> None:
    args = parse_args()
    config = load_yaml(args.config)
    base_dir = args.config.resolve().parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    output_dir = args.output_dir.resolve()
    horizons = parse_horizons(args.horizons)
    factors = selected_factor_list(args.factors)
    calibration = load_calibration_module()
    params = calibration.load_calibration_params(config)
    strict_feature_lag = (
        bool(args.strict_feature_lag)
        if args.strict_feature_lag is not None
        else calibration.as_bool(cfg_get(config, "calibration.tier1.strict_feature_lag", True), True)
    )
    next_bar_entry = (
        bool(args.next_bar_entry)
        if args.next_bar_entry is not None
        else calibration.as_bool(cfg_get(config, "calibration.tier1.next_bar_entry", True), True)
    )
    market_sources_raw = args.market_sources if str(args.market_sources or "").strip() else None
    market_sources = [
        str(source)
        for source in normalize_string_list(market_sources_raw, calibration_market_sources(config))
        if str(source).strip()
    ]
    if not market_sources:
        raise ValueError("No market sources configured for forward-return lookup.")
    extra_exclusions = {
        normalize_ticker(ticker)
        for ticker in normalize_string_list(cfg_get(config, "calibration.exclude_tickers", []))
        if normalize_ticker(ticker)
    }
    min_addv20 = float(
        cfg_get(
            config,
            "biotech_scoring.core_structural_veto.min_addv20",
            cfg_get(config, "multibagger.min_addv20", 1_000_000.0),
        )
    )

    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        snapshot_dates = calibration.load_snapshot_dates(
            conn,
            start_asof=parse_optional_date(args.start_asof),
            end_asof=parse_optional_date(args.end_asof),
            fridays_only=not bool(args.include_non_fridays),
            max_snapshots=max(0, int(args.max_snapshots)),
        )
        if not snapshot_dates:
            raise ValueError("No daily_features snapshot dates found for feature IC monitor.")
        excluded_tickers = calibration.load_excluded_tickers(
            conn,
            exclude_current_removals=False,
            extra=extra_exclusions,
        )
        observations = calibration.load_observations(
            conn,
            snapshot_dates,
            excluded_tickers,
            config,
            min_addv20=min_addv20,
            strict_feature_lag=strict_feature_lag,
            growth_drag_curve=params.growth_drag_curve,
            use_decomposed_risk_for_penalty=params.use_decomposed_risk_for_penalty,
        )
        if not observations:
            raise ValueError("No feature observations remain after exclusions.")
        tickers = {ticker for row in observations if (ticker := normalize_ticker(row.get("ticker")))}
        market_tickers = set(tickers)
        if params.alpha_adjustment_enabled and params.benchmark_ticker:
            market_tickers.add(params.benchmark_ticker)
        asof_dates = [parsed for row in observations if (parsed := calibration.parse_date(row.get("asof_date"))) is not None]
        if not asof_dates:
            raise ValueError("Feature observations have no valid as-of dates.")
        bars_by_ticker = calibration.load_bars(
            conn,
            tickers=market_tickers,
            min_date=min(asof_dates),
            market_sources=market_sources,
        )

    calibration.add_forward_returns(
        observations,
        bars_by_ticker,
        horizons,
        round_trip_cost_bps=params.round_trip_cost_bps,
        next_bar_entry=next_bar_entry,
        benchmark_ticker=params.benchmark_ticker if params.alpha_adjustment_enabled else "",
        benchmark_bars=bars_by_ticker.get(params.benchmark_ticker, []) if params.alpha_adjustment_enabled else [],
    )

    summary_rows: list[dict[str, Any]] = []
    cohort_rows: list[dict[str, Any]] = []
    quintile_rows: list[dict[str, Any]] = []
    classification_rows: list[dict[str, Any]] = []
    for horizon in horizons:
        ret_key = calibration.objective_return_key(horizon, params)
        for factor in factors:
            all_values = completed_rows(observations, factor, ret_key)
            grouped = rows_by_groups(all_values, factor=factor)
            for (cohort, source_group), values in sorted(grouped.items()):
                values = dedupe_group_values(values)
                if source_group == "none" and factor in CATALYST_FACTORS:
                    continue
                summary, q_rows = summary_for_group(
                    factor=factor,
                    horizon=horizon,
                    cohort=cohort,
                    source_group=source_group,
                    values=values,
                    lcb_z=float(params.lcb_z),
                    min_observations=max(1, int(args.min_observations)),
                    min_quintile_observations=max(1, int(args.min_quintile_observations)),
                )
                if cohort == "ALL" and source_group == "ALL":
                    summary_rows.append(summary)
                else:
                    cohort_rows.append(summary)
                quintile_rows.extend(q_rows)
                classification_rows.append(
                    {
                        "factor": factor,
                        "horizon": horizon,
                        "cohort": cohort,
                        "source_group": source_group,
                        "classification": summary["classification"],
                        "n": summary["n"],
                        "unique_tickers": summary["unique_tickers"],
                        "spearman_ic": summary["spearman_ic"],
                        "quintile_mean_spread_pct": summary["quintile_mean_spread_pct"],
                        "quintile_lcb_spread_pct": summary["quintile_lcb_spread_pct"],
                        "monotonicity_score": summary["monotonicity_score"],
                        "factor_unique_values": summary["factor_unique_values"],
                    }
                )

    manifest = [
        {
            "snapshot_date_count": len(snapshot_dates),
            "first_snapshot_date": snapshot_dates[0],
            "last_snapshot_date": snapshot_dates[-1],
            "observation_count": len(observations),
            "factor_count": len(factors),
            "horizons": ",".join(str(horizon) for horizon in horizons),
            "return_objective": calibration.return_objective_label(params),
            "strict_feature_lag": 1 if strict_feature_lag else 0,
            "next_bar_entry": 1 if next_bar_entry else 0,
            "market_sources": "|".join(market_sources),
            "min_observations": int(args.min_observations),
            "min_quintile_observations": int(args.min_quintile_observations),
        }
    ]
    write_csv(output_dir / "feature_ic_monitor_manifest.csv", manifest)
    write_csv(output_dir / "feature_ic_summary.csv", summary_rows)
    write_csv(output_dir / "feature_ic_by_cohort.csv", cohort_rows)
    write_csv(output_dir / "feature_ic_quintiles.csv", quintile_rows)
    write_csv(output_dir / "feature_ic_classification.csv", classification_rows)
    print(f"Wrote feature IC / monotonicity monitor outputs to {output_dir}")


if __name__ == "__main__":
    main()
