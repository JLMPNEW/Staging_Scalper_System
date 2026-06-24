#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from macro_raw_config import (  # noqa: E402
    cfg_get,
    configure_pipeline_logging,
    connect_sqlite,
    load_macro_raw_config,
    parse_boolish,
    parse_iso_date,
    resolve_path,
)
from macro_serving_common import resolve_serving_db_path  # noqa: E402

logger = logging.getLogger(__name__)

_BACKTEST_IMPORT_ERROR: Exception | None = None
_BACKTEST: dict[str, Any] = {}
try:
    from BackTest.config_loader import load_config as _load_backtest_config
    from BackTest.prices import load_prices as _load_prices

    _BACKTEST.update({"load_config": _load_backtest_config, "load_prices": _load_prices})
except ModuleNotFoundError as exc:
    _BACKTEST_IMPORT_ERROR = exc

_TIER1_IMPORT_ERROR: Exception | None = None
_TIER1: dict[str, Any] = {}
try:
    from tier1_common import _get_tier1_cfg as _tier1_get_cfg
    from tier1_portfolio_optimizer import load_yaml as _tier1_load_yaml

    _TIER1.update({"get_cfg": _tier1_get_cfg, "load_yaml": _tier1_load_yaml})
except ModuleNotFoundError as exc:
    _TIER1_IMPORT_ERROR = exc


def _require_backtest(name: str) -> Any:
    if name in _BACKTEST:
        return _BACKTEST[name]
    raise RuntimeError(
        "The copied MacroLayer no longer loads BackTest at import time. "
        "Shadow backtests require a Staging-owned price adapter before use."
    ) from _BACKTEST_IMPORT_ERROR


def _require_tier1(name: str) -> Any:
    if name in _TIER1:
        return _TIER1[name]
    raise RuntimeError(
        "The copied MacroLayer no longer loads tier1 optimizer modules at import time. "
        "Use the portfolio-layer optimizer adapter for Stage 7 integration."
    ) from _TIER1_IMPORT_ERROR


@dataclass(frozen=True)
class ShadowBacktestConfig:
    output_dir: Path
    base_config_path: Path
    backtest_config_path: Path
    price_cache_path: Path
    benchmark_ticker: str
    start_date: date | None
    end_date: date | None
    holding_period_trading_days: int
    cash_weight: float
    max_us_stocks: int
    enforce_rating_quotas: bool
    require_base_optimizer_eligible: bool
    exclude_earnings_blocked: bool
    macro_required_state: str
    stock_weight_method: str
    use_stage12c_foreign_weights: bool
    max_foreign_weight: float
    cases: list[str]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run fast Stage 12 shadow validation without full optimizer solves.")
    parser.add_argument("--config", type=Path, default=Path("MacroLayer/config_macro_raw.yaml"))
    parser.add_argument("--serving-db-path", type=Path, default=None)
    parser.add_argument("--start-date", type=str, default=None)
    parser.add_argument("--end-date", type=str, default=None)
    parser.add_argument("--case", action="append", default=None, help="Case to run. Repeatable.")
    return parser.parse_args()


def _load_yaml(path: Path) -> dict[str, Any]:
    load_yaml = _require_tier1("load_yaml")
    return load_yaml(str(path))


