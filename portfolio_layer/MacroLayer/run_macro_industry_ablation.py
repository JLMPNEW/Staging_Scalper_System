#!/usr/bin/env python3
"""Historical industry-basket ablation for the Stage 9 macro industry tilt signal.

Measures whether the weekly industry macro fit signal (``industry_macro_fit_daily.final_score``,
surfaced downstream as ``industry_macro_fit``) adds value over a signal-free equal-weight
industry allocation, using ~2019-01..2026-08 weekly history and NO stock-level scores.

Arms (weekly rebalance on the Stage 9 weekly ``as_of_date`` grid):
  * NEUTRAL: equal weight across industries with ``coverage_flag = 1`` that week.
  * TILT:    weights proportional to ``softplus(industry_macro_fit)`` (beta 1.0) over the same
             covered industries, renormalized with a 0.20 per-industry cap via
             ``macro_allocation.bounded_normalize(allow_partial=True)`` (any infeasible residual
             stays in cash at 0 return).
  * GROSS:   TILT weights scaled by the portfolio-layer regime gross scalar
             (``config.yaml`` ``black_litterman_fusion.regime_to_gross_scalar`` applied to
             ``macro_regime_decision_daily.active_current_regime`` as of each signal date);
             the residual sits in cash at 0 return.

Basket membership provenance (documented per the ablation spec):
  Stage 9 (``build_macro_industry_fit.py``) does not persist per-week ticker membership in the
  serving DB, and ``MacroLayer/out/industry_macro`` only carries static prior exports
  (``industry_regime_prior.csv`` / ``industry_shock_prior.csv``), so membership is reconstructed
  from the exact snapshot source Stage 9 is configured with
  (``industry_macro_layer.source_mode = staging_snapshot_store``):
  ``staging_portfolio_adapter.load_staging_score_panel`` (sealed portfolio-layer snapshot-store
  CSVs plus sealed run outputs), restricted to the fit table's ``as_of_date`` grid and joined on
  the (sector, industry_aggregate, industry) triple. Only the classification columns are used --
  stock-level scores never enter this ablation. Reconstructed member counts are cross-checked
  against the persisted ``member_count`` column and the match statistics are recorded in the
  manifest.

PIT rules:
  Signals dated ``as_of_date`` trade at the NEXT trading day's close (first price-panel bar
  strictly after the signal date); the holding period runs to the next week's entry bar. Prices
  come from ``load_staging_prices`` with ``freshness_as_of`` set to the max signal date being
  evaluated (never a future date), so no bar after the signal history's right edge can be used.
  Member tickers without both entry and exit bars are dropped and the basket is renormalized;
  an industry with no priced members that week is excluded from BOTH arms for fairness. Weights
  are built only from same-week signals (no lookahead).

Costs:
  ``shadow_validation_layer.round_trip_cost_bps`` (config_macro_raw.yaml) applied to weekly
  one-way turnover: ``turnover_t = 0.5 * sum(|w_t - w_{t-1}|)`` (all-cash start) and
  ``cost_t = turnover_t * round_trip_cost_bps / 1e4`` -- i.e. each traded side pays half the
  round trip. Cash trades are free.

Outputs:
  ``MacroLayer/out/industry_ablation/<latest as_of>/`` with ``weekly_returns.csv``,
  ``summary.json`` and ``manifest.json`` (sha256 of this script + the config), plus a compact
  summary table on stdout. The TILT-minus-NEUTRAL mean weekly return difference carries a
  moving-block bootstrap CI (block 8 weeks, 1000 resamples, seed 20260804) and per-calendar-year
  deltas.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from macro_allocation import bounded_normalize  # noqa: E402
from macro_raw_config import (  # noqa: E402
    cfg_get,
    configure_pipeline_logging,
    load_macro_raw_config,
    resolve_path,
    utc_now_iso,
)
from macro_serving_common import resolve_serving_db_path  # noqa: E402
from staging_portfolio_adapter import (  # noqa: E402
    latest_accepted_survivorship_panel,
    load_staging_prices,
    load_staging_score_panel,
)

logger = logging.getLogger(__name__)

WEEKS_PER_YEAR = 52.0
TILT_SOFTPLUS_BETA = 1.0
TILT_INDUSTRY_CAP = 0.20
BOOTSTRAP_BLOCK_WEEKS = 8
BOOTSTRAP_RESAMPLES = 1000
BOOTSTRAP_SEED = 20260804
DEFAULT_GROSS_SCALAR = 1.0
OUTPUT_SUBDIR = "MacroLayer/out/industry_ablation"
PORTFOLIO_CONFIG_RELATIVE = "config.yaml"
ARM_NAMES = ("neutral", "tilt", "gross")


# --------------------------------------------------------------------------------------
# Pure, unit-testable building blocks.
# --------------------------------------------------------------------------------------
def softplus(values: pd.Series, *, beta: float = TILT_SOFTPLUS_BETA) -> pd.Series:
    """Numerically stable softplus(beta * x) / beta."""
    if beta <= 0.0:
        raise ValueError(f"softplus beta must be positive; got {beta!r}.")
    arr = pd.to_numeric(values, errors="coerce").astype(float).to_numpy()
    out = np.logaddexp(0.0, beta * arr) / beta
    return pd.Series(out, index=values.index, dtype="float64")


def tilt_weights(
    scores: pd.Series,
    *,
    beta: float = TILT_SOFTPLUS_BETA,
    cap: float = TILT_INDUSTRY_CAP,
) -> pd.Series:
    """Softplus tilt weights over covered industries with a hard per-industry cap.

    Uses ``bounded_normalize(allow_partial=True)`` so an infeasible cap (fewer than
    ``ceil(1/cap)`` industries) yields an explicitly under-invested book (cash residual)
    instead of a broken cap.
    """
    if scores.empty:
        return pd.Series(dtype="float64")
    raw = softplus(scores, beta=beta)
    return bounded_normalize(raw, lower=0.0, upper=float(cap), target_sum=1.0, allow_partial=True)


def neutral_weights(index: pd.Index) -> pd.Series:
    if len(index) == 0:
        return pd.Series(dtype="float64")
    return pd.Series(1.0 / float(len(index)), index=index, dtype="float64")


def turnover_series(weight_frame: pd.DataFrame) -> pd.Series:
    """One-way turnover per rebalance: 0.5 * sum(|w_t - w_{t-1}|) with an all-cash start."""
    filled = weight_frame.astype(float).fillna(0.0)
    prior = filled.shift(1).fillna(0.0)
    return (filled - prior).abs().sum(axis=1) * 0.5


def cost_series(turnover: pd.Series, *, round_trip_cost_bps: float) -> pd.Series:
    """Weekly cost drag: one-way turnover * round-trip bps (each side pays half the round trip)."""
    if round_trip_cost_bps < 0.0:
        raise ValueError(f"round_trip_cost_bps must be non-negative; got {round_trip_cost_bps!r}.")
    return turnover.astype(float) * (float(round_trip_cost_bps) / 10_000.0)


def moving_block_bootstrap_ci(
    diffs: np.ndarray | pd.Series,
    *,
    block_weeks: int = BOOTSTRAP_BLOCK_WEEKS,
    resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
    ci_level: float = 0.95,
) -> dict[str, float]:
    """Moving-block bootstrap CI on the mean of a weekly difference series (deterministic seed)."""
    arr = np.asarray(pd.to_numeric(pd.Series(diffs), errors="coerce"), dtype=float)
    arr = arr[np.isfinite(arr)]
    if arr.size == 0:
        raise ValueError("Bootstrap requires at least one finite weekly difference.")
    if resamples <= 0:
        raise ValueError(f"Bootstrap resamples must be positive; got {resamples!r}.")
    n = int(arr.size)
    block = int(max(1, min(int(block_weeks), n)))
    n_blocks = int(math.ceil(n / block))
    rng = np.random.default_rng(int(seed))
    starts = rng.integers(0, n - block + 1, size=(int(resamples), n_blocks))
    offsets = np.arange(block, dtype=np.int64)
    idx = (starts[:, :, None] + offsets[None, None, :]).reshape(int(resamples), n_blocks * block)[:, :n]
    means = arr[idx].mean(axis=1)
    tail = (1.0 - float(ci_level)) / 2.0
    lo, hi = np.quantile(means, [tail, 1.0 - tail])
    return {
        "mean_weekly_diff": float(arr.mean()),
        "ci_low": float(lo),
        "ci_high": float(hi),
        "ci_level": float(ci_level),
        "prob_diff_positive": float(np.mean(means > 0.0)),
        "block_weeks": int(block),
        "resamples": int(resamples),
        "seed": int(seed),
        "n_weeks": int(n),
    }


def arm_performance(
    returns: pd.Series,
    *,
    turnover: pd.Series,
    costs: pd.Series,
) -> dict[str, float]:
    """Gross and net-of-cost performance statistics for one arm."""
    rets = returns.astype(float)
    cost = costs.astype(float).reindex(rets.index).fillna(0.0)
    net = rets - cost

    def _stats(series: pd.Series, prefix: str) -> dict[str, float]:
        n = int(len(series))
        if n == 0:
            raise ValueError("Cannot compute performance statistics on an empty return series.")
        total = float((1.0 + series).prod() - 1.0)
        years = n / WEEKS_PER_YEAR
        ann_return = float((1.0 + total) ** (1.0 / years) - 1.0) if total > -1.0 else -1.0
        std = float(series.std(ddof=1)) if n > 1 else float("nan")
        ann_vol = float(std * math.sqrt(WEEKS_PER_YEAR)) if np.isfinite(std) else float("nan")
        sharpe = (
            float(series.mean() / std * math.sqrt(WEEKS_PER_YEAR))
            if np.isfinite(std) and std > 0.0
            else float("nan")
        )
        curve = (1.0 + series).cumprod()
        max_drawdown = float((curve / curve.cummax() - 1.0).min())
        hit_rate = float((series > 0.0).mean())
        return {
            f"{prefix}total_return": total,
            f"{prefix}ann_return": ann_return,
            f"{prefix}ann_vol": ann_vol,
            f"{prefix}sharpe": sharpe,
            f"{prefix}max_drawdown": max_drawdown,
            f"{prefix}hit_rate": hit_rate,
        }

    out = _stats(rets, "")
    out.update(_stats(net, "net_"))
    out["ann_turnover"] = float(turnover.astype(float).mean() * WEEKS_PER_YEAR)
    out["avg_weekly_cost"] = float(cost.mean())
    out["n_weeks"] = int(len(rets))
    return out


# --------------------------------------------------------------------------------------
# Input loading.
# --------------------------------------------------------------------------------------
def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def _industry_key(sector: pd.Series, aggregate: pd.Series, industry: pd.Series) -> pd.Series:
    return (
        sector.fillna("").astype(str).str.strip()
        + "||"
        + aggregate.fillna("").astype(str).str.strip()
        + "||"
        + industry.fillna("").astype(str).str.strip()
    )


def load_industry_signals(conn: sqlite3.Connection) -> pd.DataFrame:
    frame = pd.read_sql_query(
        """
        SELECT as_of_date, sector_name, industry_aggregate_name, industry_name,
               final_score AS industry_macro_fit, member_count, coverage_flag
        FROM industry_macro_fit_daily
        ORDER BY as_of_date, sector_name, industry_aggregate_name, industry_name
        """,
        conn,
        parse_dates=["as_of_date"],
    )
    if frame.empty:
        raise ValueError("industry_macro_fit_daily is empty; cannot run the industry ablation.")
    frame["industry_macro_fit"] = pd.to_numeric(frame["industry_macro_fit"], errors="coerce")
    frame["coverage_flag"] = pd.to_numeric(frame["coverage_flag"], errors="coerce").fillna(0).astype(int)
    frame["member_count"] = pd.to_numeric(frame["member_count"], errors="coerce").fillna(0).astype(int)
    frame["industry_key"] = _industry_key(
        frame["sector_name"], frame["industry_aggregate_name"], frame["industry_name"]
    )
    return frame


def load_regime_scalars(
    conn: sqlite3.Connection,
    *,
    signal_dates: pd.DatetimeIndex,
    regime_to_gross_scalar: dict[str, float],
    max_age_days: int,
) -> pd.DataFrame:
    """Active current regime as of each signal date (backward as-of, covered rows only)."""
    regimes = pd.read_sql_query(
        """
        SELECT as_of_date, active_current_regime
        FROM macro_regime_decision_daily
        WHERE coverage_flag = 1 AND active_current_regime IS NOT NULL
        ORDER BY as_of_date
        """,
        conn,
        parse_dates=["as_of_date"],
    )
    scaffold = pd.DataFrame({"as_of_date": pd.DatetimeIndex(signal_dates).sort_values()})
    if regimes.empty:
        scaffold["active_current_regime"] = ""
        scaffold["gross_scalar"] = DEFAULT_GROSS_SCALAR
        return scaffold
    merged = pd.merge_asof(
        scaffold,
        regimes.sort_values("as_of_date"),
        on="as_of_date",
        direction="backward",
        tolerance=pd.Timedelta(days=int(max_age_days)),
    )
    merged["active_current_regime"] = merged["active_current_regime"].fillna("").astype(str)
    merged["gross_scalar"] = (
        merged["active_current_regime"].map(regime_to_gross_scalar).astype(float).fillna(DEFAULT_GROSS_SCALAR)
    )
    return merged


def load_membership_panel(signal_dates: pd.DatetimeIndex) -> pd.DataFrame:
    """Reconstruct per-week ticker -> industry membership from the Stage 9 snapshot source."""
    panel = load_staging_score_panel(
        start_date=pd.Timestamp(signal_dates.min()),
        end_date=pd.Timestamp(signal_dates.max()),
    )
    panel = panel.loc[
        panel["Date"].isin(pd.DatetimeIndex(signal_dates)),
        ["Date", "Ticker", "sector", "industry", "industry_aggregate"],
    ].copy()
    if panel.empty:
        raise ValueError("Membership reconstruction produced no rows on the Stage 9 weekly grid.")
    panel["industry_key"] = _industry_key(panel["sector"], panel["industry_aggregate"], panel["industry"])
    return panel.drop_duplicates(subset=["Date", "Ticker"], keep="last").reset_index(drop=True)


def load_portfolio_gross_scalars(portfolio_config_path: Path) -> dict[str, float]:
    if not portfolio_config_path.exists():
        raise FileNotFoundError(f"Portfolio-layer config not found: {portfolio_config_path}")
    with open(portfolio_config_path, "r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    mapping = cfg_get(data, "black_litterman_fusion", "regime_to_gross_scalar", default=None)
    if not isinstance(mapping, dict) or not mapping:
        raise ValueError(
            "black_litterman_fusion.regime_to_gross_scalar is missing from the portfolio-layer config."
        )
    return {str(key): float(value) for key, value in mapping.items()}


# --------------------------------------------------------------------------------------
# Weekly basket returns (PIT: entry next trading day after signal, exit next week's entry).
# --------------------------------------------------------------------------------------
def entry_dates_for_signals(signal_dates: pd.DatetimeIndex, price_index: pd.DatetimeIndex) -> list[pd.Timestamp | None]:
    """First trading day strictly after each signal date (None when the panel has no later bar)."""
    ordered = pd.DatetimeIndex(signal_dates).sort_values()
    out: list[pd.Timestamp | None] = []
    for signal_date in ordered:
        pos = int(price_index.searchsorted(signal_date, side="right"))
        out.append(pd.Timestamp(price_index[pos]) if pos < len(price_index) else None)
    return out


def compute_industry_week_returns(
    *,
    membership: pd.DataFrame,
    prices: pd.DataFrame,
    signal_dates: pd.DatetimeIndex,
) -> pd.DataFrame:
    """Equal-weight member returns per (signal week, industry_key) with entry/exit PIT bars."""
    ordered = pd.DatetimeIndex(signal_dates).sort_values()
    price_index = pd.DatetimeIndex(prices.index)
    entries = entry_dates_for_signals(ordered, price_index)
    grouped = {date: frame for date, frame in membership.groupby("Date", sort=True)}

    rows: list[dict[str, Any]] = []
    skipped_no_bars: list[str] = []
    missing_membership: list[str] = []
    for idx in range(len(ordered) - 1):
        signal_date = pd.Timestamp(ordered[idx])
        entry = entries[idx]
        exit_ = entries[idx + 1]
        if entry is None or exit_ is None or entry >= exit_:
            skipped_no_bars.append(signal_date.date().isoformat())
            continue
        members = grouped.get(signal_date)
        if members is None or members.empty:
            missing_membership.append(signal_date.date().isoformat())
            continue
        tickers = [t for t in members["Ticker"].tolist() if t in prices.columns]
        if not tickers:
            missing_membership.append(signal_date.date().isoformat())
            continue
        entry_px = prices.loc[entry, tickers].astype(float)
        exit_px = prices.loc[exit_, tickers].astype(float)
        valid = entry_px.notna() & exit_px.notna() & (entry_px > 0.0)
        if not bool(valid.any()):
            skipped_no_bars.append(signal_date.date().isoformat())
            continue
        member_returns = (exit_px[valid] / entry_px[valid] - 1.0).rename("member_return")
        keyed = members.set_index("Ticker")["industry_key"].reindex(member_returns.index)
        by_industry = (
            pd.DataFrame({"industry_key": keyed, "member_return": member_returns})
            .groupby("industry_key")["member_return"]
            .agg(["mean", "count"])
        )
        for industry_key, stats_row in by_industry.iterrows():
            rows.append(
                {
                    "as_of_date": signal_date,
                    "industry_key": str(industry_key),
                    "industry_return": float(stats_row["mean"]),
                    "priced_members": int(stats_row["count"]),
                    "entry_date": entry,
                    "exit_date": exit_,
                }
            )
    if missing_membership:
        raise ValueError(
            "Membership reconstruction is missing signal weeks with valid trading bars "
            f"(fail closed): {missing_membership[:10]} (total {len(missing_membership)})."
        )
    if skipped_no_bars:
        logger.info(
            "Skipped %d signal week(s) without both entry and exit bars (PIT right edge): %s",
            len(skipped_no_bars),
            skipped_no_bars[-4:],
        )
    if not rows:
        raise ValueError("No industry-week returns could be computed; ablation cannot proceed.")
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------------------
# Ablation assembly.
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class AblationResult:
    weekly: pd.DataFrame
    weights: dict[str, pd.DataFrame]
    membership_validation: dict[str, float]


def build_weekly_arms(
    *,
    signals: pd.DataFrame,
    industry_returns: pd.DataFrame,
    regime_scalars: pd.DataFrame,
) -> AblationResult:
    """Construct NEUTRAL / TILT / GROSS weekly weights and portfolio returns."""
    scalar_map = regime_scalars.set_index("as_of_date")
    return_map = {date: frame for date, frame in industry_returns.groupby("as_of_date", sort=True)}
    signal_map = {date: frame for date, frame in signals.groupby("as_of_date", sort=True)}

    weekly_rows: list[dict[str, Any]] = []
    weight_rows: dict[str, list[pd.Series]] = {name: [] for name in ARM_NAMES}
    weight_index: list[pd.Timestamp] = []
    validation_pairs: list[tuple[int, int]] = []

    for signal_date in sorted(return_map.keys()):
        ret_frame = return_map[signal_date]
        sig_frame = signal_map.get(signal_date)
        if sig_frame is None or sig_frame.empty:
            raise ValueError(f"Signal rows missing for evaluated week {pd.Timestamp(signal_date).date()}.")
        covered = sig_frame.loc[
            (sig_frame["coverage_flag"] == 1) & sig_frame["industry_macro_fit"].notna()
        ].set_index("industry_key")
        priced = ret_frame.set_index("industry_key")
        used_keys = covered.index.intersection(priced.index)
        if len(used_keys) == 0:
            logger.warning(
                "Week %s has no covered industries with priced members; excluded from all arms.",
                pd.Timestamp(signal_date).date(),
            )
            continue
        for key in used_keys:
            validation_pairs.append(
                (int(covered.loc[key, "member_count"]), int(priced.loc[key, "priced_members"]))
            )
        rets = priced.loc[used_keys, "industry_return"].astype(float)
        w_neutral = neutral_weights(used_keys)
        w_tilt = tilt_weights(covered.loc[used_keys, "industry_macro_fit"].astype(float))
        scalar = (
            float(scalar_map.loc[signal_date, "gross_scalar"])
            if signal_date in scalar_map.index
            else DEFAULT_GROSS_SCALAR
        )
        regime = (
            str(scalar_map.loc[signal_date, "active_current_regime"])
            if signal_date in scalar_map.index
            else ""
        )
        w_gross = w_tilt * scalar

        weekly_rows.append(
            {
                "as_of_date": pd.Timestamp(signal_date),
                "entry_date": pd.Timestamp(ret_frame["entry_date"].iloc[0]),
                "exit_date": pd.Timestamp(ret_frame["exit_date"].iloc[0]),
                "active_current_regime": regime,
                "gross_scalar": scalar,
                "n_industries_covered": int(len(covered)),
                "n_industries_used": int(len(used_keys)),
                "neutral_return": float((w_neutral * rets).sum()),
                "tilt_return": float((w_tilt * rets).sum()),
                "gross_return": float((w_gross * rets).sum()),
                "tilt_invested_weight": float(w_tilt.sum()),
            }
        )
        weight_index.append(pd.Timestamp(signal_date))
        weight_rows["neutral"].append(w_neutral)
        weight_rows["tilt"].append(w_tilt)
        weight_rows["gross"].append(w_gross)

    if not weekly_rows:
        raise ValueError("No evaluable weeks remained after coverage and pricing filters.")

    weights: dict[str, pd.DataFrame] = {}
    for name in ARM_NAMES:
        frame = pd.DataFrame(weight_rows[name], index=pd.DatetimeIndex(weight_index, name="as_of_date"))
        weights[name] = frame.fillna(0.0)

    pairs = np.asarray(validation_pairs, dtype=float)
    exact = float(np.mean(pairs[:, 0] == pairs[:, 1])) if len(pairs) else float("nan")
    mean_abs = float(np.mean(np.abs(pairs[:, 0] - pairs[:, 1]))) if len(pairs) else float("nan")
    membership_validation = {
        "industry_weeks_compared": float(len(pairs)),
        "member_count_exact_match_fraction": exact,
        "member_count_mean_abs_diff": mean_abs,
    }
    weekly = pd.DataFrame(weekly_rows).sort_values("as_of_date").reset_index(drop=True)
    return AblationResult(weekly=weekly, weights=weights, membership_validation=membership_validation)


def attach_costs(
    result: AblationResult,
    *,
    round_trip_cost_bps: float,
) -> pd.DataFrame:
    weekly = result.weekly.set_index("as_of_date")
    for name in ARM_NAMES:
        turn = turnover_series(result.weights[name])
        cost = cost_series(turn, round_trip_cost_bps=round_trip_cost_bps)
        weekly[f"{name}_turnover"] = turn
        weekly[f"{name}_cost"] = cost
        weekly[f"{name}_net_return"] = weekly[f"{name}_return"] - cost
    weekly["tilt_minus_neutral"] = weekly["tilt_return"] - weekly["neutral_return"]
    weekly["tilt_minus_neutral_net"] = weekly["tilt_net_return"] - weekly["neutral_net_return"]
    return weekly.reset_index()


def per_year_deltas(weekly: pd.DataFrame) -> list[dict[str, Any]]:
    frame = weekly.copy()
    frame["year"] = pd.to_datetime(frame["as_of_date"]).dt.year
    out: list[dict[str, Any]] = []
    for year, group in frame.groupby("year", sort=True):
        neutral_total = float((1.0 + group["neutral_return"]).prod() - 1.0)
        tilt_total = float((1.0 + group["tilt_return"]).prod() - 1.0)
        gross_total = float((1.0 + group["gross_return"]).prod() - 1.0)
        neutral_net_total = float((1.0 + group["neutral_net_return"]).prod() - 1.0)
        tilt_net_total = float((1.0 + group["tilt_net_return"]).prod() - 1.0)
        out.append(
            {
                "year": int(year),
                "n_weeks": int(len(group)),
                "neutral_total_return": neutral_total,
                "tilt_total_return": tilt_total,
                "gross_total_return": gross_total,
                "tilt_minus_neutral_total": tilt_total - neutral_total,
                "tilt_minus_neutral_net_total": tilt_net_total - neutral_net_total,
                "mean_weekly_diff": float(group["tilt_minus_neutral"].mean()),
                "mean_weekly_diff_net": float(group["tilt_minus_neutral_net"].mean()),
            }
        )
    return out


# --------------------------------------------------------------------------------------
# Output writing.
# --------------------------------------------------------------------------------------
def _write_atomic_csv(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    frame.to_csv(tmp_path, index=False)
    tmp_path.replace(path)


def _write_atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, allow_nan=True)
    tmp_path.replace(path)


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fmt_pct(value: float) -> str:
    return f"{value * 100.0:+.2f}%" if np.isfinite(value) else "nan"


def print_summary_table(summary: dict[str, Any]) -> None:
    print("\n=== Macro industry-basket ablation (TILT vs NEUTRAL, weekly) ===")
    header = (
        f"{'arm':<8} {'total':>9} {'ann':>8} {'vol':>7} {'sharpe':>7} {'maxDD':>8} "
        f"{'hit':>6} {'annTO':>7} {'netTotal':>9} {'netAnn':>8} {'netSharpe':>9}"
    )
    print(header)
    for name in ARM_NAMES:
        arm = summary["arms"][name]
        print(
            f"{name:<8} {_fmt_pct(arm['total_return']):>9} {_fmt_pct(arm['ann_return']):>8} "
            f"{_fmt_pct(arm['ann_vol']):>7} {arm['sharpe']:>7.2f} {_fmt_pct(arm['max_drawdown']):>8} "
            f"{arm['hit_rate'] * 100.0:>5.1f}% {arm['ann_turnover']:>6.2f}x "
            f"{_fmt_pct(arm['net_total_return']):>9} {_fmt_pct(arm['net_ann_return']):>8} "
            f"{arm['net_sharpe']:>9.2f}"
        )
    boot = summary["bootstrap"]["tilt_minus_neutral"]
    boot_net = summary["bootstrap"]["tilt_minus_neutral_net"]
    print(
        f"\nTILT-NEUTRAL mean weekly diff: {boot['mean_weekly_diff'] * 1e4:+.2f} bps "
        f"[{boot['ci_low'] * 1e4:+.2f}, {boot['ci_high'] * 1e4:+.2f}] bps "
        f"(95% MBB, block={boot['block_weeks']}w, n={boot['n_weeks']}, P(diff>0)={boot['prob_diff_positive']:.2f})"
    )
    print(
        f"net of costs:                  {boot_net['mean_weekly_diff'] * 1e4:+.2f} bps "
        f"[{boot_net['ci_low'] * 1e4:+.2f}, {boot_net['ci_high'] * 1e4:+.2f}] bps "
        f"(P(diff>0)={boot_net['prob_diff_positive']:.2f})"
    )
    print("\nPer-year TILT-minus-NEUTRAL (total-return delta, pp = percentage points):")
    print(f"{'year':<6} {'weeks':>5} {'neutral':>9} {'tilt':>9} {'delta':>9} {'net delta':>10}")
    for row in summary["per_year"]:
        print(
            f"{row['year']:<6} {row['n_weeks']:>5} {_fmt_pct(row['neutral_total_return']):>9} "
            f"{_fmt_pct(row['tilt_total_return']):>9} "
            f"{row['tilt_minus_neutral_total'] * 100.0:>+8.2f}p "
            f"{row['tilt_minus_neutral_net_total'] * 100.0:>+9.2f}p"
        )


# --------------------------------------------------------------------------------------
# Entrypoint.
# --------------------------------------------------------------------------------------
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Historical industry-basket macro ablation (NEUTRAL vs softplus TILT vs regime GROSS)."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to macro raw YAML config (default MacroLayer/config_macro_raw.yaml).",
    )
    parser.add_argument("--serving-db-path", type=Path, default=None, help="Optional serving SQLite path override.")
    return parser.parse_args()


def main() -> None:
    configure_pipeline_logging()
    args = parse_args()
    config_path, cfg = load_macro_raw_config(args.config)

    shadow_cfg = dict(cfg_get(cfg, "shadow_validation_layer", default={}) or {})
    if "round_trip_cost_bps" not in shadow_cfg:
        raise ValueError("shadow_validation_layer.round_trip_cost_bps is required for the ablation (fail closed).")
    round_trip_cost_bps = float(shadow_cfg["round_trip_cost_bps"])
    industry_cfg = dict(cfg_get(cfg, "industry_macro_layer", default={}) or {})
    context_max_age_days = max(0, int(industry_cfg.get("context_max_age_days", 10)))

    portfolio_config_path = resolve_path(config_path, PORTFOLIO_CONFIG_RELATIVE)
    if portfolio_config_path is None:
        raise ValueError("Could not resolve the portfolio-layer config path.")
    regime_to_gross_scalar = load_portfolio_gross_scalars(portfolio_config_path)

    serving_db_path = resolve_serving_db_path(cfg, config_path, override=args.serving_db_path)
    if not Path(serving_db_path).exists():
        raise FileNotFoundError(f"Serving DB does not exist: {serving_db_path}")

    conn = _connect_readonly(Path(serving_db_path))
    try:
        signals = load_industry_signals(conn)
        signal_dates = pd.DatetimeIndex(sorted(signals["as_of_date"].unique()))
        latest_as_of = pd.Timestamp(signal_dates.max())
        logger.info(
            "Loaded %d industry-week signal rows over %d weekly dates (%s..%s).",
            len(signals),
            len(signal_dates),
            signal_dates.min().date(),
            latest_as_of.date(),
        )
        regime_scalars = load_regime_scalars(
            conn,
            signal_dates=signal_dates,
            regime_to_gross_scalar=regime_to_gross_scalar,
            max_age_days=context_max_age_days,
        )
    finally:
        conn.close()

    membership = load_membership_panel(signal_dates)
    logger.info(
        "Reconstructed membership: rows=%d weeks=%d tickers=%d (source=staging snapshot store, Stage 9 source_mode).",
        len(membership),
        membership["Date"].nunique(),
        membership["Ticker"].nunique(),
    )

    tickers = sorted(membership["Ticker"].unique().tolist())
    survivorship_dir = latest_accepted_survivorship_panel()
    prices = load_staging_prices(
        tickers=tickers,
        start_date=pd.Timestamp(signal_dates.min()),
        end_date=latest_as_of,
        freshness_as_of=latest_as_of,
    )
    if prices.empty:
        raise ValueError("Price panel is empty; ablation cannot proceed (fail closed).")

    industry_returns = compute_industry_week_returns(
        membership=membership,
        prices=prices,
        signal_dates=signal_dates,
    )
    result = build_weekly_arms(
        signals=signals,
        industry_returns=industry_returns,
        regime_scalars=regime_scalars,
    )
    weekly = attach_costs(result, round_trip_cost_bps=round_trip_cost_bps)

    arms: dict[str, dict[str, float]] = {}
    for name in ARM_NAMES:
        arms[name] = arm_performance(
            weekly[f"{name}_return"],
            turnover=weekly[f"{name}_turnover"],
            costs=weekly[f"{name}_cost"],
        )
    bootstrap = {
        "tilt_minus_neutral": moving_block_bootstrap_ci(weekly["tilt_minus_neutral"]),
        "tilt_minus_neutral_net": moving_block_bootstrap_ci(weekly["tilt_minus_neutral_net"]),
    }
    summary: dict[str, Any] = {
        "as_of": latest_as_of.date().isoformat(),
        "evaluated_weeks": int(len(weekly)),
        "evaluated_range": {
            "first_signal": pd.Timestamp(weekly["as_of_date"].min()).date().isoformat(),
            "last_signal": pd.Timestamp(weekly["as_of_date"].max()).date().isoformat(),
        },
        "parameters": {
            "softplus_beta": TILT_SOFTPLUS_BETA,
            "industry_cap": TILT_INDUSTRY_CAP,
            "round_trip_cost_bps": round_trip_cost_bps,
            "regime_to_gross_scalar": regime_to_gross_scalar,
            "regime_context_max_age_days": context_max_age_days,
            "weeks_per_year": WEEKS_PER_YEAR,
        },
        "arms": arms,
        "bootstrap": bootstrap,
        "per_year": per_year_deltas(weekly),
        "membership_validation": result.membership_validation,
    }

    out_root = resolve_path(config_path, OUTPUT_SUBDIR)
    if out_root is None:
        raise ValueError("Could not resolve the industry ablation output directory.")
    out_dir = out_root / latest_as_of.date().isoformat()
    weekly_out = weekly.copy()
    for column in ("as_of_date", "entry_date", "exit_date"):
        weekly_out[column] = pd.to_datetime(weekly_out[column]).dt.strftime("%Y-%m-%d")
    _write_atomic_csv(out_dir / "weekly_returns.csv", weekly_out)
    _write_atomic_json(out_dir / "summary.json", summary)
    manifest = {
        "created_at_utc": utc_now_iso(),
        "script_sha256": _sha256_of(Path(__file__).resolve()),
        "config_sha256": _sha256_of(config_path.resolve()),
        "config_path": str(config_path.resolve()),
        "serving_db_path": str(Path(serving_db_path).resolve()),
        "portfolio_config_path": str(portfolio_config_path.resolve()),
        "survivorship_panel_dir": str(survivorship_dir),
        "signal_weeks_total": int(len(signal_dates)),
        "evaluated_weeks": int(len(weekly)),
        "membership_rows": int(len(membership)),
        "membership_tickers": int(membership["Ticker"].nunique()),
        "price_panel_shape": [int(prices.shape[0]), int(prices.shape[1])],
        "freshness_as_of": latest_as_of.date().isoformat(),
        "membership_validation": result.membership_validation,
        "outputs": ["weekly_returns.csv", "summary.json", "manifest.json"],
    }
    _write_atomic_json(out_dir / "manifest.json", manifest)

    print_summary_table(summary)
    print(f"\nOutputs written to: {out_dir}")
    logger.info(
        "Industry ablation complete: weeks=%d out_dir=%s member_count_match=%.3f",
        len(weekly),
        out_dir,
        result.membership_validation["member_count_exact_match_fraction"],
    )


if __name__ == "__main__":
    main()
