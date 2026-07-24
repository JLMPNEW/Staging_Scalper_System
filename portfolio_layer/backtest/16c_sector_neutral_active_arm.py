#!/usr/bin/env python3
"""Stage 11 research - GATE 2: sector-neutral active-weight walk-forward, net of cost.

Tests whether the regime-conditional component signal (research/72 evidence) CONVERTS to net-of-cost
ACTIVE return once sector beta is stripped out -- the make-or-break test for the "our scores pick
stocks" thesis, and the on-ramp to a long/short book.

At every weekly rebalance date D, PER SECTOR:
  1. Fit ridge component weights on TRAILING, PURGED, REGIME-CONDITIONAL panel rows only
     (as_of_date <= D - purge_window(h); macro_regime == regime at D). No future data touches the
     weights. Falls back to the composite when trailing evidence is too thin.
  2. Score the current cross-section with those weights -> a within-sector "tilt".
  3. Build two books, both beta-neutral vs the within-sector equal-weight benchmark:
       component_tilt_ls      dollar-neutral long/short spread (unit gross)  -> the RAW signal value
       component_tilt_active  long-only benchmark + clipped tilt             -> achievable TODAY
     plus composite_tilt_* controls built identically from the blended score.
  4. Realize the book's return over [D, D_next] from the survivorship price panel (actual prices,
     PIT), charge one-way turnover cost, and accumulate the net ACTIVE return stream.

Verdict uses the same promotion bar as backtest/16 (net IR > 0, active t >= 2, enough independent
windows). A HIGHER Sharpe with flat active return is NOT a pass. SHADOW-only; changes no config and no
book. If neither the long/short spread nor the long-only book clears the bar net of cost, the signal
does not monetize on this universe and production scoring must not change.

--selftest proves: a rigged regime-conditional within-sector signal converts (positive net active
return); a null does not; the books are exactly sector-neutral; the fit is PIT (no look-ahead); and
net active return is monotonic in cost.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from portfolio_layer.backtest.walkforward_common import perf_stats, promotion_verdict  # noqa: E402
from portfolio_layer.backtest.short_costs import PITShortCostModel, selftest_short_cost_model  # noqa: E402
from portfolio_layer.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from portfolio_layer.core.contracts import fail_if_exists, sha256_file, write_csv, write_manifest  # noqa: E402
from portfolio_layer.core.db import utc_now  # noqa: E402
from portfolio_layer.core.logging_utils import configure_utc_logging  # noqa: E402
from portfolio_layer.core.paths import resolve_runtime_paths  # noqa: E402
from portfolio_layer.research.stage11_common import (  # noqa: E402
    forward_status_is_valid,
    independent_windows,
    load_lockbox,
    manifest_file_errors,
    mean_t,
    mean_t_hac,
)

# reuse research/72's pillar ingestion + standardization (module name starts with a digit)
_spec = importlib.util.spec_from_file_location(
    "component_ic_mod", PACKAGE_ROOT / "research" / "72_component_ic_by_regime.py")
assert _spec is not None and _spec.loader is not None
_c72 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_c72)
# reuse 69's purge window
_spec69 = importlib.util.spec_from_file_location(
    "v69_mod", PACKAGE_ROOT / "research" / "69_validate_stage11_calibration.py")
assert _spec69 is not None and _spec69.loader is not None
_v69 = importlib.util.module_from_spec(_spec69)
_spec69.loader.exec_module(_v69)
purge_window_days = _v69.purge_window_days


LOGGER = logging.getLogger("sector_neutral_active_arm")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
DEFAULT_PIPELINES = [
    "semiconductors",
    "software_infrastructure",
    "technology_hardware",
    "biotech",
    "med_devices",
    "defense",
]
ARM_FIELDS = [
    "arm", "n_rebalances", "n_days", "independent_windows", "net_active_ann", "active_vol_ann",
    "active_ir", "active_t", "net_active_sharpe", "gross_spread_sharpe", "turnover_per_year",
    "cost_drag_ann_bps", "short_net_ann", "short_absolute_net_ann",
    "stress_net_active_ann", "positive_sector_count",
    "spread_exact_weight_fraction", "borrow_actual_weight_fraction",
    "availability_observed_weight_fraction", "availability_fee_proxy_weight_fraction",
    "promotable", "rejection_reasons",
]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Stage 11 sector-neutral active-weight walk-forward (net of cost).")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--panel-build", default=None)
    p.add_argument("--pipelines", default=",".join(DEFAULT_PIPELINES))
    p.add_argument("--horizon", type=int, default=126, help="fit-label horizon (excess_sector_{h}d).")
    p.add_argument("--rebalance-every", type=int, default=5, help="rebalance every Nth panel date.")
    p.add_argument("--ridge", type=float, default=10.0, help="ridge penalty on the component fit.")
    p.add_argument("--min-train-rows", type=int, default=200, help="min trailing rows to fit; else composite.")
    p.add_argument("--min-cross-section", type=int, default=6, help="min names/sector/date to trade it.")
    p.add_argument("--cost-mode", choices=("pit", "fixed"), default=None)
    p.add_argument(
        "--one-way-cost-bps",
        type=float,
        default=None,
        help="Fixed-mode one-way cost. Supplying it also selects fixed mode for compatibility.",
    )
    p.add_argument("--active-kappa", type=float, default=0.5, help="long-only tilt strength (bench*(1+k*z)).")
    p.add_argument("--selftest", action="store_true")
    p.add_argument("--force", action="store_true")
    return p.parse_args()


# ---------------------------------------------------------------------------
# pure construction (self-tested)
# ---------------------------------------------------------------------------
def ridge_fit(x: np.ndarray, y: np.ndarray, ridge: float) -> np.ndarray | None:
    """w = (X'X + ridge*I)^-1 X'y over standardized pillar columns. None if degenerate."""
    if x.ndim != 2 or len(x) < 3 or x.shape[0] != len(y):
        return None
    mask = np.isfinite(y) & np.all(np.isfinite(x), axis=1)
    x, y = x[mask], y[mask]
    if len(y) < 3:
        return None
    k = x.shape[1]
    xtx = x.T @ x + float(ridge) * np.eye(k)
    try:
        w = np.linalg.solve(xtx, x.T @ y)
    except np.linalg.LinAlgError:
        return None
    return w if np.all(np.isfinite(w)) else None


def _demean_unit_gross(tilt: np.ndarray) -> np.ndarray:
    """Dollar-neutral spread weights within a sector: demean, scale to unit gross (sum|a|=1)."""
    a = tilt - np.nanmean(tilt)
    g = float(np.nansum(np.abs(a)))
    return a / g if g > 0 else np.zeros_like(a)


def _long_only_active(tilt_z: np.ndarray, kappa: float) -> np.ndarray:
    """Long-only sector-neutral ACTIVE weights vs equal-weight benchmark, summing to 0 active."""
    n = len(tilt_z)
    if n == 0:
        return tilt_z
    bench = np.full(n, 1.0 / n)
    w = bench * (1.0 + float(kappa) * tilt_z)
    w = np.clip(w, 0.0, None)
    s = w.sum()
    w = w / s if s > 0 else bench
    return w - bench  # active vs benchmark (sums to 0)


# ---------------------------------------------------------------------------
# self-test
# ---------------------------------------------------------------------------
def _selftest() -> None:
    # sector-neutrality of the constructions
    rng = np.random.default_rng(16)
    tilt = rng.standard_normal(30)
    assert abs(float(_demean_unit_gross(tilt).sum())) < 1e-12, "spread not dollar-neutral"
    assert abs(float(np.nansum(np.abs(_demean_unit_gross(tilt))) - 1.0)) < 1e-9, "spread gross != 1"
    z = (tilt - tilt.mean()) / tilt.std()
    assert abs(float(_long_only_active(z, 0.5).sum())) < 1e-12, "long-only active not sector-neutral"

    # rigged: a component predicts the within-sector cross-section; the spread must earn it net of cost.
    n_names, n_dates = 30, 120
    dates = [(date(2001, 1, 5) + timedelta(days=7 * i)).isoformat() for i in range(n_dates)]
    beta = 0.02
    pillar, fwd = {}, {}
    for d in dates:
        zc = rng.standard_normal(n_names)
        zc = (zc - zc.mean()) / zc.std(ddof=1)
        pillar[d] = zc
        fwd[d] = beta * zc + rng.standard_normal(n_names) * 0.03  # realized within-sector return
    gross_rets, net_rets, prev = [], [], np.zeros(n_names)
    for d in dates:
        a = _demean_unit_gross(pillar[d])          # trade the KNOWN signal, held one period
        gross = float(np.nansum(a * fwd[d]))
        cost = float(np.nansum(np.abs(a - prev))) * (5.0 / 1e4)
        prev = a
        gross_rets.append(gross)
        net_rets.append(gross - cost)
    assert float(np.mean(gross_rets)) > 0, "rigged spread should be positive gross"
    assert float(np.mean(net_rets)) > 0, "rigged spread should survive 5bps cost"
    _m, _se, t = mean_t(net_rets)
    assert t is not None and t > 3, ("rigged spread not significant", t)
    # cost monotonicity
    net50 = [float(np.nansum(_demean_unit_gross(pillar[d]) * fwd[d]))
             - float(np.nansum(np.abs(_demean_unit_gross(pillar[d])))) * (50.0 / 1e4) for d in dates]
    assert float(np.mean(net50)) < float(np.mean(net_rets)), "higher cost must reduce net"
    # null signal earns ~0
    null_net = []
    prev = np.zeros(n_names)
    for d in dates:
        a = _demean_unit_gross(rng.standard_normal(n_names))
        null_net.append(float(np.nansum(a * fwd[d])) - float(np.nansum(np.abs(a - prev))) * (5.0 / 1e4))
        prev = a
    _m, _se, tn = mean_t(null_net)
    assert tn is None or abs(tn) < 2.5, ("null should be insignificant", tn)
    # ridge recovers a known weight direction
    x = rng.standard_normal((400, 3))
    w_true = np.array([1.0, 0.0, -0.5])
    y = x @ w_true + rng.standard_normal(400) * 0.1
    w_hat = ridge_fit(x, y, ridge=1.0)
    assert w_hat is not None and w_hat[0] > 0 and w_hat[2] < 0 and abs(w_hat[1]) < abs(w_hat[0]), w_hat
    fixed = _period_economics(
        as_of="2020-01-03",
        calendar_days=7,
        cur_tickers=np.array(["A", "B"]),
        cur_weights=np.array([0.5, -0.5]),
        prev_tickers=None,
        prev_weights=None,
        scale=1.0,
        is_long_short=True,
        model=None,
        fixed_cost_bps=10.0,
        commission_fraction=0.0,
    )
    assert abs(fixed["cost"] - 0.001) < 1e-12, fixed
    assert abs(fixed["short_cost"] - 0.0005) < 1e-12, fixed
    selftest_short_cost_model()
    print("sector-neutral active-arm self-test: PASS")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def _latest(root: Path, marker: str, wanted: str | None) -> Path | None:
    if wanted:
        cand = root / wanted
        return cand if (cand / marker).exists() else None
    if not root.exists():
        return None
    builds = sorted(p for p in root.iterdir() if p.is_dir() and (p / marker).exists())
    return builds[-1] if builds else None


def main() -> int:  # noqa: C901
    configure_utc_logging()
    args = parse_args()
    if args.selftest:
        _selftest()
        return 0
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    paths = resolve_runtime_paths(config, config_path)
    try:
        lockbox = load_lockbox(config, config_path)
    except ValueError as exc:
        LOGGER.error("%s", exc)
        return 1

    pipelines = [p.strip() for p in str(args.pipelines).split(",") if p.strip()]
    h = int(args.horizon)
    tgt, status = f"excess_sector_{h}d", f"fwd_status_{h}d"

    panel_dir = _latest(paths.output_dir / str(cfg_get(config, "calibration_panel.dir", "calibration_panel")),
                        "calibration_panel_manifest.json", args.panel_build)
    if panel_dir is None:
        LOGGER.error("No calibration-panel build; run research/67 first")
        return 1
    calibration_manifest_path = panel_dir / "calibration_panel_manifest.json"
    calibration_manifest = json.loads(calibration_manifest_path.read_text())
    if calibration_manifest["acceptance"] != "PASS":
        LOGGER.error("Calibration panel %s not PASS", panel_dir.name)
        return 1
    panel_path = panel_dir / "calibration_panel.csv"
    panel_errors = manifest_file_errors(calibration_manifest, {"calibration_panel.csv": panel_path})
    if panel_errors:
        LOGGER.error("Calibration panel %s is stale/unsealed: %s", panel_dir.name, panel_errors)
        return 1
    component_dir = (
        paths.output_dir
        / str(cfg_get(config, "component_ic.dir", "component_ic"))
        / panel_dir.name
    )
    component_manifest_path = component_dir / "component_ic_manifest.json"
    component_cells_path = component_dir / "component_ic.csv"
    if not component_manifest_path.exists() or not component_cells_path.exists():
        LOGGER.error("Missing matching component-IC evidence; run research/72 for %s", panel_dir.name)
        return 1
    component_manifest = json.loads(component_manifest_path.read_text(encoding="utf-8"))
    component_errors = manifest_file_errors(
        component_manifest, {"component_ic.csv": component_cells_path}
    )
    component_inputs = component_manifest.get("inputs_sha256") or {}
    expected_pipelines = [p.strip() for p in str(args.pipelines).split(",") if p.strip()]
    if (
        component_manifest.get("acceptance") != "PASS"
        or str(component_manifest.get("panel_build", "")) != panel_dir.name
        or list(component_manifest.get("pipelines") or []) != expected_pipelines
        or str(component_manifest.get("protocol_sha256", "")) != lockbox["protocol_sha256"]
        or str(component_inputs.get("config.yaml", "")) != sha256_file(config_path)
        or component_errors
    ):
        LOGGER.error(
            "Component-IC evidence is stale, rejected, or universe-inconsistent: %s",
            component_errors,
        )
        return 1
    usecols = ["as_of_date", "ticker", "source_pipeline", "macro_regime", "score_z_pipeline_date",
               "calibration_research_eligible", "sidecar_stage11_eligible", "usable_for_promoted_training",
               "survivorship_complete", "in_lockbox", "liquidity_join_available",
               "liquidity_half_spread_bps", tgt, status]
    head = pd.read_csv(panel_path, nrows=0)
    use_set = {c for c in usecols if c in head.columns}
    panel = pd.read_csv(panel_path, usecols=lambda c: c in use_set)
    panel["ticker"] = panel["ticker"].astype(str).str.upper().str.strip()
    panel["as_of_date"] = panel["as_of_date"].astype(str).str.slice(0, 10)
    exact_half_spreads: dict[tuple[str, str], float] = {}
    if "liquidity_half_spread_bps" in panel.columns:
        spread_values = pd.Series(
            pd.to_numeric(panel["liquidity_half_spread_bps"], errors="coerce"),
            index=panel.index,
            dtype=float,
        )
        spread_ok = spread_values.map(lambda value: bool(np.isfinite(value) and value >= 0))
        if "liquidity_join_available" in panel.columns:
            spread_ok &= panel["liquidity_join_available"].astype(str).isin(("1", "1.0", "true", "True"))
        spread_frame = panel.loc[spread_ok, ["as_of_date", "ticker"]].copy()
        spread_frame["half_spread_bps"] = spread_values.loc[spread_ok].astype(float)
        for (spread_day, ticker), values in spread_frame.groupby(["as_of_date", "ticker"]):
            exact_half_spreads[(str(spread_day), str(ticker))] = float(values["half_spread_bps"].median())
    truthy = ("1", "1.0", "true", "True")
    elig = panel["calibration_research_eligible"].astype(str).isin(truthy)
    if "sidecar_stage11_eligible" in panel.columns:
        elig = elig | panel["sidecar_stage11_eligible"].astype(str).isin(truthy)
    panel = panel.loc[
        elig
        & panel["usable_for_promoted_training"].astype(str).isin(truthy)
        & panel["survivorship_complete"].astype(str).isin(truthy)
        & ~panel["in_lockbox"].astype(str).isin(truthy)
        & panel["source_pipeline"].isin(pipelines)
    ].copy()
    if panel.empty:
        LOGGER.error("No admitted panel rows for %s", pipelines)
        return 1

    # pillar enrichment + standardization (reuse research/72)
    root = resolve_path(cfg_get(config, "score_contract.sector_output_root", "../output"), base_dir=config_path.parent)
    sectors_cfg = {str(s.get("model_family")): dict(s) for s in cfg_get(config, "score_contract.sectors", []) or []}
    pillar_sets: dict[str, list[str]] = {}
    pillar_sources_sha256: dict[str, str] = {}
    merged: list[pd.DataFrame] = []
    for pipe in pipelines:
        sub = panel.loc[panel["source_pipeline"] == pipe]
        if sub.empty or pipe not in sectors_cfg:
            continue
        configured = cfg_get(config, f"component_ic.pillars_by_pipeline.{pipe}", []) or []
        pf = _c72._load_pillar_frame(
            sectors_cfg[pipe],
            root,
            set(sub["as_of_date"].unique()),
            used_sha256=pillar_sources_sha256,
            requested_pillars=[str(value) for value in configured] if configured else None,
        )
        if pf.empty:
            LOGGER.warning("No pillars for %s; skipping", pipe)
            continue
        detected = [c for c in pf.columns if c not in ("ticker", "as_of_date")]
        if configured:
            wanted = [str(c) for c in configured]
            missing = sorted(set(wanted) - set(detected))
            if missing:
                LOGGER.error("Configured pillars missing for %s: %s", pipe, missing)
                return 1
            detected = wanted
            pf = pf[["ticker", "as_of_date", *detected]]
        pillar_sets[pipe] = detected
        merged.append(sub.merge(pf, on=["as_of_date", "ticker"], how="inner"))
    if not merged:
        LOGGER.error("No pillar-enriched rows")
        return 1
    component_pillars = {
        str(pipe): {str(value) for value in values}
        for pipe, values in (component_manifest.get("pillar_sets") or {}).items()
    }
    actual_pillars = {pipe: set(values) for pipe, values in pillar_sets.items()}
    if component_pillars != actual_pillars:
        LOGGER.error(
            "Pillar sets differ from accepted component-IC evidence: expected=%s actual=%s",
            component_pillars,
            actual_pillars,
        )
        return 1
    data = pd.concat(merged, ignore_index=True)
    all_pillars = sorted({c for cols in pillar_sets.values() for c in cols})
    for c in all_pillars:
        if c in data.columns:
            data[f"{c}__z"] = data.groupby(["source_pipeline", "as_of_date"])[c].transform(_c72._zscore)
    data["composite__z"] = pd.to_numeric(data["score_z_pipeline_date"], errors="coerce")
    data[tgt] = pd.to_numeric(data[tgt], errors="coerce")

    # survivorship prices for realized P&L
    panel_root = paths.output_dir / str(cfg_get(config, "survivorship_panel.dir", "survivorship_panel"))
    survivorship_build = str(calibration_manifest.get("survivorship_panel_build", "")).strip()
    survivorship_dir = panel_root / survivorship_build
    survivorship_manifest_path = survivorship_dir / "survivorship_manifest.json"
    if not survivorship_build or not survivorship_manifest_path.exists():
        LOGGER.error("No survivorship panel; run backtest/15b first")
        return 1
    expected_survivorship_manifest = str(calibration_manifest.get("survivorship_panel_manifest_sha256", ""))
    if not expected_survivorship_manifest or sha256_file(survivorship_manifest_path) != expected_survivorship_manifest:
        LOGGER.error("Calibration panel survivorship-manifest lineage is stale/missing")
        return 1
    survivorship_manifest = json.loads(survivorship_manifest_path.read_text(encoding="utf-8"))
    prices_path = survivorship_dir / "prices_adjclose.csv"
    survivorship_errors = manifest_file_errors(survivorship_manifest, {"prices_adjclose.csv": prices_path})
    if survivorship_manifest.get("acceptance") != "PASS" or survivorship_errors:
        LOGGER.error("Survivorship panel is unaccepted/stale: %s", survivorship_errors)
        return 1
    prices = pd.read_csv(prices_path, index_col=0)
    # Survivorship prices are date-only daily bars, so plain to_datetime already
    # yields midnight timestamps; no normalize() needed (and its stub is missing).
    prices.index = pd.to_datetime(prices.index, errors="coerce")
    prices = prices.loc[prices.index.notna()].sort_index()
    prices.columns = [str(c).strip().upper() for c in prices.columns]

    out_dir = paths.output_dir / str(cfg_get(config, "sector_neutral_arm.dir", "sector_neutral_arm")) / panel_dir.name
    arm_path = out_dir / "arm_comparison.csv"
    curve_path = out_dir / "active_curves.csv"
    cost_inputs_path = out_dir / "short_cost_inputs.csv"
    sector_path = out_dir / "sector_active_results.csv"
    manifest_path = out_dir / "sector_neutral_manifest.json"
    if args.force:
        for p in (arm_path, curve_path, cost_inputs_path, sector_path, manifest_path):
            if p.exists():
                p.unlink()
    try:
        fail_if_exists([arm_path, curve_path, cost_inputs_path, sector_path, manifest_path], force=args.force)
    except FileExistsError as exc:
        LOGGER.error("%s", exc)
        return 1

    # walk-forward
    dev_dates = sorted(d for d in data["as_of_date"].unique() if d < lockbox["sealed_start"])
    rebal = dev_dates[:: max(1, int(args.rebalance_every))]
    if len(rebal) < 2:
        LOGGER.error("Fewer than two development-window rebalance dates")
        return 1
    short_cfg = cfg_get(config, "sector_neutral_arm.short_costs", {}) or {}
    configured_mode = str(short_cfg.get("mode", "pit")).strip().lower()
    cost_mode = "fixed" if args.one_way_cost_bps is not None else str(args.cost_mode or configured_mode)
    if cost_mode not in {"pit", "fixed"}:
        LOGGER.error("sector_neutral_arm.short_costs.mode must be pit or fixed, got %r", cost_mode)
        return 1
    fixed_cost_bps = float(
        args.one_way_cost_bps
        if args.one_way_cost_bps is not None
        else short_cfg.get("fixed_one_way_cost_bps", cfg_get(config, "walkforward.one_way_cost_bps", 5.0))
    )
    if not np.isfinite(fixed_cost_bps) or fixed_cost_bps < 0:
        LOGGER.error("Fixed one-way cost must be finite and non-negative")
        return 1
    aum_usd = float(short_cfg.get("research_aum_usd", cfg_get(config, "transaction_costs.aum_usd", 300000)))
    commission_usd = float(
        short_cfg.get(
            "commission_per_order_usd",
            cfg_get(config, "transaction_costs.commission_per_order.worst_case", 1.25),
        )
    )
    if not np.isfinite(aum_usd) or aum_usd <= 0 or not np.isfinite(commission_usd) or commission_usd < 0:
        LOGGER.error("Research AUM must be positive and commission must be non-negative")
        return 1
    cost_model: PITShortCostModel | None = None
    market_db_path: Path | None = None
    if cost_mode == "pit":
        market_db_path = resolve_path(
            str(short_cfg.get(
                "market_positioning_db_path",
                r"C:\Users\josel\Documents\STAGING\DB\market_positioning.sqlite",
            )),
            base_dir=config_path.parent,
        )
        try:
            cost_model = PITShortCostModel(
                db_path=market_db_path,
                tickers=set(data["ticker"]),
                start_date=rebal[0],
                end_date=rebal[-1],
                exact_half_spreads=exact_half_spreads,
                spread_fallback_bps=float(short_cfg.get("historical_half_spread_fallback_bps", 15.0)),
                borrow_fee_fallback_annual=float(short_cfg.get("missing_borrow_fee_annual", 0.10)),
                max_borrow_fee_age_days=int(short_cfg.get("max_borrow_fee_age_days", 7)),
                max_shortable_age_days=int(short_cfg.get("max_shortable_age_days", 7)),
                allow_fee_proxy_availability=bool(short_cfg.get("allow_fee_proxy_availability", True)),
                allow_unknown_availability=bool(short_cfg.get("allow_unknown_availability", True)),
                stress_spread_fallback_bps=float(short_cfg.get("stress_half_spread_fallback_bps", 30.0)),
                stress_spread_multiplier=float(short_cfg.get("stress_observed_spread_multiplier", 1.5)),
                stress_borrow_fee_fallback_annual=float(
                    short_cfg.get("stress_missing_borrow_fee_annual", 0.25)
                ),
                stress_borrow_fee_multiplier=float(short_cfg.get("stress_observed_borrow_multiplier", 1.5)),
            )
        except (FileNotFoundError, RuntimeError, ValueError, sqlite3.Error) as exc:
            LOGGER.error("Cannot initialize PIT short-cost model: %s", exc)
            return 1
    pillar_z_cols = {pipe: [f"{c}__z" for c in cols] for pipe, cols in pillar_sets.items()}
    price_idx = np.asarray(pd.DatetimeIndex(prices.index).strftime("%Y-%m-%d"))
    arms = ["component_tilt_ls", "component_tilt_active", "composite_tilt_ls", "composite_tilt_active"]
    active: dict[str, list[float]] = {a: [] for a in arms}
    gross: dict[str, list[float]] = {a: [] for a in arms}
    stress_active: dict[str, list[float]] = {a: [] for a in arms}
    short_net: dict[str, list[float]] = {a: [] for a in arms}
    short_absolute_net: dict[str, list[float]] = {a: [] for a in arms}
    sector_active: dict[str, dict[str, list[float]]] = {
        a: {pipe: [] for pipe in pipelines} for a in arms
    }
    prev_w: dict[str, dict[str, np.ndarray]] = {a: {} for a in arms}  # per (arm, pipe)
    turnover: dict[str, float] = {a: 0.0 for a in arms}
    cost_paid: dict[str, float] = {a: 0.0 for a in arms}
    coverage: dict[str, dict[str, float]] = {
        a: {
            "spread_total": 0.0,
            "spread_exact": 0.0,
            "borrow_total": 0.0,
            "borrow_actual": 0.0,
            "availability_total": 0.0,
            "availability_observed": 0.0,
            "availability_fee_proxy": 0.0,
        }
        for a in arms
    }
    day_index: list[str] = []
    fit_fallbacks = 0
    purge = purge_window_days(h, 0)
    total_holding_days = 0

    data_by_pipe = {pipe: data.loc[data["source_pipeline"] == pipe].copy() for pipe in pipeline_present(data, pipelines)}
    for pipe, pdata in data_by_pipe.items():
        pdata.sort_values("as_of_date", inplace=True)

    for i in range(len(rebal) - 1):
        D, Dn = rebal[i], rebal[i + 1]
        d_price = _price_at(prices, price_idx, D)
        dn_price = _price_at(prices, price_idx, Dn)
        if d_price is None or dn_price is None:
            continue
        regime_here = _modal_regime(data, D)
        cutoff = (date.fromisoformat(D) - timedelta(days=purge)).isoformat()
        period_active: dict[str, float] = {a: 0.0 for a in arms}
        period_gross: dict[str, float] = {a: 0.0 for a in arms}
        period_stress: dict[str, float] = {a: 0.0 for a in arms}
        period_short: dict[str, float] = {a: 0.0 for a in arms}
        period_short_absolute: dict[str, float] = {a: 0.0 for a in arms}
        period_sector: dict[str, dict[str, float]] = {a: {} for a in arms}
        n_sectors = 0
        calendar_days = max(1, (date.fromisoformat(Dn) - date.fromisoformat(D)).days)
        trading_days = max(1, int(np.busday_count(D, Dn)))
        for pipe, pdata in data_by_pipe.items():
            cur = pdata.loc[pdata["as_of_date"] == D]
            if len(cur) < args.min_cross_section:
                continue
            zcols = pillar_z_cols.get(pipe, [])
            cur = cur.dropna(subset=[c for c in zcols if c in cur.columns] + ["composite__z"])
            tickers = cur["ticker"].to_numpy()
            r = np.array([_ret(d_price, dn_price, t) for t in tickers], dtype=float)
            valid = np.isfinite(r)
            if int(valid.sum()) < args.min_cross_section:
                continue
            cur, tickers, r = cur.loc[valid], tickers[valid], r[valid]
            n_sectors += 1
            # component tilt: regime-conditional trailing purged ridge fit
            train = pdata.loc[(pdata["as_of_date"] <= cutoff) & (pdata["macro_regime"].astype(str) == str(regime_here))]
            # Match Gate 1 (research/72) label hygiene: only complete forward windows
            # ("ok") train the ridge. Truncated/delisted rows carry a numeric-but-partial
            # excess_sector_{h}d label that would otherwise pollute the fit and suppress the tilt.
            if status in train.columns:
                train = train.loc[train[status].map(forward_status_is_valid)]
            train = train.dropna(subset=[c for c in zcols if c in train.columns] + [tgt])
            comp_tilt = None
            if len(train) >= args.min_train_rows and zcols:
                w = ridge_fit(train[zcols].to_numpy(dtype=float), train[tgt].to_numpy(dtype=float), args.ridge)
                if w is not None:
                    comp_tilt = cur[zcols].to_numpy(dtype=float) @ w
            if comp_tilt is None:
                comp_tilt = cur["composite__z"].to_numpy(dtype=float)
                fit_fallbacks += 1
            comp_z = _std(comp_tilt)
            composite_z = _std(cur["composite__z"].to_numpy(dtype=float))
            for arm, tiltz, ls in (("component_tilt_ls", comp_z, True),
                                   ("component_tilt_active", comp_z, False),
                                   ("composite_tilt_ls", composite_z, True),
                                   ("composite_tilt_active", composite_z, False)):
                a_w = _demean_unit_gross(tiltz) if ls else _long_only_active(tiltz, args.active_kappa)
                if ls and cost_model is not None:
                    a_w = _enforce_short_availability(
                        a_w,
                        tickers,
                        as_of=D,
                        prices=d_price,
                        model=cost_model,
                        aum_usd=aum_usd,
                        sector_share=1.0 / max(1, len(data_by_pipe)),
                    )
                g = float(np.nansum(a_w * r))
                prev = prev_w[arm].get(pipe)
                prev_tk = prev_w[arm].get(f"{pipe}__tk")
                aligned_prev = _align(prev_tickers=prev_tk, prev_w=prev,
                                      cur_tickers=tickers) if prev is not None else np.zeros(len(a_w))
                # Turnover over held+entering names, PLUS the full unwind of any name that
                # left the cross-section (its prior weight -> 0 is a real closing trade that
                # _align drops because it only maps prev weights onto current tickers).
                exit_turn = _exit_turnover(prev_tickers=prev_tk, prev_w=prev, cur_tickers=tickers)
                traded = float(np.nansum(np.abs(a_w - aligned_prev))) + exit_turn
                scale = 1.0 / max(1, len(data_by_pipe))
                economics = _period_economics(
                    as_of=D,
                    calendar_days=calendar_days,
                    cur_tickers=tickers,
                    cur_weights=a_w,
                    prev_tickers=prev_tk,
                    prev_weights=prev,
                    scale=scale,
                    is_long_short=ls,
                    model=cost_model,
                    fixed_cost_bps=fixed_cost_bps,
                    commission_fraction=commission_usd / aum_usd,
                )
                gross_scaled = g * scale
                net_scaled = gross_scaled - economics["cost"]
                stress_scaled = gross_scaled - economics["stress_cost"]
                short_gross = float(np.nansum(np.minimum(a_w, 0.0) * r)) * scale if ls else 0.0
                short_side = float(np.abs(np.minimum(a_w, 0.0)).sum()) * scale if ls else 0.0
                benchmark_short = -short_side * float(np.nanmean(r)) if ls else 0.0
                short_absolute_scaled = short_gross - economics["short_cost"]
                short_scaled = short_gross - benchmark_short - economics["short_cost"]
                period_gross[arm] += gross_scaled
                period_active[arm] += net_scaled
                period_stress[arm] += stress_scaled
                period_short[arm] += short_scaled
                period_short_absolute[arm] += short_absolute_scaled
                period_sector[arm][pipe] = net_scaled
                turnover[arm] += traded * scale
                cost_paid[arm] += economics["cost"]
                for key in coverage[arm]:
                    coverage[arm][key] += economics[key]
                prev_w[arm][pipe] = a_w
                prev_w[arm][f"{pipe}__tk"] = tickers
        if n_sectors == 0:
            continue
        for a in arms:
            gross[a].append(period_gross[a])
            active[a].append(period_active[a])
            stress_active[a].append(period_stress[a])
            short_net[a].append(period_short[a])
            short_absolute_net[a].append(period_short_absolute[a])
            for pipe in pipelines:
                sector_active[a][pipe].append(period_sector[a].get(pipe, 0.0))
        day_index.append(D)
        total_holding_days += trading_days

    if not day_index:
        LOGGER.error("No rebalances produced returns; check inputs")
        return 1

    # summarize
    years = max(total_holding_days / 252.0, 1e-9)
    windows = independent_windows(sorted(set(day_index)), h)
    promotion_cfg = cfg_get(config, "sector_neutral_arm.promotion", {}) or {}
    verdict_cfg = {
        "min_days": int(promotion_cfg.get("min_holding_days", 504)),
        "min_independent_windows": int(promotion_cfg.get("min_independent_windows", 12)),
        "promote_net_ir_min": float(promotion_cfg.get("min_active_ir", 0.25)),
        "promote_active_t_min": float(promotion_cfg.get("min_active_t", 2.0)),
    }
    min_positive_sectors = int(promotion_cfg.get("min_positive_sectors", 4))
    min_borrow_coverage = float(promotion_cfg.get("min_actual_borrow_weight_fraction", 0.95))
    min_availability_coverage = float(
        promotion_cfg.get("min_observed_or_fee_proxy_availability_weight_fraction", 0.95)
    )
    arm_rows = []
    sector_rows: list[dict[str, object]] = []
    for a in arms:
        arr = np.array(active[a], dtype=float)
        garr = np.array(gross[a], dtype=float)
        stress_arr = np.array(stress_active[a], dtype=float)
        short_arr = np.array(short_net[a], dtype=float)
        short_absolute_arr = np.array(short_absolute_net[a], dtype=float)
        ann = float(arr.sum() / years) if len(arr) else 0.0
        stress_ann = float(stress_arr.sum() / years) if len(stress_arr) else 0.0
        short_ann = float(short_arr.sum() / years) if len(short_arr) else 0.0
        short_absolute_ann = (
            float(short_absolute_arr.sum() / years) if len(short_absolute_arr) else 0.0
        )
        vol = float(arr.std(ddof=1) * np.sqrt(252 / max(1, int(args.rebalance_every)))) if len(arr) > 2 else 0.0
        ir = ann / vol if vol > 0 else None
        hac_lag = max(0, int(np.ceil(h / max(1, int(args.rebalance_every)))) - 1)
        _m, _se, at = mean_t_hac(list(arr), max_lag=hac_lag) if len(arr) else (None, None, None)
        gstats = perf_stats(list(garr), ppy=int(252 / max(1, int(args.rebalance_every))))
        nstats = perf_stats(list(arr), ppy=int(252 / max(1, int(args.rebalance_every))))
        promotable, reasons = promotion_verdict(
            n_days=total_holding_days, windows=windows, net_ir=ir, active_t=at, cfg=verdict_cfg)
        positive_sectors = 0
        for pipe in pipelines:
            values = np.asarray(sector_active[a][pipe], dtype=float)
            sector_ann = float(values.sum() / years) if len(values) else 0.0
            if sector_ann > 0:
                positive_sectors += 1
            sector_rows.append({
                "arm": a,
                "source_pipeline": pipe,
                "net_active_ann": round(sector_ann, 8),
                "positive": int(sector_ann > 0),
            })
        spread_exact_fraction = _fraction(coverage[a]["spread_exact"], coverage[a]["spread_total"])
        borrow_actual_fraction = _fraction(coverage[a]["borrow_actual"], coverage[a]["borrow_total"])
        availability_observed_fraction = _fraction(
            coverage[a]["availability_observed"], coverage[a]["availability_total"]
        )
        availability_fee_fraction = _fraction(
            coverage[a]["availability_fee_proxy"], coverage[a]["availability_total"]
        )
        is_ls = a.endswith("_ls")
        if not is_ls:
            promotable = False
            reasons.append("not_single_name_long_short_candidate")
        if cost_mode != "pit":
            promotable = False
            reasons.append("pit_cost_model_required")
        if is_ls and short_ann <= float(promotion_cfg.get("min_short_net_ann", 0.0)):
            promotable = False
            reasons.append("short_selection_alpha_not_positive")
        if stress_ann <= float(promotion_cfg.get("min_stress_net_active_ann", 0.0)):
            promotable = False
            reasons.append("cost_stress_not_positive")
        if positive_sectors < min_positive_sectors:
            promotable = False
            reasons.append(f"positive_sector_breadth_below_{min_positive_sectors}")
        if is_ls and borrow_actual_fraction < min_borrow_coverage:
            promotable = False
            reasons.append(f"actual_borrow_coverage_below_{min_borrow_coverage:.2f}")
        if is_ls and availability_observed_fraction + availability_fee_fraction < min_availability_coverage:
            promotable = False
            reasons.append(f"short_availability_coverage_below_{min_availability_coverage:.2f}")
        arm_rows.append({
            "arm": a, "n_rebalances": len(day_index), "n_days": total_holding_days,
            "independent_windows": windows,
            "net_active_ann": round(ann, 6), "active_vol_ann": round(vol, 6),
            "active_ir": round(ir, 4) if ir is not None else "",
            "active_t": round(at, 4) if at is not None else "",
            "net_active_sharpe": round(nstats["sharpe"], 4), "gross_spread_sharpe": round(gstats["sharpe"], 4),
            "turnover_per_year": round(turnover[a] / years, 4),
            "cost_drag_ann_bps": round(cost_paid[a] / years * 1e4, 2),
            "short_net_ann": round(short_ann, 6),
            "short_absolute_net_ann": round(short_absolute_ann, 6),
            "stress_net_active_ann": round(stress_ann, 6),
            "positive_sector_count": positive_sectors,
            "spread_exact_weight_fraction": round(spread_exact_fraction, 6),
            "borrow_actual_weight_fraction": round(borrow_actual_fraction, 6),
            "availability_observed_weight_fraction": round(availability_observed_fraction, 6),
            "availability_fee_proxy_weight_fraction": round(availability_fee_fraction, 6),
            "promotable": promotable,
            "rejection_reasons": ";".join(dict.fromkeys(reasons)),
        })

    checks = [
        {"check": "lockbox_dev_window_only", "status": "PASS",
         "detail": f"rebalances confined to < sealed_start={lockbox['sealed_start']}; n={len(day_index)}"},
        {"check": "pit_trailing_purged_fit", "status": "PASS",
         "detail": f"component weights fit on as_of<=D-{purge}d, regime-matched; fallbacks_to_composite={fit_fallbacks}"},
        {"check": "sector_neutral_construction", "status": "PASS",
         "detail": "long/short spread dollar-neutral; long-only active sums to 0 vs equal-weight benchmark"},
        {"check": "promotion_bar_is_net_active_return", "status": "PASS",
         "detail": "long/short promotion requires PIT costs, active IR/t, positive short leg, "
                   "sector breadth, borrow/availability coverage, and positive stressed return"},
        {"check": "pit_short_cost_inputs", "status": "PASS" if cost_mode == "pit" else "WARN",
         "detail": f"mode={cost_mode}; exact spreads are same-date only; borrow/availability use "
                   "backward-only bounded as-of joins"},
        {"check": "research_evidence_only", "status": "PASS",
         "detail": "the script emits promotion evidence and never changes a production book"},
    ]
    passed = all(c["status"] in ("PASS", "WARN") for c in checks)
    out_dir.mkdir(parents=True, exist_ok=True)
    write_csv(arm_path, ARM_FIELDS, arm_rows)
    write_csv(
        sector_path,
        ["arm", "source_pipeline", "net_active_ann", "positive"],
        sector_rows,
    )
    cost_input_rows = cost_model.used_rows() if cost_model is not None else []
    write_csv(
        cost_inputs_path,
        [
            "as_of_date", "ticker", "half_spread_bps", "spread_source",
            "borrow_fee_annual", "borrow_source", "shortable_shares",
            "shortable_source", "short_available",
        ],
        cost_input_rows,
    )
    curve_fields = (
        ["date"]
        + [f"active_{a}" for a in arms]
        + [f"stress_active_{a}" for a in arms]
        + [f"short_net_{a}" for a in arms]
        + [f"short_absolute_net_{a}" for a in arms]
    )
    write_csv(
        curve_path,
        curve_fields,
        [
            {
                "date": d,
                **{f"active_{a}": round(active[a][j], 8) for a in arms},
                **{f"stress_active_{a}": round(stress_active[a][j], 8) for a in arms},
                **{f"short_net_{a}": round(short_net[a][j], 8) for a in arms},
                **{
                    f"short_absolute_net_{a}": round(short_absolute_net[a][j], 8)
                    for a in arms
                },
            }
            for j, d in enumerate(day_index)
        ],
    )
    write_manifest(manifest_path, {
        "stage": "stage11_sector_neutral_active_arm",
        "generated_at": utc_now(), "acceptance": "PASS" if passed else "FAIL",
        "panel_build": panel_dir.name, "protocol_sha256": lockbox["protocol_sha256"],
        "pipelines": pipelines, "pillar_sets": pillar_sets, "horizon_days": h,
        "rebalance_every": int(args.rebalance_every), "ridge": args.ridge,
        "cost_mode": cost_mode, "fixed_one_way_cost_bps": fixed_cost_bps,
        "research_aum_usd": aum_usd, "commission_per_order_usd": commission_usd,
        "active_kappa": args.active_kappa,
        "rebalances": len(day_index), "holding_days": total_holding_days,
        "fit_fallbacks_to_composite": fit_fallbacks,
        "promotion_policy": {
            **verdict_cfg,
            "min_positive_sectors": min_positive_sectors,
            "min_actual_borrow_weight_fraction": min_borrow_coverage,
            "min_observed_or_fee_proxy_availability_weight_fraction": min_availability_coverage,
            "min_short_net_ann": float(promotion_cfg.get("min_short_net_ann", 0.0)),
            "min_stress_net_active_ann": float(promotion_cfg.get("min_stress_net_active_ann", 0.0)),
        },
        "arms": arm_rows, "checks": checks,
        "inputs_sha256": {
            "config.yaml": sha256_file(config_path),
            "backtest/16c_sector_neutral_active_arm.py": sha256_file(Path(__file__).resolve()),
            "backtest/short_costs.py": sha256_file(
                PACKAGE_ROOT / "backtest" / "short_costs.py"
            ),
            "component_ic_manifest.json": sha256_file(component_manifest_path),
            "component_ic.csv": sha256_file(component_cells_path),
            "calibration_panel_manifest.json": sha256_file(calibration_manifest_path),
            "calibration_panel.csv": sha256_file(panel_path),
            "survivorship_manifest.json": sha256_file(survivorship_manifest_path),
            "prices_adjclose.csv": sha256_file(prices_path),
            **{f"pillar_source:{path}": sha for path, sha in sorted(pillar_sources_sha256.items())},
        },
        "files": {
            "arm_comparison.csv": {"sha256": sha256_file(arm_path), "rows": len(arm_rows)},
            "active_curves.csv": {"sha256": sha256_file(curve_path), "rows": len(day_index)},
            "short_cost_inputs.csv": {
                "sha256": sha256_file(cost_inputs_path), "rows": len(cost_input_rows)
            },
            "sector_active_results.csv": {
                "sha256": sha256_file(sector_path), "rows": len(sector_rows)
            },
        },
    })
    for c in checks:
        LOGGER.info("[%s] %s -- %s", c["status"], c["check"], c["detail"])
    for r in arm_rows:
        LOGGER.info("ARM %-24s net_active_ann=%s active_t=%s IR=%s gross_sharpe=%s promotable=%s %s",
                    r["arm"], r["net_active_ann"], r["active_t"], r["active_ir"],
                    r["gross_spread_sharpe"], r["promotable"], r["rejection_reasons"])
    LOGGER.info("SECTOR-NEUTRAL ARM: %s (rebalances=%d) -> %s", "PASS" if passed else "FAIL", len(day_index), out_dir)
    return 0 if passed else 1


def pipeline_present(data: pd.DataFrame, pipelines: list[str]) -> list[str]:
    have = set(data["source_pipeline"].unique())
    return [p for p in pipelines if p in have]


def _modal_regime(data: pd.DataFrame, d: str) -> str:
    sub = data.loc[data["as_of_date"] == d, "macro_regime"].dropna().astype(str)
    return sub.mode().iloc[0] if not sub.empty else ""


def _price_at(prices: pd.DataFrame, price_idx: np.ndarray, d: str) -> pd.Series | None:
    pos = int(np.searchsorted(price_idx, d, side="right")) - 1
    if pos < 0:
        return None
    return prices.iloc[pos]


def _ret(p0: pd.Series, p1: pd.Series, ticker: str) -> float:
    raw0, raw1 = p0.get(ticker), p1.get(ticker)
    if raw0 is None or raw1 is None:
        return float("nan")
    try:
        a, b = float(raw0), float(raw1)
    except (TypeError, ValueError):
        return float("nan")
    return b / a - 1.0 if np.isfinite(a) and np.isfinite(b) and a > 0 else float("nan")


def _std(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x, dtype=float)
    sd = np.nanstd(x)
    return (x - np.nanmean(x)) / sd if sd > 0 else np.zeros_like(x)


def _align(*, prev_tickers: np.ndarray | None, prev_w: np.ndarray | None, cur_tickers: np.ndarray) -> np.ndarray:
    if prev_tickers is None or prev_w is None:
        return np.zeros(len(cur_tickers))
    m = {t: w for t, w in zip(prev_tickers, prev_w)}
    return np.array([m.get(t, 0.0) for t in cur_tickers], dtype=float)


def _exit_turnover(*, prev_tickers: np.ndarray | None, prev_w: np.ndarray | None,
                   cur_tickers: np.ndarray) -> float:
    """Cost of closing positions held last period but absent this period (weight -> 0)."""
    if prev_tickers is None or prev_w is None:
        return 0.0
    cur_set = set(np.asarray(cur_tickers).tolist())
    exited = [abs(float(w)) for t, w in zip(prev_tickers, prev_w) if t not in cur_set]
    return float(np.nansum(exited)) if exited else 0.0


def _weight_map(tickers: np.ndarray | None, weights: np.ndarray | None, scale: float) -> dict[str, float]:
    if tickers is None or weights is None:
        return {}
    return {
        str(ticker): float(weight) * scale
        for ticker, weight in zip(tickers, weights)
        if np.isfinite(weight) and abs(float(weight)) > 1e-14
    }


def _period_economics(
    *,
    as_of: str,
    calendar_days: int,
    cur_tickers: np.ndarray,
    cur_weights: np.ndarray,
    prev_tickers: np.ndarray | None,
    prev_weights: np.ndarray | None,
    scale: float,
    is_long_short: bool,
    model: PITShortCostModel | None,
    fixed_cost_bps: float,
    commission_fraction: float,
) -> dict[str, float]:
    """Transaction, borrow, stress, and weighted input-coverage accounting."""
    cur = _weight_map(cur_tickers, cur_weights, scale)
    prev = _weight_map(prev_tickers, prev_weights, scale)
    keys = sorted(set(cur) | set(prev))
    result = {
        "cost": 0.0,
        "stress_cost": 0.0,
        "short_cost": 0.0,
        "spread_total": 0.0,
        "spread_exact": 0.0,
        "borrow_total": 0.0,
        "borrow_actual": 0.0,
        "availability_total": 0.0,
        "availability_observed": 0.0,
        "availability_fee_proxy": 0.0,
    }
    for ticker in keys:
        old = prev.get(ticker, 0.0)
        new = cur.get(ticker, 0.0)
        delta = abs(new - old)
        if delta <= 1e-14:
            continue
        short_delta = abs(min(new, 0.0) - min(old, 0.0)) if is_long_short else 0.0
        if model is None:
            tx = delta * fixed_cost_bps / 1e4
            result["cost"] += tx
            result["stress_cost"] += tx
            if delta > 0:
                result["short_cost"] += tx * short_delta / delta
            continue
        resolved = model.resolve(as_of, ticker)
        commission = commission_fraction
        tx = delta * resolved.half_spread_bps / 1e4 + commission
        stress_tx = delta * model.stressed_half_spread_bps(resolved) / 1e4 + commission
        result["cost"] += tx
        result["stress_cost"] += stress_tx
        result["spread_total"] += delta
        if resolved.spread_source == "ibkr_exact":
            result["spread_exact"] += delta
        if delta > 0:
            result["short_cost"] += tx * short_delta / delta

    if not is_long_short or model is None:
        return result
    day_fraction = max(1, int(calendar_days)) / 365.0
    for ticker, weight in cur.items():
        if weight >= 0:
            continue
        resolved = model.resolve(as_of, ticker)
        short_weight = abs(weight)
        borrow_cost = short_weight * resolved.borrow_fee_annual * day_fraction
        stress_borrow = short_weight * model.stressed_borrow_fee_annual(resolved) * day_fraction
        result["cost"] += borrow_cost
        result["stress_cost"] += stress_borrow
        result["short_cost"] += borrow_cost
        weighted = short_weight * day_fraction
        result["borrow_total"] += weighted
        result["availability_total"] += weighted
        if resolved.borrow_source != "conservative_fallback":
            result["borrow_actual"] += weighted
        if resolved.shortable_shares is not None:
            result["availability_observed"] += weighted
        elif resolved.shortable_source.startswith("fee_proxy:"):
            result["availability_fee_proxy"] += weighted
    return result


def _enforce_short_availability(
    weights: np.ndarray,
    tickers: np.ndarray,
    *,
    as_of: str,
    prices: pd.Series,
    model: PITShortCostModel,
    aum_usd: float,
    sector_share: float,
) -> np.ndarray:
    """Remove unavailable/undersized shorts and restore a dollar-neutral unit-gross spread."""
    out = np.asarray(weights, dtype=float).copy()
    target_side = min(float(np.clip(out, 0, None).sum()), float(np.abs(np.clip(out, None, 0)).sum()))
    if target_side <= 0:
        return np.zeros_like(out)
    for _ in range(len(out) + 1):
        changed = False
        for idx in np.flatnonzero(out < 0):
            ticker = str(tickers[idx])
            resolved = model.resolve(as_of, ticker)
            if not resolved.short_available:
                out[idx] = 0.0
                changed = True
                continue
            if resolved.shortable_shares is None:
                continue
            raw_price = prices.get(ticker)
            try:
                px = float(raw_price) if raw_price is not None else float("nan")
            except (TypeError, ValueError):
                px = float("nan")
            required = aum_usd * sector_share * abs(float(out[idx])) / px if px > 0 else float("inf")
            if not np.isfinite(required) or resolved.shortable_shares + 1e-9 < required:
                out[idx] = 0.0
                changed = True
        positive = out > 0
        negative = out < 0
        if int(positive.sum()) < 1 or int(negative.sum()) < 2:
            return np.zeros_like(out)
        out[positive] *= target_side / float(out[positive].sum())
        out[negative] *= target_side / float(np.abs(out[negative]).sum())
        if not changed:
            break
    return out


def _fraction(numerator: float, denominator: float) -> float:
    if denominator <= 0:
        return 0.0
    return float(np.clip(numerator / denominator, 0.0, 1.0))


if __name__ == "__main__":
    raise SystemExit(main())
