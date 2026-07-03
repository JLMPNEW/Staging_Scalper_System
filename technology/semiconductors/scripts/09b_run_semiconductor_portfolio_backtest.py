#!/usr/bin/env python3
"""Stage 9 report-only portfolio simulations for semiconductor scores."""
from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import math
import statistics
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from technology.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from technology.core.logging_utils import configure_utc_logging  # noqa: E402
from technology.semiconductors.optuna_calibration import (  # noqa: E402
    Candidate,
    build_panel,
    json_ready_weights,
    score_row,
    stage7_candidate,
    write_csv,
)


LOGGER = logging.getLogger("semiconductor_portfolio_backtest")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
CONFIG_KEY = "semiconductor_portfolio_backtest"
STATIC_CANDIDATE_SCRIPT = PACKAGE_ROOT / "semiconductors" / "scripts" / "14_compare_semiconductor_stage7_static_candidates.py"


@dataclass(frozen=True)
class PortfolioSpec:
    portfolio_name: str
    long_quantile: float
    short_quantile: float
    weight_method: str
    exposure_mode: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Stage 9 semiconductor portfolio simulations.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    return parser.parse_args()


def load_static_candidate_module() -> ModuleType | None:
    spec = importlib.util.spec_from_file_location("semiconductor_static_candidates", STATIC_CANDIDATE_SCRIPT)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def candidate_from_json(payload: dict[str, Any]) -> Candidate:
    subfeatures: dict[str, list[tuple[str, float]]] = {}
    for component, weights in (payload.get("subfeature_weights") or {}).items():
        if isinstance(weights, dict):
            subfeatures[str(component)] = [(str(key), float(value)) for key, value in weights.items() if float(value) > 0]
    return Candidate(
        component_weights={str(key): float(value) for key, value in (payload.get("component_weights") or {}).items()},
        subfeature_specs=subfeatures,
    )


