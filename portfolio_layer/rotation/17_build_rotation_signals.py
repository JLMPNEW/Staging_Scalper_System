#!/usr/bin/env python3
"""Stage 5 - build tactical rotation signals from the sealed Stage 2 panel (SHADOW-ONLY).

Emits canonical audit tables + optimizer-contract tables and a sealed provenance manifest:
  runs/<as_of>/rotation/sector_rotation.csv            (canonical)
  runs/<as_of>/rotation/sector_rotation_optimizer.csv  (SectorName, Ticker, ScorePct, State)
  runs/<as_of>/rotation/foreign_etfs.csv               (canonical)
  runs/<as_of>/rotation/foreign_etfs_optimizer.csv     (Ticker, MarketName, Score, ScorePct, State)
  runs/<as_of>/rotation/rotation_signals_meta.json     (sealed; hashes inputs + sources)

Never writes into optimizer/ or costs/ - this stage cannot move the live book.
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, timezone
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd  # noqa: E402

from portfolio_layer.core.config import cfg_get, load_yaml  # noqa: E402
from portfolio_layer.core.artifacts import invalidate_rotation_outputs_after_signal_change  # noqa: E402
from portfolio_layer.core.contracts import fail_if_exists, sha256_file, write_csv, write_manifest  # noqa: E402
from portfolio_layer.core.logging_utils import configure_utc_logging  # noqa: E402
from portfolio_layer.core.paths import resolve_runtime_paths  # noqa: E402
from portfolio_layer.risk.liquidity import finite_float  # noqa: E402
from portfolio_layer.risk.readiness import latest_run_with  # noqa: E402
from portfolio_layer.rotation.foreign_market_evaluator import build_foreign_rotation  # noqa: E402
from portfolio_layer.rotation.sector_rotation_selector import build_sector_rotation  # noqa: E402


LOGGER = logging.getLogger("build_rotation_signals")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"

SECTOR_FIELDS = ["source_pipeline", "etf", "score", "score_pct", "state", "trend_state",
                 "trend_gate", "above_ma", "ma_slope", "rotation_multiplier", "present_in_panel"]
SECTOR_OPT_FIELDS = ["SectorName", "Ticker", "ScorePct", "State"]
FOREIGN_FIELDS = ["ticker", "market_name", "score", "score_pct", "state", "trend_state",
                  "eligible", "present_in_panel"]
FOREIGN_OPT_FIELDS = ["Ticker", "MarketName", "Score", "ScorePct", "State"]

SOURCE_FILES = [
    "rotation_timeseries.py",
    "sector_rotation_selector.py",
    "foreign_market_evaluator.py",
    "rotation_book.py",
    "17_build_rotation_signals.py",
]


def iso_date_arg(raw: str) -> str:
    try:
        date.fromisoformat(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"must be YYYY-MM-DD, got {raw!r}") from exc
    return raw


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build Stage 5 rotation signals (shadow-only).")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--as-of", type=iso_date_arg, default=None)
    p.add_argument("--force", action="store_true")
    return p.parse_args()


def _int_list(values, name: str) -> list[int]:
    return [int(finite_float(v, name=f"{name}[{i}]")) for i, v in enumerate(values or [])]


def _float_list(values, name: str) -> list[float]:
    return [finite_float(v, name=f"{name}[{i}]") for i, v in enumerate(values or [])]


def _validate_rotation_config(
    *,
    windows: list[int],
    weights: list[float],
    ma_days: int,
    slope_lookback: int,
    pos_pct: float,
    neg_pct: float,
    mult_min: float,
    mult_max: float,
    max_shift: float,
    eligible_pct: float,
) -> None:
    if len(windows) != len(weights) or not windows:
        raise ValueError("rotation.momentum_windows_days and momentum_weights must be equal-length, non-empty")
    if any(w <= 0 for w in windows):
        raise ValueError(f"rotation.momentum_windows_days must be positive, got {windows}")
    if any(w <= 0.0 for w in weights) or sum(weights) <= 0.0:
        raise ValueError(f"rotation.momentum_weights must be positive with positive sum, got {weights}")
    if ma_days <= 0 or slope_lookback <= 0:
        raise ValueError(
            "rotation.trend_filter.ma_days and slope_lookback_days must be positive; "
            f"got ma_days={ma_days}, slope_lookback_days={slope_lookback}"
        )
    for name, value in {
        "positive_score_pct": pos_pct,
        "negative_score_pct": neg_pct,
        "eligible_score_pct": eligible_pct,
    }.items():
        if not (0.0 <= value <= 100.0):
            raise ValueError(f"rotation {name} must be in [0, 100], got {value}")
    if neg_pct > pos_pct:
        raise ValueError(
            f"rotation.state_thresholds.negative_score_pct ({neg_pct}) cannot exceed "
            f"positive_score_pct ({pos_pct})"
        )
    if not (0.0 < mult_min <= 1.0 <= mult_max):
        raise ValueError(
            f"rotation.tilt bounds must satisfy 0 < mult_min <= 1 <= mult_max; got [{mult_min}, {mult_max}]"
        )
    if max_shift < 0.0:
        raise ValueError(f"rotation.tilt.max_sector_budget_shift must be non-negative, got {max_shift}")


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
    returns_path = risk_dir / "returns_panel.csv"
    if not (prices_path.exists() and returns_path.exists()):
        LOGGER.error("Need a sealed Stage 2 panel (prices_adjclose.csv + returns_panel.csv)")
        return 1

    rotation_dir = run_dir / "rotation"
    sector_path = rotation_dir / "sector_rotation.csv"
    sector_opt_path = rotation_dir / "sector_rotation_optimizer.csv"
    foreign_path = rotation_dir / "foreign_etfs.csv"
    foreign_opt_path = rotation_dir / "foreign_etfs_optimizer.csv"
    meta_path = rotation_dir / "rotation_signals_meta.json"
    out_paths = [sector_path, sector_opt_path, foreign_path, foreign_opt_path, meta_path]
    if args.force:
        invalidate_rotation_outputs_after_signal_change(rotation_dir)
    try:
        fail_if_exists(out_paths, force=args.force)
    except FileExistsError as exc:
        LOGGER.error("%s", exc)
        return 1

    try:
        sector_etf_map = dict(cfg_get(config, "risk_panel.sector_etf_map", {}) or {})
        if not sector_etf_map:
            raise ValueError("risk_panel.sector_etf_map is required for rotation")
        rank_universe = [str(t).strip().upper() for t in cfg_get(config, "rotation.rank_universe_etfs", []) or []]
        if not rank_universe:
            raise ValueError("rotation.rank_universe_etfs is required")
        windows = _int_list(cfg_get(config, "rotation.momentum_windows_days", [21, 63, 126]),
                            "rotation.momentum_windows_days")
        weights = _float_list(cfg_get(config, "rotation.momentum_weights", [0.5, 0.3, 0.2]),
                             "rotation.momentum_weights")
        if len(windows) != len(weights) or not windows:
            raise ValueError("rotation.momentum_windows_days and momentum_weights must be equal-length, non-empty")
        ma_days = int(finite_float(cfg_get(config, "rotation.trend_filter.ma_days", 200), name="rotation.trend_filter.ma_days"))
        slope_lookback = int(finite_float(cfg_get(config, "rotation.trend_filter.slope_lookback_days", 21),
                                          name="rotation.trend_filter.slope_lookback_days"))
        pos_pct = finite_float(cfg_get(config, "rotation.state_thresholds.positive_score_pct", 60.0),
                              name="rotation.state_thresholds.positive_score_pct")
        neg_pct = finite_float(cfg_get(config, "rotation.state_thresholds.negative_score_pct", 40.0),
                              name="rotation.state_thresholds.negative_score_pct")
        mult_min = finite_float(cfg_get(config, "rotation.tilt.mult_min", 0.7), name="rotation.tilt.mult_min")
        mult_max = finite_float(cfg_get(config, "rotation.tilt.mult_max", 1.3), name="rotation.tilt.mult_max")
        market_map = dict(cfg_get(config, "rotation.foreign.market_map", {}) or {})
        if not market_map:
            raise ValueError("rotation.foreign.market_map is required")
        eligible_pct = finite_float(cfg_get(config, "rotation.foreign.eligible_score_pct", 55.0),
                                   name="rotation.foreign.eligible_score_pct")
        applied_budget = finite_float(cfg_get(config, "rotation.foreign.applied_budget", 0.0),
                                     name="rotation.foreign.applied_budget")
        max_shift = finite_float(cfg_get(config, "rotation.tilt.max_sector_budget_shift", 0.30),
                                 name="rotation.tilt.max_sector_budget_shift")
        _validate_rotation_config(
            windows=windows,
            weights=weights,
            ma_days=ma_days,
            slope_lookback=slope_lookback,
            pos_pct=pos_pct,
            neg_pct=neg_pct,
            mult_min=mult_min,
            mult_max=mult_max,
            max_shift=max_shift,
            eligible_pct=eligible_pct,
        )
    except ValueError as exc:
        LOGGER.error("%s", exc)
        return 1
    if applied_budget != 0.0:
        LOGGER.error("rotation.foreign.applied_budget must be 0 until Stage 6 (got %s)", applied_budget)
        return 1

    prices = pd.read_csv(prices_path, index_col=0)
    returns = pd.read_csv(returns_path, index_col=0)
    prices.columns = [str(c).strip().upper() for c in prices.columns]
    returns.columns = [str(c).strip().upper() for c in returns.columns]
    sector_etf_map = {str(k).strip(): str(v).strip().upper() for k, v in sector_etf_map.items()}
    market_map = {str(k).strip().upper(): str(v).strip() for k, v in market_map.items()}
    panel_end = str(prices.index[-1]) if not prices.empty else ""
    if panel_end and panel_end > run_as_of:
        LOGGER.error("Panel right edge %s is after run as_of %s (lookahead)", panel_end, run_as_of)
        return 1

    sector_rows = build_sector_rotation(
        prices, returns, sector_etf_map=sector_etf_map, rank_universe=rank_universe,
        windows=windows, weights=weights, ma_days=ma_days, slope_lookback=slope_lookback,
        positive_score_pct=pos_pct, negative_score_pct=neg_pct, mult_min=mult_min, mult_max=mult_max,
    )
    foreign_rows = build_foreign_rotation(
        prices, returns, market_map=market_map, windows=windows, weights=weights,
        ma_days=ma_days, slope_lookback=slope_lookback, eligible_score_pct=eligible_pct,
    )

    sector_opt_rows = [{"SectorName": r["source_pipeline"], "Ticker": r["etf"],
                        "ScorePct": r["score_pct"], "State": r["state"]} for r in sector_rows]
    foreign_opt_rows = [{"Ticker": r["ticker"], "MarketName": r["market_name"], "Score": r["score"],
                         "ScorePct": r["score_pct"], "State": r["state"]} for r in foreign_rows]

    rotation_dir.mkdir(parents=True, exist_ok=True)
    write_csv(sector_path, SECTOR_FIELDS, sector_rows)
    write_csv(sector_opt_path, SECTOR_OPT_FIELDS, sector_opt_rows)
    write_csv(foreign_path, FOREIGN_FIELDS, foreign_rows)
    write_csv(foreign_opt_path, FOREIGN_OPT_FIELDS, foreign_opt_rows)

    meta = {
        "run_as_of": run_as_of,
        "stage": "stage5_rotation_signals",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "shadow_only": True,
        "enabled_in_production": bool(cfg_get(config, "rotation.enabled_in_production", False)),
        "panel_end": panel_end,
        "params": {
            "rank_universe_etfs": rank_universe, "momentum_windows_days": windows,
            "momentum_weights": weights, "ma_days": ma_days, "slope_lookback_days": slope_lookback,
            "positive_score_pct": pos_pct, "negative_score_pct": neg_pct,
            "tilt": {"mult_min": mult_min, "mult_max": mult_max,
                     "max_sector_budget_shift": max_shift},
            "foreign": {"applied_budget": applied_budget, "eligible_score_pct": eligible_pct},
        },
        "sector_etf_map": sector_etf_map,
        "foreign_market_map": market_map,
        "counts": {"sector_rows": len(sector_rows), "foreign_rows": len(foreign_rows),
                   "sector_present": sum(1 for r in sector_rows if r["present_in_panel"]),
                   "foreign_present": sum(1 for r in foreign_rows if r["present_in_panel"])},
        "inputs_sha256": {
            "prices_adjclose.csv": sha256_file(prices_path),
            "returns_panel.csv": sha256_file(returns_path),
            "config.yaml": sha256_file(config_path),
        },
        "source_sha256": {name: sha256_file(PACKAGE_ROOT / "rotation" / name)
                          for name in SOURCE_FILES if (PACKAGE_ROOT / "rotation" / name).exists()},
        "files": {
            "sector_rotation.csv": {"sha256": sha256_file(sector_path), "rows": len(sector_rows)},
            "sector_rotation_optimizer.csv": {"sha256": sha256_file(sector_opt_path), "rows": len(sector_opt_rows)},
            "foreign_etfs.csv": {"sha256": sha256_file(foreign_path), "rows": len(foreign_rows)},
            "foreign_etfs_optimizer.csv": {"sha256": sha256_file(foreign_opt_path), "rows": len(foreign_opt_rows)},
        },
    }
    write_manifest(meta_path, meta)

    in_favor = sum(1 for r in sector_rows if r["state"] == "Positive")
    gated = sum(1 for r in sector_rows if r["trend_gate"] == "fail")
    LOGGER.info("Rotation signals: %d sleeves (%d Positive, %d trend-gated), %d foreign -> %s",
                len(sector_rows), in_favor, gated, len(foreign_rows), sector_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
