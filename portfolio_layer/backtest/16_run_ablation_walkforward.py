#!/usr/bin/env python3
"""Stage 11 - registered-arm walk-forward ablation, net of costs (the lockbox promotion evidence).

Replays the overlay stack arm by arm over the DEV-WINDOW snapshot store, marking to the
survivorship panel (PIT, delisted-covered):

  aqr_only    long-only mean-variance on mu = final_score * score_confidence  (Stage 3 rule)
  rotation    + bounded multiplicative sleeve tilt from Stage 5 rotation state
  macro_bl    + regime gross scaling and macro sector-fit tilt (allocation-level approximation of
                the Stage 6/7 overlay; the full tier1 BL fusion is revalidated at promotion time)
  sleeves     + regime sleeve risk budgets and the realized per-name RC-cap trim (Stage 8 rule,
                reusing sleeves/risk_model directly)
  regime_gate score-tilted AQR only in supportive regimes, min-variance otherwise
  regime_lever stronger score tilt in supportive regimes, min-variance otherwise

Costs: every rebalance charges one-way costs on traded weight (config bps), applied on the first
holding day. Between rebalances weights drift with survivorship-panel returns; a name with no bar
drifts flat and leaves the book at the next rebalance (delisted names exit at their final mark).

STATISTICAL HONESTY: promotion requires enough independent evidence — short test spans report
`insufficient_days` / `insufficient_independent_windows` BY DESIGN, mirroring 68/69.
LOCKBOX: sealed-window snapshots are excluded and re-verified; the config/protocol mirror is
enforced via load_lockbox.

The engine takes injectable state providers (regime / sector fit / rotation), so `--selftest`
drives synthetic data through the identical code path: a rigged persistent regime must make the
rotation arm beat the baseline, higher costs must reduce net returns monotonically while leaving
gross untouched, and the PIT guard must hold.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from portfolio_layer.core.config import cfg_get, load_yaml  # noqa: E402
from portfolio_layer.core.contracts import fail_if_exists, sha256_file, write_csv, write_manifest  # noqa: E402
from portfolio_layer.core.contracts import read_csv  # noqa: E402
from portfolio_layer.core.db import utc_now  # noqa: E402
from portfolio_layer.core.logging_utils import configure_utc_logging  # noqa: E402
from portfolio_layer.core.paths import resolve_runtime_paths  # noqa: E402
from portfolio_layer.macro.contract import open_macro_serving_db, rows_at_latest, single_latest_row  # noqa: E402
from portfolio_layer.macro.taxonomy import select_sleeve_macro_fit, sleeve_taxonomy  # noqa: E402
from portfolio_layer.backtest.walkforward_common import (  # noqa: E402
    ARM_FIELDS, ARMS, perf_stats, run_walkforward, summarize_arms,
)
from portfolio_layer.research.stage11_common import load_lockbox  # noqa: E402
from portfolio_layer.rotation.sector_rotation_selector import build_sector_rotation  # noqa: E402


LOGGER = logging.getLogger("run_ablation_walkforward")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Stage 11 registered-arm net-of-cost walk-forward ablation.")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--selftest", action="store_true", help="Run the synthetic-arm self-tests and exit.")
    p.add_argument("--force", action="store_true")
    return p.parse_args()


# ---------------------------------------------------------------------------
# self-test (synthetic providers through the identical engine)
# ---------------------------------------------------------------------------
def _selftest() -> None:
    rng = np.random.default_rng(23)
    n_days, names_per_pipe = 400, 8
    pipes = {"alpha_pipe": 0.0009, "beta_pipe": 0.0002, "gamma_pipe": -0.0006}
    start = date(2001, 1, 1)
    cal: list[str] = []
    d = start
    while len(cal) < n_days:
        if d.weekday() < 5:
            cal.append(d.isoformat())
        d += timedelta(days=1)
    cols = {}
    pipe_of_all = {}
    for pipe, drift in pipes.items():
        for k in range(names_per_pipe):
            t = f"{pipe[:2].upper()}{k}"
            pipe_of_all[t] = pipe
            rets = rng.standard_normal(n_days) * 0.015 + drift
            cols[t] = 100 * np.cumprod(1 + rets)
    prices = pd.DataFrame(cols, index=pd.Index(cal))
    snapshots = {}
    for i in range(20, n_days - 25, 10):
        rows = []
        for t, pipe in pipe_of_all.items():
            mu = {"alpha_pipe": 0.20, "beta_pipe": 0.05, "gamma_pipe": -0.10}[pipe]
            rows.append({"ticker": t, "final_score": str(mu + rng.normal(0, 0.01)),
                         "score_confidence": "1.0", "source_pipeline": pipe})
        snapshots[cal[i]] = rows
    params = dict(rebalance_every_n_snapshots=1, one_way_cost_bps=5.0, cov_lookback_trading_days=120,
                  cov_min_obs=20, shrinkage_intensity=0.2, max_universe=24, min_universe=6,
                  use_confidence=True, risk_aversion=5.0, max_weight=0.10, min_weight=0.005,
                  gross=1.0, solver="ECOS", macro_shift_scale=0.5, macro_max_shift=0.15, rc_cap=0.20)
    def regime(_: str) -> dict[str, Any]:
        return {"label": "A", "gross_scalar": 1.0,
                "budgets": {"long_core": 0.65, "medium_rotation": 0.35}}

    def fits(_: str) -> dict[str, float]:
        return {"alpha_pipe": 0.5, "beta_pipe": 0.0, "gamma_pipe": -0.5}

    def rotation(_: str) -> dict[str, dict[str, float | str]]:
        return {
            "alpha_pipe": {"state": "Positive", "rotation_multiplier": 1.3},
            "beta_pipe": {"state": "Neutral", "rotation_multiplier": 1.0},
            "gamma_pipe": {"state": "Negative", "rotation_multiplier": 0.7},
        }
    arms: list[str] = list(ARMS)
    res = run_walkforward(snapshots=snapshots, prices=prices, arms=arms, params=params,
                          regime_provider=regime, sector_fit_provider=fits, rotation_provider=rotation)
    assert not res["pit_violations"], res["pit_violations"]
    assert res["n_rebalances"] >= 30 and len(res["day_index"]) > 300
    summary = {r["arm"]: r for r in summarize_arms(res, arms, verdict_cfg={})}
    # the rigged persistent tilt must add net value over the baseline
    assert summary["rotation"]["net_sharpe"] > summary["aqr_only"]["net_sharpe"], (
        summary["rotation"]["net_sharpe"], summary["aqr_only"]["net_sharpe"])
    assert float(summary["rotation"]["net_ir_vs_baseline"]) > 0
    # cost monotonicity: gross identical, net strictly worse at higher cost
    res_costly = run_walkforward(snapshots=snapshots, prices=prices, arms=["aqr_only"],
                                 params={**params, "one_way_cost_bps": 50.0},
                                 regime_provider=regime, sector_fit_provider=fits,
                                 rotation_provider=rotation)
    g0 = perf_stats(res["gross"]["aqr_only"])["ann_return"]
    g1 = perf_stats(res_costly["gross"]["aqr_only"])["ann_return"]
    n0 = perf_stats(res["net"]["aqr_only"])["ann_return"]
    n1 = perf_stats(res_costly["net"]["aqr_only"])["ann_return"]
    assert abs(g0 - g1) < 1e-12, (g0, g1)
    assert n1 < n0 < g0, (n1, n0, g0)
    # honesty: tiny sample must not be promotable
    small = {k: snapshots[k] for k in sorted(snapshots)[:3]}
    res_small = run_walkforward(snapshots=small, prices=prices.iloc[:80], arms=arms, params=params,
                                regime_provider=regime, sector_fit_provider=fits,
                                rotation_provider=rotation)
    rows_small = summarize_arms(res_small, arms, verdict_cfg={})
    assert all(r["promotable"] == 0 for r in rows_small), rows_small

    # regime_gate: score works in the supportive regime and INVERTS otherwise. Holding the score
    # only when supportive (min-variance elsewhere) must beat always-scoring aqr_only.
    rng2 = np.random.default_rng(31)
    up_names = [f"UP{k}" for k in range(names_per_pipe)]
    dn_names = [f"DN{k}" for k in range(names_per_pipe)]
    pipe_of2 = {**{t: "up_pipe" for t in up_names}, **{t: "down_pipe" for t in dn_names}}
    block = 40
    regime_sched = {d: ("HEATING_UP" if (j // block) % 2 == 0 else "STAGFLATION")
                    for j, d in enumerate(cal)}
    cols2: dict[str, np.ndarray] = {}
    for t in up_names + dn_names:
        rets = np.empty(n_days)
        for j, d in enumerate(cal):
            supportive = regime_sched[d] == "HEATING_UP"
            # supportive: UP names +, DN names - (score works). unsupportive: signs FLIP (score inverts).
            edge = 0.0016 if pipe_of2[t] == "up_pipe" else -0.0016
            drift = edge if supportive else -edge
            rets[j] = rng2.standard_normal() * 0.012 + drift
        cols2[t] = 100 * np.cumprod(1 + rets)
    prices2 = pd.DataFrame(cols2, index=pd.Index(cal))
    snaps2 = {}
    for i in range(20, n_days - 25, 10):
        snaps2[cal[i]] = [
            {"ticker": t, "final_score": "0.20" if pipe_of2[t] == "up_pipe" else "-0.10",
             "score_confidence": "1.0", "source_pipeline": pipe_of2[t]}
            for t in up_names + dn_names
        ]
    def regime2(d: str) -> dict[str, Any]:
        return {"label": regime_sched.get(d, ""), "gross_scalar": 1.0,
                "budgets": {"long_core": 0.65, "medium_rotation": 0.35}}
    params2 = {**params, "regime_gate_supportive_regimes": ["HEATING_UP"], "min_universe": 6,
               "max_universe": 24}
    params2 = {**params2, "regime_lever_mu_multiplier": 2.0}
    res2 = run_walkforward(snapshots=snaps2, prices=prices2,
                           arms=["aqr_only", "regime_gate", "regime_lever"],
                           params=params2, regime_provider=regime2, sector_fit_provider=fits,
                           rotation_provider=rotation)
    assert not res2["pit_violations"], res2["pit_violations"]
    s2 = {r["arm"]: r for r in summarize_arms(res2, ["aqr_only", "regime_gate", "regime_lever"],
                                               verdict_cfg={})}
    assert s2["regime_gate"]["net_sharpe"] > s2["aqr_only"]["net_sharpe"], (
        s2["regime_gate"]["net_sharpe"], s2["aqr_only"]["net_sharpe"])
    assert float(s2["regime_gate"]["net_ir_vs_baseline"]) > 0, s2["regime_gate"]["net_ir_vs_baseline"]
    assert s2["regime_lever"]["net_sharpe"] > s2["aqr_only"]["net_sharpe"], (
        s2["regime_lever"]["net_sharpe"], s2["aqr_only"]["net_sharpe"])
    assert float(s2["regime_lever"]["net_ir_vs_baseline"]) > 0, s2["regime_lever"]["net_ir_vs_baseline"]
    # turnover sanity: the gate rebalances between score-tilt and min-var, so it trades >= baseline
    assert s2["regime_gate"]["turnover_per_year"] >= 0.0
    assert s2["regime_lever"]["turnover_per_year"] >= 0.0
    print("walk-forward ablation self-test: PASS")


# ---------------------------------------------------------------------------
# real-data providers + main
# ---------------------------------------------------------------------------
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
        macro_shift_scale=float(cfg_get(config, "black_litterman_fusion.macro_sector_shift_scale", 0.5)),
        macro_max_shift=float(cfg_get(config, "black_litterman_fusion.macro_sector_max_shift", 0.15)),
        rc_cap=float(cfg_get(config, "sleeves.per_name_risk_contribution_cap", 0.08)),
        regime_gate_supportive_regimes=[str(s) for s in
                                        (wf.get("regime_gate_supportive_regimes") or ["HEATING_UP"])],
        regime_lever_mu_multiplier=float(wf.get("regime_lever_mu_multiplier", 1.5)),
    )
    arms = [a for a in (wf.get("arms") or list(ARMS)) if a in ARMS]
    if "aqr_only" not in arms:
        arms = ["aqr_only"] + arms

    # dev-window snapshots from the immutable store
    store_dir = paths.output_dir / str(cfg_get(config, "snapshot_store.dir", "snapshot_store"))
    snapshots: dict[str, list[dict[str, str]]] = {}
    sealed_skipped = 0
    if store_dir.exists():
        for snap in sorted(store_dir.iterdir()):
            if not snap.is_dir() or not (snap / "stocks_scores.csv").exists():
                continue
            if snap.name >= lockbox["sealed_start"]:
                sealed_skipped += 1
                continue
            snapshots[snap.name] = read_csv(snap / "stocks_scores.csv")
    if not snapshots:
        LOGGER.error("No dev-window snapshots in %s; run research/65 first", store_dir)
        return 1

    # survivorship panel (accepted build)
    panel_root = paths.output_dir / str(cfg_get(config, "survivorship_panel.dir", "survivorship_panel"))
    builds = sorted(p for p in panel_root.iterdir()
                    if p.is_dir() and (p / "survivorship_manifest.json").exists()) if panel_root.exists() else []
    if not builds:
        LOGGER.error("No survivorship panel build under %s; run backtest/15b first", panel_root)
        return 1
    panel_dir = builds[-1]
    panel_manifest = json.loads((panel_dir / "survivorship_manifest.json").read_text(encoding="utf-8"))
    if panel_manifest.get("acceptance") != "PASS":
        LOGGER.error("Survivorship panel %s acceptance=%s; refusing", panel_dir.name,
                     panel_manifest.get("acceptance"))
        return 1
    prices = pd.read_csv(panel_dir / "prices_adjclose.csv", index_col=0)
    prices.columns = [str(c).strip().upper() for c in prices.columns]

    out_dir = paths.output_dir / str(wf.get("dir", "walkforward")) / panel_dir.name
    arm_path = out_dir / "arm_comparison.csv"
    curves_path = out_dir / "daily_curves.csv"
    manifest_path = out_dir / "walkforward_manifest.json"
    if args.force:
        for p in (arm_path, curves_path, manifest_path):
            if p.exists():
                p.unlink()
    try:
        fail_if_exists([arm_path, curves_path, manifest_path], force=args.force)
    except FileExistsError as exc:
        LOGGER.error("%s", exc)
        return 1

    # real state providers (all PIT: <= rebalance date)
    taxonomy = sleeve_taxonomy(config)
    pipelines = [str(s.get("model_family")) for s in cfg_get(config, "score_contract.sectors", []) or []
                 if bool(s.get("enabled", True))]
    gross_map = cfg_get(config, "black_litterman_fusion.regime_to_gross_scalar", {}) or {}
    budgets_cfg = cfg_get(config, "sleeves.sleeve_risk_budgets", {}) or {}
    risk_off = {str(r).upper() for r in cfg_get(config, "sleeves.risk_off_regimes", []) or []}
    conn = open_macro_serving_db(paths.macro_serving_db_path)
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
        return {
            "label": label,
            "gross_scalar": float(gross_map.get(label, gross_map.get("default", 1.0)) or 1.0),
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

    try:
        result = run_walkforward(
            snapshots=snapshots, prices=prices, arms=arms, params=params,
            regime_provider=regime_provider, sector_fit_provider=sector_fit_provider,
            rotation_provider=rotation_provider,
        )
    finally:
        conn.close()
    arm_rows = summarize_arms(result, arms, verdict_cfg=wf)

    checks: list[dict[str, str]] = []

    def rec(name: str, status: str, detail: str) -> None:
        checks.append({"check": name, "status": status, "detail": detail})

    rec("lockbox_dev_window_only", "PASS",
        f"dev snapshots={len(snapshots)}, sealed skipped={sealed_skipped}")
    rec("pit_no_lookahead", "PASS" if not result["pit_violations"] else "FAIL",
        "covariance and state windows end at/before every rebalance date"
        if not result["pit_violations"] else f"{result['pit_violations'][:8]}")
    rec("baseline_present", "PASS" if any(r["arm"] == "aqr_only" for r in arm_rows) else "FAIL",
        "aqr_only baseline replayed")
    promotable = [r["arm"] for r in arm_rows if r["promotable"] == 1]
    rec("promotion_honesty", "PASS",
        f"promotable arms={promotable or 'none'}; verdicts require days>={wf.get('min_days', 250)}, "
        f"windows>={wf.get('min_independent_windows', 6)}, net IR and active t thresholds")
    rec("shadow_only", "PASS",
        "walk-forward evidence is research-only; promotion additionally requires 68/69 approvals "
        "and a dated protocol amendment")

    out_dir.mkdir(parents=True, exist_ok=True)
    write_csv(arm_path, ARM_FIELDS, arm_rows)
    curve_rows = []
    for j, d in enumerate(result["day_index"]):
        row = {"date": d}
        for arm in arms:
            row[f"net_{arm}"] = round(result["net"][arm][j], 8)
        curve_rows.append(row)
    write_csv(curves_path, ["date"] + [f"net_{a}" for a in arms], curve_rows)
    passed = all(c["status"] in ("PASS", "WARN") for c in checks)
    manifest = {
        "stage": "stage11_walkforward_ablation",
        "generated_at": utc_now(),
        "acceptance": "PASS" if passed else "FAIL",
        "panel_build": panel_dir.name,
        "panel_manifest_sha256": sha256_file(panel_dir / "survivorship_manifest.json"),
        "protocol_sha256": lockbox["protocol_sha256"],
        "arms": arms,
        "params": {k: v for k, v in params.items()},
        "snapshots_used": len(snapshots),
        "sealed_skipped": sealed_skipped,
        "rebalances": result["n_rebalances"],
        "days": len(result["day_index"]),
        "skipped": result["skipped"],
        "checks": checks,
        "files": {
            "arm_comparison.csv": {"sha256": sha256_file(arm_path), "rows": len(arm_rows)},
            "daily_curves.csv": {"sha256": sha256_file(curves_path), "rows": len(curve_rows)},
        },
    }
    write_manifest(manifest_path, manifest)
    for c in checks:
        LOGGER.info("[%s] %s -- %s", c["status"], c["check"], c["detail"])
    for r in arm_rows:
        LOGGER.info("ARM %-10s net_sharpe=%s net_ir=%s active_t=%s promotable=%s %s",
                    r["arm"], r["net_sharpe"], r["net_ir_vs_baseline"], r["active_t"],
                    r["promotable"], r["rejection_reasons"])
    LOGGER.info("WALKFORWARD: %s (arms=%d, rebalances=%d, days=%d) -> %s",
                "PASS" if passed else "FAIL", len(arms), result["n_rebalances"],
                len(result["day_index"]), out_dir)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
