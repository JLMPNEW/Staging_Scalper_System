#!/usr/bin/env python3
"""Stage 16d - A1.7 economic non-inferiority gate: V1-regime vs H1-regime gross-scalar arm pair.

SHADOW-ONLY / DIAGNOSTIC. Implements Amendment A1.7 of the H1 campaign
(portfolio_layer/MacroLayer/H1_CANDIDATE_SPEC.md). A standing walk-forward arm PAIR measures the
economic effect of V1 vs H1 macro-regime labels through IDENTICAL machinery:

  baseline_unscaled  the Stage-3 long-only mean-variance book on mu = final_score*score_confidence,
                     held at full gross (scalar 1.0) - reference context only.
  v1_regime_scalar   the SAME baseline book scaled by regime_to_gross_scalar[label] where label is
                     V1's PIT current regime (macro_regime_decision_daily) at each rebalance date;
                     the freed weight sits in cash.
  h1_regime_scalar   identical, but label comes from macro_regime_v2_decision_daily WHERE
                     model_version='macro_regime_h1_hybrid_v1'. FAIL CLOSED: an uncovered/missing H1
                     label at a rebalance date falls back to the V1 label for that date
                     (H1_CANDIDATE_SPEC.md fallback rule), never to a partial quadrant.

Gross scalars come from portfolio_layer/config.yaml black_litterman_fusion.regime_to_gross_scalar
(missing label -> 'default'). This script REUSES the Stage 16 walk-forward machinery
(walkforward_common: pairwise_shrunk_cov, drift_weights, turnover_between, perf_stats and the same
PIT covariance/purge window discipline; optimizer_core: solve_long_only_mv/finalize) over the SAME
sealed snapshot store and survivorship price panel that backtest/16 consumes. It is a separate
script (not new arms in the shared engine) because the pair needs TWO distinct regime label sources
with H1-specific fail-closed fallback, bespoke per-regime attribution / gross-cash outputs, and a
stable gate artifact - none of which fit the shared ARMS/summarize_arms contract that Stage 17's
sealed lockbox also depends on.

A1.7 GATE (thresholds frozen in the spec):
  a17_gate_pass = net_ann_return(H1) >= net_ann_return(V1) - 0.005
              AND max_drawdown(H1)   <= max_drawdown(V1)   + 0.02
where max_drawdown is a POSITIVE magnitude (deeper drawdown = larger value). Historical results are
DIAGNOSTIC ONLY (stamped diagnostic_only=true); the boolean is what the promotion validator consumes
later. It is written to a stable path (output/h1_walkforward/latest_a17_gate.json, atomic overwrite)
plus a dated sealed dir.

RULES: shadow-only (modifies no optimizer/production config, book, or artifact); read-only DB access
(mode=ro, busy_timeout). --selftest drives synthetic prices + labels through the identical engine and
proves: scalar application (0.5x gross halves exposure and cuts return in an up market), fallback to
the V1 label on uncovered H1 dates, and the A1.7 gate logic both passing and failing.
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from portfolio_layer.backtest.walkforward_common import (  # noqa: E402
    drift_weights,
    finite_or_default,
    pairwise_shrunk_cov,
    perf_stats,
    turnover_between,
)
from portfolio_layer.core.config import cfg_get, load_yaml  # noqa: E402
from portfolio_layer.core.contracts import (  # noqa: E402
    fail_if_exists,
    manifest_accepts,
    read_csv,
    read_manifest,
    sha256_file,
    write_csv,
    write_manifest,
)
from portfolio_layer.core.db import utc_now  # noqa: E402
from portfolio_layer.core.logging_utils import configure_utc_logging  # noqa: E402
from portfolio_layer.core.paths import resolve_runtime_paths  # noqa: E402
from portfolio_layer.macro.contract import (  # noqa: E402
    open_macro_serving_db,
    single_latest_regime_row,
    single_latest_row,
)
from portfolio_layer.optimizer.optimizer_core import (  # noqa: E402
    finalize_long_only_weights,
    solve_long_only_mv,
)
from portfolio_layer.research.stage11_common import independent_windows  # noqa: E402

LOGGER = logging.getLogger("run_h1_v1_regime_arms")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"

H1_MODEL_VERSION = "macro_regime_h1_hybrid_v1"
BASELINE_ARM = "baseline_unscaled"
V1_ARM = "v1_regime_scalar"
H1_ARM = "h1_regime_scalar"
ARMS: tuple[str, ...] = (BASELINE_ARM, V1_ARM, H1_ARM)

# A1.7 frozen thresholds (H1_CANDIDATE_SPEC.md).
A17_RETURN_TOL = 0.005
A17_DRAWDOWN_TOL = 0.02

DIAGNOSTIC_BANNER = (
    "DIAGNOSTIC ONLY - historical A1.7 V1/H1 regime-scalar arm comparison; NOT promotable alpha "
    "evidence (H1_CANDIDATE_SPEC.md). The a17_gate_pass boolean exists to catch gross mis-mapping, "
    "not to prove alpha; the promotion validator consumes it on the latest SEALED comparison."
)

ARM_METRIC_FIELDS = [
    "arm", "scalar_source", "n_days", "n_rebalances", "independent_windows_21d",
    "net_ann_return", "net_ann_vol", "net_sharpe", "max_drawdown", "gross_ann_return",
    "turnover_per_year", "cost_drag_per_year_bps", "mean_gross_exposure", "mean_cash",
]


# ---------------------------------------------------------------------------
# pure helpers (self-tested)
# ---------------------------------------------------------------------------
def gross_scalar_for(label: str, gross_map: dict[str, float]) -> float:
    """Map a regime label through regime_to_gross_scalar; missing label -> 'default'."""
    if label in gross_map:
        raw = gross_map[label]
    elif "default" in gross_map:
        raw = gross_map["default"]
    else:
        raise ValueError("regime_to_gross_scalar has no 'default' entry to fall back to")
    scalar = float(raw)
    if not np.isfinite(scalar):
        raise ValueError(f"non-finite gross scalar for label {label!r}: {raw!r}")
    return scalar


def evaluate_a17_gate(
    *,
    net_ann_return_v1: float,
    max_drawdown_v1: float,
    net_ann_return_h1: float,
    max_drawdown_h1: float,
    return_tol: float = A17_RETURN_TOL,
    drawdown_tol: float = A17_DRAWDOWN_TOL,
) -> dict[str, Any]:
    """A1.7 non-inferiority gate. max_drawdown_* are POSITIVE magnitudes (deeper = larger)."""
    return_leg_pass = bool(net_ann_return_h1 >= net_ann_return_v1 - return_tol)
    drawdown_leg_pass = bool(max_drawdown_h1 <= max_drawdown_v1 + drawdown_tol)
    return {
        "a17_gate_pass": bool(return_leg_pass and drawdown_leg_pass),
        "return_leg_pass": return_leg_pass,
        "drawdown_leg_pass": drawdown_leg_pass,
        "net_ann_return_v1": round(float(net_ann_return_v1), 6),
        "net_ann_return_h1": round(float(net_ann_return_h1), 6),
        "net_ann_return_margin": round(float(net_ann_return_h1 - (net_ann_return_v1 - return_tol)), 6),
        "max_drawdown_v1": round(float(max_drawdown_v1), 6),
        "max_drawdown_h1": round(float(max_drawdown_h1), 6),
        "max_drawdown_margin": round(float((max_drawdown_v1 + drawdown_tol) - max_drawdown_h1), 6),
        "return_tol": return_tol,
        "drawdown_tol": drawdown_tol,
    }


# ---------------------------------------------------------------------------
# the walk-forward engine (identical code path for selftest and real runs)
# ---------------------------------------------------------------------------
def run_regime_scalar_arms(
    *,
    snapshots: dict[str, list[dict[str, str]]],
    prices: pd.DataFrame,
    params: dict[str, Any],
    v1_label_provider: Callable[[str], str],
    h1_label_provider: Callable[[str], str],
    gross_map: dict[str, float],
) -> dict[str, Any]:
    """Replay the baseline book at three gross scalings (1.0 / V1-regime / H1-regime).

    The baseline book is solved ONCE per rebalance; each arm scales it and parks the freed weight in
    cash. Between rebalances weights drift with survivorship-panel returns (drift_weights carries the
    cash sleeve at 0% and renormalizes), so a scalar<1 book keeps a drifting gross<1.
    """
    calendar = [str(d) for d in prices.index]
    cal_arr = np.array(calendar)
    returns = prices.apply(pd.to_numeric, errors="coerce").pct_change(fill_method=None)
    rebalance_dates = sorted(snapshots)[:: max(1, int(params["rebalance_every_n_snapshots"]))]
    cost_rate = float(params["one_way_cost_bps"]) / 1e4
    lookback = int(params["cov_lookback_trading_days"])

    arms = list(ARMS)
    gross: dict[str, list[float]] = {a: [] for a in arms}
    net: dict[str, list[float]] = {a: [] for a in arms}
    gross_exposure: dict[str, list[float]] = {a: [] for a in arms}
    cash: dict[str, list[float]] = {a: [] for a in arms}
    day_index: list[str] = []
    turnovers: dict[str, float] = {a: 0.0 for a in arms}
    costs_paid: dict[str, float] = {a: 0.0 for a in arms}
    holdings: dict[str, dict[str, float]] = {a: {} for a in arms}
    current_label: dict[str, str] = {a: "UNKNOWN" for a in arms}
    # attribution[arm][label] = [net_return_sum, day_count]
    attribution: dict[str, dict[str, list[float]]] = {a: {} for a in arms}
    rebalance_rows: list[dict[str, Any]] = []
    pit_violations: list[str] = []
    skipped = {"no_calendar": 0, "thin_universe": 0, "solver": 0}
    h1_fallback_rebalances = 0
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
            scored.append((ticker, mu * conf if params["use_confidence"] else mu))
        scored.sort(key=lambda x: -abs(x[1]))
        scored = scored[: int(params["max_universe"])]
        if len(scored) < int(params["min_universe"]):
            skipped["thin_universe"] += 1
            continue
        tickers = [t for t, _m in scored]
        cov = pairwise_shrunk_cov(window[[t for t in tickers if t in window.columns]],
                                  intensity=float(params["shrinkage_intensity"]),
                                  min_obs=int(params["cov_min_obs"]))
        common = [t for t in tickers if t in cov.index]
        if len(common) < int(params["min_universe"]):
            skipped["thin_universe"] += 1
            continue
        mu_map = {t: m for t, m in scored}
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
        base_book = {t: float(w) for t, w in zip(common, base_w) if w > 0}

        # PIT regime labels at the rebalance date (fail closed on uncovered rows).
        v1_label = str(v1_label_provider(rb_date) or "").upper()
        h1_raw = str(h1_label_provider(rb_date) or "").upper()
        if h1_raw:
            h1_effective, fell_back = h1_raw, False
        else:
            h1_effective, fell_back = v1_label, True  # fail closed -> V1 label
        if fell_back:
            h1_fallback_rebalances += 1
        arm_label = {
            BASELINE_ARM: "UNSCALED",
            V1_ARM: v1_label or "UNKNOWN",
            H1_ARM: h1_effective or "UNKNOWN",
        }
        arm_scalar = {
            BASELINE_ARM: 1.0,
            V1_ARM: gross_scalar_for(v1_label, gross_map),
            H1_ARM: gross_scalar_for(h1_effective, gross_map),
        }
        rebalance_rows.append({
            "date": rb_date,
            "v1_label": v1_label or "UNCOVERED",
            "h1_label_raw": h1_raw or "UNCOVERED",
            "h1_effective_label": h1_effective or "UNCOVERED",
            "h1_fell_back_to_v1": int(fell_back),
            "v1_gross_scalar": round(arm_scalar[V1_ARM], 6),
            "h1_gross_scalar": round(arm_scalar[H1_ARM], 6),
        })

        pending_cost: dict[str, float] = {}
        for arm in arms:
            scalar = float(arm_scalar[arm])
            scaled = {t: w * scalar for t, w in base_book.items()}
            traded = turnover_between(holdings[arm], scaled)
            pending_cost[arm] = traded * cost_rate
            turnovers[arm] += traded
            costs_paid[arm] += pending_cost[arm]
            holdings[arm] = scaled
            current_label[arm] = arm_label[arm]
        n_rebalances += 1

        end_pos = len(calendar)
        if i + 1 < len(rebalance_dates):
            end_pos = int(np.searchsorted(cal_arr, rebalance_dates[i + 1], side="right"))
        for day_pos in range(pos, end_pos):
            for arm in arms:
                holdings[arm], port_ret = drift_weights(holdings[arm], returns.iloc[day_pos])
                net_ret = port_ret - pending_cost[arm]
                gross[arm].append(port_ret)
                net[arm].append(net_ret)
                exposure = float(sum(holdings[arm].values()))
                gross_exposure[arm].append(exposure)
                cash[arm].append(max(0.0, 1.0 - exposure))
                bucket = attribution[arm].setdefault(current_label[arm], [0.0, 0.0])
                bucket[0] += net_ret
                bucket[1] += 1.0
                pending_cost[arm] = 0.0  # rebalance cost hits the first holding day only
            day_index.append(calendar[day_pos])

    return {
        "arms": arms,
        "gross": gross,
        "net": net,
        "gross_exposure": gross_exposure,
        "cash": cash,
        "day_index": day_index,
        "turnovers": turnovers,
        "costs_paid": costs_paid,
        "attribution": attribution,
        "rebalance_rows": rebalance_rows,
        "n_rebalances": n_rebalances,
        "h1_fallback_rebalances": h1_fallback_rebalances,
        "pit_violations": pit_violations,
        "skipped": skipped,
    }


def summarize_arms(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Per-arm net/gross performance, exposure, turnover/cost, and per-regime attribution."""
    n_days = len(result["day_index"])
    years = max(n_days / 252.0, 1e-9)
    windows = independent_windows(sorted(set(result["day_index"])), 21) if n_days else 0
    scalar_source = {BASELINE_ARM: "constant_1.0", V1_ARM: "V1 macro_regime_decision_daily",
                     H1_ARM: f"H1 {H1_MODEL_VERSION} (fallback V1)"}
    out: dict[str, dict[str, Any]] = {}
    for arm in result["arms"]:
        g = perf_stats(result["gross"][arm])
        n = perf_stats(result["net"][arm])
        exposure = np.array(result["gross_exposure"][arm], dtype=float)
        cash_arr = np.array(result["cash"][arm], dtype=float)
        total_net = sum(v[0] for v in result["attribution"][arm].values()) or 0.0
        attrib = []
        for label, (net_sum, days) in sorted(result["attribution"][arm].items()):
            attrib.append({
                "label": label,
                "net_return_contribution": round(float(net_sum), 6),
                "days_active": int(days),
                "pct_of_total_net": round(float(net_sum / total_net), 6) if abs(total_net) > 1e-12 else 0.0,
            })
        out[arm] = {
            "arm": arm,
            "scalar_source": scalar_source[arm],
            "n_days": n_days,
            "n_rebalances": result["n_rebalances"],
            "independent_windows_21d": windows,
            "net_ann_return": round(n["ann_return"], 6),
            "net_ann_vol": round(n["ann_vol"], 6),
            "net_sharpe": round(n["sharpe"], 4),
            "max_drawdown": round(abs(n["max_dd"]), 6),  # POSITIVE magnitude for the A1.7 gate
            "gross_ann_return": round(g["ann_return"], 6),
            "turnover_per_year": round(result["turnovers"][arm] / years, 4),
            "cost_drag_per_year_bps": round(result["costs_paid"][arm] / years * 1e4, 2),
            "mean_gross_exposure": round(float(exposure.mean()), 6) if len(exposure) else 0.0,
            "mean_cash": round(float(cash_arr.mean()), 6) if len(cash_arr) else 0.0,
            "attribution": attrib,
        }
    return out


