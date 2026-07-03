#!/usr/bin/env python3
"""Stage 5 - AQR-only vs AQR+rotation net-of-cost ablation (DIAGNOSTIC, WARN-only).

Single-snapshot trailing replay (lookahead/in-sample by construction) that tilts the Stage 3 book by the
rotation multipliers and compares net-of-cost trailing performance, charging the *incremental* turnover
(AQR -> tilted) at the Stage 4 per-name spread + flat commission. This NEVER gates Stage 5 acceptance and
NEVER promotes rotation - the true OOS promotion test is Stage 11. Writes only under rotation/.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime, timezone
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from portfolio_layer.core.config import cfg_get, load_yaml  # noqa: E402
from portfolio_layer.core.contracts import fail_if_exists, read_csv, sha256_file, write_csv  # noqa: E402
from portfolio_layer.core.logging_utils import configure_utc_logging  # noqa: E402
from portfolio_layer.core.paths import resolve_runtime_paths  # noqa: E402
from portfolio_layer.risk.liquidity import finite_float  # noqa: E402
from portfolio_layer.risk.readiness import latest_run_with  # noqa: E402
from portfolio_layer.rotation.rotation_book import apply_rotation_tilt  # noqa: E402


LOGGER = logging.getLogger("rotation_ablation")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
TRADING_DAYS = 252


def iso_date_arg(raw: str) -> str:
    try:
        date.fromisoformat(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"must be YYYY-MM-DD, got {raw!r}") from exc
    return raw


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Stage 5 rotation ablation replay (diagnostic).")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--as-of", type=iso_date_arg, default=None)
    p.add_argument("--force", action="store_true")
    return p.parse_args()


def perf_stats(returns: pd.Series, cost_frac: float) -> dict:
    r = returns.dropna()
    if r.empty:
        return {"observations": 0}
    gross_cum = float((1.0 + r).prod() - 1.0)
    net_cum = (1.0 + gross_cum) * (1.0 - cost_frac) - 1.0
    ann_ret = float((1.0 + net_cum) ** (TRADING_DAYS / len(r)) - 1.0)
    ann_vol = float(r.std(ddof=1) * np.sqrt(TRADING_DAYS))
    return {
        "observations": int(len(r)),
        "gross_cumulative_return": round(gross_cum, 6),
        "net_cumulative_return": round(net_cum, 6),
        "net_annualized_return": round(ann_ret, 6),
        "annualized_vol": round(ann_vol, 6),
        "net_sharpe": round(float(ann_ret / ann_vol) if ann_vol > 0 else float("nan"), 4),
        "cost_drag_bps": round(cost_frac * 1e4, 4),
    }


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _verify_manifest_hash(manifest: dict, label: str, path: Path, errors: list[str]) -> None:
    expected = (manifest.get("provenance_sha256") or {}).get(label)
    if expected is None:
        errors.append(f"{label}:missing_manifest_hash")
    elif not path.exists():
        errors.append(f"{label}:missing_file")
    elif sha256_file(path) != expected:
        errors.append(f"{label}:manifest_hash_mismatch")


def _load_half_spreads(
    *,
    run_dir: Path,
    cost_report_path: Path,
    cost_summary: dict,
    default_bps: float,
) -> dict[str, float]:
    spreads: dict[str, float] = {}
    for row in read_csv(cost_report_path):
        ticker = str(row.get("ticker", "")).strip().upper()
        if ticker:
            spreads[ticker] = finite_float(
                row.get("half_spread_bps_used"),
                name=f"cost_report:{ticker}.half_spread_bps_used",
            )

    if str(cost_summary.get("spread_mode", "")).strip() == "liquidity_panel":
        fallback = finite_float(cost_summary.get("fallback_half_spread_bps", default_bps), name="fallback_half_spread_bps")
        snapshot_path = run_dir / "risk" / "spread_snapshot.csv"
        if snapshot_path.exists():
            for row in read_csv(snapshot_path):
                ticker = str(row.get("ticker", "")).strip().upper()
                if not ticker or ticker in spreads:
                    continue
                try:
                    spreads[ticker] = finite_float(
                        row.get("median_half_spread_bps"),
                        name=f"spread_snapshot:{ticker}.median_half_spread_bps",
                    )
                except ValueError:
                    spreads[ticker] = fallback
    return spreads


def _book_establishment_cost(
    weights: dict[str, float],
    *,
    aum: float,
    commission_base: float,
    half_spread_by_ticker: dict[str, float],
    fallback_half_spread_bps: float,
    min_trade_notional: float,
) -> tuple[float, int]:
    """One-way cost to establish exactly this replayed book from cash."""
    total = 0.0
    n_orders = 0
    for ticker, weight in weights.items():
        notional = float(weight) * aum
        if notional < min_trade_notional:
            continue
        half_spread = half_spread_by_ticker.get(ticker, fallback_half_spread_bps)
        total += commission_base + half_spread / 1e4 * notional
        n_orders += 1
    return total, n_orders


def main() -> int:  # noqa: C901
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    paths = resolve_runtime_paths(config, config_path)
    runs_root = paths.output_dir / "runs"
    run_as_of = args.as_of or latest_run_with(runs_root, "manifest.json")
    if not run_as_of:
        LOGGER.error("No run found under %s", runs_root)
        return 1
    run_dir = runs_root / run_as_of
    rotation_dir = run_dir / "rotation"
    target_path = run_dir / "optimizer" / "target_weights.csv"
    sector_path = rotation_dir / "sector_rotation.csv"
    rot_manifest_path = rotation_dir / "rotation_manifest.json"
    returns_path = run_dir / "risk" / "returns_panel.csv"
    cost_report_path = run_dir / "costs" / "cost_report.csv"
    cost_summary_path = run_dir / "costs" / "cost_summary.json"
    cost_manifest_path = run_dir / "costs" / "cost_manifest.json"
    optimizer_manifest_path = run_dir / "optimizer" / "optimizer_manifest.json"
    scores_path = run_dir / "stocks_scores.csv"
    for required in (
        target_path, sector_path, rot_manifest_path, returns_path, scores_path,
        cost_report_path, cost_summary_path, cost_manifest_path, optimizer_manifest_path,
    ):
        if not required.exists():
            LOGGER.error("Run 17/18 + sealed Stage 2/3/4 first; missing %s", required)
            return 1
    metrics_path = rotation_dir / "rotation_ablation_metrics.json"
    weights_path = rotation_dir / "rotation_ablation_weights.csv"
    try:
        fail_if_exists([metrics_path, weights_path], force=args.force)
    except FileExistsError as exc:
        LOGGER.error("%s", exc)
        return 1

    rot_manifest = _load_json(rot_manifest_path)
    if rot_manifest.get("acceptance") != "PASS":
        LOGGER.error("Rotation manifest acceptance is not PASS; run 18 first")
        return 1
    cost_manifest = _load_json(cost_manifest_path)
    optimizer_manifest = _load_json(optimizer_manifest_path)
    seal_errors: list[str] = []
    if cost_manifest.get("acceptance") != "PASS":
        seal_errors.append(f"cost_manifest_acceptance={cost_manifest.get('acceptance')}")
    if optimizer_manifest.get("acceptance") != "PASS":
        seal_errors.append(f"optimizer_manifest_acceptance={optimizer_manifest.get('acceptance')}")
    _verify_manifest_hash(rot_manifest, "sector_rotation.csv", sector_path, seal_errors)
    _verify_manifest_hash(rot_manifest, "returns_panel.csv", returns_path, seal_errors)
    _verify_manifest_hash(
        rot_manifest,
        "rotation_signals_meta.json",
        rotation_dir / "rotation_signals_meta.json",
        seal_errors,
    )
    _verify_manifest_hash(cost_manifest, "cost_report.csv", cost_report_path, seal_errors)
    _verify_manifest_hash(cost_manifest, "cost_summary.json", cost_summary_path, seal_errors)
    _verify_manifest_hash(cost_manifest, "target_weights.csv", target_path, seal_errors)
    _verify_manifest_hash(cost_manifest, "optimizer_manifest.json", optimizer_manifest_path, seal_errors)
    if "spread_snapshot.csv" in (cost_manifest.get("provenance_sha256") or {}):
        _verify_manifest_hash(cost_manifest, "spread_snapshot.csv", run_dir / "risk" / "spread_snapshot.csv", seal_errors)
    if seal_errors:
        LOGGER.error("Stage 4/optimizer/rotation seal mismatch: %s", seal_errors[:8])
        return 1

    gross = float(cfg_get(config, "optimizer.gross_exposure", 1.0))
    max_w = float(cfg_get(config, "optimizer.max_weight_per_name", 0.05))
    half_spread_default = finite_float(cfg_get(config, "transaction_costs.half_spread_bps_default", 5.0),
                                       name="half_spread_bps_default")
    comm_base = finite_float(cfg_get(config, "transaction_costs.commission_per_order.base", 1.125),
                             name="commission_per_order.base")
    min_trade_notional = finite_float(
        cfg_get(config, "rotation.ablation_min_trade_notional_usd", 1.0),
        name="rotation.ablation_min_trade_notional_usd",
    )
    if min_trade_notional < 0.0:
        LOGGER.error("rotation.ablation_min_trade_notional_usd must be non-negative")
        return 1
    tol = finite_float(cfg_get(config, "rotation.net_sharpe_degradation_tolerance", 0.0),
                       name="rotation.net_sharpe_degradation_tolerance")
    window = int(finite_float(cfg_get(config, "rotation.ablation_window_days", 126), name="rotation.ablation_window_days"))
    if tol < 0.0:
        LOGGER.error("rotation.net_sharpe_degradation_tolerance must be non-negative")
        return 1
    if window <= 1:
        LOGGER.error("rotation.ablation_window_days must be > 1")
        return 1

    summary = _load_json(cost_summary_path)
    aum = finite_float(summary.get("aum_usd"), name="cost_summary.aum_usd")
    if aum <= 0:
        LOGGER.error("AUM unavailable for the ablation cost model")
        return 1

    aqr = {str(r["ticker"]).strip().upper(): finite_float(r["weight"], name="weight")
           for r in read_csv(target_path) if finite_float(r["weight"], name="weight") > 0}
    pipe_by_ticker = {str(r["ticker"]).strip().upper(): str(r.get("source_pipeline", "")).strip()
                      for r in read_csv(scores_path)}
    mult_by_pipe = {str(r["source_pipeline"]).strip(): finite_float(r["rotation_multiplier"], name="mult")
                    for r in read_csv(sector_path)}
    half_spread_by_ticker = _load_half_spreads(
        run_dir=run_dir,
        cost_report_path=cost_report_path,
        cost_summary=summary,
        default_bps=half_spread_default,
    )
    fallback_half_spread = finite_float(
        summary.get("fallback_half_spread_bps", half_spread_default),
        name="cost_summary.fallback_half_spread_bps",
    )
    spread_mode = str(summary.get("spread_mode", "config_default")).strip()
    allow_spread_fallback = bool(cfg_get(config, "liquidity_panel.allow_fallback_to_default", True))

    tilted = apply_rotation_tilt(aqr, pipe_by_ticker, mult_by_pipe, gross=gross, max_weight=max_w)

    # Incremental turnover cost (AQR -> tilted): flat commission per changed name + per-name spread on |dnotional|.
    incr_cost = 0.0
    rows = []
    missing_spread = []
    fallback_spread_tickers = []
    for t in sorted(set(aqr) | set(tilted)):
        wa, wt = aqr.get(t, 0.0), tilted.get(t, 0.0)
        dnotional = abs(wt - wa) * aum
        hs = half_spread_by_ticker.get(t)
        if hs is None:
            if spread_mode == "liquidity_panel" and not allow_spread_fallback:
                missing_spread.append(t)
            else:
                fallback_spread_tickers.append(t)
            hs = fallback_half_spread if spread_mode == "liquidity_panel" else half_spread_default
        trade_executed = dnotional >= min_trade_notional
        name_cost = (comm_base + hs / 1e4 * dnotional) if trade_executed else 0.0
        incr_cost += name_cost
        rows.append({"ticker": t, "aqr_weight": round(wa, 10), "rotation_weight": round(wt, 10),
                     "delta_weight": round(wt - wa, 10), "source_pipeline": pipe_by_ticker.get(t, ""),
                     "rotation_multiplier": round(mult_by_pipe.get(pipe_by_ticker.get(t, ""), 1.0), 6),
                     "half_spread_bps_used": round(hs, 6),
                     "incremental_trade_notional": round(dnotional, 4),
                     "incremental_trade_executed": int(trade_executed),
                     "incremental_cost_usd": round(name_cost, 4)})
    if missing_spread:
        LOGGER.error("Missing Stage 4/spread snapshot half-spread for tilted names: %s", missing_spread[:20])
        return 1
    turnover = sum(abs(tilted.get(t, 0.0) - aqr.get(t, 0.0)) for t in set(aqr) | set(tilted))

    aqr_establishment_cost, aqr_establishment_orders = _book_establishment_cost(
        aqr,
        aum=aum,
        commission_base=comm_base,
        half_spread_by_ticker=half_spread_by_ticker,
        fallback_half_spread_bps=(fallback_half_spread if spread_mode == "liquidity_panel" else half_spread_default),
        min_trade_notional=min_trade_notional,
    )
    aqr_cost_frac = aqr_establishment_cost / aum
    incr_cost_frac = incr_cost / aum

    returns = pd.read_csv(returns_path, index_col=0)
    returns.columns = [str(c).strip().upper() for c in returns.columns]
    if window > 0 and len(returns) > window:
        returns = returns.iloc[-window:]
    required_return_tickers = sorted(t for t in set(aqr) | set(tilted) if max(aqr.get(t, 0.0), tilted.get(t, 0.0)) > 0.0)
    missing_returns = sorted(set(required_return_tickers) - set(returns.columns))
    if missing_returns:
        LOGGER.error("Missing return columns for positive-weight ablation tickers: %s", missing_returns[:20])
        return 1
    missing_cells = returns[required_return_tickers].isna().sum()
    bad_missing = missing_cells[missing_cells > 0]
    if not bad_missing.empty:
        LOGGER.error("Missing return observations in ablation window: %s", bad_missing.head(20).to_dict())
        return 1

    def book_return(weights: dict[str, float]) -> pd.Series:
        held = [t for t in weights if t in returns.columns and weights[t] > 0]
        R = returns[held]
        if R.empty:
            return pd.Series(dtype=float)
        w = pd.Series({t: weights[t] for t in held}).reindex(R.columns)
        return pd.Series(R.to_numpy() @ w.to_numpy(), index=R.index)

    aqr_perf = perf_stats(book_return(aqr), aqr_cost_frac)
    rot_perf = perf_stats(book_return(tilted), aqr_cost_frac + incr_cost_frac)

    aqr_sharpe = aqr_perf.get("net_sharpe", float("nan"))
    rot_sharpe = rot_perf.get("net_sharpe", float("nan"))
    degraded = (rot_sharpe == rot_sharpe and aqr_sharpe == aqr_sharpe and rot_sharpe < aqr_sharpe - tol)
    ablation_status = "WARN" if degraded else "PASS"

    write_csv(weights_path, ["ticker", "aqr_weight", "rotation_weight", "delta_weight", "source_pipeline",
                             "rotation_multiplier", "half_spread_bps_used", "incremental_trade_notional",
                             "incremental_trade_executed", "incremental_cost_usd"],
              sorted(rows, key=lambda r: r["ticker"]))
    metrics = {
        "run_as_of": run_as_of, "stage": "stage5_rotation_ablation",
        "artifact_type": "net_of_cost_trailing_ablation_diagnostic",
        "WARNING": "lookahead/in-sample single-snapshot diagnostic; WARN-only, never gates/promotes. OOS test is Stage 11.",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "window": {"start": str(returns.index[0]), "end": str(returns.index[-1]), "rows": int(len(returns))},
        "aum_usd": aum,
        "turnover_aqr_to_rotation": round(float(turnover), 6),
        "aqr_establishment_cost_usd": round(aqr_establishment_cost, 4),
        "aqr_establishment_cost_bps_of_aum": round(aqr_cost_frac * 1e4, 4),
        "aqr_establishment_orders": aqr_establishment_orders,
        "aqr_establishment_cost_note": "recomputed from Stage 3 raw target_weights.csv, not borrowed from Stage 4 adjusted book",
        "incremental_rotation_cost_usd": round(incr_cost, 4),
        "incremental_rotation_cost_bps_of_aum": round(incr_cost_frac * 1e4, 4),
        "fallback_spread_tickers": sorted(set(fallback_spread_tickers)),
        "ablation_min_trade_notional_usd": min_trade_notional,
        "aqr_only": aqr_perf,
        "aqr_plus_rotation": rot_perf,
        "net_sharpe_delta": round(rot_sharpe - aqr_sharpe, 4) if (rot_sharpe == rot_sharpe and aqr_sharpe == aqr_sharpe) else None,
        "ablation_status": ablation_status,
        "net_sharpe_degradation_tolerance": tol,
        "inputs_sha256": {
            "target_weights.csv": sha256_file(target_path),
            "sector_rotation.csv": sha256_file(sector_path),
            "returns_panel.csv": sha256_file(returns_path),
            "rotation_manifest.json": sha256_file(rot_manifest_path),
            "cost_manifest.json": sha256_file(cost_manifest_path),
            "cost_report.csv": sha256_file(cost_report_path),
            "cost_summary.json": sha256_file(cost_summary_path),
        },
    }
    metrics_path.write_text(json.dumps(metrics, indent=2, sort_keys=True), encoding="utf-8")
    LOGGER.info("[%s] ablation: AQR net Sharpe=%.3f vs AQR+rot=%.3f (delta=%s); turnover=%.3f, incr cost=%.2f bps -> %s",
                ablation_status, aqr_sharpe, rot_sharpe, metrics["net_sharpe_delta"],
                turnover, metrics["incremental_rotation_cost_bps_of_aum"], metrics_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