def _resolve_layer_config(cfg: dict[str, Any], config_path: Path, args: argparse.Namespace) -> ShadowBacktestConfig:
    raw_cfg = dict(cfg_get(cfg, "shadow_validation_layer", default={}) or {})
    output_dir = resolve_path(config_path, str(raw_cfg.get("output_dir", "MacroLayer/out/shadow_backtest")))
    if output_dir is None:
        raise ValueError("shadow_validation_layer.output_dir could not be resolved.")
    base_config_path = resolve_path(config_path, str(raw_cfg.get("base_config_path", "config.yaml")))
    if base_config_path is None:
        raise ValueError("shadow_validation_layer.base_config_path could not be resolved.")
    backtest_config_path = resolve_path(
        config_path,
        str(raw_cfg.get("backtest_config_path", cfg_get(cfg, "industry_macro_layer", "backtest_config_path", default="BackTest/config_backtest.yaml"))),
    )
    if backtest_config_path is None:
        raise ValueError("shadow_validation_layer.backtest_config_path could not be resolved.")

    load_backtest_config = _require_backtest("load_config")
    backtest_cfg = load_backtest_config(backtest_config_path)
    price_cache_raw = raw_cfg.get("price_cache_path", None) or cfg_get(backtest_cfg, "prices", "cache_path", default=None)
    if price_cache_raw is None or str(price_cache_raw).strip() == "":
        raise ValueError("shadow_validation_layer.price_cache_path or BackTest prices.cache_path is required.")
    price_cache_path = Path(str(price_cache_raw)).expanduser()
    if not price_cache_path.is_absolute():
        price_cache_path = (REPO_ROOT / price_cache_path).resolve()

    get_tier1_cfg = _require_tier1("get_cfg")
    base_cfg = get_tier1_cfg(_load_yaml(base_config_path))
    universe_cfg = dict(base_cfg.get("universe", {}) or {})
    allocation_cfg = dict(base_cfg.get("allocation", {}) or {})
    cash_budget = dict(dict(allocation_cfg.get("region_budgets", {}) or {}).get("CASH", {}) or {})
    cash_weight = float(raw_cfg.get("cash_weight", cash_budget.get("max", 0.20)))
    cash_weight = min(1.0, max(0.0, cash_weight))
    max_us_stocks = int(raw_cfg.get("max_us_stocks", universe_cfg.get("max_us_stocks_long_only", 25)))

    cases = [str(x).strip() for x in list(raw_cfg.get("cases", ["baseline_no_macro", "macro_stocks_only", "macro_full"]) or []) if str(x).strip()]
    if args.case:
        selected = {str(x).strip() for x in args.case if str(x).strip()}
        cases = [case for case in cases if case in selected]
    if not cases:
        raise ValueError("No shadow-validation cases selected.")

    start_date = parse_iso_date(args.start_date) or parse_iso_date(raw_cfg.get("start_date"))
    end_date = parse_iso_date(args.end_date) or parse_iso_date(raw_cfg.get("end_date"))
    selection_cfg = dict(raw_cfg.get("selection", {}) or {})
    foreign_cfg = dict(raw_cfg.get("foreign", {}) or {})

    stock_weight_method = str(selection_cfg.get("stock_weight_method", "equal")).strip().lower() or "equal"
    if stock_weight_method not in {"equal", "score_softmax"}:
        raise ValueError("shadow_validation_layer.selection.stock_weight_method must be equal or score_softmax.")

    return ShadowBacktestConfig(
        output_dir=output_dir,
        base_config_path=base_config_path,
        backtest_config_path=backtest_config_path,
        price_cache_path=price_cache_path,
        benchmark_ticker=str(raw_cfg.get("benchmark_ticker", cfg_get(backtest_cfg, "prices", "benchmark_ticker", default="SPY"))).upper().strip(),
        start_date=start_date,
        end_date=end_date,
        holding_period_trading_days=max(1, int(raw_cfg.get("holding_period_trading_days", 5))),
        cash_weight=cash_weight,
        max_us_stocks=max(1, max_us_stocks),
        enforce_rating_quotas=parse_boolish(selection_cfg.get("enforce_rating_quotas"), default=True),
        require_base_optimizer_eligible=parse_boolish(selection_cfg.get("require_base_optimizer_eligible"), default=True),
        exclude_earnings_blocked=parse_boolish(selection_cfg.get("exclude_earnings_blocked"), default=True),
        macro_required_state=str(selection_cfg.get("macro_required_state", "Eligible")).strip() or "Eligible",
        stock_weight_method=stock_weight_method,
        use_stage12c_foreign_weights=parse_boolish(foreign_cfg.get("use_stage12c_candidate_weights"), default=True),
        max_foreign_weight=max(0.0, float(foreign_cfg.get("max_foreign_weight", 0.20))),
        cases=cases,
    )


def _rating_quotas(base_config_path: Path) -> dict[str, int]:
    get_tier1_cfg = _require_tier1("get_cfg")
    cfg = get_tier1_cfg(_load_yaml(base_config_path))
    universe = dict(cfg.get("universe", {}) or {})
    raw = dict(universe.get("per_rating_quota_long_only", {}) or {})
    return {str(k).strip(): max(0, int(v)) for k, v in raw.items() if str(k).strip() and int(v) > 0}