# ---------------------------------------------------------------------------
# self-test (synthetic providers through the identical engine)
# ---------------------------------------------------------------------------
def _synthetic_market(drift: float, seed: int, n_days: int = 400) -> tuple[pd.DataFrame, dict[str, list[dict[str, str]]]]:
    rng = np.random.default_rng(seed)
    names_per_pipe = 8
    pipes = {"alpha_pipe": drift + 0.0007, "beta_pipe": drift, "gamma_pipe": drift - 0.0005}
    start = date(2001, 1, 1)
    cal: list[str] = []
    d = start
    while len(cal) < n_days:
        if d.weekday() < 5:
            cal.append(d.isoformat())
        d += timedelta(days=1)
    cols: dict[str, np.ndarray] = {}
    pipe_of: dict[str, str] = {}
    for pipe, mu in pipes.items():
        for k in range(names_per_pipe):
            t = f"{pipe[:2].upper()}{k}"
            pipe_of[t] = pipe
            rets = rng.standard_normal(n_days) * 0.012 + mu
            cols[t] = 100 * np.cumprod(1 + rets)
    prices = pd.DataFrame(cols, index=pd.Index(cal))
    snapshots: dict[str, list[dict[str, str]]] = {}
    for i in range(20, n_days - 25, 10):
        rows = []
        for t, pipe in pipe_of.items():
            score = {"alpha_pipe": 0.20, "beta_pipe": 0.05, "gamma_pipe": -0.10}[pipe]
            rows.append({"ticker": t, "final_score": str(score), "score_confidence": "1.0",
                         "source_pipeline": pipe})
        snapshots[cal[i]] = rows
    return prices, snapshots


