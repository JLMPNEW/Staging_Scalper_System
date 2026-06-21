#!/usr/bin/env python3
"""Stage 4 - net-of-(one-way)-cost static replay (DIAGNOSTIC, NOT an out-of-sample backtest).

Replays the cost-adjusted book (with its CASH line) over the trailing return panel and applies the
one-time one-way establishment cost. Still lookahead/in-sample - the official OOS net baseline is Stage 11.
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

from portfolio_layer.core.config import load_yaml  # noqa: E402
from portfolio_layer.core.contracts import fail_if_exists, read_csv, sha256_file  # noqa: E402
from portfolio_layer.core.logging_utils import configure_utc_logging  # noqa: E402
from portfolio_layer.core.paths import resolve_runtime_paths  # noqa: E402
from portfolio_layer.risk.readiness import latest_run_with  # noqa: E402


LOGGER = logging.getLogger("net_static_replay")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"


def iso_date_arg(raw: str) -> str:
    try:
        date.fromisoformat(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"must be YYYY-MM-DD, got {raw!r}") from exc
    return raw


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Stage 4 net static replay (diagnostic).")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--as-of", type=iso_date_arg, default=None)
    p.add_argument("--force", action="store_true")
    return p.parse_args()


def perf_stats(returns: pd.Series, ppy: int = 252) -> dict[str, float]:
    r = returns.dropna()
    if r.empty:
        return {"observations": 0}
    ann_ret = float((1.0 + r).prod() ** (ppy / len(r)) - 1.0)
    ann_vol = float(r.std(ddof=1) * np.sqrt(ppy))
    curve = (1.0 + r).cumprod()
    return {
        "observations": int(len(r)),
        "cumulative_return": round(float((1.0 + r).prod() - 1.0), 6),
        "annualized_return": round(ann_ret, 6),
        "annualized_vol": round(ann_vol, 6),
        "sharpe_ratio": round(float(ann_ret / ann_vol) if ann_vol > 0 else float("nan"), 4),
        "max_drawdown": round(float((curve / curve.cummax() - 1.0).min()), 6),
    }


def terminal_net_stats(gross_returns: pd.Series, *, cost_drag: float, ppy: int = 252) -> dict[str, float]:
    r = gross_returns.dropna()
    if r.empty:
        return {"observations": 0}
    gross_curve = (1.0 + r).cumprod()
    net_curve = (1.0 - cost_drag) * gross_curve
    net_cum = float(net_curve.iloc[-1] - 1.0)
    ann_ret = float((1.0 + net_cum) ** (ppy / len(r)) - 1.0)
    ann_vol = float(r.std(ddof=1) * np.sqrt(ppy))
    equity = np.concatenate(([1.0], net_curve.to_numpy(dtype=float)))
    running_max = np.maximum.accumulate(equity)
    max_dd = float((equity / running_max - 1.0).min())
    return {
        "observations": int(len(r)),
        "cumulative_return": round(net_cum, 6),
        "annualized_return": round(ann_ret, 6),
        "annualized_vol": round(ann_vol, 6),
        "sharpe_ratio": round(float(ann_ret / ann_vol) if ann_vol > 0 else float("nan"), 4),
        "max_drawdown": round(max_dd, 6),
        "one_way_cost_drag_bps": round(cost_drag * 1e4, 4),
    }


def main() -> int:
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
    costs_dir = run_dir / "costs"
    adjusted_path = costs_dir / "cost_adjusted_target_weights.csv"
    cost_manifest_path = costs_dir / "cost_manifest.json"
    summary_path = costs_dir / "cost_summary.json"
    returns_path = run_dir / "risk" / "returns_panel.csv"
    if not (adjusted_path.exists() and cost_manifest_path.exists() and summary_path.exists() and returns_path.exists()):
        LOGGER.error("Need a validated cost overlay (run 14/15) and returns_panel.csv")
        return 1
    metrics_path = costs_dir / "net_static_replay_metrics.json"
    try:
        fail_if_exists([metrics_path], force=args.force)
    except FileExistsError as exc:
        LOGGER.error("%s", exc)
        return 1

    cost_manifest = json.loads(cost_manifest_path.read_text(encoding="utf-8"))
    if cost_manifest.get("acceptance") != "PASS":
        LOGGER.error("Cost manifest acceptance is not PASS: %s", cost_manifest.get("acceptance"))
        return 1
    provenance = cost_manifest.get("provenance_sha256") or {}
    stale = []
    for name, path in (
        ("cost_adjusted_target_weights.csv", adjusted_path),
        ("cost_summary.json", summary_path),
    ):
        if provenance.get(name) != sha256_file(path):
            stale.append(name)
    if stale:
        LOGGER.error("Cost manifest is stale for: %s", stale)
        return 1
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    one_way_cost_drag = float(summary.get("one_way_cost_base_usd", 0.0)) / float(summary.get("aum_usd", 1.0))
    returns = pd.read_csv(returns_path, index_col=0)
    adjusted_rows = read_csv(adjusted_path)
    cash_values = [float(r["weight"]) for r in adjusted_rows if str(r["ticker"]).upper() == "CASH"]
    if len(cash_values) != 1:
        LOGGER.error("Expected exactly one CASH row in cost_adjusted_target_weights.csv, found %d", len(cash_values))
        return 1
    explicit_cash = cash_values[0]
    weights = {r["ticker"]: float(r["weight"]) for r in adjusted_rows
               if str(r["ticker"]).upper() != "CASH" and float(r["weight"]) > 0}
    held = [t for t in weights if t in returns.columns]
    missing_held = sorted(t for t in weights if t not in returns.columns)
    if missing_held:
        LOGGER.error("Cost-adjusted held names missing from returns_panel.csv: %s", missing_held[:20])
        return 1
    if not held:
        LOGGER.error("No held names in the return panel")
        return 1
    w = pd.Series({t: weights[t] for t in held})  # CASH (the rest) earns 0; do NOT renormalize away the cash drag
    if abs(float(w.sum()) + explicit_cash - 1.0) > 1e-6:
        LOGGER.error("Cost-adjusted weights do not close to 1: invested=%.10f cash=%.10f", float(w.sum()), explicit_cash)
        return 1

    R = returns[held].dropna(how="any")
    if R.empty:
        LOGGER.error("No complete-case replay window across %d held names", len(held))
        return 1
    gross_ret = pd.Series(R.to_numpy() @ w.reindex(R.columns).to_numpy(), index=R.index)
    gross = perf_stats(gross_ret)
    # One-time one-way establishment cost paid at t0; recompute terminal/annualized net metrics.
    net = terminal_net_stats(gross_ret, cost_drag=one_way_cost_drag)

    metrics = {
        "run_as_of": run_as_of,
        "artifact_type": "net_static_trailing_replay_diagnostic",
        "WARNING": "lookahead/in-sample diagnostic; one-way cost applied once. OOS net baseline is Stage 11.",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "invested_weight": round(float(w.sum()), 6),
        "cash_weight": round(explicit_cash, 6),
        "window": {"start": str(R.index[0]), "end": str(R.index[-1]), "rows": int(len(R))},
        "gross": gross,
        "net_of_one_way_cost": net,
        "one_way_cost_bps_of_aum": summary.get("one_way_cost_bps_of_aum"),
        "round_trip_cost_bps_of_aum_DIAGNOSTIC": summary.get("round_trip_cost_bps_of_aum_DIAGNOSTIC"),
        "inputs_sha256": {
            "cost_adjusted_target_weights.csv": sha256_file(adjusted_path),
            "cost_manifest.json": sha256_file(cost_manifest_path),
            "returns_panel.csv": sha256_file(returns_path),
        },
    }
    metrics_path.write_text(json.dumps(metrics, indent=2, sort_keys=True), encoding="utf-8")
    LOGGER.info("NET REPLAY (diag): gross cum=%.4f -> net cum=%.4f (one-way cost %.2f bps of AUM); invested=%.3f -> %s",
                gross["cumulative_return"], net["cumulative_return"], net["one_way_cost_drag_bps"], float(w.sum()), metrics_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