def _load_stock_inputs(conn: sqlite3.Connection, layer_cfg: ShadowBacktestConfig) -> pd.DataFrame:
    where = []
    params: list[Any] = []
    if layer_cfg.start_date is not None:
        where.append("as_of_date >= ?")
        params.append(layer_cfg.start_date.isoformat())
    if layer_cfg.end_date is not None:
        where.append("as_of_date <= ?")
        params.append(layer_cfg.end_date.isoformat())
    where_sql = " AND " + " AND ".join(where) if where else ""
    frame = pd.read_sql_query(
        f"""
        SELECT
            as_of_date,
            ticker,
            rating,
            state,
            base_final_score,
            final_score,
            selection_score,
            weight_score,
            base_optimizer_eligible,
            earnings_blocked_7d
        FROM portfolio_inputs_daily
        WHERE asset_type = 'US_STOCK'
        {where_sql}
        """,
        conn,
        params=params,
        parse_dates=["as_of_date"],
    )
    if frame.empty:
        raise ValueError("No US_STOCK rows found in portfolio_inputs_daily for the requested shadow backtest range.")
    for col in ("ticker", "rating", "state"):
        frame[col] = frame[col].fillna("").astype(str).str.strip()
    frame["ticker"] = frame["ticker"].str.upper()
    return frame.dropna(subset=["as_of_date", "ticker"]).reset_index(drop=True)


def _load_foreign_candidates(conn: sqlite3.Connection, layer_cfg: ShadowBacktestConfig) -> pd.DataFrame:
    where = []
    params: list[Any] = []
    if layer_cfg.start_date is not None:
        where.append("as_of_date >= ?")
        params.append(layer_cfg.start_date.isoformat())
    if layer_cfg.end_date is not None:
        where.append("as_of_date <= ?")
        params.append(layer_cfg.end_date.isoformat())
    where_sql = " AND " + " AND ".join(where) if where else ""
    try:
        frame = pd.read_sql_query(
            f"""
            SELECT
                as_of_date,
                ticker,
                foreign_fused_alpha,
                sleeve_weight,
                portfolio_weight_at_budget,
                selected_flag,
                active_flag,
                coverage_flag
            FROM foreign_sleeve_candidate_daily
            WHERE selected_flag = 1
              AND active_flag = 1
              AND coverage_flag = 1
            {where_sql}
            """,
            conn,
            params=params,
            parse_dates=["as_of_date"],
        )
    except Exception:
        return pd.DataFrame()
    if frame.empty:
        return frame
    frame["ticker"] = frame["ticker"].fillna("").astype(str).str.upper().str.strip()
    return frame.dropna(subset=["as_of_date", "ticker"]).reset_index(drop=True)


def _eligible_stock_frame(day: pd.DataFrame, *, case_name: str, layer_cfg: ShadowBacktestConfig) -> tuple[pd.DataFrame, str]:
    out = day.copy()
    if layer_cfg.require_base_optimizer_eligible and "base_optimizer_eligible" in out.columns:
        out = out.loc[pd.to_numeric(out["base_optimizer_eligible"], errors="coerce").fillna(0).astype(int).eq(1)].copy()
    if layer_cfg.exclude_earnings_blocked and "earnings_blocked_7d" in out.columns:
        out = out.loc[pd.to_numeric(out["earnings_blocked_7d"], errors="coerce").fillna(0).astype(int).eq(0)].copy()
    if case_name == "baseline_no_macro":
        score_col = "base_final_score"
    else:
        out = out.loc[out["state"].astype(str).str.strip().eq(layer_cfg.macro_required_state)].copy()
        score_col = "selection_score"
    out[score_col] = pd.to_numeric(out[score_col], errors="coerce")
    out = out.dropna(subset=[score_col])
    return out, score_col


def _select_with_quotas(day: pd.DataFrame, *, score_col: str, quotas: dict[str, int], top_n: int) -> pd.DataFrame:
    selected_parts: list[pd.DataFrame] = []
    selected_idx: set[int] = set()
    for rating, quota in quotas.items():
        if quota <= 0:
            continue
        part = day.loc[day["rating"].eq(rating) & ~day.index.isin(selected_idx)].sort_values(score_col, ascending=False).head(quota)
        selected_parts.append(part)
        selected_idx.update(part.index.tolist())
    selected = pd.concat(selected_parts, axis=0) if selected_parts else pd.DataFrame(columns=day.columns)
    if len(selected) < top_n:
        fill = day.loc[~day.index.isin(selected_idx)].sort_values(score_col, ascending=False).head(top_n - len(selected))
        selected = pd.concat([selected, fill], axis=0)
    return selected.sort_values(score_col, ascending=False).head(top_n).copy()