def _selftest() -> None:
    gross_map = {"HEATING_UP": 1.0, "SLOW_GROWTH": 0.85, "STAGFLATION": 0.70, "default": 0.85}
    params = dict(rebalance_every_n_snapshots=1, one_way_cost_bps=5.0, cov_lookback_trading_days=120,
                  cov_min_obs=20, shrinkage_intensity=0.2, max_universe=24, min_universe=6,
                  use_confidence=True, risk_aversion=5.0, max_weight=0.10, min_weight=0.005,
                  gross=1.0, solver="ECOS")

    # (1) scalar application: strong up market; V1 always full gross (1.0), H1 always 0.5x (a custom
    # de-risk scalar). The H1 arm must hold ~half the gross and earn strictly less in an up market.
    prices, snapshots = _synthetic_market(drift=0.0009, seed=23)
    half_map = {"FULL": 1.0, "HALF": 0.5}

    def v1_full(_: str) -> str:
        return "FULL"

    def h1_half(_: str) -> str:
        return "HALF"

    res = run_regime_scalar_arms(snapshots=snapshots, prices=prices, params=params,
                                 v1_label_provider=v1_full, h1_label_provider=h1_half,
                                 gross_map=half_map)
    assert not res["pit_violations"], res["pit_violations"]
    assert res["n_rebalances"] >= 30 and len(res["day_index"]) > 300
    summ = summarize_arms(res)
    # gross exposure: baseline ~1.0, V1 ~1.0, H1 ~0.5
    assert 0.9 <= summ[BASELINE_ARM]["mean_gross_exposure"] <= 1.05, summ[BASELINE_ARM]["mean_gross_exposure"]
    assert 0.9 <= summ[V1_ARM]["mean_gross_exposure"] <= 1.05, summ[V1_ARM]["mean_gross_exposure"]
    assert 0.42 <= summ[H1_ARM]["mean_gross_exposure"] <= 0.58, summ[H1_ARM]["mean_gross_exposure"]
    # up market: halving gross must cut the net annual return
    assert summ[H1_ARM]["net_ann_return"] < summ[V1_ARM]["net_ann_return"], (
        summ[H1_ARM]["net_ann_return"], summ[V1_ARM]["net_ann_return"])
    # attribution keyed by the active label
    assert any(a["label"] == "HALF" for a in summ[H1_ARM]["attribution"]), summ[H1_ARM]["attribution"]
    assert any(a["label"] == "FULL" for a in summ[V1_ARM]["attribution"]), summ[V1_ARM]["attribution"]

    # (2) fallback: H1 label source is uncovered ("") on EVERY rebalance -> H1 must adopt the V1 label
    # (and therefore the V1 scalar), producing metrics identical to the V1 arm.
    def h1_uncovered(_: str) -> str:
        return ""

    def v1_slow(_: str) -> str:
        return "SLOW_GROWTH"

    res_fb = run_regime_scalar_arms(snapshots=snapshots, prices=prices, params=params,
                                    v1_label_provider=v1_slow, h1_label_provider=h1_uncovered,
                                    gross_map=gross_map)
    assert res_fb["h1_fallback_rebalances"] == res_fb["n_rebalances"], (
        res_fb["h1_fallback_rebalances"], res_fb["n_rebalances"])
    for row in res_fb["rebalance_rows"]:
        assert row["h1_fell_back_to_v1"] == 1
        assert row["h1_effective_label"] == row["v1_label"] == "SLOW_GROWTH"
        assert abs(row["h1_gross_scalar"] - row["v1_gross_scalar"]) < 1e-12
    summ_fb = summarize_arms(res_fb)
    # identical scalars every day => identical arms
    assert abs(summ_fb[H1_ARM]["net_ann_return"] - summ_fb[V1_ARM]["net_ann_return"]) < 1e-9, (
        summ_fb[H1_ARM]["net_ann_return"], summ_fb[V1_ARM]["net_ann_return"])
    assert abs(summ_fb[H1_ARM]["max_drawdown"] - summ_fb[V1_ARM]["max_drawdown"]) < 1e-9

    # partial fallback: covered on some rebalances, uncovered on others.
    call = {"n": 0}

    def h1_alternating(_: str) -> str:
        call["n"] += 1
        return "STAGFLATION" if call["n"] % 2 == 0 else ""

    res_pf = run_regime_scalar_arms(snapshots=snapshots, prices=prices, params=params,
                                    v1_label_provider=v1_slow, h1_label_provider=h1_alternating,
                                    gross_map=gross_map)
    assert 0 < res_pf["h1_fallback_rebalances"] < res_pf["n_rebalances"], res_pf["h1_fallback_rebalances"]
    covered = [r for r in res_pf["rebalance_rows"] if r["h1_fell_back_to_v1"] == 0]
    fell = [r for r in res_pf["rebalance_rows"] if r["h1_fell_back_to_v1"] == 1]
    assert all(r["h1_effective_label"] == "STAGFLATION" for r in covered), covered[:3]
    assert all(r["h1_effective_label"] == "SLOW_GROWTH" for r in fell), fell[:3]

    # (3) A1.7 gate logic - passing and failing (pure function, exact thresholds).
    g_pass = evaluate_a17_gate(net_ann_return_v1=0.10, max_drawdown_v1=0.15,
                               net_ann_return_h1=0.10, max_drawdown_h1=0.15)
    assert g_pass["a17_gate_pass"] is True, g_pass
    # boundary: H1 exactly at both tolerances -> still passes (>= and <=)
    g_edge = evaluate_a17_gate(net_ann_return_v1=0.10, max_drawdown_v1=0.15,
                               net_ann_return_h1=0.10 - A17_RETURN_TOL,
                               max_drawdown_h1=0.15 + A17_DRAWDOWN_TOL)
    assert g_edge["a17_gate_pass"] is True, g_edge
    # return leg fails (H1 lags by more than 0.005)
    g_ret = evaluate_a17_gate(net_ann_return_v1=0.10, max_drawdown_v1=0.15,
                              net_ann_return_h1=0.05, max_drawdown_h1=0.15)
    assert g_ret["a17_gate_pass"] is False and g_ret["return_leg_pass"] is False, g_ret
    assert g_ret["drawdown_leg_pass"] is True, g_ret
    # drawdown leg fails (H1 drawdown deeper by more than 0.02)
    g_dd = evaluate_a17_gate(net_ann_return_v1=0.10, max_drawdown_v1=0.15,
                             net_ann_return_h1=0.10, max_drawdown_h1=0.30)
    assert g_dd["a17_gate_pass"] is False and g_dd["drawdown_leg_pass"] is False, g_dd
    assert g_dd["return_leg_pass"] is True, g_dd

    # (4) end-to-end gate on the identical-arms fallback case: H1 == V1 => gate passes.
    gate_fb = evaluate_a17_gate(
        net_ann_return_v1=summ_fb[V1_ARM]["net_ann_return"], max_drawdown_v1=summ_fb[V1_ARM]["max_drawdown"],
        net_ann_return_h1=summ_fb[H1_ARM]["net_ann_return"], max_drawdown_h1=summ_fb[H1_ARM]["max_drawdown"])
    assert gate_fb["a17_gate_pass"] is True, gate_fb
    # end-to-end gate on the half-gross case in an up market: H1 return lags V1 badly => gate fails.
    gate_half = evaluate_a17_gate(
        net_ann_return_v1=summ[V1_ARM]["net_ann_return"], max_drawdown_v1=summ[V1_ARM]["max_drawdown"],
        net_ann_return_h1=summ[H1_ARM]["net_ann_return"], max_drawdown_h1=summ[H1_ARM]["max_drawdown"])
    assert gate_half["a17_gate_pass"] is False, gate_half

    print("h1-v1 regime-scalar arm self-test: PASS")


