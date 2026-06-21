#!/usr/bin/env python3
"""Stage 2 - classify each ticker's risk-data coverage (direct / shrunk / excluded).

Emits risk_coverage.csv with SEPARATE risk fields. It never mutates Stage 1 investable_eligible.
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd  # noqa: E402

from portfolio_layer.core.config import cfg_get, load_yaml  # noqa: E402
from portfolio_layer.core.contracts import fail_if_exists, read_csv, write_csv  # noqa: E402
from portfolio_layer.core.logging_utils import configure_utc_logging  # noqa: E402
from portfolio_layer.core.paths import resolve_runtime_paths  # noqa: E402
from portfolio_layer.risk.panel import build_universe  # noqa: E402
from portfolio_layer.risk.readiness import latest_run_with  # noqa: E402


LOGGER = logging.getLogger("build_risk_coverage")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
COVERAGE_FIELDS = [
    "ticker", "role", "source_pipeline", "score_eligible", "risk_status", "risk_eligible",
    "observation_count", "missing_day_count", "missing_day_fraction", "start_date", "end_date",
    "right_edge_missing_day_count", "shrinkage_target", "risk_reason",
]


def iso_date_arg(raw: str) -> str:
    try:
        date.fromisoformat(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"must be YYYY-MM-DD, got {raw!r}") from exc
    return raw


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Classify Stage 2 risk-data coverage.")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--as-of", type=iso_date_arg, default=None)
    p.add_argument("--force", action="store_true")
    return p.parse_args()


def unlink_artifacts(paths: list[Path]) -> None:
    for path in paths:
        if path.exists():
            path.unlink()


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
    risk_dir = run_dir / "risk"
    prices_path = risk_dir / "prices_adjclose.csv"
    if not prices_path.exists():
        LOGGER.error("prices_adjclose.csv missing; run 05 first: %s", prices_path)
        return 1
    coverage_path = risk_dir / "risk_coverage.csv"
    if args.force:
        unlink_artifacts([
            risk_dir / "covariance.csv",
            risk_dir / "covariance_period.csv",
            risk_dir / "correlation_clusters.csv",
            risk_dir / "covariance_meta.json",
            risk_dir / "return_outliers.csv",
            risk_dir / "data_quality_review.csv",
            risk_dir / "validation" / "risk_panel_validation.csv",
            risk_dir / "risk_manifest.json",
        ])
    try:
        fail_if_exists([coverage_path], force=args.force)
    except FileExistsError as exc:
        LOGGER.error("%s", exc)
        return 1

    min_direct = int(cfg_get(config, "risk_panel.min_direct_history_days", 252))
    hard_floor = int(cfg_get(config, "risk_panel.hard_floor_history_days", 60))
    max_gap_frac = float(cfg_get(config, "risk_panel.max_missing_day_fraction", 0.10))
    max_stale_days = int(cfg_get(config, "risk_panel.max_stale_price_trading_days", 0))
    sector_etf = {str(k): str(v).upper() for k, v in (cfg_get(config, "risk_panel.sector_etf_map", {}) or {}).items()}
    fallback_etf = str(cfg_get(config, "risk_panel.master_calendar_ticker", "SPY")).upper()

    prices = pd.read_csv(prices_path, index_col=0)
    panel_end = str(prices.index[-1]) if not prices.empty else run_as_of
    score_rows = {r["ticker"]: r for r in read_csv(run_dir / "stocks_scores.csv")}
    fetch_meta = {r["ticker"]: r for r in read_csv(risk_dir / "fetch_results.csv")}
    rc = cfg_get(config, "risk_panel", {})
    # Iterate the full universe (not just fetched columns) so fetch-failures are recorded, never dropped.
    universe_tickers = sorted({u["ticker"] for u in build_universe(run_dir / "stocks_scores.csv", rc)}
                              | set(prices.columns))

    out: list[dict] = []
    for ticker in universe_tickers:
        col = prices[ticker] if ticker in prices.columns else None
        obs = int(col.notna().sum()) if col is not None else 0
        if obs > 0:
            present = col.dropna()
            first, last = str(present.index[0]), str(present.index[-1])
            listed_span = prices.loc[first:panel_end].shape[0]
            missing_days = max(0, listed_span - obs)
            right_edge_missing = max(0, prices.loc[last:panel_end].shape[0] - 1)
            gap_frac = round(missing_days / listed_span, 4) if listed_span else 0.0
        else:
            first = last = ""
            missing_days = prices.shape[0]
            right_edge_missing = prices.shape[0]
            gap_frac = 1.0
        meta = fetch_meta.get(ticker, {})
        role = str(meta.get("role", ""))
        pipeline = str(meta.get("source_pipeline", ""))
        score_row = score_rows.get(ticker)
        score_eligible = str(score_row["investable_eligible"]) if score_row else ("n/a" if role else "")

        if obs == 0:
            status, reason = "excluded", f"no_price_data:{meta.get('status', 'missing')}"
        elif right_edge_missing > max_stale_days:
            status, reason = "excluded", f"stale_right_edge:{last}"
        elif obs < hard_floor:
            status, reason = "excluded", "below_hard_floor"
        elif obs < min_direct or gap_frac > max_gap_frac:
            status = "shrunk"
            reason = "partial_history" if obs < min_direct else "high_internal_gaps"
        else:
            status, reason = "direct", "direct"
        risk_eligible = 0 if status == "excluded" else 1
        target = "" if status != "shrunk" else (sector_etf.get(pipeline, fallback_etf))

        out.append({
            "ticker": ticker, "role": role, "source_pipeline": pipeline, "score_eligible": score_eligible,
            "risk_status": status, "risk_eligible": risk_eligible, "observation_count": obs,
            "missing_day_count": missing_days, "missing_day_fraction": gap_frac,
            "start_date": first, "end_date": last, "right_edge_missing_day_count": right_edge_missing,
            "shrinkage_target": target, "risk_reason": reason,
        })

    n = write_csv(coverage_path, COVERAGE_FIELDS, sorted(out, key=lambda r: r["ticker"]))
    by_status = pd.Series([r["risk_status"] for r in out]).value_counts().to_dict()
    LOGGER.info("Risk coverage: %d names %s -> %s", n, by_status, coverage_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
