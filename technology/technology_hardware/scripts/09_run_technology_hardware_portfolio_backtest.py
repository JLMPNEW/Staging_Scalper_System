#!/usr/bin/env python3
"""Stage 9 report-only portfolio simulations for technology hardware scores."""
from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import statistics
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from technology.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from technology.core.logging_utils import configure_utc_logging  # noqa: E402
from technology.technology_hardware.optuna_calibration import (  # noqa: E402
    Candidate,
    json_ready_weights,
    load_panel,
    score_row,
    stage7_candidate,
    write_csv,
)


LOGGER = logging.getLogger("technology_hardware_portfolio_backtest")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
CONFIG_KEY = "technology_hardware_portfolio_backtest"
OUTPUT_PREFIX = "technology_hardware_portfolio_backtest"


@dataclass(frozen=True)
class PortfolioSpec:
    portfolio_name: str
    long_quantile: float
    short_quantile: float
    weight_method: str
    exposure_mode: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Stage 9 technology hardware portfolio simulations.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    return parser.parse_args()


def as_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def candidate_from_json(payload: dict[str, Any]) -> Candidate:
    subfeatures: dict[str, list[tuple[str, float]]] = {}
    for component, weights in (payload.get("subfeature_weights") or {}).items():
        if isinstance(weights, dict):
            subfeatures[str(component)] = [(str(key), float(value)) for key, value in weights.items() if float(value) > 0]
    return Candidate(
        component_weights={str(key): float(value) for key, value in (payload.get("component_weights") or {}).items()},
        subfeature_specs=subfeatures,
    )


def candidate_from_config_section(config: dict[str, Any], config_key: str) -> Candidate:
    raw_components = cfg_get(config, f"{config_key}.component_weights", {}) or {}
    raw_subfeatures = cfg_get(config, f"{config_key}.subfeature_weights", {}) or {}
    component_weights = {
        str(key): float(value)
        for key, value in raw_components.items()
        if as_float(value) is not None and float(value) > 0
    } if isinstance(raw_components, dict) else {}
    subfeatures: dict[str, list[tuple[str, float]]] = {}
    if isinstance(raw_subfeatures, dict):
        for component, weights in raw_subfeatures.items():
            if isinstance(weights, dict):
                subfeatures[str(component)] = [
                    (str(key), float(value))
                    for key, value in weights.items()
                    if as_float(value) is not None and float(value) > 0
                ]
    return Candidate(component_weights=component_weights, subfeature_specs=subfeatures)