# ---------------------------------------------------------------------------
# real-data run
# ---------------------------------------------------------------------------
def _covered_label(row: Any) -> str:
    """The PIT current regime label if the row is covered (coverage_flag==1), else '' (fail closed)."""
    if row is None:
        return ""
    try:
        coverage = float(row["coverage_flag"])
    except (TypeError, ValueError, IndexError, KeyError):
        return ""
    if coverage != 1.0:
        return ""
    label = str(row["active_current_regime"] or "").strip().upper()
    return label


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Stage 16d - A1.7 V1 vs H1 regime-scalar walk-forward arm pair.")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--selftest", action="store_true", help="Run the synthetic self-tests and exit.")
    p.add_argument("--force", action="store_true")
    p.add_argument("--busy-timeout-ms", type=int, default=30000, help="sqlite PRAGMA busy_timeout (ms).")
    return p.parse_args()


def _load_snapshots(store_dir: Path) -> tuple[dict[str, list[dict[str, str]]], list[tuple[str, str]], int]:
    """Load every sealed snapshot (integrity-verified). Diagnostic: a bad snapshot is skipped, not fatal."""
    snapshots: dict[str, list[dict[str, str]]] = {}
    snapshot_shas: list[tuple[str, str]] = []
    skipped_unsealed = 0
    if not store_dir.exists():
        return snapshots, snapshot_shas, skipped_unsealed
    for snap in sorted(store_dir.iterdir()):
        scores_path = snap / "stocks_scores.csv"
        meta_path = snap / "snapshot_meta.json"
        if not snap.is_dir() or not scores_path.exists() or not meta_path.exists():
            continue
        try:
            meta = read_manifest(meta_path)
        except ValueError as exc:
            LOGGER.warning("Snapshot %s metadata unreadable, skipping: %s", snap.name, exc)
            skipped_unsealed += 1
            continue
        scores_sha = sha256_file(scores_path)
        if not manifest_accepts(meta) or str(meta.get("stocks_scores_sha256", "")) != scores_sha:
            LOGGER.warning("Snapshot %s unsealed/stale, skipping", snap.name)
            skipped_unsealed += 1
            continue
        snapshots[snap.name] = read_csv(scores_path)
        snapshot_shas.append((snap.name, scores_sha))
    return snapshots, snapshot_shas, skipped_unsealed