def _stock_weights(selected: pd.DataFrame, *, score_col: str, sleeve_weight: float, method: str) -> pd.Series:
    if selected.empty or sleeve_weight <= 0.0:
        return pd.Series(dtype="float64")
    if method == "score_softmax":
        values = pd.to_numeric(selected[score_col], errors="coerce").fillna(0.0)
        values = values - float(values.max())
        raw = np.exp(values.to_numpy(dtype=float))
        if not np.isfinite(raw).all() or raw.sum() <= 0.0:
            raw = np.ones(len(selected), dtype=float)
        weights = raw / raw.sum() * sleeve_weight
    else:
        weights = np.repeat(sleeve_weight / len(selected), len(selected))
    return pd.Series(weights, index=selected["ticker"].astype(str).str.upper().to_numpy(), dtype="float64")


def _build_case_holdings(
    stocks: pd.DataFrame,
    foreign: pd.DataFrame,
    *,
    case_name: str,
    layer_cfg: ShadowBacktestConfig,
    quotas: dict[str, int],
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for as_of_date, day in stocks.groupby("as_of_date", sort=True):
        eligible, score_col = _eligible_stock_frame(day, case_name=case_name, layer_cfg=layer_cfg)
        if eligible.empty:
            continue
        selected = (
            _select_with_quotas(eligible, score_col=score_col, quotas=quotas, top_n=layer_cfg.max_us_stocks)
            if layer_cfg.enforce_rating_quotas
            else eligible.sort_values(score_col, ascending=False).head(layer_cfg.max_us_stocks).copy()
        )

        foreign_weights = pd.Series(dtype="float64")
        if case_name == "macro_full" and layer_cfg.use_stage12c_foreign_weights and not foreign.empty:
            fday = foreign.loc[pd.to_datetime(foreign["as_of_date"]).dt.normalize().eq(pd.Timestamp(as_of_date).normalize())].copy()
            if not fday.empty:
                fw = pd.to_numeric(fday["portfolio_weight_at_budget"], errors="coerce").fillna(0.0).clip(lower=0.0)
                total_fw = min(float(fw.sum()), layer_cfg.max_foreign_weight)
                if total_fw > 0.0 and float(fw.sum()) > 0.0:
                    foreign_weights = pd.Series(
                        (fw / float(fw.sum()) * total_fw).to_numpy(dtype=float),
                        index=fday["ticker"].astype(str).str.upper().to_numpy(),
                        dtype="float64",
                    )
        stock_sleeve = max(0.0, 1.0 - layer_cfg.cash_weight - float(foreign_weights.sum()))
        stock_weights = _stock_weights(selected, score_col=score_col, sleeve_weight=stock_sleeve, method=layer_cfg.stock_weight_method)
        combined = pd.concat([stock_weights, foreign_weights]).groupby(level=0).sum()
        for ticker, weight in combined.items():
            if weight <= 0.0:
                continue
            rows.append(
                {
                    "case_name": case_name,
                    "as_of_date": pd.Timestamp(as_of_date).strftime("%Y-%m-%d"),
                    "ticker": str(ticker).upper(),
                    "weight": float(weight),
                    "asset_bucket": "FOREIGN" if ticker in set(foreign_weights.index) else "US",
                }
            )
        rows.append(
            {
                "case_name": case_name,
                "as_of_date": pd.Timestamp(as_of_date).strftime("%Y-%m-%d"),
                "ticker": "CASH",
                "weight": float(layer_cfg.cash_weight),
                "asset_bucket": "CASH",
            }
        )
    return pd.DataFrame(rows)


def _load_price_panel(holdings: pd.DataFrame, layer_cfg: ShadowBacktestConfig, base_cfg_path: Path) -> pd.DataFrame:
    tickers = sorted(set(holdings.loc[holdings["ticker"].ne("CASH"), "ticker"].astype(str).str.upper()))
    if layer_cfg.benchmark_ticker:
        tickers.append(layer_cfg.benchmark_ticker)
    tickers = sorted(set(tickers))
    dates = pd.to_datetime(holdings["as_of_date"], errors="coerce").dropna()
    start = dates.min() - pd.Timedelta(days=10)
    end = dates.max() + pd.Timedelta(days=max(30, layer_cfg.holding_period_trading_days * 3))
    logger.info("Loading cached prices for %d tickers from %s to %s", len(tickers), start.date(), end.date())
    load_prices = _require_backtest("load_prices")
    return load_prices(
        cache_path=layer_cfg.price_cache_path,
        tickers=tickers,
        start_date=start,
        end_date=end,
    )


def _trade_date_map(signal_dates: pd.Series, price_index: pd.DatetimeIndex, holding_period_days: int) -> pd.DataFrame:
    unique_dates = pd.DatetimeIndex(pd.to_datetime(signal_dates, errors="coerce").dropna().unique()).sort_values()
    base_pos = np.searchsorted(price_index.to_numpy(dtype="datetime64[ns]"), unique_dates.to_numpy(dtype="datetime64[ns]"), side="right")
    entry_pos = base_pos
    exit_pos = entry_pos + int(holding_period_days)
    valid = exit_pos < len(price_index)
    out = pd.DataFrame({"as_of_date": unique_dates, "entry_pos": entry_pos, "exit_pos": exit_pos, "valid": valid})
    out = out.loc[out["valid"]].copy()
    out["entry_date"] = pd.DatetimeIndex(price_index[out["entry_pos"].to_numpy(dtype="int64")]).normalize()
    out["exit_date"] = pd.DatetimeIndex(price_index[out["exit_pos"].to_numpy(dtype="int64")]).normalize()
    return out[["as_of_date", "entry_date", "exit_date"]].reset_index(drop=True)


def _cash_period_return(base_config_path: Path, holding_days: int) -> float:
    get_tier1_cfg = _require_tier1("get_cfg")
    cfg = get_tier1_cfg(_load_yaml(base_config_path))
    cash_ann = float(dict(cfg.get("cash", {}) or {}).get("annual_yield", 0.0))
    return (1.0 + cash_ann) ** (float(holding_days) / 252.0) - 1.0


def _compute_period_returns(holdings: pd.DataFrame, prices: pd.DataFrame, layer_cfg: ShadowBacktestConfig) -> pd.DataFrame:
    price_index = pd.DatetimeIndex(prices.index).sort_values()
    date_map = _trade_date_map(holdings["as_of_date"], price_index, layer_cfg.holding_period_trading_days)
    if date_map.empty:
        raise ValueError("No valid trade windows found in price history.")
    h = holdings.copy()
    h["as_of_date"] = pd.to_datetime(h["as_of_date"], errors="coerce").dt.normalize()
    h = h.merge(date_map, on="as_of_date", how="inner")
    cash_ret = _cash_period_return(layer_cfg.base_config_path, layer_cfg.holding_period_trading_days)
    rows: list[dict[str, Any]] = []
    for (case_name, as_of_date), sub in h.groupby(["case_name", "as_of_date"], sort=True):
        entry_date = pd.Timestamp(sub["entry_date"].iloc[0])
        exit_date = pd.Timestamp(sub["exit_date"].iloc[0])
        cash_weight = float(pd.to_numeric(sub.loc[sub["ticker"].eq("CASH"), "weight"], errors="coerce").fillna(0.0).sum())
        risky = sub.loc[sub["ticker"].ne("CASH")].copy()
        risky["weight"] = pd.to_numeric(risky["weight"], errors="coerce").fillna(0.0)
        available_weight = 0.0
        risky_return = 0.0
        missing: list[str] = []
        for row in risky.itertuples(index=False):
            ticker = str(row.ticker).upper()
            weight = float(row.weight)
            if ticker not in prices.columns:
                missing.append(ticker)
                continue
            entry = pd.to_numeric(pd.Series([prices.at[entry_date, ticker] if entry_date in prices.index else np.nan]), errors="coerce").iloc[0]
            exit_ = pd.to_numeric(pd.Series([prices.at[exit_date, ticker] if exit_date in prices.index else np.nan]), errors="coerce").iloc[0]
            if pd.isna(entry) or pd.isna(exit_) or float(entry) <= 0.0:
                missing.append(ticker)
                continue
            available_weight += weight
            risky_return += weight * (float(exit_) / float(entry) - 1.0)
        # Keep unavailable risky weight as cash-equivalent for conservative handling.
        unavailable_weight = max(0.0, float(risky["weight"].sum()) - available_weight)
        portfolio_return = risky_return + (cash_weight + unavailable_weight) * cash_ret
        bench_ret = np.nan
        bench = layer_cfg.benchmark_ticker
        if bench and bench in prices.columns and entry_date in prices.index and exit_date in prices.index:
            b0 = pd.to_numeric(pd.Series([prices.at[entry_date, bench]]), errors="coerce").iloc[0]
            b1 = pd.to_numeric(pd.Series([prices.at[exit_date, bench]]), errors="coerce").iloc[0]
            if pd.notna(b0) and pd.notna(b1) and float(b0) > 0.0:
                bench_ret = float(b1) / float(b0) - 1.0
        rows.append(
            {
                "case_name": case_name,
                "as_of_date": pd.Timestamp(as_of_date).strftime("%Y-%m-%d"),
                "entry_date": entry_date.strftime("%Y-%m-%d"),
                "exit_date": exit_date.strftime("%Y-%m-%d"),
                "portfolio_return": float(portfolio_return),
                "benchmark_return": bench_ret,
                "benchmark_alpha": float(portfolio_return - bench_ret) if np.isfinite(bench_ret) else np.nan,
                "available_risky_weight": available_weight,
                "missing_risky_weight": unavailable_weight,
                "missing_ticker_count": len(set(missing)),
                "holding_count": int(len(risky)),
            }
        )
    return pd.DataFrame(rows)


def _max_drawdown(returns: pd.Series) -> float:
    equity = (1.0 + pd.to_numeric(returns, errors="coerce").fillna(0.0)).cumprod()
    peak = equity.cummax()
    dd = equity / peak - 1.0
    return float(dd.min()) if not dd.empty else np.nan


def _summary(periods: pd.DataFrame, layer_cfg: ShadowBacktestConfig) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    periods_per_year = 252.0 / float(layer_cfg.holding_period_trading_days)
    for case_name, sub in periods.groupby("case_name", sort=True):
        returns = pd.to_numeric(sub["portfolio_return"], errors="coerce").dropna()
        bench = pd.to_numeric(sub["benchmark_return"], errors="coerce").dropna()
        alpha = pd.to_numeric(sub["benchmark_alpha"], errors="coerce").dropna()
        total_return = float((1.0 + returns).prod() - 1.0) if not returns.empty else np.nan
        ann_return = (1.0 + total_return) ** (periods_per_year / len(returns)) - 1.0 if len(returns) > 0 and total_return > -1.0 else np.nan
        ann_vol = float(returns.std(ddof=1) * np.sqrt(periods_per_year)) if len(returns) > 1 else np.nan
        bench_total = float((1.0 + bench).prod() - 1.0) if not bench.empty else np.nan
        bench_ann = (1.0 + bench_total) ** (periods_per_year / len(bench)) - 1.0 if len(bench) > 0 and bench_total > -1.0 else np.nan
        rows.append(
            {
                "case_name": case_name,
                "period_count": int(len(returns)),
                "start_date": str(sub["as_of_date"].min()),
                "end_date": str(sub["as_of_date"].max()),
                "total_return": total_return,
                "ann_return": ann_return,
                "ann_vol": ann_vol,
                "sharpe_0rf": float(ann_return / ann_vol) if np.isfinite(ann_return) and np.isfinite(ann_vol) and ann_vol > 1e-12 else np.nan,
                "max_drawdown": _max_drawdown(returns),
                "mean_period_return": float(returns.mean()) if not returns.empty else np.nan,
                "median_period_return": float(returns.median()) if not returns.empty else np.nan,
                "hit_rate_positive": float(returns.gt(0.0).mean()) if not returns.empty else np.nan,
                "benchmark_ann_return": bench_ann,
                "ann_alpha_vs_benchmark": float(ann_return - bench_ann) if np.isfinite(ann_return) and np.isfinite(bench_ann) else np.nan,
                "mean_period_alpha": float(alpha.mean()) if not alpha.empty else np.nan,
                "avg_available_risky_weight": float(pd.to_numeric(sub["available_risky_weight"], errors="coerce").mean()),
                "avg_missing_risky_weight": float(pd.to_numeric(sub["missing_risky_weight"], errors="coerce").mean()),
            }
        )
    summary = pd.DataFrame(rows)
    if "baseline_no_macro" in set(summary["case_name"]):
        base = summary.loc[summary["case_name"].eq("baseline_no_macro")].iloc[0]
        for col in ("total_return", "ann_return", "ann_vol", "sharpe_0rf", "max_drawdown", "ann_alpha_vs_benchmark"):
            summary[f"delta_{col}_vs_baseline"] = pd.to_numeric(summary[col], errors="coerce") - float(base[col])
    return summary


def _window_summary(periods: pd.DataFrame, layer_cfg: ShadowBacktestConfig) -> pd.DataFrame:
    windows = {
        "2020_COVID": ("2020-01-01", "2020-12-31"),
        "2022_INFLATION_SHOCK": ("2022-01-01", "2022-12-31"),
        "2023_2025_DISINFLATION": ("2023-01-01", "2025-12-31"),
        "LATEST_12M": ((pd.Timestamp.today().normalize() - pd.DateOffset(months=12)).strftime("%Y-%m-%d"), "2099-12-31"),
    }
    rows: list[pd.DataFrame] = []
    dated = periods.copy()
    dated["as_of_dt"] = pd.to_datetime(dated["as_of_date"], errors="coerce")
    for window_name, (start, end) in windows.items():
        sub = dated.loc[dated["as_of_dt"].between(pd.Timestamp(start), pd.Timestamp(end))].drop(columns=["as_of_dt"])
        if sub.empty:
            continue
        wsum = _summary(sub, layer_cfg)
        wsum.insert(0, "window", window_name)
        rows.append(wsum)
    return pd.concat(rows, ignore_index=True, sort=False) if rows else pd.DataFrame()


def _write_csv(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    frame.to_csv(tmp, index=False)
    tmp.replace(path)


def main() -> None:
    configure_pipeline_logging()
    args = parse_args()
    config_path, cfg = load_macro_raw_config(args.config)
    layer_cfg = _resolve_layer_config(cfg, config_path, args)
    serving_db_path = resolve_serving_db_path(cfg, config_path, override=args.serving_db_path)
    conn = connect_sqlite(serving_db_path, row_factory=sqlite3.Row)
    try:
        stocks = _load_stock_inputs(conn, layer_cfg)
        foreign = _load_foreign_candidates(conn, layer_cfg)
    finally:
        conn.close()

    quotas = _rating_quotas(layer_cfg.base_config_path)
    holdings = pd.concat(
        [
            _build_case_holdings(stocks, foreign, case_name=case_name, layer_cfg=layer_cfg, quotas=quotas)
            for case_name in layer_cfg.cases
        ],
        ignore_index=True,
        sort=False,
    )
    if holdings.empty:
        raise ValueError("Shadow backtest produced no holdings.")

    prices = _load_price_panel(holdings, layer_cfg, layer_cfg.base_config_path)
    periods = _compute_period_returns(holdings, prices, layer_cfg)
    summary = _summary(periods, layer_cfg)
    windows = _window_summary(periods, layer_cfg)
    common_dates: set[str] | None = None
    for _, sub in periods.groupby("case_name"):
        dates = set(sub["as_of_date"].astype(str))
        common_dates = dates if common_dates is None else common_dates.intersection(dates)
    common_periods = periods.loc[periods["as_of_date"].astype(str).isin(common_dates or set())].copy()
    common_summary = _summary(common_periods, layer_cfg) if not common_periods.empty else pd.DataFrame()
    common_windows = _window_summary(common_periods, layer_cfg) if not common_periods.empty else pd.DataFrame()

    out_dir = layer_cfg.output_dir
    _write_csv(out_dir / "shadow_backtest_holdings.csv", holdings)
    _write_csv(out_dir / "shadow_backtest_period_returns.csv", periods)
    _write_csv(out_dir / "shadow_backtest_summary.csv", summary)
    _write_csv(out_dir / "shadow_backtest_window_summary.csv", windows)
    _write_csv(out_dir / "shadow_backtest_common_date_summary.csv", common_summary)
    _write_csv(out_dir / "shadow_backtest_common_date_window_summary.csv", common_windows)
    logger.info("Shadow backtest complete: output_dir=%s periods=%d holdings=%d", out_dir, len(periods), len(holdings))
    print("SHADOW_BACKTEST=PASS")
    print(common_summary.to_string(index=False) if not common_summary.empty else summary.to_string(index=False))


if __name__ == "__main__":
    main()
