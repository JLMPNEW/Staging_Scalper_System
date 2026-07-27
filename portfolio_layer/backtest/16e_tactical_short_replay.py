#!/usr/bin/env python3
"""Stage 11 tactical single-name short replay.

This is intentionally separate from 16c. It tests a path-dependent short
strategy: select the lowest-ranked names inside each sector after session D,
enter at the D+1 adjusted open, and evaluate the position at every subsequent
close including D+1. Capital returns to cash; the engine never maintains a
synthetic permanent short sleeve.

The profit target is net of estimated round-trip execution and accrued borrow.
Same-session exits pay spread and commission on both sides but no overnight
borrow. Prices come from the sealed 15c execution panel; a missing adjusted
open is never replaced by a close.

2026-07-25 hardening (tactical long/short diagnostic). The prior replay was
dominated by a single corrupt expert-market tape: 91.6% of realized short
losses came from one name (LLNW_EGIO sub-penny prints), and XTIA carried a
$291M adjusted open. The engine now refuses those names at the gate rather than
booking their arithmetic:

  * universe hygiene   - minimum entry price ($1 shared, $5 short-side), minimum
                         trailing 20-session median dollar volume, a hard drop
                         after the survivorship delist_date, and a data-fault
                         detector for >80% single-session moves printed on
                         <10k shares. Every exclusion is written to
                         tactical_short_hygiene_exclusions.csv.
  * gap guard          - a position whose exposure exceeds 3x its entry weight
                         is force-covered; a portfolio day beyond +/-50% is
                         marked data_fault and removed from P&L.
  * tiered spreads     - a price-tiered fallback half spread replaces the flat
                         15bps, which was optimistic by an order of magnitude
                         for microcaps.
  * fail-closed borrow - unknown shortability no longer admits a trade.
  * beta-adjusted alpha- constant-dollar sector-ETF hedge sized by a rolling
                         63-session beta, over windows matched to the position.
  * honest ruin        - a ruined curve publishes no annualized return or Sharpe
                         instead of leaking a clamped -1.0 into the metrics.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import sqlite3
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import date
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
    parse_spread_tiers,
    selftest_short_cost_model,
)
from portfolio_layer.backtest.walkforward_common import (  # noqa: E402
    HYGIENE_FIELDS,
    HygienePanel,
    HygienePolicy,
    hac_lag_for_hold,
    perf_stats,
    rolling_beta,
    selftest_perf_stats,
    selftest_tactical_hygiene,
)
from portfolio_layer.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from portfolio_layer.core.contracts import fail_if_exists, sha256_file, write_csv, write_manifest  # noqa: E402
from portfolio_layer.core.db import utc_now  # noqa: E402
from portfolio_layer.core.logging_utils import configure_utc_logging  # noqa: E402
from portfolio_layer.core.paths import resolve_runtime_paths  # noqa: E402
from portfolio_layer.research.stage11_common import (  # noqa: E402
    independent_windows,
    load_lockbox,
    manifest_file_errors,
    mean_t_hac,
)


LOGGER = logging.getLogger("tactical_short_replay")
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
    "exit_close",
    "entry_score_z",
    "entry_weight",
    "holding_days",
    "exit_reason",
    "gross_return",
    "net_return",
    "selection_alpha_net",
    "entry_cost_bps",
    "exit_cost_bps",
    "borrow_cost_bps",
    "stress_cost_bps",
    "borrow_source",
    "availability_source",
    "entry_beta",
    "entry_beta_source",
]
DAILY_FIELDS = [
    "date",
    "gross_return",
    "net_return",
    "stress_net_return",
    "selection_alpha_net",
    "open_positions",
    "short_gross",
    "data_fault",
]


@dataclass
class Position:
    ticker: str
    pipeline: str
    signal_date: str
    entry_date: str
    entry_price: float
    previous_price: float
    previous_mark_date: str
    entry_score_z: float
    initial_weight: float
    current_exposure: float
    # Beta-adjusted, CONSTANT-DOLLAR hedge. The hedge notional is fixed at entry
    # (beta * initial_weight); it deliberately does not compound with the stock,
    # so a name that gaps 10x cannot silently 10x its own benchmark leg.
    entry_beta: float = 1.0
    entry_beta_source: str = ""
    holding_days: int = 0
    gross_pnl: float = 0.0
    benchmark_pnl: float = 0.0
    transaction_cost: float = 0.0
    stress_cost: float = 0.0
    borrow_cost: float = 0.0
    entry_cost_bps: float = 0.0
    last_borrow_source: str = ""
    availability_source: str = ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Stage 11 tactical single-name short replay.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--panel-build", default=None)
    parser.add_argument(
        "--parameters",
        "--parameter-file",
        dest="parameter_file",
        type=Path,
        default=None,
        help=(
            "Sealed calibration artifact (16f tactical_short_parameters.json). When absent the "
            "manifest records parameters_source=config_defaults explicitly."
        ),
    )
    parser.add_argument(
        "--output-suffix",
        default="",
        help="Append to the panel-build output directory so a sealed run is never overwritten.",
    )
    parser.add_argument(
        "--market-positioning-db",
        type=Path,
        default=None,
        help="Optional sealed SQLite snapshot used by nested calibration.",
    )
    parser.add_argument(
        "--liquid-tier",
        action="store_true",
        help=(
            "Pre-registered liquid-tier variant (LIQUID_SHORT_TEST.md). Reads the "
            "tactical_short_liquid config block, which may only TIGHTEN the entry-price and "
            "dollar-volume floors, and publishes to the sibling tactical_short_liquid directory. "
            "OFF by default: without this flag the block is not read at all."
        ),
    )
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
    net_return_if_cover: float,
    holding_days: int,
    current_score_z: float | None,
    net_profit_target: float,
    stop_loss: float,
    max_holding_days: int,
    invalidation_score_z: float,
    borrow_available: bool,
    borrow_fee_annual: float,
    max_borrow_fee_annual: float,
    exposure_multiple: float = 0.0,
    gap_exposure_cap_multiple: float = math.inf,
    data_fault: bool = False,
) -> str | None:
    """Deterministic EOD exit priority. Risk exits win ties."""
    # A short whose exposure has run past the cap is an unbounded-loss position, not a thesis. It
    # leaves the book before any P&L rule is consulted, exactly as a broker forced buy-in would.
    if data_fault:
        return "data_fault"
    if exposure_multiple > gap_exposure_cap_multiple:
        return "gap_exposure_cap"
    if not borrow_available:
        return "borrow_unavailable"
    if borrow_fee_annual > max_borrow_fee_annual:
        return "borrow_fee_limit"
    if net_return_if_cover <= -stop_loss:
        return "stop_loss"
    if net_return_if_cover >= net_profit_target:
        return "profit_target"
    if current_score_z is not None and current_score_z >= invalidation_score_z:
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
        ["score_z_pipeline_date", "ticker"]
    )
    if clean.empty:
        return clean
    count = int(math.ceil(len(clean) * tail_fraction))
    count = min(len(clean), max(min_names, count), max_names)
    return clean.head(count)


def _selftest() -> None:
    assert exit_reason(
        net_return_if_cover=0.03,
        holding_days=2,
        current_score_z=-1.0,
        net_profit_target=0.02,
        stop_loss=0.05,
        max_holding_days=5,
        invalidation_score_z=0.0,
        borrow_available=True,
        borrow_fee_annual=0.02,
        max_borrow_fee_annual=0.25,
    ) == "profit_target"
    assert exit_reason(
        net_return_if_cover=-0.06,
        holding_days=1,
        current_score_z=-1.0,
        net_profit_target=0.02,
        stop_loss=0.05,
        max_holding_days=5,
        invalidation_score_z=0.0,
        borrow_available=True,
        borrow_fee_annual=0.02,
        max_borrow_fee_annual=0.25,
    ) == "stop_loss"
    assert exit_reason(
        net_return_if_cover=0.0,
        holding_days=5,
        current_score_z=-1.0,
        net_profit_target=0.02,
        stop_loss=0.05,
        max_holding_days=5,
        invalidation_score_z=0.0,
        borrow_available=True,
        borrow_fee_annual=0.02,
        max_borrow_fee_annual=0.25,
    ) == "time_stop"
    sample = pd.DataFrame(
        {
            "ticker": ["A", "B", "C", "D"],
            "score_z_pipeline_date": [-2.0, -1.0, 0.0, 1.0],
        }
    )
    chosen = select_candidates(sample, tail_fraction=0.25, min_names=1, max_names=2)
    assert list(chosen["ticker"]) == ["A"]
    assert (
        _overnight_borrow_cost(
            exposure=0.01,
            annual_fee=0.10,
            previous_day="2020-01-02",
            current_day="2020-01-03",
            is_entry_day=True,
        )
        == 0.0
    )
    carried_borrow = _overnight_borrow_cost(
        exposure=0.01,
        annual_fee=0.10,
        previous_day="2020-01-03",
        current_day="2020-01-06",
        is_entry_day=False,
    )
    assert abs(carried_borrow - 0.01 * 0.10 * 3 / 365.0) < 1e-12
    test_position = Position(
        ticker="A",
        pipeline="test",
        signal_date="2020-01-02",
        entry_date="2020-01-03",
        entry_price=10.0,
        previous_price=9.8,
        previous_mark_date="2020-01-03",
        entry_score_z=-1.0,
        initial_weight=0.01,
        current_exposure=0.0098,
        gross_pnl=0.0002,
        transaction_cost=0.00001,
    )
    assert abs(
        _net_return_if_cover(test_position, estimated_exit_cost=0.00001) - 0.018
    ) < 1e-12
    # --- gap guard (2026-07-25 fix 2): exposure cap and data faults outrank every P&L rule ---
    assert exit_reason(
        net_return_if_cover=0.01,
        holding_days=1,
        current_score_z=-1.0,
        net_profit_target=0.02,
        stop_loss=0.05,
        max_holding_days=5,
        invalidation_score_z=0.0,
        borrow_available=True,
        borrow_fee_annual=0.02,
        max_borrow_fee_annual=0.25,
        exposure_multiple=3.5,
        gap_exposure_cap_multiple=3.0,
    ) == "gap_exposure_cap"
    assert exit_reason(
        net_return_if_cover=0.05,
        holding_days=1,
        current_score_z=-1.0,
        net_profit_target=0.02,
        stop_loss=0.05,
        max_holding_days=5,
        invalidation_score_z=0.0,
        borrow_available=True,
        borrow_fee_annual=0.02,
        max_borrow_fee_annual=0.25,
        exposure_multiple=1.0,
        gap_exposure_cap_multiple=3.0,
        data_fault=True,
    ) == "data_fault"
    assert exit_reason(
        net_return_if_cover=0.01,
        holding_days=1,
        current_score_z=-1.0,
        net_profit_target=0.02,
        stop_loss=0.05,
        max_holding_days=5,
        invalidation_score_z=0.0,
        borrow_available=True,
        borrow_fee_annual=0.02,
        max_borrow_fee_annual=0.25,
        exposure_multiple=2.5,
        gap_exposure_cap_multiple=3.0,
    ) is None
    # --- min-trade band (2026-07-25 fix 6) ---
    assert _below_min_trade_band(
        weight=0.0005, commission_fraction=1.25 / 300000.0, min_fraction=0.005
    )
    assert not _below_min_trade_band(
        weight=0.015, commission_fraction=1.25 / 300000.0, min_fraction=0.005
    )
    assert not _below_min_trade_band(
        weight=0.0005, commission_fraction=1.25 / 300000.0, min_fraction=0.0
    )
    # --- daily sanity band (2026-07-25 fix 2) ---
    assert _is_data_fault_day(0.51, 0.50) and not _is_data_fault_day(-0.49, 0.50)
    assert _is_data_fault_day(-0.90, 0.50)
    # --- pre-registered liquid tier (2026-07-26): fail-closed, tighten-only, off by default ---
    liquid_gates = resolve_liquid_tier_gates(
        {"min_short_entry_price": 10.0, "min_median_dollar_volume_20d": 5_000_000},
        base_min_entry_price=5.0,
        base_min_median_dollar_volume=250_000.0,
    )
    assert liquid_gates == (10.0, 5_000_000.0)
    for bad in (
        {},
        {"min_short_entry_price": 2.0, "min_median_dollar_volume_20d": 5_000_000},
        {"min_short_entry_price": 10.0, "min_median_dollar_volume_20d": 1000},
    ):
        try:
            resolve_liquid_tier_gates(
                bad, base_min_entry_price=5.0, base_min_median_dollar_volume=250_000.0
            )
        except ValueError:
            pass
        else:  # pragma: no cover - defensive
            raise AssertionError(f"a loosening/absent liquid block must fail closed: {bad}")
    # the tightened floors reach the shared hygiene policy exactly as the full-universe ones do
    liquid_policy = HygienePolicy.from_config(
        {"min_entry_price": 1.0, "min_median_dollar_volume_20d": liquid_gates[1]},
        min_entry_price_override=liquid_gates[0],
    )
    assert liquid_policy.min_entry_price == 10.0
    assert liquid_policy.min_median_dollar_volume == 5_000_000.0
    selftest_short_cost_model()
    selftest_perf_stats()
    selftest_tactical_hygiene()
    print("tactical-short replay self-test: PASS")


def _below_min_trade_band(
    *, weight: float, commission_fraction: float, min_fraction: float
) -> bool:
    """True when flat commission would eat more than the allowed fraction of the position notional.

    transaction_costs.min_position_commission_fraction existed in config but was never read by the
    tactical replays, so sub-threshold entries were being booked as if they were free to trade.
    """
    if min_fraction <= 0:
        return False
    if weight <= 0:
        return True
    return (commission_fraction / weight) > min_fraction


def resolve_liquid_tier_gates(
    liquid_cfg: dict[str, Any],
    *,
    base_min_entry_price: float,
    base_min_median_dollar_volume: float,
) -> tuple[float, float]:
    """Entry gates for the pre-registered liquid-tier variant, or a hard failure.

    FAIL CLOSED. The liquid tier exists to test the SAME engine on a strictly more liquid slice, so
    it may only tighten the two universe floors. A block that loosens either one would silently
    widen the universe under a name that promises the opposite, and is rejected here.
    """
    if not liquid_cfg:
        raise ValueError(
            "--liquid-tier requires a tactical_short_liquid config block; none is configured"
        )
    min_entry_price = float(liquid_cfg.get("min_short_entry_price", base_min_entry_price))
    min_dollar_volume = float(
        liquid_cfg.get("min_median_dollar_volume_20d", base_min_median_dollar_volume)
    )
    if min_entry_price < base_min_entry_price or min_dollar_volume < base_min_median_dollar_volume:
        raise ValueError(
            "tactical_short_liquid must TIGHTEN the full-universe gates, never loosen them "
            f"(entry price {min_entry_price} vs {base_min_entry_price}; "
            f"median dollar volume {min_dollar_volume} vs {base_min_median_dollar_volume})"
        )
    if not math.isfinite(min_entry_price) or not math.isfinite(min_dollar_volume):
        raise ValueError("tactical_short_liquid gates must be finite")
    return min_entry_price, min_dollar_volume


def _is_data_fault_day(daily_return: float, band: float) -> bool:
    """A portfolio day outside the sanity band is a data fault, not a return."""
    if band <= 0:
        return False
    return bool(abs(float(daily_return)) > band)


def _latest(root: Path, marker: str, wanted: str | None) -> Path | None:
    if wanted:
        candidate = root / wanted
        return candidate if (candidate / marker).exists() else None
    if not root.exists():
        return None
    builds = sorted(path for path in root.iterdir() if path.is_dir() and (path / marker).exists())
    return builds[-1] if builds else None


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


def _price(prices: pd.DataFrame, day: str, ticker: str) -> float | None:
    if day not in prices.index or ticker not in prices.columns:
        return None
    try:
        value = float(prices.at[day, ticker])
    except (TypeError, ValueError):
        return None
    return value if np.isfinite(value) and value > 0 else None


def _transaction_cost(
    *,
    weight: float,
    resolved_half_spread_bps: float,
    commission_fraction: float,
) -> float:
    return abs(weight) * resolved_half_spread_bps / 1e4 + commission_fraction


def _overnight_borrow_cost(
    *,
    exposure: float,
    annual_fee: float,
    previous_day: str | None,
    current_day: str,
    is_entry_day: bool,
) -> float:
    if is_entry_day:
        return 0.0
    if previous_day is None:
        raise ValueError("A carried short position requires a previous trading session")
    calendar_days = (date.fromisoformat(current_day) - date.fromisoformat(previous_day)).days
    if calendar_days <= 0:
        raise ValueError("Short borrow interval must advance in calendar time")
    return exposure * annual_fee * calendar_days / 365.0


def _net_return_if_cover(
    position: Position,
    *,
    estimated_exit_cost: float,
) -> float:
    return (
        position.gross_pnl
        - position.borrow_cost
        - position.transaction_cost
        - estimated_exit_cost
    ) / max(position.initial_weight, 1e-12)


def _load_parameter_overrides(path: Path | None) -> tuple[dict[str, Any], str | None]:
    if path is None:
        return {}, None
    resolved = path.expanduser().resolve()
    payload = json.loads(resolved.read_text(encoding="utf-8"))
    if payload.get("acceptance") != "PASS":
        raise ValueError(f"Short parameter artifact is not accepted: {resolved}")
    parameters = payload.get("parameters")
    if not isinstance(parameters, dict):
        raise ValueError(f"Short parameter artifact lacks a parameters object: {resolved}")
    return parameters, sha256_file(resolved)


def _read_execution_ohlcv(path: Path) -> dict[str, pd.DataFrame]:
    frame = pd.read_csv(path)
    required = {
        "date",
        "ticker",
        "adj_open",
        "adj_high",
        "adj_low",
        "adj_close",
        # volume is REQUIRED: the dollar-volume floor and the <10k-share data-fault detector
        # cannot be evaluated without it, and silently skipping them would re-admit the
        # corrupt expert-market tape this gate exists to reject.
        "volume",
    }
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"Execution OHLCV panel lacks columns: {sorted(missing)}")
    frame["date"] = frame["date"].astype(str).str.slice(0, 10)
    frame["ticker"] = frame["ticker"].astype(str).str.upper().str.strip()
    if frame.duplicated(["date", "ticker"]).any():
        raise ValueError("Execution OHLCV panel has duplicate date/ticker rows")
    output: dict[str, pd.DataFrame] = {}
    for field in ("adj_open", "adj_high", "adj_low", "adj_close", "volume"):
        frame[field] = pd.to_numeric(frame[field], errors="coerce")
        output[field] = frame.pivot(index="date", columns="ticker", values=field).sort_index()
    return output


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
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        LOGGER.error("%s", exc)
        return 1
    try:
        lockbox = load_lockbox(config, config_path)
    except ValueError as exc:
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
        "usable_for_promoted_training",
        "survivorship_complete",
        "in_lockbox",
        "liquidity_join_available",
        "liquidity_half_spread_bps",
    }
    panel = pd.read_csv(panel_path, usecols=lambda column: column in wanted)
    panel["as_of_date"] = panel["as_of_date"].astype(str).str.slice(0, 10)
    panel["ticker"] = panel["ticker"].astype(str).str.upper().str.strip()
    panel["score_z_pipeline_date"] = pd.to_numeric(
        panel["score_z_pipeline_date"], errors="coerce"
    )
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
        LOGGER.error("No admitted tactical-short rows")
        return 1
    duplicate_rows = panel.duplicated(["as_of_date", "ticker"], keep=False)
    if duplicate_rows.any():
        sample = panel.loc[
            duplicate_rows, ["as_of_date", "ticker", "source_pipeline"]
        ].head(10)
        LOGGER.error("Tactical-short panel has duplicate date/ticker rows: %s", sample.to_dict("records"))
        return 1

    survivorship_build = str(panel_manifest.get("survivorship_panel_build", "")).strip()
    survivorship_dir = (
        paths.output_dir
        / str(cfg_get(config, "survivorship_panel.dir", "survivorship_panel"))
        / survivorship_build
    )
    survivorship_manifest_path = survivorship_dir / "survivorship_manifest.json"
    prices_path = survivorship_dir / "prices_adjclose.csv"
    delisting_events_path = survivorship_dir / "delisting_events.csv"
    if (
        not survivorship_manifest_path.exists()
        or not prices_path.exists()
        or not delisting_events_path.exists()
    ):
        LOGGER.error("Matching survivorship panel is missing")
        return 1
    survivorship_manifest = json.loads(survivorship_manifest_path.read_text(encoding="utf-8"))
    expected_manifest = str(panel_manifest.get("survivorship_panel_manifest_sha256", ""))
    survivorship_errors = manifest_file_errors(
        survivorship_manifest,
        {
            "prices_adjclose.csv": prices_path,
            "delisting_events.csv": delisting_events_path,
        },
    )
    if (
        survivorship_manifest.get("acceptance") != "PASS"
        or sha256_file(survivorship_manifest_path) != expected_manifest
        or survivorship_errors
    ):
        LOGGER.error("Survivorship panel is rejected/stale: %s", survivorship_errors)
        return 1
    prices = pd.read_csv(prices_path, index_col=0)
    prices.index = prices.index.astype(str).str.slice(0, 10)
    prices.columns = [str(column).strip().upper() for column in prices.columns]
    delisting_events = pd.read_csv(delisting_events_path)
    terminal_date_by_ticker = {
        str(row.get("ticker", "")).strip().upper(): str(row.get("delist_date", ""))[:10]
        for row in delisting_events.to_dict("records")
        if str(row.get("ticker", "")).strip() and str(row.get("delist_date", ""))[:10]
    }

    execution_dir = (
        paths.output_dir
        / str(cfg_get(config, "execution_ohlcv_panel.dir", "execution_ohlcv_panel"))
        / survivorship_build
    )
    execution_manifest_path = execution_dir / "execution_ohlcv_manifest.json"
    execution_prices_path = execution_dir / "prices_adjusted_ohlcv.csv.gz"
    if not execution_manifest_path.exists() or not execution_prices_path.exists():
        LOGGER.error(
            "Matching execution OHLCV panel is missing; run backtest/15c for build %s",
            survivorship_build,
        )
        return 1
    execution_manifest = json.loads(execution_manifest_path.read_text(encoding="utf-8"))
    execution_errors = manifest_file_errors(
        execution_manifest,
        {"prices_adjusted_ohlcv.csv.gz": execution_prices_path},
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
    # The hygiene screens and the rolling beta both need history BEFORE the development window,
    # so they are built from the unsliced series and only then consulted inside the window.
    hygiene_cfg = dict(cfg_get(config, "tactical", {}) or {})
    min_short_entry_price = float(
        cfg_get(config, "tactical_short.min_short_entry_price", 5.0)
    )
    # PRE-REGISTERED liquid-tier variant (LIQUID_SHORT_TEST.md). Nothing below runs -- and the
    # tactical_short_liquid block is not even read -- unless --liquid-tier is passed.
    liquid_cfg: dict[str, Any] = {}
    if args.liquid_tier:
        liquid_cfg = dict(cfg_get(config, "tactical_short_liquid", {}) or {})
        try:
            min_short_entry_price, liquid_dollar_volume = resolve_liquid_tier_gates(
                liquid_cfg,
                base_min_entry_price=min_short_entry_price,
                base_min_median_dollar_volume=float(
                    hygiene_cfg.get("min_median_dollar_volume_20d", 250000.0)
                ),
            )
        except ValueError as exc:
            LOGGER.error("%s", exc)
            return 1
        hygiene_cfg["min_median_dollar_volume_20d"] = liquid_dollar_volume
        LOGGER.info(
            "LIQUID TIER: entry price >= %.2f, trailing 20d median dollar volume >= %.0f "
            "(all other tactical-short machinery unchanged)",
            min_short_entry_price,
            liquid_dollar_volume,
        )
    try:
        hygiene_policy = HygienePolicy.from_config(
            hygiene_cfg,
            min_entry_price_override=min_short_entry_price,
        )
    except ValueError as exc:
        LOGGER.error("%s", exc)
        return 1
    hygiene = HygienePanel(
        closes=ohlc["adj_close"], volume=ohlc["volume"], policy=hygiene_policy
    )
    prices_full = prices.sort_index()
    returns_full = prices_full.apply(pd.to_numeric, errors="coerce").pct_change(fill_method=None)
    prices = prices.loc[
        (prices.index >= lockbox["dev_window_start"])
        & (prices.index <= lockbox["dev_window_end"])
    ].sort_index()
    calendar = list(prices.index)
    if len(calendar) < 2:
        LOGGER.error("Survivorship calendar has fewer than two development dates")
        return 1
    calendar_pos = {day: idx for idx, day in enumerate(calendar)}

    cfg = dict(cfg_get(config, "tactical_short", {}) or {})
    cfg.update(parameter_overrides)
    tail_fraction = float(cfg.get("tail_fraction", 0.10))
    min_names = int(cfg.get("min_names_per_sector", 2))
    max_names = int(cfg.get("max_names_per_sector", 10))
    signal_every = max(1, int(cfg.get("signal_every_n_snapshots", 5)))
    net_profit_target = float(cfg.get("net_profit_target", 0.03))
    stop_loss = float(cfg.get("stop_loss", 0.05))
    max_holding_days = int(cfg.get("max_holding_days", 5))
    invalidation_score_z = float(cfg.get("invalidation_score_z", 0.0))
    cooldown_days = int(cfg.get("cooldown_days", 3))
    target_short_gross = float(cfg.get("target_short_gross", 0.30))
    max_position_weight = float(cfg.get("max_position_weight", 0.015))
    max_borrow_fee_annual = float(cfg.get("max_borrow_fee_annual", 0.25))
    gap_exposure_cap_multiple = float(hygiene_cfg.get("gap_exposure_cap_multiple", 3.0))
    daily_sanity_band = float(hygiene_cfg.get("daily_return_sanity_band", 0.50))
    beta_lookback = int(hygiene_cfg.get("beta_lookback_sessions", 63))
    beta_min_obs = int(hygiene_cfg.get("beta_min_observations", 40))
    beta_clip_min = float(hygiene_cfg.get("beta_clip_min", 0.25))
    beta_clip_max = float(hygiene_cfg.get("beta_clip_max", 2.5))
    hac_min_lag = int(hygiene_cfg.get("hac_min_lag_days", 5))
    hac_divisor = int(hygiene_cfg.get("hac_horizon_divisor", 5))
    min_position_commission_fraction = float(
        cfg_get(config, "transaction_costs.min_position_commission_fraction", 0.005)
    )
    if not (
        gap_exposure_cap_multiple > 1.0
        and daily_sanity_band > 0
        and beta_lookback >= beta_min_obs >= 2
        and 0 < beta_clip_min <= beta_clip_max
        and hac_min_lag >= 0
        and hac_divisor >= 1
        and min_position_commission_fraction >= 0
    ):
        LOGGER.error("Invalid tactical hygiene/gap/beta policy")
        return 1
    if not (
        0 < tail_fraction <= 0.5
        and min_names >= 1
        and max_names >= min_names
        and 0 < net_profit_target < 1
        and 0 < stop_loss < 1
        and max_holding_days >= 1
        and 0 < target_short_gross <= 1
        and 0 < max_position_weight <= target_short_gross
    ):
        LOGGER.error("Invalid tactical_short policy")
        return 1

    cost_cfg = cfg.get("short_costs", {}) or {}
    db_path = (
        args.market_positioning_db.expanduser().resolve()
        if args.market_positioning_db is not None
        else resolve_path(
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
    )
    all_tickers = set(panel["ticker"])
    closes_exec = ohlc["adj_close"]

    def _reference_price(day: str, ticker: str) -> float | None:
        """Session adjusted close used only to place a name in a fallback spread tier."""
        return _price(closes_exec, day, ticker)

    try:
        spread_tiers = parse_spread_tiers(cost_cfg.get("tiered_half_spread_fallback_bps"))
    except ValueError as exc:
        LOGGER.error("Invalid tiered_half_spread_fallback_bps: %s", exc)
        return 1
    try:
        cost_model = PITShortCostModel(
            db_path=db_path,
            tickers=all_tickers,
            start_date=calendar[0],
            end_date=calendar[-1],
            exact_half_spreads=_exact_spreads(panel),
            spread_fallback_bps=float(cost_cfg.get("historical_half_spread_fallback_bps", 15.0)),
            # 0.25 default (was 0.10): a name with no IB observation at all is hard to borrow.
            borrow_fee_fallback_annual=float(cost_cfg.get("missing_borrow_fee_annual", 0.25)),
            max_borrow_fee_age_days=int(cost_cfg.get("max_borrow_fee_age_days", 7)),
            max_shortable_age_days=int(cost_cfg.get("max_shortable_age_days", 7)),
            allow_fee_proxy_availability=bool(
                cost_cfg.get("allow_fee_proxy_availability", True)
            ),
            # FAIL CLOSED (was True): unknown shortability admitted 38.6% of short trades.
            allow_unknown_availability=bool(cost_cfg.get("allow_unknown_availability", False)),
            stress_spread_fallback_bps=float(
                cost_cfg.get("stress_half_spread_fallback_bps", 30.0)
            ),
            stress_spread_multiplier=float(
                cost_cfg.get("stress_observed_spread_multiplier", 1.5)
            ),
            stress_borrow_fee_fallback_annual=float(
                cost_cfg.get("stress_missing_borrow_fee_annual", 0.375)
            ),
            stress_borrow_fee_multiplier=float(
                cost_cfg.get("stress_observed_borrow_multiplier", 1.5)
            ),
            tiered_spread_fallback_bps=spread_tiers,
            reference_price=_reference_price,
        )
    except (FileNotFoundError, RuntimeError, ValueError, sqlite3.Error) as exc:
        LOGGER.error("Cannot initialize short-cost model: %s", exc)
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
        signals = [signal for signal in signals if signal >= str(args.signal_from)]
    if args.signal_to:
        signals = [signal for signal in signals if signal <= str(args.signal_to)]
    if not signals:
        LOGGER.error("No signal dates remain in the requested evaluation window")
        return 1
    entries_by_day: dict[str, list[dict[str, Any]]] = {}
    for signal_day in signals:
        pos = calendar_pos.get(signal_day)
        # Entry is pos+1 and holding day one ends on that same session. The
        # latest possible close is therefore pos+max_holding_days.
        if pos is None or pos + max_holding_days >= len(calendar):
            continue
        entry_day = calendar[pos + 1]
        day_frame = panel.loc[panel["as_of_date"] == signal_day]
        selected: list[dict[str, Any]] = []
        for pipeline in pipelines:
            candidates = select_candidates(
                day_frame.loc[day_frame["source_pipeline"] == pipeline],
                tail_fraction=tail_fraction,
                min_names=min_names,
                max_names=max_names,
            )
            for row in candidates.to_dict("records"):
                selected.append(row)
        entries_by_day[entry_day] = selected

    score_lookup = {
        (str(row.as_of_date), str(row.ticker)): float(row.score_z_pipeline_date)
        for row in panel[["as_of_date", "ticker", "score_z_pipeline_date"]]
        .dropna()
        .itertuples(index=False)
    }
    active: dict[str, Position] = {}
    cooldown_until: dict[str, int] = {}
    trades: list[dict[str, object]] = []
    daily_rows: list[dict[str, object]] = []
    sector_net: dict[str, float] = {pipeline: 0.0 for pipeline in pipelines}
    coverage = {
        "borrow_total": 0.0,
        "borrow_actual": 0.0,
        "availability_total": 0.0,
        "availability_observed": 0.0,
        "availability_fee_proxy": 0.0,
        "spread_total": 0.0,
        "spread_exact": 0.0,
        "candidate_entry_total": 0.0,
        "candidate_entry_ohlc": 0.0,
        "candidate_terminal_excluded": 0.0,
    }
    missing_close_observations = 0
    hygiene_rows: list[dict[str, object]] = []
    hygiene_counts: Counter[str] = Counter()
    forced_exit_counts: Counter[str] = Counter()
    data_fault_days: list[dict[str, object]] = []
    beta_source_counts: Counter[str] = Counter()
    beta_cache: dict[tuple[str, str, str], tuple[float, str]] = {}

    def _record_exclusion(
        *,
        as_of: str,
        entry_day: str,
        ticker: str,
        pipeline: str,
        reason: str,
        entry_price: float | None,
        adv: float | None,
    ) -> None:
        """Fail VISIBLE: every rejected candidate is written out with its reason."""
        hygiene_counts[reason] += 1
        hygiene_rows.append(
            {
                "as_of_date": as_of,
                "entry_date": entry_day,
                "ticker": ticker,
                "source_pipeline": pipeline,
                "reason": reason,
                "entry_price": "" if entry_price is None else round(float(entry_price), 8),
                "median_dollar_volume_20d": "" if adv is None else round(float(adv), 2),
                "book": "short",
            }
        )

    def _entry_beta(ticker: str, pipeline: str, as_of: str) -> tuple[float, str]:
        etf = sector_etfs.get(pipeline, "")
        key = (ticker, etf, as_of)
        cached = beta_cache.get(key)
        if cached is not None:
            return cached
        if not etf or ticker not in returns_full.columns or etf not in returns_full.columns:
            value = (1.0, "missing_benchmark")
        else:
            value = rolling_beta(
                pd.Series(returns_full[ticker]),
                pd.Series(returns_full[etf]),
                as_of=as_of,
                lookback=beta_lookback,
                min_observations=beta_min_obs,
                clip_min=beta_clip_min,
                clip_max=beta_clip_max,
            )
        beta_cache[key] = value
        return value

    for calendar_index, day in enumerate(calendar):
        previous_day = calendar[calendar_index - 1] if calendar_index > 0 else None
        daily_gross = 0.0
        daily_net = 0.0
        daily_stress = 0.0
        daily_selection = 0.0
        # Signals formed at D close enter at D+1 open. New positions are added
        # before the close loop so the D+1 close is holding day one.
        candidates = entries_by_day.get(day, [])
        if candidates:
            current_gross = sum(position.current_exposure for position in active.values())
            remaining = max(0.0, target_short_gross - current_gross)
            available_rows: list[tuple[dict[str, Any], Any]] = []
            for row in candidates:
                ticker = str(row["ticker"])
                pipeline_name = str(row["source_pipeline"])
                signal_as_of = str(row["as_of_date"])
                if ticker in active or calendar_index <= cooldown_until.get(ticker, -1):
                    continue
                terminal_date = terminal_date_by_ticker.get(ticker)
                # Hard drop on/after the sealed survivorship delist_date. Entering a name on its
                # own terminal session is not an executable trade.
                if terminal_date and terminal_date <= day:
                    coverage["candidate_terminal_excluded"] += 1.0
                    _record_exclusion(
                        as_of=signal_as_of, entry_day=day, ticker=ticker,
                        pipeline=pipeline_name, reason="delisted_on_or_before_entry",
                        entry_price=None, adv=None,
                    )
                    continue
                coverage["candidate_entry_total"] += 1.0
                entry_open = _price(opens, day, ticker)
                if entry_open is None:
                    _record_exclusion(
                        as_of=signal_as_of, entry_day=day, ticker=ticker,
                        pipeline=pipeline_name, reason="missing_execution_open",
                        entry_price=None, adv=None,
                    )
                    continue
                coverage["candidate_entry_ohlc"] += 1.0
                rejection = hygiene.entry_rejection(day, ticker, entry_open)
                if rejection is not None:
                    reason, adv = rejection
                    _record_exclusion(
                        as_of=signal_as_of, entry_day=day, ticker=ticker,
                        pipeline=pipeline_name, reason=reason,
                        entry_price=entry_open, adv=adv,
                    )
                    continue
                resolved = cost_model.resolve(signal_as_of, ticker)
                if not resolved.short_available:
                    _record_exclusion(
                        as_of=signal_as_of, entry_day=day, ticker=ticker,
                        pipeline=pipeline_name, reason="borrow_unavailable",
                        entry_price=entry_open, adv=None,
                    )
                    continue
                if resolved.borrow_fee_annual > max_borrow_fee_annual:
                    _record_exclusion(
                        as_of=signal_as_of, entry_day=day, ticker=ticker,
                        pipeline=pipeline_name, reason="borrow_fee_above_limit",
                        entry_price=entry_open, adv=None,
                    )
                    continue
                available_rows.append((row, resolved))
            if available_rows and remaining > 0:
                grouped: dict[str, list[tuple[dict[str, Any], Any]]] = {}
                for row, resolved in available_rows:
                    grouped.setdefault(str(row["source_pipeline"]), []).append((row, resolved))
                for pipeline in sorted(grouped):
                    rows = grouped[pipeline]
                    existing_sector = sum(
                        position.current_exposure
                        for position in active.values()
                        if position.pipeline == pipeline
                    )
                    sector_target = target_short_gross / max(1, len(pipelines))
                    sector_remaining = max(0.0, sector_target - existing_sector)
                    weight = min(max_position_weight, sector_remaining / len(rows))
                    for row, resolved in rows:
                        ticker = str(row["ticker"])
                        entry_open = _price(opens, day, ticker)
                        if entry_open is None or weight <= 0:
                            continue
                        if _below_min_trade_band(
                            weight=weight,
                            commission_fraction=commission_fraction,
                            min_fraction=min_position_commission_fraction,
                        ):
                            _record_exclusion(
                                as_of=str(row["as_of_date"]), entry_day=day, ticker=ticker,
                                pipeline=pipeline, reason="below_min_trade_band",
                                entry_price=entry_open, adv=None,
                            )
                            continue
                        if resolved.shortable_shares is not None:
                            required_shares = aum_usd * weight / entry_open
                            if resolved.shortable_shares + 1e-9 < required_shares:
                                _record_exclusion(
                                    as_of=str(row["as_of_date"]), entry_day=day, ticker=ticker,
                                    pipeline=pipeline, reason="insufficient_shortable_shares",
                                    entry_price=entry_open, adv=None,
                                )
                                continue
                        entry_cost = _transaction_cost(
                            weight=weight,
                            resolved_half_spread_bps=resolved.half_spread_bps,
                            commission_fraction=commission_fraction,
                        )
                        stress_entry = _transaction_cost(
                            weight=weight,
                            resolved_half_spread_bps=cost_model.stressed_half_spread_bps(resolved),
                            commission_fraction=commission_fraction,
                        )
                        coverage["spread_total"] += weight
                        if resolved.spread_source == "ibkr_exact":
                            coverage["spread_exact"] += weight
                        coverage["availability_total"] += weight
                        if resolved.shortable_shares is not None:
                            coverage["availability_observed"] += weight
                        elif resolved.shortable_source.startswith("fee_proxy:"):
                            coverage["availability_fee_proxy"] += weight
                        beta_value, beta_source = _entry_beta(
                            ticker, pipeline, str(row["as_of_date"])
                        )
                        beta_source_counts[beta_source] += 1
                        active[ticker] = Position(
                            ticker=ticker,
                            pipeline=pipeline,
                            signal_date=str(row["as_of_date"]),
                            entry_date=day,
                            entry_price=entry_open,
                            previous_price=entry_open,
                            previous_mark_date="",
                            entry_score_z=float(row["score_z_pipeline_date"]),
                            initial_weight=weight,
                            current_exposure=weight,
                            entry_beta=beta_value,
                            entry_beta_source=beta_source,
                            transaction_cost=entry_cost,
                            stress_cost=stress_entry,
                            entry_cost_bps=entry_cost / weight * 1e4,
                            last_borrow_source=resolved.borrow_source,
                            availability_source=resolved.shortable_source,
                        )
                        daily_net -= entry_cost
                        daily_stress -= stress_entry
                        daily_selection -= entry_cost

        exits: list[tuple[str, str, Any, float, float, float]] = []
        for ticker, position in list(active.items()):
            # The sealed 15b close is authoritative. 15c is required only for
            # the entry open; Yahoo may omit an intraday row that a published
            # delisted export legitimately supplies at the close.
            close_price = _price(prices, day, ticker)
            if close_price is None:
                # A halt/delisting gap is not permission to look ahead or execute at a stale
                # mark. Carry the last observable mark, accrue borrow, and defer every exit
                # decision until an executable close (or sealed terminal settlement) exists.
                new_today = position.entry_date == day
                if not new_today:
                    if previous_day is None:
                        LOGGER.error("Prior session missing for carried position %s on %s", ticker, day)
                        return 1
                    resolved_borrow = cost_model.resolve(str(previous_day), ticker)
                    borrow = _overnight_borrow_cost(
                        exposure=position.current_exposure,
                        annual_fee=resolved_borrow.borrow_fee_annual,
                        previous_day=str(previous_day),
                        current_day=day,
                        is_entry_day=False,
                    )
                    stress_borrow = _overnight_borrow_cost(
                        exposure=position.current_exposure,
                        annual_fee=cost_model.stressed_borrow_fee_annual(resolved_borrow),
                        previous_day=str(previous_day),
                        current_day=day,
                        is_entry_day=False,
                    )
                    calendar_days = (
                        date.fromisoformat(day) - date.fromisoformat(str(previous_day))
                    ).days
                    weighted = position.current_exposure * calendar_days / 365.0
                    coverage["borrow_total"] += weighted
                    if resolved_borrow.borrow_source != "conservative_fallback":
                        coverage["borrow_actual"] += weighted
                    position.borrow_cost += borrow
                    position.stress_cost += stress_borrow
                    position.last_borrow_source = resolved_borrow.borrow_source
                    daily_net -= borrow
                    daily_stress -= stress_borrow
                    daily_selection -= borrow
                position.holding_days += 1
                missing_close_observations += 1
                continue
            new_today = position.entry_date == day
            if not new_today and previous_day is None:
                LOGGER.error("Prior session missing for carried position %s on %s", ticker, day)
                return 1
            start_price = position.entry_price if new_today else position.previous_price
            price_return = close_price / start_price - 1.0
            gross = -position.current_exposure * price_return
            etf = sector_etfs.get(position.pipeline, "")
            etf_close = _price(prices, day, etf)
            etf_start = (
                _price(opens, day, etf)
                if new_today
                else (
                    _price(prices, position.previous_mark_date, etf)
                    if position.previous_mark_date
                    else _price(opens, position.entry_date, etf)
                )
            )
            if etf_close is None or etf_start is None:
                LOGGER.error("Execution OHLCV lacks sector ETF %s on %s", etf, day)
                return 1
            # Beta-adjusted, CONSTANT-DOLLAR hedge over a window matched exactly to the position's
            # own mark window (etf_start is the same session boundary as start_price). The hedge
            # notional stays at beta * initial_weight; compounding it with the stock -- as the prior
            # implementation did -- let a runaway short inflate its own benchmark leg and hid the
            # true active loss.
            hedge_notional = position.entry_beta * position.initial_weight
            benchmark = -hedge_notional * (etf_close / etf_start - 1.0)

            borrow = 0.0
            stress_borrow = 0.0
            if new_today:
                resolved_borrow = cost_model.resolve(position.signal_date, ticker)
            else:
                resolved_borrow = cost_model.resolve(str(previous_day), ticker)
                borrow = _overnight_borrow_cost(
                    exposure=position.current_exposure,
                    annual_fee=resolved_borrow.borrow_fee_annual,
                    previous_day=str(previous_day),
                    current_day=day,
                    is_entry_day=False,
                )
                stress_borrow = _overnight_borrow_cost(
                    exposure=position.current_exposure,
                    annual_fee=cost_model.stressed_borrow_fee_annual(resolved_borrow),
                    previous_day=str(previous_day),
                    current_day=day,
                    is_entry_day=False,
                )
                calendar_days = (
                    date.fromisoformat(day) - date.fromisoformat(str(previous_day))
                ).days
                weighted = position.current_exposure * calendar_days / 365.0
                coverage["borrow_total"] += weighted
                if resolved_borrow.borrow_source != "conservative_fallback":
                    coverage["borrow_actual"] += weighted

            position.gross_pnl += gross
            position.benchmark_pnl += benchmark
            position.borrow_cost += borrow
            position.stress_cost += stress_borrow
            position.holding_days += 1
            position.current_exposure *= close_price / start_price
            position.previous_price = close_price
            position.previous_mark_date = day
            position.last_borrow_source = resolved_borrow.borrow_source
            daily_gross += gross
            daily_net += gross - borrow
            daily_stress += gross - stress_borrow
            daily_selection += gross - benchmark - borrow

            resolved_exit = cost_model.resolve(day, ticker)
            exit_cost = _transaction_cost(
                weight=position.current_exposure,
                resolved_half_spread_bps=resolved_exit.half_spread_bps,
                commission_fraction=commission_fraction,
            )
            stress_exit = _transaction_cost(
                weight=position.current_exposure,
                resolved_half_spread_bps=cost_model.stressed_half_spread_bps(resolved_exit),
                commission_fraction=commission_fraction,
            )
            net_if_cover = _net_return_if_cover(
                position, estimated_exit_cost=exit_cost
            )
            current_score = score_lookup.get((day, ticker))
            exposure_multiple = position.current_exposure / max(position.initial_weight, 1e-12)
            reason = (
                "terminal_delisting"
                if terminal_date_by_ticker.get(ticker) == day
                else exit_reason(
                    net_return_if_cover=net_if_cover,
                    holding_days=position.holding_days,
                    current_score_z=current_score,
                    net_profit_target=net_profit_target,
                    stop_loss=stop_loss,
                    max_holding_days=max_holding_days,
                    invalidation_score_z=invalidation_score_z,
                    borrow_available=resolved_exit.short_available,
                    borrow_fee_annual=resolved_exit.borrow_fee_annual,
                    max_borrow_fee_annual=max_borrow_fee_annual,
                    exposure_multiple=exposure_multiple,
                    gap_exposure_cap_multiple=gap_exposure_cap_multiple,
                    data_fault=hygiene.has_data_fault(day, ticker),
                )
            )
            if reason in ("gap_exposure_cap", "data_fault"):
                forced_exit_counts[reason] += 1
            if reason:
                exits.append(
                    (ticker, reason, resolved_exit, exit_cost, stress_exit, close_price)
                )

        for ticker, reason, resolved_exit, exit_cost, stress_exit, exit_close in exits:
            position = active.pop(ticker)
            coverage["spread_total"] += position.current_exposure
            if resolved_exit.spread_source == "ibkr_exact":
                coverage["spread_exact"] += position.current_exposure
            position.transaction_cost += exit_cost
            position.stress_cost += stress_exit
            daily_net -= exit_cost
            daily_stress -= stress_exit
            daily_selection -= exit_cost
            net = position.gross_pnl - position.borrow_cost - position.transaction_cost
            selection = (
                position.gross_pnl
                - position.benchmark_pnl
                - position.borrow_cost
                - position.transaction_cost
            )
            sector_net[position.pipeline] += selection
            trades.append(
                {
                    "ticker": ticker,
                    "source_pipeline": position.pipeline,
                    "signal_date": position.signal_date,
                    "entry_date": position.entry_date,
                    "exit_date": day,
                    "entry_open": round(position.entry_price, 8),
                    "exit_close": round(exit_close, 8),
                    "entry_score_z": round(position.entry_score_z, 8),
                    "entry_weight": round(position.initial_weight, 8),
                    "holding_days": position.holding_days,
                    "exit_reason": reason,
                    "gross_return": round(position.gross_pnl, 10),
                    "net_return": round(net, 10),
                    "selection_alpha_net": round(selection, 10),
                    "entry_cost_bps": round(position.entry_cost_bps, 4),
                    "exit_cost_bps": round(
                        exit_cost / max(position.current_exposure, 1e-12) * 1e4, 4
                    ),
                    "borrow_cost_bps": round(
                        position.borrow_cost / max(position.initial_weight, 1e-12) * 1e4, 4
                    ),
                    "stress_cost_bps": round(
                        position.stress_cost / max(position.initial_weight, 1e-12) * 1e4,
                        4,
                    ),
                    "borrow_source": position.last_borrow_source,
                    "availability_source": position.availability_source,
                    "entry_beta": round(position.entry_beta, 6),
                    "entry_beta_source": position.entry_beta_source,
                }
            )
            cooldown_until[ticker] = calendar_index + cooldown_days

        # Portfolio-level sanity band. A day outside it is a data fault, not a return: it is
        # flagged, excluded from every published P&L series, and counted in the manifest.
        day_is_fault = (
            _is_data_fault_day(daily_net, daily_sanity_band)
            or _is_data_fault_day(daily_gross, daily_sanity_band)
            or _is_data_fault_day(daily_selection, daily_sanity_band)
        )
        if day_is_fault:
            data_fault_days.append(
                {
                    "date": day,
                    "gross_return": round(daily_gross, 10),
                    "net_return": round(daily_net, 10),
                    "selection_alpha_net": round(daily_selection, 10),
                }
            )
        daily_rows.append(
            {
                "date": day,
                "gross_return": round(daily_gross, 10),
                "net_return": round(daily_net, 10),
                "stress_net_return": round(daily_stress, 10),
                "selection_alpha_net": round(daily_selection, 10),
                "open_positions": len(active),
                "short_gross": round(
                    sum(position.current_exposure for position in active.values()), 8
                ),
                "data_fault": int(day_is_fault),
            }
        )

    if active:
        LOGGER.error(
            "Open positions remain at development-window end; entry horizon/terminal guard failed: %s",
            {
                ticker: {
                    "entry_date": position.entry_date,
                    "holding_days": position.holding_days,
                    "last_mark": position.previous_mark_date,
                }
                for ticker, position in sorted(active.items())
            },
        )
        return 1
    if not trades:
        LOGGER.error("No tactical short trades were completed")
        return 1

    metric_rows = daily_rows
    if args.signal_from or args.signal_to:
        first_entry = min(str(row["entry_date"]) for row in trades)
        last_exit = max(str(row["exit_date"]) for row in trades)
        metric_rows = [
            row for row in daily_rows if first_entry <= str(row["date"]) <= last_exit
        ]
    # Data-fault days are removed from every published series. They remain in the daily CSV with
    # data_fault=1 and are counted in the manifest, so the exclusion is auditable, not silent.
    metric_rows = [row for row in metric_rows if not int(str(row.get("data_fault", 0)))]
    if not metric_rows:
        LOGGER.error("Every evaluation day was flagged as a data fault; nothing is publishable")
        return 1
    metric_dates = [str(row["date"]) for row in metric_rows]
    daily_net = np.asarray([float(str(row["net_return"])) for row in metric_rows], dtype=float)
    daily_stress = np.asarray(
        [float(str(row["stress_net_return"])) for row in metric_rows], dtype=float
    )
    daily_selection = np.asarray(
        [float(str(row["selection_alpha_net"])) for row in metric_rows], dtype=float
    )
    stats = perf_stats(list(daily_net), ppy=252, dates=metric_dates)
    selection_stats = perf_stats(list(daily_selection), ppy=252, dates=metric_dates)
    net_ann = float(daily_net.sum() / max(len(daily_net) / 252.0, 1e-9))
    stress_ann = float(daily_stress.sum() / max(len(daily_stress) / 252.0, 1e-9))
    selection_ann = float(daily_selection.sum() / max(len(daily_selection) / 252.0, 1e-9))
    completed = pd.DataFrame(trades)
    mean_hold_days = float(completed["holding_days"].mean()) if len(completed) else 0.0
    # HAC lag now tracks the realized horizon instead of a hardcoded 5.
    hac_lag = hac_lag_for_hold(mean_hold_days, min_lag=hac_min_lag, divisor=hac_divisor)
    _mean, _se, active_t = mean_t_hac(list(daily_selection), max_lag=hac_lag)
    replay_windows = independent_windows(sorted(set(metric_dates)), max(1, max_holding_days))
    win_rate = float((completed["net_return"] > 0).mean())
    gains = float(completed.loc[completed["net_return"] > 0, "net_return"].sum())
    losses = abs(float(completed.loc[completed["net_return"] < 0, "net_return"].sum()))
    profit_factor = gains / losses if losses > 0 else None
    positive_sectors = sum(value > 0 for value in sector_net.values())
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
    borrow_fraction = _fraction(coverage["borrow_actual"], coverage["borrow_total"])
    availability_fraction = _fraction(
        coverage["availability_observed"] + coverage["availability_fee_proxy"],
        coverage["availability_total"],
    )
    ohlcv_fraction = _fraction(
        coverage["candidate_entry_ohlc"], coverage["candidate_entry_total"]
    )
    if borrow_fraction < float(promotion.get("min_actual_borrow_weight_fraction", 0.90)):
        reasons.append("borrow_coverage_below_threshold")
    if availability_fraction < float(
        promotion.get("min_observed_or_fee_proxy_availability_weight_fraction", 0.90)
    ):
        reasons.append("availability_coverage_below_threshold")
    if ohlcv_fraction < float(promotion.get("min_candidate_execution_ohlcv_fraction", 0.95)):
        reasons.append("execution_ohlcv_coverage_below_threshold")
    min_windows = int(
        promotion.get(
            "min_independent_windows",
            cfg_get(config, "walkforward.min_independent_windows", 6),
        )
    )
    if replay_windows < min_windows:
        reasons.append("insufficient_independent_windows")
    net_ruined = bool(stats["ruin"])
    selection_ruined = bool(selection_stats["ruin"])
    if net_ruined or selection_ruined:
        # A ruined book has no annualized return and no Sharpe. Publishing the clamped -1.0 and
        # the ratio built on top of it manufactured a finite-looking number out of a destroyed
        # curve; the ruin is now the headline instead.
        reasons.append("capital_ruin_observed")
    promotable = not reasons

    def _optional(value: Any, digits: int) -> Any:
        return round(float(value), digits) if value is not None else ""

    summary = {
        "trades": len(trades),
        # NAMING CONVENTION (2026-07-25): *_ann_arithmetic == sum(daily)/years (no compounding);
        # *_ann_geometric == annualized terminal wealth. The legacy net_ann/selection_alpha_ann/
        # stress_net_ann columns are ARITHMETIC and are retained under their original names for
        # 16f and the evidence chain. net_sharpe is GEOMETRIC ann_return / ann_vol.
        "net_ann": round(net_ann, 8),
        "selection_alpha_ann": round(selection_ann, 8),
        "stress_net_ann": round(stress_ann, 8),
        "net_ann_arithmetic": round(net_ann, 8),
        "selection_alpha_ann_arithmetic": round(selection_ann, 8),
        "stress_net_ann_arithmetic": round(stress_ann, 8),
        "net_ann_geometric": _optional(stats["ann_return_or_none"], 8),
        "selection_alpha_ann_geometric": _optional(
            selection_stats["ann_return_or_none"], 8
        ),
        "net_sharpe": _optional(stats["sharpe_or_none"], 6),
        "selection_sharpe": _optional(selection_stats["sharpe_or_none"], 6),
        "net_ruin": int(net_ruined),
        "net_ruin_date": stats["ruin_date"] or "",
        "selection_ruin": int(selection_ruined),
        "selection_ruin_date": selection_stats["ruin_date"] or "",
        "active_t": round(float(active_t), 6) if active_t is not None else "",
        "active_t_hac_lag_days": hac_lag,
        "mean_holding_days": round(mean_hold_days, 4),
        "independent_windows": replay_windows,
        "max_drawdown": round(float(stats["max_dd"]), 8),
        "win_rate": round(win_rate, 6),
        "profit_factor": round(profit_factor, 6) if profit_factor is not None else "",
        "positive_sectors": positive_sectors,
        "borrow_actual_weight_fraction": round(borrow_fraction, 6),
        "availability_covered_weight_fraction": round(availability_fraction, 6),
        "candidate_execution_ohlcv_fraction": round(ohlcv_fraction, 6),
        "spread_exact_weight_fraction": round(
            _fraction(coverage["spread_exact"], coverage["spread_total"]), 6
        ),
        "missing_close_observations_carried": missing_close_observations,
        "candidate_terminal_excluded": int(coverage["candidate_terminal_excluded"]),
        "hygiene_excluded_total": int(sum(hygiene_counts.values())),
        "data_fault_days": len(data_fault_days),
        "gap_exposure_cap_exits": int(forced_exit_counts.get("gap_exposure_cap", 0)),
        "data_fault_exits": int(forced_exit_counts.get("data_fault", 0)),
        "promotable": int(promotable),
        "rejection_reasons": ";".join(reasons),
    }
    if args.evaluation_json is not None:
        evaluation_path = args.evaluation_json.expanduser().resolve()
        write_manifest(
            evaluation_path,
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
                    "net_profit_target": net_profit_target,
                    "stop_loss": stop_loss,
                    "max_holding_days": max_holding_days,
                    "invalidation_score_z": invalidation_score_z,
                    "cooldown_days": cooldown_days,
                    "target_short_gross": target_short_gross,
                    "max_position_weight": max_position_weight,
                },
                "liquid_tier": bool(liquid_cfg),
                "liquid_tier_gates": {
                    "min_entry_price": hygiene_policy.min_entry_price,
                    "min_median_dollar_volume_20d": hygiene_policy.min_median_dollar_volume,
                }
                if liquid_cfg
                else None,
                "hygiene_excluded_total": int(sum(hygiene_counts.values())),
                "data_fault_days": len(data_fault_days),
                "active_t_hac_lag_days": hac_lag,
                "independent_windows": replay_windows,
                "ruin": bool(stats["ruin"]) or bool(selection_stats["ruin"]),
                "summary": summary,
                "sector_selection_alpha": sector_net,
                "daily_selection_returns": [
                    float(str(row["selection_alpha_net"])) for row in metric_rows
                ],
                "daily_stress_returns": [
                    float(str(row["stress_net_return"])) for row in metric_rows
                ],
                "trade_net_returns": [
                    float(str(row["net_return"])) for row in trades
                ],
                "source_sha256": sha256_file(Path(__file__).resolve()),
                "market_positioning_db_sha256": sha256_file(db_path),
            },
        )
        return 0
    # A suffix produces a SIBLING run directory so a previously sealed build is never overwritten.
    # The liquid tier additionally publishes under its own root, so the sealed full-universe
    # artifacts cannot be touched even without a suffix.
    replay_dir_name = (
        str(liquid_cfg.get("replay_dir", "tactical_short_liquid"))
        if liquid_cfg
        else str(cfg.get("dir", "tactical_short"))
    )
    out_dir = (
        paths.output_dir
        / replay_dir_name
        / f"{panel_dir.name}{str(args.output_suffix or '').strip()}"
    )
    trades_path = out_dir / "tactical_short_trades.csv"
    daily_path = out_dir / "tactical_short_daily.csv"
    summary_path = out_dir / "tactical_short_summary.csv"
    cost_inputs_path = out_dir / "short_cost_inputs.csv"
    hygiene_path = out_dir / "tactical_short_hygiene_exclusions.csv"
    manifest_path = out_dir / "tactical_short_manifest.json"
    output_paths = [
        trades_path,
        daily_path,
        summary_path,
        cost_inputs_path,
        hygiene_path,
        manifest_path,
    ]
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
    write_csv(daily_path, DAILY_FIELDS, daily_rows)
    write_csv(summary_path, list(summary), [summary])
    write_csv(hygiene_path, HYGIENE_FIELDS, hygiene_rows)
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
            "stage": "stage11_tactical_single_name_short",
            "generated_at": utc_now(),
            "acceptance": "PASS",
            "promotion_status": "PROMOTABLE" if promotable else "NOT_PROMOTABLE",
            "panel_build": panel_dir.name,
            "pipelines": pipelines,
            # Pre-registered liquid-tier variant (LIQUID_SHORT_TEST.md). False here means the run
            # used the full-universe gates; the effective floors are always in hygiene.policy.
            "liquid_tier": bool(liquid_cfg),
            "liquid_tier_config": dict(liquid_cfg),
            "policy": {
                "execution": (
                    "signal_D_close; enter_D_plus_1_adjusted_open; evaluate_every_close_"
                    "including_entry_day; no_same_session_borrow"
                ),
                "tail_fraction": tail_fraction,
                "signal_every_n_snapshots": signal_every,
                "net_profit_target": net_profit_target,
                "stop_loss": stop_loss,
                "max_holding_days": max_holding_days,
                "invalidation_score_z": invalidation_score_z,
                "cooldown_days": cooldown_days,
                "target_short_gross": target_short_gross,
                "max_position_weight": max_position_weight,
                "max_borrow_fee_annual": max_borrow_fee_annual,
                "parameter_artifact": str(args.parameter_file.resolve())
                if args.parameter_file
                else "",
            },
            # Replay-parameter integrity: a run either names the sealed calibration artifact it
            # replayed (with its sha256 and the exact overridden values) or states outright that it
            # used config defaults. There is no third, ambiguous state.
            "parameters_source": (
                "calibration_artifact" if parameter_overrides else "config_defaults"
            ),
            "parameter_artifact_sha256": parameter_sha256 or "",
            "parameter_artifact_values": dict(parameter_overrides),
            "metric_conventions": {
                "net_ann": "arithmetic_sum_over_years",
                "selection_alpha_ann": "arithmetic_sum_over_years",
                "stress_net_ann": "arithmetic_sum_over_years",
                "net_ann_geometric": "annualized_terminal_wealth",
                "net_sharpe": "geometric_ann_return_over_ann_vol",
                "ruin_policy": "ruined_curves_publish_no_ann_return_and_no_sharpe",
            },
            "hygiene": {
                "policy": {
                    "min_entry_price": hygiene_policy.min_entry_price,
                    "min_median_dollar_volume_20d": hygiene_policy.min_median_dollar_volume,
                    "lookback_sessions": hygiene_policy.lookback_sessions,
                    "data_fault_single_session_move": hygiene_policy.data_fault_move,
                    "data_fault_max_session_shares": hygiene_policy.data_fault_max_shares,
                    "gap_exposure_cap_multiple": gap_exposure_cap_multiple,
                    "daily_return_sanity_band": daily_sanity_band,
                    "min_position_commission_fraction": min_position_commission_fraction,
                },
                "excluded_by_reason": dict(sorted(hygiene_counts.items())),
                "excluded_total": int(sum(hygiene_counts.values())),
                "forced_exits_by_reason": dict(sorted(forced_exit_counts.items())),
                "data_fault_days": data_fault_days,
                "data_fault_day_count": len(data_fault_days),
            },
            "beta_policy": {
                "lookback_sessions": beta_lookback,
                "min_observations": beta_min_obs,
                "clip": [beta_clip_min, beta_clip_max],
                "hedge": "constant_dollar_at_entry",
                "source_counts": dict(sorted(beta_source_counts.items())),
            },
            "cost_provenance": {
                "spread_source_counts": cost_model.spread_source_distribution(),
                "borrow_source_counts": cost_model.borrow_source_distribution(),
                "tiered_half_spread_fallback_bps": [
                    {"min_price": price, "half_spread_bps": bps} for price, bps in spread_tiers
                ],
                "allow_unknown_availability": cost_model.allow_unknown_availability,
                "missing_borrow_fee_annual": cost_model.borrow_fee_fallback_annual,
            },
            "evidence": {
                "independent_windows": replay_windows,
                "min_independent_windows": min_windows,
                "active_t_hac_lag_days": hac_lag,
                "mean_holding_days": round(mean_hold_days, 4),
                "ruin": {
                    "net": bool(stats["ruin"]),
                    "net_ruin_date": stats["ruin_date"],
                    "selection": bool(selection_stats["ruin"]),
                    "selection_ruin_date": selection_stats["ruin_date"],
                },
            },
            "summary": summary,
            "sector_selection_alpha": sector_net,
            "protocol_sha256": lockbox["protocol_sha256"],
            "inputs_sha256": {
                "config.yaml": sha256_file(config_path),
                "backtest/16e_tactical_short_replay.py": sha256_file(Path(__file__).resolve()),
                "backtest/short_costs.py": sha256_file(
                    PACKAGE_ROOT / "backtest" / "short_costs.py"
                ),
                "backtest/walkforward_common.py": sha256_file(
                    PACKAGE_ROOT / "backtest" / "walkforward_common.py"
                ),
                "market_positioning.sqlite": sha256_file(db_path),
                "calibration_panel_manifest.json": sha256_file(panel_manifest_path),
                "calibration_panel.csv": sha256_file(panel_path),
                "survivorship_manifest.json": sha256_file(survivorship_manifest_path),
                "prices_adjclose.csv": sha256_file(prices_path),
                "delisting_events.csv": sha256_file(delisting_events_path),
                "execution_ohlcv_manifest.json": sha256_file(execution_manifest_path),
                "prices_adjusted_ohlcv.csv.gz": sha256_file(execution_prices_path),
                **(
                    {"tactical_short_parameter_artifact.json": parameter_sha256}
                    if parameter_sha256
                    else {}
                ),
            },
            "files": {
                "tactical_short_trades.csv": {
                    "sha256": sha256_file(trades_path),
                    "rows": len(trades),
                },
                "tactical_short_daily.csv": {
                    "sha256": sha256_file(daily_path),
                    "rows": len(daily_rows),
                },
                "tactical_short_summary.csv": {
                    "sha256": sha256_file(summary_path),
                    "rows": 1,
                },
                "short_cost_inputs.csv": {
                    "sha256": sha256_file(cost_inputs_path),
                    "rows": len(cost_rows),
                },
                "tactical_short_hygiene_exclusions.csv": {
                    "sha256": sha256_file(hygiene_path),
                    "rows": len(hygiene_rows),
                },
            },
        },
    )
    LOGGER.info(
        "TACTICAL SHORT: PASS / %s trades=%d net_ann(arith)=%.4f selection_alpha(arith)=%.4f "
        "active_t=%s(lag=%d) stress=%.4f ruin=%s hygiene_excluded=%d data_fault_days=%d -> %s",
        "PROMOTABLE" if promotable else "NOT_PROMOTABLE",
        len(trades),
        net_ann,
        selection_ann,
        f"{active_t:.3f}" if active_t is not None else "NA",
        hac_lag,
        stress_ann,
        bool(stats["ruin"]) or bool(selection_stats["ruin"]),
        int(sum(hygiene_counts.values())),
        len(data_fault_days),
        out_dir,
    )
    if reasons:
        LOGGER.info("Promotion rejections: %s", ";".join(reasons))
    return 0


def _fraction(numerator: float, denominator: float) -> float:
    if denominator <= 0:
        return 0.0
    return float(np.clip(numerator / denominator, 0.0, 1.0))


if __name__ == "__main__":
    raise SystemExit(main())