def main() -> int:  # noqa: C901
    configure_utc_logging()
    args = parse_args()
    if args.selftest:
        _selftest()
        return 0

    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    paths = resolve_runtime_paths(config, config_path)

    wf = cfg_get(config, "walkforward", {}) or {}
    params = dict(
        rebalance_every_n_snapshots=int(wf.get("rebalance_every_n_snapshots", 5)),
        one_way_cost_bps=float(wf.get("one_way_cost_bps", 5.0)),
        cov_lookback_trading_days=int(wf.get("cov_lookback_trading_days", 252)),
        cov_min_obs=int(wf.get("cov_min_obs", 60)),
        shrinkage_intensity=float(cfg_get(config, "risk_panel.shrinkage_intensity", 0.2)),
        max_universe=int(wf.get("max_universe", 150)),
        min_universe=int(wf.get("min_universe", 20)),
        use_confidence=bool(cfg_get(config, "optimizer.use_score_confidence", True)),
        risk_aversion=float(cfg_get(config, "optimizer.risk_aversion", 5.0)),
        max_weight=float(cfg_get(config, "optimizer.max_weight_per_name", 0.05)),
        min_weight=float(cfg_get(config, "optimizer.min_weight_to_hold", 0.002)),
        gross=float(cfg_get(config, "optimizer.gross_exposure", 1.0)),
        solver=str(cfg_get(config, "optimizer.solver", "ECOS")),
    )
    gross_map_raw = cfg_get(config, "black_litterman_fusion.regime_to_gross_scalar", {}) or {}
    gross_map = {str(k): finite_or_default(v, float("nan")) for k, v in gross_map_raw.items()}
    gross_map = {k: v for k, v in gross_map.items() if np.isfinite(v)}
    if "default" not in gross_map:
        LOGGER.error("black_litterman_fusion.regime_to_gross_scalar has no 'default' entry")
        return 1

    # snapshots (sealed store) and the survivorship price panel - the SAME inputs backtest/16 uses.
    store_dir = paths.output_dir / str(cfg_get(config, "snapshot_store.dir", "snapshot_store"))
    snapshots, snapshot_shas, skipped_unsealed = _load_snapshots(store_dir)
    if not snapshots:
        LOGGER.error("No sealed snapshots in %s; run research/65 first", store_dir)
        return 1

    panel_root = paths.output_dir / str(cfg_get(config, "survivorship_panel.dir", "survivorship_panel"))
    builds = sorted(p for p in panel_root.iterdir()
                    if p.is_dir() and (p / "survivorship_manifest.json").exists()) if panel_root.exists() else []
    if not builds:
        LOGGER.error("No survivorship panel build under %s; run backtest/15b first", panel_root)
        return 1
    panel_dir = builds[-1]
    panel_manifest_path = panel_dir / "survivorship_manifest.json"
    panel_manifest = read_manifest(panel_manifest_path)
    if not manifest_accepts(panel_manifest, allow_deferred=False):
        LOGGER.error("Survivorship panel %s acceptance=%s; refusing", panel_dir.name,
                     panel_manifest.get("acceptance"))
        return 1
    prices_path = panel_dir / "prices_adjclose.csv"
    prices = pd.read_csv(prices_path, index_col=0)
    prices.columns = [str(c).strip().upper() for c in prices.columns]

    out_root = paths.output_dir / "h1_walkforward"
    out_dir = out_root / panel_dir.name
    arm_path = out_dir / "arm_metrics.csv"
    curves_path = out_dir / "daily_gross_cash.csv"
    rebalance_path = out_dir / "rebalance_labels.csv"
    attribution_path = out_dir / "regime_attribution.csv"
    manifest_path = out_dir / "h1_walkforward_manifest.json"
    dated_gate_path = out_dir / "a17_gate.json"
    latest_gate_path = out_root / "latest_a17_gate.json"
    sealed_outputs = [arm_path, curves_path, rebalance_path, attribution_path, manifest_path, dated_gate_path]
    if args.force:
        for p in sealed_outputs:
            if p.exists():
                p.unlink()
    try:
        fail_if_exists(sealed_outputs, force=args.force)
    except FileExistsError as exc:
        LOGGER.error("%s", exc)
        return 1

    db_path = paths.macro_serving_db_path
    if not db_path.exists():
        LOGGER.error("Macro serving DB not found: %s", db_path)
        return 1
    macro_hash_before = sha256_file(db_path)
    conn = open_macro_serving_db(db_path)  # mode=ro
    conn.execute(f"PRAGMA busy_timeout = {int(args.busy_timeout_ms)}")

    def v1_label_provider(d: str) -> str:
        return _covered_label(single_latest_row(conn, "macro_regime_decision_daily", d))

    def h1_label_provider(d: str) -> str:
        return _covered_label(single_latest_regime_row(
            conn, source="h1", run_as_of=d, model_version=H1_MODEL_VERSION))

    try:
        result = run_regime_scalar_arms(
            snapshots=snapshots, prices=prices, params=params,
            v1_label_provider=v1_label_provider, h1_label_provider=h1_label_provider,
            gross_map=gross_map,
        )
    finally:
        conn.close()
    macro_hash_after = sha256_file(db_path)
    if macro_hash_before != macro_hash_after:
        LOGGER.error("Macro serving DB changed during replay; retry the build")
        return 1

    summary = summarize_arms(result)
    gate = evaluate_a17_gate(
        net_ann_return_v1=summary[V1_ARM]["net_ann_return"], max_drawdown_v1=summary[V1_ARM]["max_drawdown"],
        net_ann_return_h1=summary[H1_ARM]["net_ann_return"], max_drawdown_h1=summary[H1_ARM]["max_drawdown"],
    )

    checks: list[dict[str, str]] = []

    def rec(name: str, status: str, detail: str) -> None:
        checks.append({"check": name, "status": status, "detail": detail})

    rec("pit_no_lookahead", "PASS" if not result["pit_violations"] else "FAIL",
        "covariance/state windows end at/before every rebalance date"
        if not result["pit_violations"] else f"{result['pit_violations'][:8]}")
    total_solver = int(result["skipped"].get("solver", 0))
    rec("solver_reliability", "PASS" if total_solver == 0 else "WARN",
        f"solver_skips={total_solver}; thin_universe={result['skipped'].get('thin_universe', 0)}")
    rec("arms_present", "PASS" if all(a in summary for a in ARMS) else "FAIL",
        f"arms={list(summary)}")
    rec("shadow_only", "PASS",
        "diagnostic-only; modifies no optimizer/production config, book, or artifact; DB opened mode=ro")
    days = len(result["day_index"])
    rec("evidence_span", "WARN" if days < int(wf.get("min_days", 250)) else "PASS",
        f"net holding days={days} (historical window is diagnostic context per A1.7)")

    out_dir.mkdir(parents=True, exist_ok=True)
    arm_rows = [{k: summary[a][k] for k in ARM_METRIC_FIELDS} for a in ARMS]
    write_csv(arm_path, ARM_METRIC_FIELDS, arm_rows)

    curve_fields = ["date"]
    for a in ARMS:
        curve_fields += [f"net_{a}", f"gross_{a}", f"cash_{a}"]
    curve_rows = []
    for j, d in enumerate(result["day_index"]):
        row: dict[str, Any] = {"date": d}
        for a in ARMS:
            row[f"net_{a}"] = round(result["net"][a][j], 8)
            row[f"gross_{a}"] = round(result["gross_exposure"][a][j], 8)
            row[f"cash_{a}"] = round(result["cash"][a][j], 8)
        curve_rows.append(row)
    write_csv(curves_path, curve_fields, curve_rows)

    write_csv(rebalance_path,
              ["date", "v1_label", "h1_label_raw", "h1_effective_label", "h1_fell_back_to_v1",
               "v1_gross_scalar", "h1_gross_scalar"],
              result["rebalance_rows"])

    attribution_rows = [
        {"arm": a, **entry}
        for a in ARMS for entry in summary[a]["attribution"]
    ]
    write_csv(attribution_path,
              ["arm", "label", "net_return_contribution", "days_active", "pct_of_total_net"],
              attribution_rows)

    passed = all(c["status"] in ("PASS", "WARN") for c in checks)
    snapshot_determinism_hash = hashlib.sha256(
        "\n".join(f"{d}:{sha}" for d, sha in sorted(snapshot_shas)).encode("utf-8")
    ).hexdigest()
    date_range = {"start": result["day_index"][0], "end": result["day_index"][-1]} if days else {}

    manifest = {
        "stage": "stage16d_h1_v1_regime_scalar_arms",
        "amendment": "A1.7",
        "diagnostic_only": True,
        "notice": DIAGNOSTIC_BANNER,
        "generated_at": utc_now(),
        "acceptance": "PASS" if passed else "FAIL",
        "panel_build": panel_dir.name,
        "panel_manifest_sha256": sha256_file(panel_manifest_path),
        "h1_model_version": H1_MODEL_VERSION,
        "arms": list(ARMS),
        "params": params,
        "regime_to_gross_scalar": gross_map,
        "snapshots_used": len(snapshots),
        "snapshots_skipped_unsealed": skipped_unsealed,
        "snapshot_determinism_sha256": snapshot_determinism_hash,
        "rebalances": result["n_rebalances"],
        "h1_fallback_rebalances": result["h1_fallback_rebalances"],
        "days": days,
        "date_range": date_range,
        "skipped": result["skipped"],
        "arm_metrics": {a: {k: v for k, v in summary[a].items() if k != "attribution"} for a in ARMS},
        "attribution": {a: summary[a]["attribution"] for a in ARMS},
        "a17_gate": gate,
        "checks": checks,
        "inputs_sha256": {
            "survivorship_manifest.json": sha256_file(panel_manifest_path),
            "prices_adjclose.csv": sha256_file(prices_path),
            "config.yaml": sha256_file(config_path),
            "macro_serving.sqlite": macro_hash_after,
        },
        "files": {
            "arm_metrics.csv": {"sha256": sha256_file(arm_path), "rows": len(arm_rows)},
            "daily_gross_cash.csv": {"sha256": sha256_file(curves_path), "rows": len(curve_rows)},
            "rebalance_labels.csv": {"sha256": sha256_file(rebalance_path), "rows": len(result["rebalance_rows"])},
            "regime_attribution.csv": {"sha256": sha256_file(attribution_path), "rows": len(attribution_rows)},
        },
    }
    write_manifest(manifest_path, manifest)

    # Stable gate artifact the promotion validator will consume (atomic overwrite each run), plus a
    # dated copy sealed alongside the run.
    gate_artifact = {
        "amendment": "A1.7",
        "diagnostic_only": True,
        "notice": DIAGNOSTIC_BANNER,
        "generated_at": utc_now(),
        "panel_build": panel_dir.name,
        "sealed_dir": str(out_dir),
        "manifest_sha256": sha256_file(manifest_path),
        "date_range": date_range,
        "h1_model_version": H1_MODEL_VERSION,
        **gate,
    }
    write_manifest(dated_gate_path, gate_artifact)
    write_manifest(latest_gate_path, gate_artifact)

    for c in checks:
        LOGGER.info("[%s] %s -- %s", c["status"], c["check"], c["detail"])
    for a in ARMS:
        s = summary[a]
        LOGGER.info("ARM %-18s net_ann=%+.4f sharpe=%+.3f maxDD=%.4f turnover=%.2f gross=%.3f",
                    a, s["net_ann_return"], s["net_sharpe"], s["max_drawdown"],
                    s["turnover_per_year"], s["mean_gross_exposure"])
    LOGGER.info("A1.7 GATE: pass=%s (return_leg=%s margin=%+.4f | dd_leg=%s margin=%+.4f)",
                gate["a17_gate_pass"], gate["return_leg_pass"], gate["net_ann_return_margin"],
                gate["drawdown_leg_pass"], gate["max_drawdown_margin"])
    LOGGER.info("H1-WALKFORWARD %s (rebalances=%d, days=%d, h1_fallbacks=%d) -> %s",
                "PASS" if passed else "FAIL", result["n_rebalances"], days,
                result["h1_fallback_rebalances"], out_dir)
    LOGGER.info("latest gate artifact -> %s", latest_gate_path)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