def load_candidates(config: dict[str, Any], base_dir: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [
        {
            "model_name": str(cfg_get(config, f"{CONFIG_KEY}.production_model_name", "stage7_current_production_v1")),
            "model_type": str(cfg_get(config, f"{CONFIG_KEY}.production_model_type", "production")),
            "promotion_candidate": 1,
            "candidate": stage7_candidate(config),
        }
    ]
    challenger_key = str(cfg_get(config, f"{CONFIG_KEY}.stage7_challenger_config_key", ""))
    if challenger_key:
        out.append(
            {
                "model_name": str(cfg_get(config, f"{CONFIG_KEY}.stage7_challenger_model_name", "stage7_challenger_v1")),
                "model_type": "challenger_baseline",
                "promotion_candidate": 0,
                "candidate": candidate_from_config_section(config, challenger_key),
            }
        )
    stage8_path = resolve_path(
        cfg_get(config, f"{CONFIG_KEY}.stage8_weights_json", "../output/technology_reports/technology_hardware/optuna_calibration/stage8_best_weights.json"),
        base_dir=base_dir,
    )
    if bool(cfg_get(config, f"{CONFIG_KEY}.include_stage8_report_only_candidate", False)) and stage8_path.exists():
        payload = json.loads(stage8_path.read_text(encoding="utf-8"))
        out.append(
            {
                "model_name": "stage8_best_candidate_report_only",
                "model_type": "optuna_report_only",
                "promotion_candidate": int(payload.get("promotion_candidate") or 0),
                "candidate": candidate_from_json(payload),
            }
        )
    return out


def portfolio_specs(config: dict[str, Any]) -> list[PortfolioSpec]:
    raw_methods = cfg_get(config, f"{CONFIG_KEY}.weight_methods", ["equal_weight", "score_weight"])
    methods = [str(value) for value in raw_methods] if isinstance(raw_methods, list) else ["equal_weight", "score_weight"]
    raw_specs = cfg_get(config, f"{CONFIG_KEY}.portfolio_specs", []) or []
    out: list[PortfolioSpec] = []
    if not isinstance(raw_specs, list):
        return out
    for spec in raw_specs:
        if not isinstance(spec, dict):
            continue
        name = str(spec.get("portfolio_name") or "").strip()
        if not name:
            continue
        long_q = float(spec.get("long_quantile") or 0.0)
        short_q = float(spec.get("short_quantile") or 0.0)
        raw_modes = spec.get("exposure_modes")
        if isinstance(raw_modes, list) and raw_modes:
            modes = [str(value).strip() for value in raw_modes if str(value).strip()]
        elif short_q > 0:
            modes = ["dollar_neutral", "beta_neutral"]
        else:
            modes = ["long_only", "hedged_long"]
        for method in methods:
            for mode in modes:
                out.append(PortfolioSpec(name, long_q, short_q, method, mode))
    return out


def weighted_leg(rows: list[dict[str, Any]], *, side: str, method: str) -> dict[str, float]:
    if not rows:
        return {}
    if method == "score_weight":
        scores = [float(row["score"]) for row in rows]
        if side == "long":
            floor = min(scores)
            raw = [max(0.0, score - floor) + 1e-6 for score in scores]
        else:
            ceiling = max(scores)
            raw = [max(0.0, ceiling - score) + 1e-6 for score in scores]
        total = sum(raw)
        return {str(row["ticker"]): value / total for row, value in zip(rows, raw)} if total > 0 else {}
    weight = 1.0 / len(rows)
    return {str(row["ticker"]): weight for row in rows}


def beta_exposure(weights: dict[str, float], by_ticker: dict[str, dict[str, Any]]) -> float:
    return sum(weight * (as_float(by_ticker.get(ticker, {}).get("beta_to_benchmark")) or 1.0) for ticker, weight in weights.items())


def normalize_gross(weights: dict[str, float], target_gross: float = 1.0) -> dict[str, float]:
    gross = sum(abs(weight) for weight in weights.values())
    if gross <= 0:
        return weights
    return {ticker: weight * target_gross / gross for ticker, weight in weights.items()}


def build_weights(scored_rows: list[dict[str, Any]], spec: PortfolioSpec, min_positions: int) -> tuple[dict[str, float], float]:
    ordered = sorted(scored_rows, key=lambda row: (-float(row["score"]), str(row["ticker"])))
    n = len(ordered)
    long_n = min(n, max(min_positions, int(math.ceil(n * spec.long_quantile))))
    weights = weighted_leg(ordered[:long_n], side="long", method=spec.weight_method)
    if spec.short_quantile > 0:
        short_n = min(n - long_n, max(min_positions, int(math.ceil(n * spec.short_quantile))))
        short_rows = list(reversed(ordered[-short_n:])) if short_n > 0 else []
        for ticker, weight in weighted_leg(short_rows, side="short", method=spec.weight_method).items():
            weights[ticker] = weights.get(ticker, 0.0) - weight
    by_ticker = {str(row["ticker"]): row for row in scored_rows}
    hedge_weight = 0.0
    if spec.short_quantile > 0:
        if spec.exposure_mode == "beta_neutral":
            long_beta = sum(weight * (as_float(by_ticker.get(ticker, {}).get("beta_to_benchmark")) or 1.0) for ticker, weight in weights.items() if weight > 0)
            short_beta = -sum(weight * (as_float(by_ticker.get(ticker, {}).get("beta_to_benchmark")) or 1.0) for ticker, weight in weights.items() if weight < 0)
            if short_beta > 0:
                weights = {ticker: (weight * long_beta / short_beta if weight < 0 else weight) for ticker, weight in weights.items()}
        weights = normalize_gross(weights, target_gross=1.0)
    elif spec.exposure_mode == "hedged_long":
        hedge_weight = -beta_exposure(weights, by_ticker)
    return weights, hedge_weight


def turnover(prev: dict[str, float] | None, current: dict[str, float]) -> float:
    if prev is None:
        return sum(abs(value) for value in current.values())
    return 0.5 * sum(abs(current.get(ticker, 0.0) - prev.get(ticker, 0.0)) for ticker in (set(prev) | set(current)))


def max_cohort_share(weights: dict[str, float], cohort_by_ticker: dict[str, str]) -> float:
    gross = sum(abs(weight) for weight in weights.values())
    if gross <= 0:
        return 0.0
    by_cohort: dict[str, float] = {}
    for ticker, weight in weights.items():
        cohort = cohort_by_ticker.get(ticker) or "unknown"
        by_cohort[cohort] = by_cohort.get(cohort, 0.0) + abs(weight)
    return max(by_cohort.values()) / gross if by_cohort else 0.0


def annual_borrow_rate(value: Any, default_rate: float) -> float:
    rate = as_float(value)
    if rate is None:
        return default_rate
    return rate / 100.0 if abs(rate) > 1.0 else rate


def borrow_cost_for_period(weights: dict[str, float], by_ticker: dict[str, dict[str, Any]], *, horizon_days: int, default_annual_rate: float) -> float:
    cost = 0.0
    for ticker, weight in weights.items():
        if weight < 0:
            cost += abs(weight) * max(0.0, annual_borrow_rate(by_ticker.get(ticker, {}).get("latest_borrow_fee_rate"), default_annual_rate)) * horizon_days / 252.0
    return cost


def drawdown(values: list[float]) -> float:
    equity = 1.0
    peak = 1.0
    max_dd = 0.0
    for value in values:
        equity *= 1.0 + value
        peak = max(peak, equity)
        if peak > 0:
            max_dd = min(max_dd, equity / peak - 1.0)
    return max_dd


def summarize_returns(rows: list[dict[str, Any]], horizon: int) -> dict[str, Any]:
    returns = [float(row["net_return"]) for row in rows]
    if not returns:
        return {}
    periods_per_year = 252.0 / max(1, horizon)
    total = math.prod(1.0 + value for value in returns) - 1.0
    mean_return = sum(returns) / len(returns)
    vol = statistics.stdev(returns) * math.sqrt(periods_per_year) if len(returns) > 2 else 0.0
    annualized = (1.0 + total) ** (periods_per_year / len(returns)) - 1.0 if total > -1.0 else -1.0
    return {
        "periods": len(returns),
        "total_return": total,
        "annualized_return": annualized,
        "annualized_vol": vol,
        "sharpe": (mean_return * periods_per_year / vol) if vol > 0 else "",
        "hit_rate": sum(1 for value in returns if value > 0) / len(returns),
        "avg_period_return": mean_return,
        "median_period_return": statistics.median(returns),
        "max_drawdown": drawdown(returns),
        "avg_excess_return_vs_qqq": sum(float(row["excess_return_vs_benchmark"]) for row in rows) / len(rows),
        "avg_excess_return_vs_equal_weight": sum(float(row["excess_return_vs_equal_weight"]) for row in rows) / len(rows),
        "avg_turnover": sum(float(row["turnover"]) for row in rows) / len(rows),
        "avg_stock_turnover": sum(float(row["stock_turnover"]) for row in rows) / len(rows),
        "avg_hedge_turnover": sum(float(row["hedge_turnover"]) for row in rows) / len(rows),
        "avg_transaction_cost": sum(float(row["transaction_cost"]) for row in rows) / len(rows),
        "avg_stock_transaction_cost": sum(float(row["stock_transaction_cost"]) for row in rows) / len(rows),
        "avg_hedge_transaction_cost": sum(float(row["hedge_transaction_cost"]) for row in rows) / len(rows),
        "avg_borrow_cost": sum(float(row["borrow_cost"]) for row in rows) / len(rows),
        "avg_total_cost": sum(float(row["total_cost"]) for row in rows) / len(rows),
        "avg_positions": sum(int(row["position_count"]) for row in rows) / len(rows),
        "avg_gross_exposure": sum(float(row["gross_exposure"]) for row in rows) / len(rows),
        "avg_beta_exposure": sum(float(row["beta_exposure"]) for row in rows) / len(rows),
        "avg_max_cohort_share": sum(float(row["max_cohort_share"]) for row in rows) / len(rows),
    }


def simulate(
    panel: list[dict[str, Any]],
    dates: list[date],
    horizons: list[int],
    candidate: Candidate,
    spec: PortfolioSpec,
    *,
    model_name: str,
    neutral_score: float,
    min_cross_section: int,
    min_positions: int,
    transaction_cost_bps: float,
    borrow_cost_default_annual_rate: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    horizon = int(horizons[0])
    fwd_key = f"fwd_return_{horizon}d"
    bench_key = f"benchmark_return_{horizon}d"
    rows_by_date: dict[date, list[dict[str, Any]]] = {}
    for row in panel:
        if row["asof_date"] in dates:
            rows_by_date.setdefault(row["asof_date"], []).append(row)
    previous_stock_weights: dict[str, float] | None = None
    previous_hedge_weight: float | None = None
    period_rows: list[dict[str, Any]] = []
    holding_rows: list[dict[str, Any]] = []
    for asof in sorted(rows_by_date):
        scored: list[dict[str, Any]] = []
        for row in rows_by_date[asof]:
            if row.get(fwd_key) is None:
                continue
            score, quality, component_scores, _component_quality = score_row(row, candidate, neutral_score=neutral_score)
            if quality <= 0:
                continue
            item = dict(row)
            item["score"] = score
            item["score_quality"] = quality
            item["_component_scores"] = component_scores
            scored.append(item)
        if len(scored) < min_cross_section:
            continue
        weights, hedge_weight = build_weights(scored, spec, min_positions)
        if not weights:
            continue
        by_ticker = {str(row["ticker"]): row for row in scored}
        benchmark_return = as_float(scored[0].get(bench_key)) or 0.0
        equal_weight_benchmark_return = sum(float(row[fwd_key]) for row in scored if row.get(fwd_key) is not None) / len(scored)
        stock_return = sum(weight * float(by_ticker[ticker][fwd_key]) for ticker, weight in weights.items() if ticker in by_ticker)
        hedge_return = hedge_weight * benchmark_return
        raw_return = stock_return + hedge_return
        stock_turnover = turnover(previous_stock_weights, weights)
        hedge_turnover = abs(hedge_weight) if previous_hedge_weight is None else abs(hedge_weight - previous_hedge_weight)
        transaction_cost = (stock_turnover + hedge_turnover) * transaction_cost_bps / 10000.0
        stock_transaction_cost = stock_turnover * transaction_cost_bps / 10000.0
        hedge_transaction_cost = hedge_turnover * transaction_cost_bps / 10000.0
        borrow_cost = borrow_cost_for_period(weights, by_ticker, horizon_days=horizon, default_annual_rate=borrow_cost_default_annual_rate)
        total_cost = transaction_cost + borrow_cost
        effective_weights = dict(weights)
        if hedge_weight:
            effective_weights["__QQQ_HEDGE__"] = hedge_weight
        beta_exp = beta_exposure(weights, by_ticker) + hedge_weight
        cohort_by_ticker = {str(row["ticker"]): str(row.get("cohort") or "") for row in scored}
        short_count = sum(1 for value in weights.values() if value < 0)
        period_rows.append(
            {
                "asof_date": asof.isoformat(),
                "model_name": model_name,
                "portfolio_name": spec.portfolio_name,
                "weight_method": spec.weight_method,
                "exposure_mode": spec.exposure_mode,
                "horizon_days": horizon,
                "stock_return": stock_return,
                "hedge_return": hedge_return,
                "raw_return": raw_return,
                "transaction_cost": transaction_cost,
                "stock_transaction_cost": stock_transaction_cost,
                "hedge_transaction_cost": hedge_transaction_cost,
                "borrow_cost": borrow_cost,
                "total_cost": total_cost,
                "net_return": raw_return - total_cost,
                "benchmark_return": benchmark_return,
                "equal_weight_benchmark_return": equal_weight_benchmark_return,
                "excess_return": raw_return - total_cost - equal_weight_benchmark_return if short_count == 0 else raw_return - total_cost,
                "excess_return_vs_benchmark": raw_return - total_cost - benchmark_return,
                "excess_return_vs_equal_weight": raw_return - total_cost - equal_weight_benchmark_return,
                "turnover": stock_turnover + hedge_turnover,
                "stock_turnover": stock_turnover,
                "hedge_turnover": hedge_turnover,
                "gross_exposure": sum(abs(weight) for weight in effective_weights.values()),
                "beta_exposure": beta_exp,
                "hedge_weight": hedge_weight,
                "position_count": len(weights),
                "long_count": sum(1 for value in weights.values() if value > 0),
                "short_count": short_count,
                "max_cohort_share": max_cohort_share(weights, cohort_by_ticker),
                "cross_section_count": len(scored),
            }
        )
        for ticker, weight in sorted(weights.items(), key=lambda item: (-abs(item[1]), item[0])):
            source = by_ticker[ticker]
            component_scores = source.get("_component_scores") or {}
            holding_rows.append(
                {
                    "asof_date": asof.isoformat(),
                    "model_name": model_name,
                    "portfolio_name": spec.portfolio_name,
                    "weight_method": spec.weight_method,
                    "exposure_mode": spec.exposure_mode,
                    "ticker": ticker,
                    "weight": weight,
                    "score": source["score"],
                    "score_quality": source["score_quality"],
                    "cohort": source.get("cohort") or "",
                    "beta_to_benchmark": source.get("beta_to_benchmark"),
                    "borrow_fee_rate": source.get("latest_borrow_fee_rate"),
                    "fwd_return": source.get(fwd_key),
                    "quality_score": component_scores.get("quality"),
                    "valuation_score": component_scores.get("valuation"),
                    "risk_control_score": component_scores.get("risk_control"),
                    "positioning_score": component_scores.get("positioning"),
                    "market_behavior_score": component_scores.get("market_behavior"),
                    "growth_score": component_scores.get("growth"),
                }
            )
        if hedge_weight:
            holding_rows.append(
                {
                    "asof_date": asof.isoformat(),
                    "model_name": model_name,
                    "portfolio_name": spec.portfolio_name,
                    "weight_method": spec.weight_method,
                    "exposure_mode": spec.exposure_mode,
                    "ticker": "__QQQ_HEDGE__",
                    "weight": hedge_weight,
                    "score": "",
                    "score_quality": "",
                    "cohort": "benchmark_hedge",
                    "beta_to_benchmark": 1.0,
                    "borrow_fee_rate": "",
                    "fwd_return": benchmark_return,
                    "quality_score": "",
                    "valuation_score": "",
                    "risk_control_score": "",
                    "positioning_score": "",
                    "market_behavior_score": "",
                    "growth_score": "",
                }
            )
        previous_stock_weights = dict(weights)
        previous_hedge_weight = hedge_weight
    return period_rows, holding_rows


def main() -> int:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    output_dir = args.output_dir.expanduser().resolve() if args.output_dir else resolve_path(
        cfg_get(config, f"{CONFIG_KEY}.output_dir", "../output/technology_reports/technology_hardware/backtests"),
        base_dir=base_dir,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    panel, panel_dates, horizons = load_panel(config, base_dir)
    if not panel_dates:
        raise RuntimeError("No Stage 8A panel dates available for portfolio simulation.")
    neutral_score = float(cfg_get(config, "technology_hardware_scoring_features.neutral_score", 50.0))
    min_cross_section = int(cfg_get(config, f"{CONFIG_KEY}.min_cross_section", 30))
    min_positions = int(cfg_get(config, f"{CONFIG_KEY}.min_positions", 5))
    transaction_cost_bps = float(cfg_get(config, f"{CONFIG_KEY}.transaction_cost_bps", 20.0))
    borrow_cost_default_annual_rate = float(cfg_get(config, f"{CONFIG_KEY}.borrow_cost_default_annual_rate", 0.0))
    benchmark_ticker = str(cfg_get(config, f"{CONFIG_KEY}.benchmark_ticker", "QQQ"))
    candidates = load_candidates(config, base_dir)
    specs = portfolio_specs(config)
    if not specs:
        raise RuntimeError("No technology hardware portfolio specs configured.")
    all_periods: list[dict[str, Any]] = []
    all_holdings: list[dict[str, Any]] = []
    summary_rows: list[dict[str, Any]] = []
    for candidate_item in candidates:
        candidate = candidate_item["candidate"]
        model_name = str(candidate_item["model_name"])
        for spec in specs:
            period_rows, holding_rows = simulate(
                panel,
                panel_dates,
                horizons,
                candidate,
                spec,
                model_name=model_name,
                neutral_score=neutral_score,
                min_cross_section=min_cross_section,
                min_positions=min_positions,
                transaction_cost_bps=transaction_cost_bps,
                borrow_cost_default_annual_rate=borrow_cost_default_annual_rate,
            )
            all_periods.extend(period_rows)
            all_holdings.extend(holding_rows)
            metrics = summarize_returns(period_rows, int(horizons[0]))
            if metrics:
                summary_rows.append(
                    {
                        "model_name": model_name,
                        "model_type": candidate_item["model_type"],
                        "promotion_candidate": candidate_item["promotion_candidate"],
                        "portfolio_name": spec.portfolio_name,
                        "weight_method": spec.weight_method,
                        "exposure_mode": spec.exposure_mode,
                        "horizon_days": int(horizons[0]),
                        **metrics,
                        "component_weights_json": json.dumps(json_ready_weights(candidate)["component_weights"], sort_keys=True),
                    }
                )
    summary_rows.sort(key=lambda row: (str(row["portfolio_name"]), str(row["weight_method"]), str(row["exposure_mode"]), -float(row["annualized_return"])))
    summary_path = output_dir / f"{OUTPUT_PREFIX}_summary.csv"
    periods_path = output_dir / f"{OUTPUT_PREFIX}_periods.csv"
    holdings_path = output_dir / f"{OUTPUT_PREFIX}_holdings.csv"
    manifest_path = output_dir / f"{OUTPUT_PREFIX}_manifest.json"
    write_csv(summary_path, summary_rows)
    write_csv(periods_path, all_periods)
    write_csv(holdings_path, all_holdings)
    manifest = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "database_path": str(db_path),
        "panel_rows": len(panel),
        "panel_dates": len(panel_dates),
        "date_range": [panel_dates[0].isoformat(), panel_dates[-1].isoformat()],
        "horizon_days": int(horizons[0]),
        "benchmark_ticker": benchmark_ticker,
        "models": [item["model_name"] for item in candidates],
        "portfolio_specs": [spec.__dict__ for spec in specs],
        "transaction_cost_bps": transaction_cost_bps,
        "borrow_cost_default_annual_rate": borrow_cost_default_annual_rate,
        "return_normalization": {
            "long_short": "stock weights are gross-normalized to 1.0 for dollar-neutral and beta-neutral exposure modes",
            "hedged_long": f"long basket return minus trailing-beta-scaled {benchmark_ticker} return; hedge leg has explicit turnover/cost columns and is included in total turnover, transaction cost, and gross exposure",
        },
        "outputs": {"summary_csv": str(summary_path), "periods_csv": str(periods_path), "holdings_csv": str(holdings_path)},
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    LOGGER.info("Stage 9 portfolio backtest complete: models=%d portfolios=%d output=%s", len(candidates), len(specs), output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