def load_candidates(config: dict[str, Any], base_dir: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [
        {
            "model_name": "stage7_current_v1",
            "model_type": "production_baseline",
            "promotion_candidate": 1,
            "candidate": stage7_candidate(config),
        }
    ]

    module = load_static_candidate_module()
    if module is not None and hasattr(module, "static_candidates"):
        for item in module.static_candidates(config):
            name = str(item.get("candidate_name") or "")
            if not name or name == "stage7_current_v1":
                continue
            out.append(
                {
                    "model_name": name,
                    "model_type": "static_review",
                    "promotion_candidate": int(item.get("production_eligible") or 0),
                    "candidate": item["candidate"],
                }
            )

    stage8_path = resolve_path(
        cfg_get(config, "semiconductor_portfolio_backtest.stage8_weights_json", "../output/technology_reports/optuna_calibration/stage8_best_weights.json"),
        base_dir=base_dir,
    )
    if stage8_path.exists():
        payload = json.loads(stage8_path.read_text(encoding="utf-8"))
        out.append(
            {
                "model_name": "stage8_best_candidate_report_only",
                "model_type": "optuna_report_only",
                "promotion_candidate": int(payload.get("promotion_candidate") or 0),
                "candidate": candidate_from_json(payload),
            }
        )

    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for item in out:
        name = str(item["model_name"])
        if name in seen:
            continue
        seen.add(name)
        unique.append(item)
    return unique


def portfolio_specs(config: dict[str, Any]) -> list[PortfolioSpec]:
    raw_methods = cfg_get(config, f"{CONFIG_KEY}.weight_methods", ["equal_weight", "score_weight"])
    methods = [str(value) for value in raw_methods] if isinstance(raw_methods, list) else ["equal_weight", "score_weight"]
    raw_specs = cfg_get(
        config,
        f"{CONFIG_KEY}.portfolio_specs",
        [
            {"portfolio_name": "top_decile", "long_quantile": 0.10, "short_quantile": 0.0},
            {"portfolio_name": "top_quintile", "long_quantile": 0.20, "short_quantile": 0.0},
            {"portfolio_name": "long_short_decile", "long_quantile": 0.10, "short_quantile": 0.10},
            {"portfolio_name": "long_short_quintile", "long_quantile": 0.20, "short_quantile": 0.20},
        ],
    )
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
    total = 0.0
    for ticker, weight in weights.items():
        beta = as_float(by_ticker.get(ticker, {}).get("beta_to_benchmark"))
        total += weight * (beta if beta is not None else 1.0)
    return total


def normalize_gross(weights: dict[str, float], target_gross: float = 1.0) -> dict[str, float]:
    gross = sum(abs(weight) for weight in weights.values())
    if gross <= 0:
        return weights
    scale = target_gross / gross
    return {ticker: weight * scale for ticker, weight in weights.items()}


def build_weights(scored_rows: list[dict[str, Any]], spec: PortfolioSpec, min_positions: int) -> tuple[dict[str, float], float]:
    ordered = sorted(scored_rows, key=lambda row: (-float(row["score"]), str(row["ticker"])))
    n = len(ordered)
    long_n = min(n, max(min_positions, int(math.ceil(n * spec.long_quantile))))
    long_rows = ordered[:long_n]
    weights = weighted_leg(long_rows, side="long", method=spec.weight_method)
    if spec.short_quantile > 0:
        short_n = min(n - long_n, max(min_positions, int(math.ceil(n * spec.short_quantile))))
        short_rows = list(reversed(ordered[-short_n:])) if short_n > 0 else []
        short_weights = weighted_leg(short_rows, side="short", method=spec.weight_method)
        for ticker, weight in short_weights.items():
            weights[ticker] = weights.get(ticker, 0.0) - weight
    by_ticker = {str(row["ticker"]): row for row in scored_rows}
    hedge_weight = 0.0
    if spec.short_quantile > 0:
        if spec.exposure_mode == "beta_neutral":
            long_beta = sum(weight * (as_float(by_ticker.get(ticker, {}).get("beta_to_benchmark")) or 1.0) for ticker, weight in weights.items() if weight > 0)
            short_beta = -sum(weight * (as_float(by_ticker.get(ticker, {}).get("beta_to_benchmark")) or 1.0) for ticker, weight in weights.items() if weight < 0)
            if short_beta > 0:
                scale = long_beta / short_beta
                weights = {ticker: (weight * scale if weight < 0 else weight) for ticker, weight in weights.items()}
        weights = normalize_gross(weights, target_gross=1.0)
    elif spec.exposure_mode == "hedged_long":
        hedge_weight = -beta_exposure(weights, by_ticker)
    return weights, hedge_weight


def turnover(prev: dict[str, float] | None, current: dict[str, float]) -> float:
    if prev is None:
        return sum(abs(value) for value in current.values())
    tickers = set(prev) | set(current)
    return 0.5 * sum(abs(current.get(ticker, 0.0) - prev.get(ticker, 0.0)) for ticker in tickers)


def max_cohort_share(weights: dict[str, float], cohort_by_ticker: dict[str, str]) -> float:
    gross = sum(abs(weight) for weight in weights.values())
    if gross <= 0:
        return 0.0
    by_cohort: dict[str, float] = {}
    for ticker, weight in weights.items():
        cohort = cohort_by_ticker.get(ticker) or "unknown"
        by_cohort[cohort] = by_cohort.get(cohort, 0.0) + abs(weight)
    return max(by_cohort.values()) / gross if by_cohort else 0.0


def drawdown(values: list[float]) -> tuple[float, list[float]]:
    equity = 1.0
    peak = 1.0
    max_dd = 0.0
    curve: list[float] = []
    for value in values:
        equity *= 1.0 + value
        curve.append(equity)
        peak = max(peak, equity)
        if peak > 0:
            max_dd = min(max_dd, equity / peak - 1.0)
    return max_dd, curve


def summarize_returns(rows: list[dict[str, Any]], horizon: int, *, expected_periods: int = 0) -> dict[str, Any]:
    returns = [float(row["net_return"]) for row in rows]
    excess = [float(row["excess_return"]) for row in rows if row.get("excess_return") not in ("", None)]
    if not returns:
        return {}
    periods_per_year = 252.0 / max(1, horizon)
    total = math.prod(1.0 + value for value in returns) - 1.0
    mean_return = sum(returns) / len(returns)
    vol = statistics.stdev(returns) * math.sqrt(periods_per_year) if len(returns) > 2 else 0.0
    annualized = (1.0 + total) ** (periods_per_year / len(returns)) - 1.0 if total > -1.0 else -1.0
    max_dd, _curve = drawdown(returns)
    return {
        "periods": len(returns),
        # Skipped panel dates (thin cross-sections, missing weights) are
        # invisible to the annualization above, which compounds only realized
        # periods. Surface the gap so readers can judge the coverage instead
        # of changing the return math.
        "periods_skipped": max(0, expected_periods - len(returns)) if expected_periods else 0,
        "coverage_fraction": len(returns) / expected_periods if expected_periods else "",
        "total_return": total,
        "annualized_return": annualized,
        "annualized_vol": vol,
        "sharpe": (mean_return * periods_per_year / vol) if vol > 0 else "",
        "hit_rate": sum(1 for value in returns if value > 0) / len(returns),
        "avg_period_return": mean_return,
        "median_period_return": statistics.median(returns),
        "max_drawdown": max_dd,
        "avg_excess_return": sum(excess) / len(excess) if excess else "",
        "avg_excess_return_vs_smh": sum(float(row["excess_return_vs_smh"]) for row in rows) / len(rows),
        "avg_excess_return_vs_equal_weight": sum(float(row["excess_return_vs_equal_weight"]) for row in rows) / len(rows),
        "avg_turnover": sum(float(row["turnover"]) for row in rows) / len(rows),
        "avg_stock_turnover": sum(float(row.get("stock_turnover") or 0.0) for row in rows) / len(rows),
        "avg_hedge_turnover": sum(float(row.get("hedge_turnover") or 0.0) for row in rows) / len(rows),
        "avg_transaction_cost": sum(float(row["transaction_cost"]) for row in rows) / len(rows),
        "avg_stock_transaction_cost": sum(float(row.get("stock_transaction_cost") or 0.0) for row in rows) / len(rows),
        "avg_hedge_transaction_cost": sum(float(row.get("hedge_transaction_cost") or 0.0) for row in rows) / len(rows),
        "avg_borrow_cost": sum(float(row["borrow_cost"]) for row in rows) / len(rows),
        "avg_total_cost": sum(float(row["total_cost"]) for row in rows) / len(rows),
        "avg_positions": sum(int(row["position_count"]) for row in rows) / len(rows),
        "avg_gross_exposure": sum(float(row["gross_exposure"]) for row in rows) / len(rows),
        "avg_beta_exposure": sum(float(row["beta_exposure"]) for row in rows) / len(rows),
        "avg_max_cohort_share": sum(float(row["max_cohort_share"]) for row in rows) / len(rows),
    }


def as_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def annual_borrow_rate(value: Any, default_rate: float) -> float:
    rate = as_float(value)
    if rate is None:
        return default_rate
    # IBKR and upstream feeds may store either decimal rates or percent values.
    return rate / 100.0 if abs(rate) > 1.0 else rate


def borrow_cost_for_period(
    weights: dict[str, float],
    by_ticker: dict[str, dict[str, Any]],
    *,
    horizon_days: int,
    default_annual_rate: float,
) -> float:
    cost = 0.0
    for ticker, weight in weights.items():
        if weight >= 0:
            continue
        rate = annual_borrow_rate(by_ticker.get(ticker, {}).get("latest_borrow_fee_rate"), default_annual_rate)
        cost += abs(weight) * max(0.0, rate) * horizon_days / 252.0
    return cost


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
            if row.get(f"fwd_{horizon}") is None:
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
        smh_benchmark_return = as_float(scored[0].get(f"bench_{horizon}")) or 0.0
        equal_weight_benchmark_return = sum(float(row[f"fwd_{horizon}"]) for row in scored if row.get(f"fwd_{horizon}") is not None) / len(scored)
        stock_return = sum(weight * float(by_ticker[ticker][f"fwd_{horizon}"]) for ticker, weight in weights.items() if ticker in by_ticker)
        hedge_return = hedge_weight * smh_benchmark_return
        raw_return = stock_return + hedge_return
        effective_weights = dict(weights)
        if hedge_weight:
            effective_weights["__SMH_HEDGE__"] = hedge_weight
        gross_exposure = sum(abs(weight) for weight in effective_weights.values())
        beta_exp = beta_exposure(weights, by_ticker) + hedge_weight
        stock_turnover = turnover(previous_stock_weights, weights)
        hedge_turnover = abs(hedge_weight) if previous_hedge_weight is None else abs(hedge_weight - previous_hedge_weight)
        period_turnover = stock_turnover + hedge_turnover
        stock_transaction_cost = stock_turnover * transaction_cost_bps / 10000.0
        hedge_transaction_cost = hedge_turnover * transaction_cost_bps / 10000.0
        transaction_cost = stock_transaction_cost + hedge_transaction_cost
        borrow_cost = borrow_cost_for_period(
            weights,
            by_ticker,
            horizon_days=horizon,
            default_annual_rate=borrow_cost_default_annual_rate,
        )
        total_cost = transaction_cost + borrow_cost
        net_return = raw_return - total_cost
        cohort_by_ticker = {str(row["ticker"]): str(row.get("cohort") or "") for row in scored}
        max_share = max_cohort_share(weights, cohort_by_ticker)
        long_count = sum(1 for value in weights.values() if value > 0)
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
                "net_return": net_return,
                "benchmark_return": smh_benchmark_return,
                "smh_benchmark_return": smh_benchmark_return,
                "equal_weight_benchmark_return": equal_weight_benchmark_return,
                "excess_return": net_return - equal_weight_benchmark_return if short_count == 0 else net_return,
                "excess_return_vs_smh": net_return - smh_benchmark_return,
                "excess_return_vs_equal_weight": net_return - equal_weight_benchmark_return,
                "turnover": period_turnover,
                "stock_turnover": stock_turnover,
                "hedge_turnover": hedge_turnover,
                "gross_exposure": gross_exposure,
                "beta_exposure": beta_exp,
                "hedge_weight": hedge_weight,
                "position_count": len(weights),
                "long_count": long_count,
                "short_count": short_count,
                "max_cohort_share": max_share,
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
                    "fwd_return": source.get(f"fwd_{horizon}"),
                    "residual_return": source.get(f"resid_{horizon}"),
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
                    "ticker": "__SMH_HEDGE__",
                    "weight": hedge_weight,
                    "score": "",
                    "score_quality": "",
                    "cohort": "benchmark_hedge",
                    "beta_to_benchmark": 1.0,
                    "borrow_fee_rate": "",
                    "fwd_return": smh_benchmark_return,
                    "residual_return": "",
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
        cfg_get(config, f"{CONFIG_KEY}.output_dir", "../output/technology_reports/backtests"),
        base_dir=base_dir,
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    panel, panel_dates, horizons = build_panel(config, db_path)
    if not panel_dates:
        raise RuntimeError("No PIT panel dates available for portfolio simulation.")
    neutral_score = float(cfg_get(config, "semiconductor_scoring_features.neutral_score", 50.0))
    min_cross_section = int(cfg_get(config, f"{CONFIG_KEY}.min_cross_section", 30))
    min_positions = int(cfg_get(config, f"{CONFIG_KEY}.min_positions", 5))
    transaction_cost_bps = float(cfg_get(config, f"{CONFIG_KEY}.transaction_cost_bps", 20.0))
    borrow_cost_default_annual_rate = float(cfg_get(config, f"{CONFIG_KEY}.borrow_cost_default_annual_rate", 0.0))
    candidates = load_candidates(config, base_dir)
    specs = portfolio_specs(config)
    if not specs:
        raise RuntimeError("No portfolio specs configured.")

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
            metrics = summarize_returns(period_rows, int(horizons[0]), expected_periods=len(panel_dates))
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

    summary_rows.sort(
        key=lambda row: (
            str(row["portfolio_name"]),
            str(row["weight_method"]),
            str(row["exposure_mode"]),
            -float(row["annualized_return"]),
        )
    )
    write_csv(output_dir / "semiconductor_portfolio_backtest_summary.csv", summary_rows)
    write_csv(output_dir / "semiconductor_portfolio_backtest_periods.csv", all_periods)
    write_csv(output_dir / "semiconductor_portfolio_backtest_holdings.csv", all_holdings)
    model_family = str(cfg_get(config, "technology_universe.initial_subsector", "semiconductors"))
    manifest = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "database_path": str(db_path),
        "panel_rows": len(panel),
        "panel_dates": len(panel_dates),
        "date_range": [panel_dates[0].isoformat(), panel_dates[-1].isoformat()],
        # Report-only simulation: the panel deliberately runs through the
        # latest bar, so it mixes the calibration training window with
        # post-lock data and must not be read as out-of-sample evidence.
        "sample_basis": "in_sample_training_window_plus_post_lock",
        "calibration_train_end_date": str(cfg_get(config, f"oos_calibration_standards.families.{model_family}.calibration_train_end_date", "") or ""),
        "calibration_lock_date": str(cfg_get(config, f"oos_calibration_standards.families.{model_family}.calibration_lock_date", "") or ""),
        "horizon_days": int(horizons[0]),
        "models": [item["model_name"] for item in candidates],
        "portfolio_specs": [spec.__dict__ for spec in specs],
        "transaction_cost_bps": transaction_cost_bps,
        "borrow_cost_default_annual_rate": borrow_cost_default_annual_rate,
        "return_normalization": {
            "long_short": "stock weights are gross-normalized to 1.0 for dollar-neutral and beta-neutral exposure modes",
            "hedged_long": "long basket return minus trailing-beta-scaled SMH return; hedge leg has explicit turnover/cost columns and is included in total turnover, transaction cost, and gross exposure",
        },
        "outputs": {
            "summary_csv": str(output_dir / "semiconductor_portfolio_backtest_summary.csv"),
            "periods_csv": str(output_dir / "semiconductor_portfolio_backtest_periods.csv"),
            "holdings_csv": str(output_dir / "semiconductor_portfolio_backtest_holdings.csv"),
        },
    }
    (output_dir / "semiconductor_portfolio_backtest_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    LOGGER.info("Stage 9 portfolio backtest complete: models=%d portfolios=%d output=%s", len(candidates), len(specs), output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
