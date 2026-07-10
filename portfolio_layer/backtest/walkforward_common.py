"""Shared Stage 11 walk-forward engine: pure building blocks + the registered-arm replay loop.

One implementation consumed by backtest/16 (dev-window ablation) and backtest/17 (sealed-window
lockbox ledger at the Open Event) so the two can never diverge on replay semantics.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

import numpy as np
import pandas as pd

from portfolio_layer.optimizer.optimizer_core import finalize_long_only_weights, solve_long_only_mv
from portfolio_layer.research.stage11_common import independent_windows, mean_t_hac
from portfolio_layer.risk.covariance_utils import stabilize_covariance
from portfolio_layer.sleeves.risk_model import enforce_rc_cap_to_cash


LOGGER = logging.getLogger("walkforward_common")

ARMS = ("aqr_only", "rotation", "macro_bl", "sleeves", "regime_gate", "regime_lever")

ARM_FIELDS = [
    "arm", "n_rebalances", "n_days", "independent_windows_21d",
    "gross_ann_return", "gross_ann_vol", "gross_sharpe", "net_ann_return", "net_sharpe",
    "max_drawdown_net", "turnover_per_year", "cost_drag_per_year_bps",
    "active_net_ann_vs_baseline", "tracking_error_ann", "net_ir_vs_baseline", "active_t",
    "promotable", "rejection_reasons",
]


def finite_or_default(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return float(default)
    return parsed if np.isfinite(parsed) else float(default)


# ---------------------------------------------------------------------------
# pure building blocks (self-tested)
# ---------------------------------------------------------------------------
def pairwise_shrunk_cov(returns: pd.DataFrame, *, intensity: float, min_obs: int,
                        eig_floor: float = 1e-10, max_cond: float = 1e8) -> pd.DataFrame:
    """Pairwise covariance with linear shrink toward the diagonal, PSD-stabilized (07's method)."""
    cov = returns.cov(min_periods=min_obs)
    cov = cov.dropna(how="all").dropna(how="all", axis=1)
    cov = cov.loc[cov.index, cov.index].fillna(0.0)
    raw = cov.to_numpy(dtype=float)
    diag = np.diag(np.diag(raw))
    shrunk = (1.0 - intensity) * raw + intensity * diag
    stable = stabilize_covariance(shrunk, eig_floor=eig_floor, max_cond=max_cond, name="walkforward_cov")
    return pd.DataFrame(stable, index=cov.index, columns=cov.columns)


def drift_weights(weights: dict[str, float], day_returns: pd.Series) -> tuple[dict[str, float], float]:
    """One buy-and-hold day; returns (renormalized weights, portfolio return). Missing bar = flat."""
    port_ret = 0.0
    grown: dict[str, float] = {}
    for t, w in weights.items():
        raw = day_returns.get(t)
        r = float(raw) if raw is not None and np.isfinite(raw) else 0.0
        grown[t] = w * (1.0 + r)
        port_ret += w * r
    total = sum(grown.values()) + max(0.0, 1.0 - sum(weights.values()))  # cash earns 0
    if total > 0:
        grown = {t: w / total for t, w in grown.items()}
    return grown, port_ret


def turnover_between(prev: dict[str, float], new: dict[str, float]) -> float:
    keys = set(prev) | set(new)
    return sum(abs(new.get(t, 0.0) - prev.get(t, 0.0)) for t in keys)


def rotation_tilt(weights: dict[str, float], pipe_of: dict[str, str],
                  multiplier_of: dict[str, float], *, gross: float) -> dict[str, float]:
    """Bounded multiplicative sleeve tilt, renormalized to the same gross (Stage 5 semantics)."""
    tilted: dict[str, float] = {}
    for ticker, weight in weights.items():
        multiplier = float(multiplier_of.get(pipe_of.get(ticker, ""), 1.0))
        if not np.isfinite(multiplier) or multiplier < 0.0:
            raise ValueError(f"rotation multiplier must be finite and non-negative: {ticker}={multiplier}")
        tilted[ticker] = weight * multiplier
    total = sum(tilted.values())
    if total <= 0:
        return {}
    return {t: w * gross / total for t, w in tilted.items()}


def macro_overlay(weights: dict[str, float], pipe_of: dict[str, str], *, gross_scalar: float,
                  fit_of: dict[str, float], shift_scale: float, max_shift: float) -> dict[str, float]:
    """Regime gross scaling (freed weight to cash) + bounded macro sector-fit tilt."""
    tilted = {}
    for t, w in weights.items():
        fit = float(fit_of.get(pipe_of.get(t, ""), 0.0) or 0.0)
        shift = max(-max_shift, min(max_shift, shift_scale * fit))
        tilted[t] = w * (1.0 + shift)
    total = sum(tilted.values())
    base = sum(weights.values())
    if total > 0 and base > 0:
        tilted = {t: w * base / total for t, w in tilted.items()}  # tilt preserves risky mass
    scalar = max(0.0, min(1.0, float(gross_scalar)))
    return {t: w * scalar for t, w in tilted.items()}


def sleeve_overlay(weights: dict[str, float], cov: pd.DataFrame, pipe_of: dict[str, str],
                   positive_pipes: set[str], *, budgets: dict[str, float], rc_cap: float) -> dict[str, float]:
    """Regime sleeve-budget weight tilt (weight-share approximation of the RC budget) +
    realized per-name RC-cap trim to cash (exact Stage 8 rule via sleeves/risk_model)."""
    sleeve_of = {t: ("medium_rotation" if pipe_of.get(t, "") in positive_pipes else "long_core")
                 for t in weights}
    risky = sum(weights.values())
    if risky > 0:
        share = {s: sum(w for t, w in weights.items() if sleeve_of[t] == s) / risky
                 for s in ("long_core", "medium_rotation")}
        adjusted = {}
        for t, w in weights.items():
            s = sleeve_of[t]
            target = finite_or_default(budgets.get(s, share.get(s, 0.0)), share.get(s, 0.0))
            current = share.get(s, 0.0)
            if target < 0.0 or not np.isfinite(target):
                raise ValueError(f"invalid sleeve budget {s}={target}")
            scale = (target / current) if current > 1e-9 else 0.0
            adjusted[t] = w * max(0.5, min(2.0, scale))
        total = sum(adjusted.values())
        if total > 0:
            weights = {t: w * risky / total for t, w in adjusted.items()}
    names = [t for t in weights if t in cov.index]
    if names:
        sub = {t: weights[t] for t in names}
        result = enforce_rc_cap_to_cash(sub, cov.loc[names, names], rc_cap=rc_cap)
        weights = {**weights, **result.weights}
    return weights


def perf_stats(daily: list[float], *, ppy: int = 252) -> dict[str, float]:
    r = np.array(daily, dtype=float)
    if len(r) < 2:
        return {"ann_return": 0.0, "ann_vol": 0.0, "sharpe": 0.0, "max_dd": 0.0}
    curve = np.cumprod(1.0 + r)
    ann_ret = float(curve[-1] ** (ppy / len(r)) - 1.0)
    ann_vol = float(r.std(ddof=1) * np.sqrt(ppy))
    equity = np.concatenate(([1.0], curve))
    running = np.maximum.accumulate(equity)
    max_dd = float((equity / running - 1.0).min())
    return {"ann_return": ann_ret, "ann_vol": ann_vol,
            "sharpe": ann_ret / ann_vol if ann_vol > 0 else 0.0, "max_dd": max_dd}


def promotion_verdict(*, n_days: int, windows: int, net_ir: float | None, active_t: float | None,
                      cfg: dict[str, Any]) -> tuple[int, list[str]]:
    reasons: list[str] = []
    if n_days < int(cfg.get("min_days", 250)):
        reasons.append(f"insufficient_days:{n_days}")
    if windows < int(cfg.get("min_independent_windows", 6)):
        reasons.append(f"insufficient_independent_windows:{windows}")
    ir_min = float(cfg.get("promote_net_ir_min", 0.0))
    if net_ir is None or net_ir <= ir_min:
        reasons.append(f"net_ir_not_above_{ir_min:g}")
    t_min = float(cfg.get("promote_active_t_min", 2.0))
    if active_t is None or active_t < t_min:
        reasons.append(f"active_t_below_{t_min:g}")
    return (1 if not reasons else 0), reasons


# ---------------------------------------------------------------------------
# the walk-forward engine (identical code path for selftest and real runs)
# ---------------------------------------------------------------------------
def run_walkforward(
    *,
    snapshots: dict[str, list[dict[str, str]]],
    prices: pd.DataFrame,
    arms: list[str],
    params: dict[str, Any],
    regime_provider: Callable[[str], dict[str, Any]],
    sector_fit_provider: Callable[[str], dict[str, float]],
    rotation_provider: Callable[[str], dict[str, dict[str, Any]]],
) -> dict[str, Any]:
    calendar = [str(d) for d in prices.index]
    cal_arr = np.array(calendar)
    returns = prices.apply(pd.to_numeric, errors="coerce").pct_change(fill_method=None)
    rebalance_dates = sorted(snapshots)[:: max(1, int(params["rebalance_every_n_snapshots"]))]
    cost_rate = float(params["one_way_cost_bps"]) / 1e4
    lookback = int(params["cov_lookback_trading_days"])

    gross: dict[str, list[float]] = {arm: [] for arm in arms}
    net: dict[str, list[float]] = {arm: [] for arm in arms}
    day_index: list[str] = []
    turnovers: dict[str, float] = {arm: 0.0 for arm in arms}
    costs_paid: dict[str, float] = {arm: 0.0 for arm in arms}
    holdings: dict[str, dict[str, float]] = {arm: {} for arm in arms}
    pit_violations: list[str] = []
    skipped: dict[str, int] = {"no_calendar": 0, "thin_universe": 0, "solver": 0}
    arm_solver_fallbacks: dict[str, int] = {arm: 0 for arm in arms}
    n_rebalances = 0

    for i, rb_date in enumerate(rebalance_dates):
        pos = int(np.searchsorted(cal_arr, rb_date, side="right"))
        if pos == 0:
            skipped["no_calendar"] += 1
            continue
        window = returns.iloc[max(0, pos - lookback):pos]
        if len(window) and str(prices.index[pos - 1]) > rb_date:
            pit_violations.append(f"{rb_date}:cov_edge={prices.index[pos - 1]}")
        scored = []
        for r in snapshots[rb_date]:
            ticker = str(r.get("ticker", "")).strip().upper()
            raw_mu = r.get("final_score")
            if raw_mu is None:
                continue
            raw_conf = r.get("score_confidence") or "0.5"
            try:
                mu = float(raw_mu)
                conf = float(raw_conf)
            except (TypeError, ValueError):
                continue
            if not ticker or ticker not in prices.columns:
                continue
            try:
                last_px = float(prices[ticker].iloc[pos - 1])
            except (TypeError, ValueError):
                continue
            if not np.isfinite(last_px):
                continue
            scored.append((ticker, mu * conf if params["use_confidence"] else mu,
                           str(r.get("source_pipeline", ""))))
        scored.sort(key=lambda x: -abs(x[1]))
        scored = scored[: int(params["max_universe"])]
        if len(scored) < int(params["min_universe"]):
            skipped["thin_universe"] += 1
            continue
        tickers = [t for t, _m, _p in scored]
        pipe_of = {t: p for t, _m, p in scored}
        cov = pairwise_shrunk_cov(window[[t for t in tickers if t in window.columns]],
                                  intensity=float(params["shrinkage_intensity"]),
                                  min_obs=int(params["cov_min_obs"]))
        common = [t for t in tickers if t in cov.index]
        if len(common) < int(params["min_universe"]):
            skipped["thin_universe"] += 1
            continue
        mu_map = {t: m for t, m, _p in scored}
        mu_used = np.array([mu_map[t] for t in common])
        sigma = cov.loc[common, common].to_numpy(dtype=float)
        try:
            base_w, info = solve_long_only_mv(
                mu_used, sigma, risk_aversion=float(params["risk_aversion"]),
                max_weight=float(params["max_weight"]), gross=float(params["gross"]),
                solver=str(params["solver"]),
            )
        except Exception as exc:  # noqa: BLE001 - a failed solve skips one rebalance, loudly
            LOGGER.warning("solver failed at %s: %s", rb_date, exc)
            skipped["solver"] += 1
            continue
        if info.get("status") not in ("optimal", "optimal_inaccurate"):
            skipped["solver"] += 1
            continue
        base_w = finalize_long_only_weights(base_w, min_weight=float(params["min_weight"]),
                                            max_weight=float(params["max_weight"]),
                                            gross=float(params["gross"]))
        aqr = {t: float(w) for t, w in zip(common, base_w) if w > 0}

        rotation_rows = rotation_provider(rb_date)
        multiplier_of = {p: finite_or_default(r.get("rotation_multiplier"), 1.0)
                         for p, r in rotation_rows.items()}
        positive_pipes = {p for p, r in rotation_rows.items() if str(r.get("state")) == "Positive"}
        regime = regime_provider(rb_date)
        fits = sector_fit_provider(rb_date)

        # regime_gate/regime_lever arms (research/71 evidence):
        #   regime_gate  = score-tilted book only in supportive regimes; min-var otherwise.
        #   regime_lever = stronger score tilt in supportive regimes; configurable fail-closed
        #                  fallback otherwise (default: min-var, matching the original arm).
        # A missing/unknown regime label is UNSUPPORTIVE (fail closed).
        supportive = {str(s).upper() for s in
                      (params.get("regime_gate_supportive_regimes") or ["HEATING_UP"])}
        regime_label = str(regime.get("label", "")).upper()
        min_var_book: dict[str, float] | None = None

        def _min_var_or_prior(arm_name: str) -> dict[str, float]:
            nonlocal min_var_book
            if min_var_book is None:
                try:
                    mv_w, mv_info = solve_long_only_mv(
                        np.zeros(len(common)), sigma,
                        risk_aversion=float(params["risk_aversion"]),
                        max_weight=float(params["max_weight"]), gross=float(params["gross"]),
                        solver=str(params["solver"]),
                    )
                except Exception as exc:  # noqa: BLE001 - hold the prior book, never score-tilt
                    LOGGER.warning("min-variance solve failed at %s: %s", rb_date, exc)
                    mv_w, mv_info = None, {"status": "error"}
                if mv_w is not None and mv_info.get("status") in ("optimal", "optimal_inaccurate"):
                    mv_w = finalize_long_only_weights(
                        mv_w, min_weight=float(params["min_weight"]),
                        max_weight=float(params["max_weight"]), gross=float(params["gross"]))
                    min_var_book = {t: float(x) for t, x in zip(common, mv_w) if x > 0}
                else:
                    arm_solver_fallbacks[arm_name] += 1
                    return dict(holdings[arm_name])
            return dict(min_var_book)

        pending_cost: dict[str, float] = {}
        for arm in arms:
            w = dict(aqr)
            if arm == "regime_gate":
                if regime_label not in supportive:
                    w = _min_var_or_prior(arm)
            elif arm == "regime_lever":
                if regime_label in supportive:
                    lever = finite_or_default(params.get("regime_lever_mu_multiplier"), 1.5)
                    if lever < 0.0:
                        raise ValueError(f"regime_lever_mu_multiplier must be non-negative, got {lever}")
                    try:
                        lever_w, lever_info = solve_long_only_mv(
                            mu_used * lever, sigma,
                            risk_aversion=float(params["risk_aversion"]),
                            max_weight=float(params["max_weight"]), gross=float(params["gross"]),
                            solver=str(params["solver"]),
                        )
                    except Exception as exc:  # noqa: BLE001 - fail closed to the normal AQR book
                        LOGGER.warning("regime-lever solve failed at %s: %s", rb_date, exc)
                        lever_w, lever_info = None, {"status": "error"}
                    if lever_w is not None and lever_info.get("status") in ("optimal", "optimal_inaccurate"):
                        lever_w = finalize_long_only_weights(
                            lever_w, min_weight=float(params["min_weight"]),
                            max_weight=float(params["max_weight"]), gross=float(params["gross"]))
                        w = {t: float(x) for t, x in zip(common, lever_w) if x > 0}
                    else:
                        arm_solver_fallbacks[arm] += 1
                        w = dict(aqr)
                else:
                    unsupported_mode = str(
                        params.get("regime_lever_unsupported_mode", "min_var") or "min_var"
                    ).strip().lower()
                    if unsupported_mode in {"cash", "zero", "flat"}:
                        w = {}
                    elif unsupported_mode in {"aqr", "baseline", "score"}:
                        w = dict(aqr)
                    else:
                        w = _min_var_or_prior(arm)
            elif arm in ("rotation", "macro_bl", "sleeves"):
                w = rotation_tilt(w, pipe_of, multiplier_of, gross=float(params["gross"]))
            if arm in ("macro_bl", "sleeves"):
                w = macro_overlay(w, pipe_of, gross_scalar=float(regime["gross_scalar"]),
                                  fit_of=fits, shift_scale=float(params["macro_shift_scale"]),
                                  max_shift=float(params["macro_max_shift"]))
            if arm == "sleeves":
                w = sleeve_overlay(w, cov, pipe_of, positive_pipes,
                                   budgets=dict(regime["budgets"]), rc_cap=float(params["rc_cap"]))
            traded = turnover_between(holdings[arm], w)
            pending_cost[arm] = traded * cost_rate
            turnovers[arm] += traded
            costs_paid[arm] += pending_cost[arm]
            holdings[arm] = w
        n_rebalances += 1

        end_pos = len(calendar)
        if i + 1 < len(rebalance_dates):
            end_pos = int(np.searchsorted(cal_arr, rebalance_dates[i + 1], side="right"))
        for day_pos in range(pos, end_pos):
            for arm in arms:
                holdings[arm], port_ret = drift_weights(holdings[arm], returns.iloc[day_pos])
                gross[arm].append(port_ret)
                net[arm].append(port_ret - pending_cost[arm])
                pending_cost[arm] = 0.0  # the rebalance cost hits the first holding day only
            day_index.append(calendar[day_pos])

    return {
        "gross": gross, "net": net, "day_index": day_index, "turnovers": turnovers,
        "costs_paid": costs_paid, "n_rebalances": n_rebalances,
        "pit_violations": pit_violations, "skipped": skipped,
        "arm_solver_fallbacks": arm_solver_fallbacks,
    }


def summarize_arms(result: dict[str, Any], arms: list[str], *, verdict_cfg: dict[str, Any],
                   baseline: str = "aqr_only") -> list[dict[str, Any]]:
    n_days = len(result["day_index"])
    years = max(n_days / 252.0, 1e-9)
    windows = independent_windows(sorted(set(result["day_index"])), 21) if n_days else 0
    base_net = np.array(result["net"].get(baseline, []), dtype=float)
    rows = []
    for arm in arms:
        g = perf_stats(result["gross"][arm])
        n = perf_stats(result["net"][arm])
        active = np.array(result["net"][arm], dtype=float) - base_net if len(base_net) else np.array([])
        te = float(active.std(ddof=1) * np.sqrt(252)) if len(active) > 2 else 0.0
        active_ann = float(active.mean() * 252) if len(active) else 0.0
        ir = active_ann / te if te > 0 else None
        hac_lag = max(0, int(verdict_cfg.get("active_t_hac_lag_days", 20)))
        _m, _se, a_t = mean_t_hac(list(active), max_lag=hac_lag) if len(active) else (None, None, None)
        if arm == baseline:
            promotable, reasons = 0, ["baseline_arm"]
        else:
            promotable, reasons = promotion_verdict(
                n_days=n_days, windows=windows, net_ir=ir, active_t=a_t, cfg=verdict_cfg,
            )
        rows.append({
            "arm": arm, "n_rebalances": result["n_rebalances"], "n_days": n_days,
            "independent_windows_21d": windows,
            "gross_ann_return": round(g["ann_return"], 6), "gross_ann_vol": round(g["ann_vol"], 6),
            "gross_sharpe": round(g["sharpe"], 4),
            "net_ann_return": round(n["ann_return"], 6), "net_sharpe": round(n["sharpe"], 4),
            "max_drawdown_net": round(n["max_dd"], 6),
            "turnover_per_year": round(result["turnovers"][arm] / years, 4),
            "cost_drag_per_year_bps": round(result["costs_paid"][arm] / years * 1e4, 2),
            "active_net_ann_vs_baseline": round(active_ann, 6),
            "tracking_error_ann": round(te, 6),
            "net_ir_vs_baseline": round(ir, 4) if ir is not None else "",
            "active_t": round(a_t, 4) if a_t is not None else "",
            "promotable": promotable,
            "rejection_reasons": ";".join(reasons),
        })
    return rows




# ---------------------------------------------------------------------------
# real-data state providers (PIT: every query <= the rebalance date)
# ---------------------------------------------------------------------------
def build_real_providers(config: dict[str, Any], *, conn: Any, prices: pd.DataFrame,
                         pipelines: list[str], taxonomy: dict[str, dict[str, Any]]) -> tuple[
                             Callable[[str], dict[str, Any]],
                             Callable[[str], dict[str, float]],
                             Callable[[str], dict[str, dict[str, Any]]]]:
    """(regime_provider, sector_fit_provider, rotation_provider) over the serving DB + panel."""
    from portfolio_layer.core.config import cfg_get
    from portfolio_layer.macro.contract import rows_at_latest, single_latest_row
    from portfolio_layer.macro.taxonomy import select_sleeve_macro_fit
    from portfolio_layer.rotation.sector_rotation_selector import build_sector_rotation

    gross_map = cfg_get(config, "black_litterman_fusion.regime_to_gross_scalar", {}) or {}
    budgets_cfg = cfg_get(config, "sleeves.sleeve_risk_budgets", {}) or {}
    risk_off = {str(r).upper() for r in cfg_get(config, "sleeves.risk_off_regimes", []) or []}
    rotation_sector_etf_map = {
        str(k).strip(): str(v).strip().upper()
        for k, v in (cfg_get(config, "risk_panel.sector_etf_map", {}) or {}).items()
    }
    rotation_rank_universe = [str(t).strip().upper() for t in cfg_get(config, "rotation.rank_universe_etfs", []) or []]
    rotation_windows = [int(w) for w in cfg_get(config, "rotation.momentum_windows_days", [21, 63, 126])]
    rotation_weights = [float(w) for w in cfg_get(config, "rotation.momentum_weights", [0.5, 0.3, 0.2])]
    rotation_ma_days = int(cfg_get(config, "rotation.trend_filter.ma_days", 200))
    rotation_slope_lookback = int(cfg_get(config, "rotation.trend_filter.slope_lookback_days", 21))
    rotation_positive_score_pct = float(cfg_get(config, "rotation.state_thresholds.positive_score_pct", 60.0))
    rotation_negative_score_pct = float(cfg_get(config, "rotation.state_thresholds.negative_score_pct", 40.0))
    rotation_mult_min = float(cfg_get(config, "rotation.tilt.mult_min", 0.7))
    rotation_mult_max = float(cfg_get(config, "rotation.tilt.mult_max", 1.3))
    cal_arr = np.array([str(d) for d in prices.index])

    def regime_provider(d: str) -> dict[str, Any]:
        row = single_latest_row(conn, "macro_regime_decision_daily", d)
        label = str(row["active_current_regime"] or "").upper() if row is not None else ""
        bucket = "risk_off" if label in risk_off else "default"
        gross_scalar = finite_or_default(gross_map.get(label, gross_map.get("default", 1.0)), 1.0)
        if not 0.0 <= gross_scalar <= 1.0:
            raise ValueError(f"regime gross scalar outside [0,1] for {label or 'UNKNOWN'}: {gross_scalar}")
        return {
            "label": label,
            "gross_scalar": gross_scalar,
            "budgets": dict(budgets_cfg.get(bucket, budgets_cfg.get("default", {})) or {}),
        }

    def sector_fit_provider(d: str) -> dict[str, float]:
        sector_as_of, sector_rows = rows_at_latest(conn, "sector_macro_fit_daily", d)
        industry_as_of, industry_rows = rows_at_latest(conn, "industry_macro_fit_daily", d)
        aggregate_as_of, aggregate_rows = rows_at_latest(conn, "industry_aggregate_macro_fit_daily", d)
        out: dict[str, float] = {}
        for pipe in pipelines:
            fit = select_sleeve_macro_fit(
                run_as_of=d, source_pipeline=pipe, taxonomy=taxonomy.get(pipe, {}),
                sector_as_of=sector_as_of, sector_rows=sector_rows,
                industry_as_of=industry_as_of, industry_rows=industry_rows,
                aggregate_as_of=aggregate_as_of, aggregate_rows=aggregate_rows,
            )
            try:
                out[pipe] = float(fit.macro_fit_score)
            except (TypeError, ValueError):
                out[pipe] = 0.0
        return out

    def rotation_provider(d: str) -> dict[str, dict[str, Any]]:
        pos = int(np.searchsorted(cal_arr, d, side="right"))
        if pos == 0:
            return {}
        window = prices.iloc[max(0, pos - 504):pos].apply(pd.to_numeric, errors="coerce")
        rets = window.pct_change(fill_method=None)
        rows = build_sector_rotation(
            window,
            rets,
            sector_etf_map=rotation_sector_etf_map,
            rank_universe=rotation_rank_universe,
            windows=rotation_windows,
            weights=rotation_weights,
            ma_days=rotation_ma_days,
            slope_lookback=rotation_slope_lookback,
            positive_score_pct=rotation_positive_score_pct,
            negative_score_pct=rotation_negative_score_pct,
            mult_min=rotation_mult_min,
            mult_max=rotation_mult_max,
        )
        return {str(r["source_pipeline"]): r for r in rows}

    return regime_provider, sector_fit_provider, rotation_provider
