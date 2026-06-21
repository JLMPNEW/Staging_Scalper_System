#!/usr/bin/env python3
"""Stage 3 - STATIC TRAILING REPLAY (diagnostic, NOT an out-of-sample backtest).

Applies the single-snapshot AQR-only target weights to the trailing return panel. Because the scores
used information through the as-of date, replaying them over *past* returns is lookahead - so this is a
sanity/diagnostic artifact only. A true walk-forward, out-of-sample baseline belongs in Stage 11 once a
history of score snapshots exists.
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
from portfolio_layer.core.contracts import fail_if_exists, read_csv, sha256_file, write_csv, write_manifest  # noqa: E402
from portfolio_layer.core.logging_utils import configure_utc_logging  # noqa: E402
from portfolio_layer.core.paths import resolve_runtime_paths  # noqa: E402
from portfolio_layer.risk.readiness import latest_run_with  # noqa: E402


LOGGER = logging.getLogger("static_baseline_replay")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"


def iso_date_arg(raw: str) -> str:
    try:
        date.fromisoformat(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"must be YYYY-MM-DD, got {raw!r}") from exc
    return raw


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Stage 3 static trailing replay (diagnostic).")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--as-of", type=iso_date_arg, default=None)
    p.add_argument("--force", action="store_true")
    return p.parse_args()


def perf_stats(returns: pd.Series, periods_per_year: int = 252) -> dict[str, float]:
    r = returns.dropna()
    if r.empty:
        return {"observations": 0}
    cum = float((1.0 + r).prod() - 1.0)
    ann_ret = float((1.0 + r).prod() ** (periods_per_year / len(r)) - 1.0)
    ann_vol = float(r.std(ddof=1) * np.sqrt(periods_per_year))
    sharpe = float(ann_ret / ann_vol) if ann_vol > 0 else float("nan")
    curve = (1.0 + r).cumprod()
    max_dd = float((curve / curve.cummax() - 1.0).min())
    return {
        "observations": int(len(r)),
        "cumulative_return": round(cum, 6),
        "annualized_return": round(ann_ret, 6),
        "annualized_vol": round(ann_vol, 6),
        "sharpe_ratio": round(sharpe, 4),
        "max_drawdown": round(max_dd, 6),
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
    weights_path = run_dir / "optimizer" / "target_weights.csv"
    optimizer_manifest_path = run_dir / "optimizer" / "optimizer_manifest.json"
    returns_path = run_dir / "risk" / "returns_panel.csv"
    if not (weights_path.exists() and optimizer_manifest_path.exists() and returns_path.exists()):
        LOGGER.error("Need validated target_weights.csv/optimizer_manifest.json (run 09+10) and returns_panel.csv (run 05)")
        return 1
    out_dir = run_dir / "optimizer"
    metrics_path = out_dir / "static_replay_metrics.json"
    curve_path = out_dir / "static_replay_equity_curve.csv"
    manifest_path = out_dir / "static_replay_manifest.json"
    try:
        fail_if_exists([metrics_path, curve_path, manifest_path], force=args.force)
    except FileExistsError as exc:
        LOGGER.error("%s", exc)
        return 1

    benchmark = str(cfg_get(config, "risk_panel.master_calendar_ticker", "SPY")).upper()
    returns = pd.read_csv(returns_path, index_col=0)
    weights = {r["ticker"]: float(r["weight"]) for r in read_csv(weights_path) if float(r["weight"]) > 0}
    held = [t for t in weights if t in returns.columns]
    missing_held = sorted(t for t in weights if t not in returns.columns)
    if not held:
        LOGGER.error("No held names found in the return panel")
        return 1
    if missing_held:
        LOGGER.warning("Dropping %d held names missing from returns_panel.csv: %s", len(missing_held), missing_held[:10])
    w = pd.Series({t: weights[t] for t in held})
    w = w / w.sum()  # renormalize to held names present in the panel

    # Complete-case window across held names (a held shrunk name trims the early window).
    R = returns[held].dropna(how="any")
    if R.empty:
        LOGGER.error("No complete-case replay window across %d held names", len(held))
        return 1
    port = R.to_numpy() @ w.reindex(R.columns).to_numpy()
    port_ret = pd.Series(port, index=R.index)
    eqw_ret = R.mean(axis=1)
    bench_ret = returns[benchmark].reindex(R.index) if benchmark in returns.columns else pd.Series(index=R.index, dtype=float)

    metrics = {
        "run_as_of": run_as_of,
        "artifact_type": "static_trailing_replay_diagnostic",
        "WARNING": "lookahead diagnostic, NOT an out-of-sample backtest; true walk-forward baseline is Stage 11",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "held_names_total": len(weights),
        "held_names_used": len(held),
        "held_names_missing_from_returns": missing_held,
        "window": {"start": str(R.index[0]), "end": str(R.index[-1]), "rows": int(len(R))},
        "aqr_baseline": perf_stats(port_ret),
        "equal_weight_held": perf_stats(eqw_ret),
        f"benchmark_{benchmark}": perf_stats(bench_ret),
    }
    metrics_path.parent.mkdir(parents=True, exist_ok=True)
    metrics_path.write_text(json.dumps(metrics, indent=2, sort_keys=True), encoding="utf-8")

    curve_rows = []
    cum = (1.0 + port_ret).cumprod()
    eqw_cum = (1.0 + eqw_ret).cumprod()
    bench_cum = (1.0 + bench_ret).cumprod()
    for d in R.index:
        curve_rows.append({
            "date": str(d), "aqr_cum": round(float(cum.get(d, np.nan)), 6),
            "equal_weight_cum": round(float(eqw_cum.get(d, np.nan)), 6),
            f"{benchmark}_cum": round(float(bench_cum.get(d, np.nan)), 6) if benchmark in returns.columns else "",
        })
    write_csv(curve_path, ["date", "aqr_cum", "equal_weight_cum", f"{benchmark}_cum"], curve_rows)

    replay_manifest = {
        "run_as_of": run_as_of,
        "stage": "stage3_static_trailing_replay",
        "artifact_type": "static_trailing_replay_diagnostic",
        "generated_at": metrics["generated_at"],
        "warning": metrics["WARNING"],
        "files": {
            "target_weights.csv": {"sha256": sha256_file(weights_path)},
            "optimizer_manifest.json": {"sha256": sha256_file(optimizer_manifest_path)},
            "returns_panel.csv": {"sha256": sha256_file(returns_path)},
            "static_replay_metrics.json": {"sha256": sha256_file(metrics_path)},
            "static_replay_equity_curve.csv": {"sha256": sha256_file(curve_path)},
        },
        "held_names_total": len(weights),
        "held_names_used": len(held),
        "held_names_missing_from_returns": missing_held,
        "window": metrics["window"],
    }
    write_manifest(manifest_path, replay_manifest)

    b = metrics["aqr_baseline"]
    LOGGER.info("STATIC REPLAY (diagnostic): AQR cum=%.4f ann=%.4f vol=%.4f sharpe=%.2f maxDD=%.4f over %d days -> %s",
                b.get("cumulative_return", 0), b.get("annualized_return", 0), b.get("annualized_vol", 0),
                b.get("sharpe_ratio", 0), b.get("max_drawdown", 0), b.get("observations", 0), metrics_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
