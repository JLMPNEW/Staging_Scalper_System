#!/usr/bin/env python3
"""Stage 11 tactical single-name long replay.

Signals are formed after session D, positions enter at the D+1 adjusted open,
and are marked at every subsequent close. A signal or time-stop observed at a
close schedules an exit at the next adjusted open. This keeps every
information-driven trade executable and avoids same-close look-ahead.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from portfolio_layer.backtest.short_costs import (  # noqa: E402
    PITShortCostModel,
    selftest_short_cost_model,
)
from portfolio_layer.backtest.walkforward_common import perf_stats  # noqa: E402
from portfolio_layer.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from portfolio_layer.core.contracts import (  # noqa: E402
    fail_if_exists,
    sha256_file,
    write_csv,
    write_manifest,
)
from portfolio_layer.core.db import utc_now  # noqa: E402
from portfolio_layer.core.logging_utils import configure_utc_logging  # noqa: E402
from portfolio_layer.core.paths import resolve_runtime_paths  # noqa: E402
from portfolio_layer.research.stage11_common import (  # noqa: E402
    load_lockbox,
    manifest_file_errors,
    mean_t_hac,
)


LOGGER = logging.getLogger("tactical_long_replay")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
DEFAULT_PIPELINES = [
    "semiconductors",
    "software_infrastructure",
    "technology_hardware",
    "biotech",
    "med_devices",
    "defense",
]
TRADE_FIELDS = [
    "ticker",
    "source_pipeline",
    "signal_date",
    "entry_date",
    "exit_date",
    "entry_open",
    "exit_open",
    "entry_score_z",
    "entry_weight",
    "holding_days",
    "exit_reason",
    "gross_return",
    "net_return",
    "selection_alpha_net",
    "entry_cost_bps",
    "exit_cost_bps",
    "stress_cost_bps",
]


@dataclass
class Position:
    ticker: str
    pipeline: str
    signal_date: str
    entry_date: str
    entry_price: float
    previous_price: float
    previous_benchmark_price: float
    entry_score_z: float
    initial_weight: float
    current_exposure: float
    holding_days: int = 0
    gross_pnl: float = 0.0
    benchmark_pnl: float = 0.0
    transaction_cost: float = 0.0
    stress_cost: float = 0.0
    entry_cost_bps: float = 0.0
    pending_exit_reason: str | None = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Stage 11 tactical single-name long replay.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--panel-build", default=None)
    parser.add_argument("--parameter-file", type=Path, default=None)
    parser.add_argument("--signal-from", default=None)
    parser.add_argument("--signal-to", default=None)
    parser.add_argument(
        "--evaluation-json",
        type=Path,
        default=None,
        help="Calibration-only result; skips official artifact publication.",
    )
    parser.add_argument("--pipelines", default=",".join(DEFAULT_PIPELINES))
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def exit_reason(
    *,
    holding_days: int,
    current_score_z: float | None,
    max_holding_days: int,
    invalidation_score_z: float,
) -> str | None:
    """Return the first close-observable exit reason, with signal risk first."""
    if current_score_z is None:
        return "signal_missing"
    if current_score_z <= invalidation_score_z:
        return "signal_invalidation"
    if holding_days >= max_holding_days:
        return "time_stop"
    return None


def select_candidates(
    frame: pd.DataFrame,
    *,
    tail_fraction: float,
    min_names: int,
    max_names: int,
) -> pd.DataFrame:
    clean = frame.dropna(subset=["score_z_pipeline_date"]).sort_values(
        ["score_z_pipeline_date", "ticker"],
        ascending=[False, True],
    )
    if clean.empty:
        return clean
    count = int(math.ceil(len(clean) * tail_fraction))
    count = min(len(clean), max(min_names, count), max_names)
    return clean.head(count)


def _selftest() -> None:
    assert (
        exit_reason(
            holding_days=1,
            current_score_z=-0.01,
            max_holding_days=15,
            invalidation_score_z=0.0,
        )
        == "signal_invalidation"
    )
    assert (
        exit_reason(
            holding_days=15,
            current_score_z=0.5,
            max_holding_days=15,
            invalidation_score_z=0.0,
        )
        == "time_stop"
    )
    assert (
        exit_reason(
            holding_days=1,
            current_score_z=None,
            max_holding_days=15,
            invalidation_score_z=0.0,
        )
        == "signal_missing"
    )
    sample = pd.DataFrame(
        {
            "ticker": ["A", "B", "C", "D"],
            "score_z_pipeline_date": [-2.0, -1.0, 0.0, 1.0],
        }
    )
    chosen = select_candidates(sample, tail_fraction=0.25, min_names=1, max_names=2)
    assert list(chosen["ticker"]) == ["D"]
    assert abs(_transaction_cost(weight=0.02, half_spread_bps=10.0, commission=0.00001) - 0.00003) < 1e-12
    selftest_short_cost_model()
    print("tactical-long replay self-test: PASS")


def _latest(root: Path, marker: str, wanted: str | None) -> Path | None:
    if wanted:
        candidate = root / wanted
        return candidate if (candidate / marker).exists() else None
    if not root.exists():
        return None
    builds = sorted(path for path in root.iterdir() if path.is_dir() and (path / marker).exists())
    return builds[-1] if builds else None


def _price(frame: pd.DataFrame, day: str, ticker: str) -> float | None:
    if day not in frame.index or ticker not in frame.columns:
        return None
    try:
        value = float(frame.at[day, ticker])
    except (TypeError, ValueError):
        return None
    return value if np.isfinite(value) and value > 0 else None


def _exact_spreads(panel: pd.DataFrame) -> dict[tuple[str, str], float]:
    output: dict[tuple[str, str], float] = {}
    if "liquidity_half_spread_bps" not in panel.columns:
        return output
    values = pd.Series(
        pd.to_numeric(panel["liquidity_half_spread_bps"], errors="coerce"),
        index=panel.index,
        dtype=float,
    )
    valid = values.map(lambda value: bool(np.isfinite(value) and value >= 0))
    if "liquidity_join_available" in panel.columns:
        valid &= panel["liquidity_join_available"].astype(str).isin(("1", "1.0", "true", "True"))
    rows = panel.loc[valid, ["as_of_date", "ticker"]].copy()
    rows["spread"] = values.loc[valid]
    for (as_of, ticker), group in rows.groupby(["as_of_date", "ticker"]):
        output[(str(as_of), str(ticker))] = float(group["spread"].median())
    return output


def _read_execution_ohlcv(path: Path) -> dict[str, pd.DataFrame]:
    frame = pd.read_csv(path)
    required = {"date", "ticker", "adj_open", "adj_high", "adj_low", "adj_close"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"Execution OHLCV panel lacks columns: {sorted(missing)}")
    frame["date"] = frame["date"].astype(str).str.slice(0, 10)
    frame["ticker"] = frame["ticker"].astype(str).str.upper().str.strip()
    if frame.duplicated(["date", "ticker"]).any():
        raise ValueError("Execution OHLCV panel has duplicate date/ticker rows")
    output: dict[str, pd.DataFrame] = {}
    for field in ("adj_open", "adj_high", "adj_low", "adj_close"):
        frame[field] = pd.to_numeric(frame[field], errors="coerce")
        output[field] = frame.pivot(index="date", columns="ticker", values=field).sort_index()
    return output


def _transaction_cost(*, weight: float, half_spread_bps: float, commission: float) -> float:
    return abs(weight) * half_spread_bps / 1e4 + commission


def _load_parameter_overrides(path: Path | None) -> tuple[dict[str, Any], str | None]:
    if path is None:
        return {}, None
    resolved = path.expanduser().resolve()
    payload = json.loads(resolved.read_text(encoding="utf-8"))
    if payload.get("acceptance") != "PASS":
        raise ValueError(f"Long parameter artifact is not accepted: {resolved}")
    parameters = payload.get("parameters")
    if not isinstance(parameters, dict):
        raise ValueError(f"Long parameter artifact lacks a parameters object: {resolved}")
    return parameters, sha256_file(resolved)


def _fraction(numerator: float, denominator: float) -> float:
    if denominator <= 0:
        return 0.0
    return float(np.clip(numerator / denominator, 0.0, 1.0))


def main() -> int:  # noqa: C901, PLR0912, PLR0915
    configure_utc_logging()
    args = parse_args()
    if args.selftest:
        _selftest()
        return 0
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    paths = resolve_runtime_paths(config, config_path)
    try:
        parameter_overrides, parameter_sha256 = _load_parameter_overrides(args.parameter_file)
        lockbox = load_lockbox(config, config_path)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        LOGGER.error("%s", exc)
        return 1

    pipelines = [value.strip() for value in str(args.pipelines).split(",") if value.strip()]
    panel_dir = _latest(
        paths.output_dir / str(cfg_get(config, "calibration_panel.dir", "calibration_panel")),
        "calibration_panel_manifest.json",
        args.panel_build,
    )
    if panel_dir is None:
        LOGGER.error("No calibration panel; run research/67 first")
        return 1
    panel_manifest_path = panel_dir / "calibration_panel_manifest.json"
    panel_path = panel_dir / "calibration_panel.csv"
    panel_manifest = json.loads(panel_manifest_path.read_text(encoding="utf-8"))
    panel_errors = manifest_file_errors(panel_manifest, {"calibration_panel.csv": panel_path})
    if panel_manifest.get("acceptance") != "PASS" or panel_errors:
        LOGGER.error("Calibration panel is rejected/stale: %s", panel_errors)
        return 1

    wanted = {
        "as_of_date",
        "ticker",
        "source_pipeline",
        "score_z_pipeline_date",
        "calibration_research_eligible",
        "sidecar_stage11_eligible",
        "survivorship_complete",
        "in_lockbox",
        "liquidity_join_available",
        "liquidity_half_spread_bps",
    }
    panel = pd.read_csv(panel_path, usecols=lambda column: column in wanted)
    panel["as_of_date"] = panel["as_of_date"].astype(str).str.slice(0, 10)
    panel["ticker"] = panel["ticker"].astype(str).str.upper().str.strip()
    panel["score_z_pipeline_date"] = pd.to_numeric(panel["score_z_pipeline_date"], errors="coerce")
    truthy = ("1", "1.0", "true", "True")
    eligible = panel["calibration_research_eligible"].astype(str).isin(truthy)
    if "sidecar_stage11_eligible" in panel.columns:
        eligible |= panel["sidecar_stage11_eligible"].astype(str).isin(truthy)
    panel = panel.loc[
        eligible
        & panel["survivorship_complete"].astype(str).isin(truthy)
        & ~panel["in_lockbox"].astype(str).isin(truthy)
        & panel["source_pipeline"].isin(pipelines)
    ].copy()
    if panel.empty:
        LOGGER.error("No admitted tactical-long rows")
        return 1
    duplicates = panel.duplicated(["as_of_date", "ticker"], keep=False)
    if duplicates.any():
        LOGGER.error(
            "Tactical-long panel has duplicate date/ticker rows: %s",
            panel.loc[duplicates, ["as_of_date", "ticker", "source_pipeline"]]
            .head(10)
            .to_dict("records"),
        )
        return 1

    survivorship_build = str(panel_manifest.get("survivorship_panel_build", "")).strip()
    survivorship_dir = (
        paths.output_dir
        / str(cfg_get(config, "survivorship_panel.dir", "survivorship_panel"))
        / survivorship_build
    )
    survivorship_manifest_path = survivorship_dir / "survivorship_manifest.json"
    prices_path = survivorship_dir / "prices_adjclose.csv"
    if not survivorship_manifest_path.exists() or not prices_path.exists():
        LOGGER.error("Matching survivorship panel is missing")
        return 1
    survivorship_manifest = json.loads(survivorship_manifest_path.read_text(encoding="utf-8"))
    survivorship_errors = manifest_file_errors(
        survivorship_manifest, {"prices_adjclose.csv": prices_path}
    )
    if (
        survivorship_manifest.get("acceptance") != "PASS"
        or sha256_file(survivorship_manifest_path)
        != str(panel_manifest.get("survivorship_panel_manifest_sha256", ""))
        or survivorship_errors
    ):
        LOGGER.error("Survivorship panel is rejected/stale: %s", survivorship_errors)
        return 1
    prices = pd.read_csv(prices_path, index_col=0)
    prices.index = prices.index.astype(str).str.slice(0, 10)
    prices.columns = [str(column).strip().upper() for column in prices.columns]

    execution_dir = (
        paths.output_dir
        / str(cfg_get(config, "execution_ohlcv_panel.dir", "execution_ohlcv_panel"))
        / survivorship_build
    )
    execution_manifest_path = execution_dir / "execution_ohlcv_manifest.json"
    execution_prices_path = execution_dir / "prices_adjusted_ohlcv.csv.gz"
    if not execution_manifest_path.exists() or not execution_prices_path.exists():
        LOGGER.error("Matching execution OHLCV panel is missing; run backtest/15c")
        return 1
    execution_manifest = json.loads(execution_manifest_path.read_text(encoding="utf-8"))
    execution_errors = manifest_file_errors(
        execution_manifest, {"prices_adjusted_ohlcv.csv.gz": execution_prices_path}
    )
    if (
        execution_manifest.get("acceptance") != "PASS"
        or str(execution_manifest.get("panel_build", "")) != survivorship_build
        or str(execution_manifest.get("survivorship_manifest_sha256", ""))
        != sha256_file(survivorship_manifest_path)
        or execution_errors
    ):
        LOGGER.error("Execution OHLCV panel is rejected/stale: %s", execution_errors)
        return 1
    try:
        ohlc = _read_execution_ohlcv(execution_prices_path)
    except (OSError, ValueError) as exc:
        LOGGER.error("%s", exc)
        return 1
    opens = ohlc["adj_open"]
    prices = prices.loc[
        (prices.index >= lockbox["dev_window_start"])
        & (prices.index <= lockbox["dev_window_end"])
    ].sort_index()
    calendar = list(prices.index)
    if len(calendar) < 2:
        LOGGER.error("Survivorship calendar has fewer than two development dates")
        return 1
    calendar_pos = {day: idx for idx, day in enumerate(calendar)}

    cfg = dict(cfg_get(config, "tactical_long", {}) or {})
    cfg.update(parameter_overrides)
    tail_fraction = float(cfg.get("tail_fraction", 0.10))
    min_names = int(cfg.get("min_names_per_sector", 2))
    max_names = int(cfg.get("max_names_per_sector", 10))
    signal_every = max(1, int(cfg.get("signal_every_n_snapshots", 5)))
    max_holding_days = int(cfg.get("max_holding_days", 30))
    invalidation_score_z = float(cfg.get("invalidation_score_z", 0.0))
    target_long_gross = float(cfg.get("target_long_gross", 0.95))
    max_position_weight = float(cfg.get("max_position_weight", 0.05))
    if not (
        0 < tail_fraction <= 0.5
        and min_names >= 1
        and max_names >= min_names
        and max_holding_days >= 1
        and 0 < target_long_gross <= 1
        and 0 < max_position_weight <= target_long_gross
    ):
        LOGGER.error("Invalid tactical_long policy")
        return 1

    cost_cfg = cfg.get("long_costs", {}) or {}
    db_path = resolve_path(
        str(
            cost_cfg.get(
                "market_positioning_db_path",
                cfg_get(
                    config,
                    "sector_neutral_arm.short_costs.market_positioning_db_path",
                    r"C:\Users\josel\Documents\STAGING\DB\market_positioning.sqlite",
                ),
            )
        ),
        base_dir=config_path.parent,
    )
    try:
        cost_model = PITShortCostModel(
            db_path=db_path,
            tickers=set(panel["ticker"]),
            start_date=calendar[0],
            end_date=calendar[-1],
            exact_half_spreads=_exact_spreads(panel),
            spread_fallback_bps=float(
                cost_cfg.get("historical_half_spread_fallback_bps", 15.0)
            ),
            borrow_fee_fallback_annual=0.0,
            max_borrow_fee_age_days=0,
            max_shortable_age_days=0,
            allow_fee_proxy_availability=False,
            allow_unknown_availability=True,
            stress_spread_fallback_bps=float(
                cost_cfg.get("stress_half_spread_fallback_bps", 30.0)
            ),
            stress_spread_multiplier=float(
                cost_cfg.get("stress_observed_spread_multiplier", 1.5)
            ),
            stress_borrow_fee_fallback_annual=0.0,
            stress_borrow_fee_multiplier=1.0,
        )
    except (FileNotFoundError, RuntimeError, ValueError, sqlite3.Error) as exc:
        LOGGER.error("Cannot initialize PIT spread model: %s", exc)
        return 1
    aum_usd = float(
        cost_cfg.get("research_aum_usd", cfg_get(config, "transaction_costs.aum_usd", 300000))
    )
    commission_usd = float(
        cost_cfg.get(
            "commission_per_order_usd",
            cfg_get(config, "transaction_costs.commission_per_order.worst_case", 1.25),
        )
    )
    commission_fraction = commission_usd / aum_usd
    sector_etfs = {
        str(key): str(value).strip().upper()
        for key, value in (cfg_get(config, "risk_panel.sector_etf_map", {}) or {}).items()
    }

    snapshots = sorted(set(panel["as_of_date"]) & set(calendar))
    signals = snapshots[::signal_every]
    if args.signal_from:
        signals = [day for day in signals if day >= str(args.signal_from)]
    if args.signal_to:
        signals = [day for day in signals if day <= str(args.signal_to)]
    if not signals:
        LOGGER.error("No signal dates remain in the requested evaluation window")
        return 1
    entries_by_day: dict[str, list[dict[str, Any]]] = {}
    for signal_day in signals:
        pos = calendar_pos.get(signal_day)
        # H full close evaluations plus a next-open exit require H+1 future rows.
        if pos is None or pos + max_holding_days + 1 >= len(calendar):
            continue
        entry_day = calendar[pos + 1]
        day_frame = panel.loc[panel["as_of_date"] == signal_day]
        selected: list[dict[str, Any]] = []
        for pipeline in pipelines:
            selected.extend(
                select_candidates(
                    day_frame.loc[day_frame["source_pipeline"] == pipeline],
                    tail_fraction=tail_fraction,
                    min_names=min_names,
                    max_names=max_names,
                ).to_dict("records")
            )
        entries_by_day[entry_day] = selected

    score_lookup = {
        (str(row.as_of_date), str(row.ticker)): float(row.score_z_pipeline_date)
        for row in panel[["as_of_date", "ticker", "score_z_pipeline_date"]]
        .dropna()
        .itertuples(index=False)
    }
    active: dict[str, Position] = {}
    trades: list[dict[str, object]] = []
    daily_rows: list[dict[str, object]] = []
    sector_net: dict[str, float] = {pipeline: 0.0 for pipeline in pipelines}
    coverage = {
        "spread_total": 0.0,
        "spread_exact": 0.0,
        "candidate_entry_total": 0.0,
        "candidate_entry_ohlc": 0.0,
    }

    for day in calendar:
        daily_gross = 0.0
        daily_net = 0.0
        daily_stress = 0.0
        daily_selection = 0.0

        # Close-observed exits execute at the next available open.
        for ticker, position in list(active.items()):
            if position.pending_exit_reason is None:
                continue
            exit_open = _price(opens, day, ticker)
            etf = sector_etfs.get(position.pipeline, "")
            etf_open = _price(opens, day, etf)
            if exit_open is None or etf_open is None:
                LOGGER.error("Pending next-open exit lacks OHLC for %s/%s on %s", ticker, etf, day)
                return 1
            gross = position.current_exposure * (exit_open / position.previous_price - 1.0)
            benchmark = position.current_exposure * (
                etf_open / position.previous_benchmark_price - 1.0
            )
            position.gross_pnl += gross
            position.benchmark_pnl += benchmark
            position.current_exposure *= exit_open / position.previous_price
            resolved = cost_model.resolve(day, ticker)
            exit_cost = _transaction_cost(
                weight=position.current_exposure,
                half_spread_bps=resolved.half_spread_bps,
                commission=commission_fraction,
            )
            stress_exit = _transaction_cost(
                weight=position.current_exposure,
                half_spread_bps=cost_model.stressed_half_spread_bps(resolved),
                commission=commission_fraction,
            )
            coverage["spread_total"] += position.current_exposure
            if resolved.spread_source == "ibkr_exact":
                coverage["spread_exact"] += position.current_exposure
            position.transaction_cost += exit_cost
            position.stress_cost += stress_exit
            daily_gross += gross
            daily_net += gross - exit_cost
            daily_stress += gross - stress_exit
            daily_selection += gross - benchmark - exit_cost
            net = position.gross_pnl - position.transaction_cost
            selection = position.gross_pnl - position.benchmark_pnl - position.transaction_cost
            sector_net[position.pipeline] += selection
            trades.append(
                {
                    "ticker": ticker,
                    "source_pipeline": position.pipeline,
                    "signal_date": position.signal_date,
                    "entry_date": position.entry_date,
                    "exit_date": day,
                    "entry_open": round(position.entry_price, 8),
                    "exit_open": round(exit_open, 8),
                    "entry_score_z": round(position.entry_score_z, 8),
                    "entry_weight": round(position.initial_weight, 8),
                    "holding_days": position.holding_days,
                    "exit_reason": position.pending_exit_reason,
                    "gross_return": round(position.gross_pnl, 10),
                    "net_return": round(net, 10),
                    "selection_alpha_net": round(selection, 10),
                    "entry_cost_bps": round(position.entry_cost_bps, 4),
                    "exit_cost_bps": round(
                        exit_cost / max(position.current_exposure, 1e-12) * 1e4, 4
                    ),
                    "stress_cost_bps": round(
                        position.stress_cost / max(position.initial_weight, 1e-12) * 1e4, 4
                    ),
                }
            )
            del active[ticker]

        candidates = entries_by_day.get(day, [])
        if candidates:
            grouped: dict[str, list[dict[str, Any]]] = {}
            for row in candidates:
                ticker = str(row["ticker"])
                if ticker in active:
                    continue
                coverage["candidate_entry_total"] += 1.0
                if _price(opens, day, ticker) is None:
                    continue
                coverage["candidate_entry_ohlc"] += 1.0
                grouped.setdefault(str(row["source_pipeline"]), []).append(row)
            for pipeline in sorted(grouped):
                rows = grouped[pipeline]
                existing_sector = sum(
                    position.current_exposure
                    for position in active.values()
                    if position.pipeline == pipeline
                )
                sector_target = target_long_gross / max(1, len(pipelines))
                sector_remaining = max(0.0, sector_target - existing_sector)
                weight = min(max_position_weight, sector_remaining / len(rows))
                etf = sector_etfs.get(pipeline, "")
                etf_open = _price(opens, day, etf)
                if etf_open is None:
                    LOGGER.error("Entry benchmark open missing for %s on %s", etf, day)
                    return 1
                for row in rows:
                    ticker = str(row["ticker"])
                    entry_open = _price(opens, day, ticker)
                    if entry_open is None or weight <= 0:
                        continue
                    resolved = cost_model.resolve(str(row["as_of_date"]), ticker)
                    entry_cost = _transaction_cost(
                        weight=weight,
                        half_spread_bps=resolved.half_spread_bps,
                        commission=commission_fraction,
                    )
                    stress_entry = _transaction_cost(
                        weight=weight,
                        half_spread_bps=cost_model.stressed_half_spread_bps(resolved),
                        commission=commission_fraction,
                    )
                    coverage["spread_total"] += weight
                    if resolved.spread_source == "ibkr_exact":
                        coverage["spread_exact"] += weight
                    active[ticker] = Position(
                        ticker=ticker,
                        pipeline=pipeline,
                        signal_date=str(row["as_of_date"]),
                        entry_date=day,
                        entry_price=entry_open,
                        previous_price=entry_open,
                        previous_benchmark_price=etf_open,
                        entry_score_z=float(row["score_z_pipeline_date"]),
                        initial_weight=weight,
                        current_exposure=weight,
                        transaction_cost=entry_cost,
                        stress_cost=stress_entry,
                        entry_cost_bps=entry_cost / weight * 1e4,
                    )
                    daily_net -= entry_cost
                    daily_stress -= stress_entry
                    daily_selection -= entry_cost

        for ticker, position in active.items():
            close_price = _price(prices, day, ticker)
            etf = sector_etfs.get(position.pipeline, "")
            etf_close = _price(prices, day, etf)
            if close_price is None or etf_close is None:
                LOGGER.error("Active long lacks close for %s/%s on %s", ticker, etf, day)
                return 1
            gross = position.current_exposure * (close_price / position.previous_price - 1.0)
            benchmark = position.current_exposure * (
                etf_close / position.previous_benchmark_price - 1.0
            )
            position.gross_pnl += gross
            position.benchmark_pnl += benchmark
            position.holding_days += 1
            position.current_exposure *= close_price / position.previous_price
            position.previous_price = close_price
            position.previous_benchmark_price = etf_close
            daily_gross += gross
            daily_net += gross
            daily_stress += gross
            daily_selection += gross - benchmark
            if position.pending_exit_reason is None:
                position.pending_exit_reason = exit_reason(
                    holding_days=position.holding_days,
                    current_score_z=score_lookup.get((day, ticker)),
                    max_holding_days=max_holding_days,
                    invalidation_score_z=invalidation_score_z,
                )

        daily_rows.append(
            {
                "date": day,
                "gross_return": round(daily_gross, 10),
                "net_return": round(daily_net, 10),
                "stress_net_return": round(daily_stress, 10),
                "selection_alpha_net": round(daily_selection, 10),
                "open_positions": len(active),
                "long_gross": round(
                    sum(position.current_exposure for position in active.values()), 8
                ),
            }
        )

    if active:
        LOGGER.error("Open long positions remain at development-window end")
        return 1
    if not trades:
        LOGGER.error("No tactical long trades were completed")
        return 1
    metric_rows = daily_rows
    if args.signal_from or args.signal_to:
        first_entry = min(str(row["entry_date"]) for row in trades)
        last_exit = max(str(row["exit_date"]) for row in trades)
        metric_rows = [row for row in daily_rows if first_entry <= str(row["date"]) <= last_exit]
    daily_net_values = np.asarray(
        [float(str(row["net_return"])) for row in metric_rows], dtype=float
    )
    daily_stress_values = np.asarray(
        [float(str(row["stress_net_return"])) for row in metric_rows], dtype=float
    )
    daily_selection_values = np.asarray(
        [float(str(row["selection_alpha_net"])) for row in metric_rows], dtype=float
    )
    stats = perf_stats(list(daily_net_values), ppy=252)
    selection_stats = perf_stats(list(daily_selection_values), ppy=252)
    years = max(len(daily_selection_values) / 252.0, 1e-9)
    net_ann = float(daily_net_values.sum() / years)
    stress_ann = float(daily_stress_values.sum() / years)
    selection_ann = float(daily_selection_values.sum() / years)
    hac_lag = max(1, max_holding_days)
    _mean, _se, active_t = mean_t_hac(list(daily_selection_values), max_lag=hac_lag)
    completed = pd.DataFrame(trades)
    win_rate = float((completed["net_return"] > 0).mean())
    gains = float(completed.loc[completed["net_return"] > 0, "net_return"].sum())
    losses = abs(float(completed.loc[completed["net_return"] < 0, "net_return"].sum()))
    profit_factor = gains / losses if losses > 0 else None
    positive_sectors = sum(value > 0 for value in sector_net.values())
    ohlcv_fraction = _fraction(
        coverage["candidate_entry_ohlc"], coverage["candidate_entry_total"]
    )
    promotion = cfg.get("promotion", {}) or {}
    reasons: list[str] = []
    if len(trades) < int(promotion.get("min_trades", 500)):
        reasons.append("insufficient_trades")
    if selection_ann <= float(promotion.get("min_selection_alpha_ann", 0.0)):
        reasons.append("selection_alpha_not_positive")
    if active_t is None or active_t < float(promotion.get("min_active_t", 2.0)):
        reasons.append("active_t_below_threshold")
    if profit_factor is None or profit_factor < float(promotion.get("min_profit_factor", 1.10)):
        reasons.append("profit_factor_below_threshold")
    if positive_sectors < int(promotion.get("min_positive_sectors", 4)):
        reasons.append("sector_breadth_below_threshold")
    if stress_ann <= float(promotion.get("min_stress_net_ann", 0.0)):
        reasons.append("stress_return_not_positive")
    if ohlcv_fraction < float(promotion.get("min_candidate_execution_ohlcv_fraction", 0.95)):
        reasons.append("execution_ohlcv_coverage_below_threshold")
    promotable = not reasons
    summary = {
        "trades": len(trades),
        "net_ann": round(net_ann, 8),
        "selection_alpha_ann": round(selection_ann, 8),
        "stress_net_ann": round(stress_ann, 8),
        "net_sharpe": round(float(stats["sharpe"]), 6),
        "selection_sharpe": round(float(selection_stats["sharpe"]), 6),
        "active_t": round(float(active_t), 6) if active_t is not None else "",
        "max_drawdown": round(float(stats["max_dd"]), 8),
        "win_rate": round(win_rate, 6),
        "profit_factor": round(profit_factor, 6) if profit_factor is not None else "",
        "positive_sectors": positive_sectors,
        "candidate_execution_ohlcv_fraction": round(ohlcv_fraction, 6),
        "spread_exact_weight_fraction": round(
            _fraction(coverage["spread_exact"], coverage["spread_total"]), 6
        ),
        "promotable": int(promotable),
        "rejection_reasons": ";".join(reasons),
    }
    if args.evaluation_json is not None:
        write_manifest(
            args.evaluation_json.expanduser().resolve(),
            {
                "acceptance": "PASS",
                "panel_build": panel_dir.name,
                "signal_window": {
                    "from": args.signal_from or signals[0],
                    "to": args.signal_to or signals[-1],
                },
                "parameters": {
                    "tail_fraction": tail_fraction,
                    "signal_every_n_snapshots": signal_every,
                    "max_holding_days": max_holding_days,
                    "invalidation_score_z": invalidation_score_z,
                    "target_long_gross": target_long_gross,
                    "max_position_weight": max_position_weight,
                },
                "summary": summary,
                "sector_selection_alpha": sector_net,
                "daily_selection_returns": [
                    float(str(row["selection_alpha_net"])) for row in metric_rows
                ],
                "daily_stress_returns": [
                    float(str(row["stress_net_return"])) for row in metric_rows
                ],
                "trade_net_returns": [float(str(row["net_return"])) for row in trades],
                "source_sha256": sha256_file(Path(__file__).resolve()),
            },
        )
        return 0

    out_dir = paths.output_dir / str(cfg.get("dir", "tactical_long")) / panel_dir.name
    trades_path = out_dir / "tactical_long_trades.csv"
    daily_path = out_dir / "tactical_long_daily.csv"
    summary_path = out_dir / "tactical_long_summary.csv"
    cost_inputs_path = out_dir / "long_cost_inputs.csv"
    manifest_path = out_dir / "tactical_long_manifest.json"
    output_paths = [trades_path, daily_path, summary_path, cost_inputs_path, manifest_path]
    if args.force:
        for path in output_paths:
            if path.exists():
                path.unlink()
    try:
        fail_if_exists(output_paths, force=args.force)
    except FileExistsError as exc:
        LOGGER.error("%s", exc)
        return 1
    out_dir.mkdir(parents=True, exist_ok=True)
    write_csv(trades_path, TRADE_FIELDS, trades)
    write_csv(
        daily_path,
        [
            "date",
            "gross_return",
            "net_return",
            "stress_net_return",
            "selection_alpha_net",
            "open_positions",
            "long_gross",
        ],
        daily_rows,
    )
    write_csv(summary_path, list(summary), [summary])
    cost_rows = cost_model.used_rows()
    write_csv(
        cost_inputs_path,
        [
            "as_of_date",
            "ticker",
            "half_spread_bps",
            "spread_source",
            "borrow_fee_annual",
            "borrow_source",
            "shortable_shares",
            "shortable_source",
            "short_available",
        ],
        cost_rows,
    )
    write_manifest(
        manifest_path,
        {
            "stage": "stage11_tactical_single_name_long",
            "generated_at": utc_now(),
            "acceptance": "PASS",
            "promotion_status": "PROMOTABLE" if promotable else "NOT_PROMOTABLE",
            "panel_build": panel_dir.name,
            "pipelines": pipelines,
            "policy": {
                "execution": (
                    "signal_D_close; enter_D_plus_1_adjusted_open; evaluate_every_close;"
                    "first_signal_or_time_exit_executes_next_adjusted_open"
                ),
                "tail_fraction": tail_fraction,
                "signal_every_n_snapshots": signal_every,
                "max_holding_days": max_holding_days,
                "invalidation_score_z": invalidation_score_z,
                "target_long_gross": target_long_gross,
                "max_position_weight": max_position_weight,
                "parameter_artifact": str(args.parameter_file.resolve())
                if args.parameter_file
                else "",
            },
            "summary": summary,
            "sector_selection_alpha": sector_net,
            "protocol_sha256": lockbox["protocol_sha256"],
            "inputs_sha256": {
                "config.yaml": sha256_file(config_path),
                "backtest/16g_tactical_long_replay.py": sha256_file(Path(__file__).resolve()),
                "backtest/short_costs.py": sha256_file(
                    PACKAGE_ROOT / "backtest" / "short_costs.py"
                ),
                "calibration_panel_manifest.json": sha256_file(panel_manifest_path),
                "calibration_panel.csv": sha256_file(panel_path),
                "survivorship_manifest.json": sha256_file(survivorship_manifest_path),
                "prices_adjclose.csv": sha256_file(prices_path),
                "execution_ohlcv_manifest.json": sha256_file(execution_manifest_path),
                "prices_adjusted_ohlcv.csv.gz": sha256_file(execution_prices_path),
                **(
                    {"tactical_long_parameter_artifact.json": parameter_sha256}
                    if parameter_sha256
                    else {}
                ),
            },
            "files": {
                trades_path.name: {"sha256": sha256_file(trades_path), "rows": len(trades)},
                daily_path.name: {"sha256": sha256_file(daily_path), "rows": len(daily_rows)},
                summary_path.name: {"sha256": sha256_file(summary_path), "rows": 1},
                cost_inputs_path.name: {
                    "sha256": sha256_file(cost_inputs_path),
                    "rows": len(cost_rows),
                },
            },
        },
    )
    LOGGER.info(
        "TACTICAL LONG: PASS / %s trades=%d net_ann=%.4f selection_alpha=%.4f "
        "active_t=%s stress=%.4f -> %s",
        "PROMOTABLE" if promotable else "NOT_PROMOTABLE",
        len(trades),
        net_ann,
        selection_ann,
        f"{active_t:.3f}" if active_t is not None else "NA",
        stress_ann,
        out_dir,
    )
    if reasons:
        LOGGER.info("Promotion rejections: %s", ";".join(reasons))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
