#!/usr/bin/env python3
"""Stage 11 - forward-return calibration targets, joined PIT-safe to archived score snapshots.

For every snapshot in the PIT store (research/65) and every ticker in it, computes labels from the
survivorship panel (backtest/15b):

  fwd_return_{21,63,126,252}d      compounded forward return from the snapshot entry bar
  excess_sector_{h}d               minus the sector ETF over the SAME bar window
  excess_spy_{h}d                  minus SPY over the same window
  resid_sector_{h}d                fwd - beta_pit * etf_fwd (beta from trailing daily returns
                                   ENDING at the entry bar; strictly point-in-time)
  drawdown_{dd}d                   max drawdown over the next dd trading days
  realized_vol_{dd}d               annualized std of daily returns over the same forward window

PIT rules: entry = first available bar at/after the snapshot as-of (bounded lag); exits use only bars
at/before the horizon row; a delisted name's label is its return through its final bar
(status=truncated_delisted); horizons past the panel right edge are status=incomplete_future.
Every label carries the panel's `survivorship_complete` flag.

LOCKBOX: snapshots dated inside the sealed window are SKIPPED. Computing their labels requires
--lockbox-open AND `stage11_lockbox.lockbox_opened: true` in config (set only via a dated Open Event
entry in docs/LOCKBOX_PROTOCOL.md). Flag state + protocol sha256 are recorded in the manifest.

`--selftest` exercises the label math on synthetic in-memory data (no real dates touched).
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any, cast

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from portfolio_layer.core.config import cfg_get, load_yaml  # noqa: E402
from portfolio_layer.core.contracts import fail_if_exists, read_csv, sha256_file, write_csv, write_manifest  # noqa: E402
from portfolio_layer.core.db import utc_now  # noqa: E402
from portfolio_layer.core.logging_utils import configure_utc_logging  # noqa: E402
from portfolio_layer.core.paths import resolve_runtime_paths  # noqa: E402
from portfolio_layer.research.stage11_common import load_lockbox  # noqa: E402


LOGGER = logging.getLogger("define_calibration_targets")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

SCORE_CARRY_FIELDS = [
    "source_pipeline", "sector", "native_score", "final_score", "within_sector_percentile",
    "score_confidence", "investable_eligible", "calibration_research_eligible", "stage1_sample_role",
]


def iso_date_arg(raw: str) -> str:
    try:
        date.fromisoformat(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"must be YYYY-MM-DD, got {raw!r}") from exc
    return raw


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Stage 11 forward-return calibration targets (PIT, lockbox-enforced).")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--panel-date", type=iso_date_arg, default=None,
                   help="Survivorship-panel build date to consume (default: latest).")
    p.add_argument("--lockbox-open", action="store_true",
                   help="Compute labels for sealed-window snapshots. Requires lockbox_opened=true in "
                        "config, set only via an Open Event entry in the protocol Amendment Log.")
    p.add_argument("--selftest", action="store_true", help="Run synthetic label-math self-tests and exit.")
    p.add_argument("--force", action="store_true")
    return p.parse_args()


# ---------------------------------------------------------------------------
# pure label math (self-tested)
# ---------------------------------------------------------------------------
def ticker_series(prices: pd.DataFrame, ticker: str) -> tuple[np.ndarray, np.ndarray]:
    """(row_indices, prices) of a ticker's non-NaN bars on the panel calendar."""
    series = cast(pd.Series, pd.to_numeric(prices[ticker], errors="coerce"))
    col = np.asarray(series.to_numpy(dtype=np.float64), dtype=np.float64)
    rows = np.flatnonzero(~np.isnan(col))
    return rows, col[rows]


def entry_bar(rows: np.ndarray, vals: np.ndarray, base_row: int, max_lag: int) -> tuple[int, float, int] | None:
    """First bar at/after base_row within max_lag rows -> (row, price, lag)."""
    i = int(np.searchsorted(rows, base_row, side="left"))
    if i >= len(rows):
        return None
    row = int(rows[i])
    lag = row - base_row
    if lag > max_lag:
        return None
    return row, float(vals[i]), lag


def exit_bar(rows: np.ndarray, vals: np.ndarray, entry_row: int, target_row: int) -> tuple[int, float] | None:
    """Last bar at/before target_row and strictly after entry_row."""
    i = int(np.searchsorted(rows, target_row, side="right")) - 1
    if i < 0 or int(rows[i]) <= entry_row:
        return None
    return int(rows[i]), float(vals[i])


def forward_label(
    rows: np.ndarray, vals: np.ndarray, *, entry_row: int, entry_price: float, horizon: int,
    last_panel_row: int, ended: bool, verified_delisted: bool | None = None,
) -> dict[str, Any]:
    """One (ticker, horizon) forward-return label. Uses only bars in (entry_row, entry_row+horizon]."""
    if verified_delisted is None:
        verified_delisted = ended
    target_row = entry_row + horizon
    series_last = int(rows[-1]) if len(rows) else -1
    if target_row > last_panel_row and not (ended and series_last <= last_panel_row):
        return {"status": "incomplete_future", "ret": None, "end_row": None}
    hit = exit_bar(rows, vals, entry_row, min(target_row, last_panel_row))
    if hit is None:
        return {"status": "no_forward_bars", "ret": None, "end_row": None}
    end_row, end_price = hit
    ret = end_price / entry_price - 1.0
    if series_last < target_row:
        status = "truncated_delisted" if ended and verified_delisted else "truncated_data_end"
    else:
        status = "ok"
    return {"status": status, "ret": ret, "end_row": end_row}


def drawdown_label(
    rows: np.ndarray, vals: np.ndarray, *, entry_row: int, entry_price: float, horizon: int,
    last_panel_row: int,
) -> dict[str, Any]:
    target_row = min(entry_row + horizon, last_panel_row)
    lo = int(np.searchsorted(rows, entry_row + 1, side="left"))
    hi = int(np.searchsorted(rows, target_row, side="right"))
    if hi <= lo:
        return {"status": "no_forward_bars", "dd": None, "end_row": None}
    window = vals[lo:hi]
    dd = float(window.min() / entry_price - 1.0)
    status = "ok" if entry_row + horizon <= last_panel_row else "partial_window"
    return {"status": status, "dd": min(dd, 0.0), "end_row": int(rows[hi - 1])}


def realized_vol_label(
    rows: np.ndarray, vals: np.ndarray, *, entry_row: int, horizon: int,
    last_panel_row: int, min_obs: int,
) -> dict[str, Any]:
    """Annualized realized volatility of daily returns over bars in [entry_row, entry_row+horizon]."""
    target_row = min(entry_row + horizon, last_panel_row)
    lo = int(np.searchsorted(rows, entry_row, side="left"))
    hi = int(np.searchsorted(rows, target_row, side="right"))
    window = vals[lo:hi]
    if len(window) < min_obs + 1:
        return {"status": "insufficient_obs", "vol": None, "end_row": int(rows[hi - 1]) if hi > lo else None}
    rets = window[1:] / window[:-1] - 1.0
    vol = float(np.std(rets, ddof=1) * np.sqrt(252.0))
    status = "ok" if entry_row + horizon <= last_panel_row else "partial_window"
    return {"status": status, "vol": vol, "end_row": int(rows[hi - 1])}


def trailing_beta(
    prices: pd.DataFrame, tickers: list[str], etf: str, *, entry_row: int, lookback: int, min_obs: int,
) -> dict[str, tuple[float | None, int]]:
    """Pairwise OLS beta of each ticker vs the ETF from daily returns ENDING at entry_row (PIT)."""
    lo = max(0, entry_row - lookback)
    window = prices.iloc[lo:entry_row + 1]
    rets = window.apply(pd.to_numeric, errors="coerce").pct_change(fill_method=None)
    if etf not in rets.columns:
        return {t: (None, 0) for t in tickers}
    ev = rets[etf].to_numpy(dtype=float)
    out: dict[str, tuple[float | None, int]] = {}
    for t in tickers:
        if t not in rets.columns:
            out[t] = (None, 0)
            continue
        xv = rets[t].to_numpy(dtype=float)
        mask = ~np.isnan(xv) & ~np.isnan(ev)
        n = int(mask.sum())
        if n < min_obs:
            out[t] = (None, n)
            continue
        e = ev[mask] - ev[mask].mean()
        x = xv[mask] - xv[mask].mean()
        denom = float((e * e).sum())
        out[t] = ((float((x * e).sum() / denom), n) if denom > 0 else (None, n))
    return out


# ---------------------------------------------------------------------------
# self-test (synthetic, in-memory; touches no real dates)
# ---------------------------------------------------------------------------
def _selftest() -> None:
    n = 61
    idx = [f"2000-01-{i:02d}" if i <= 31 else f"2000-02-{i-31:02d}" for i in range(1, n + 1)]
    a = [100.0 + i for i in range(n)]                       # linear riser, full coverage
    b = [100.0 + i if i <= 20 else np.nan for i in range(n)]  # ends at row 20 (delisted)
    c = [np.nan if i < 12 else 200.0 + i for i in range(n)]   # starts late (entry lag)
    e_ret = [0.02 if i % 2 == 0 else -0.01 for i in range(n)]  # varying ETF returns
    e = list(np.cumprod([1.0] + [1 + r for r in e_ret[1:]]) * 100.0)
    x = list(np.cumprod([1.0] + [1 + 2 * r for r in e_ret[1:]]) * 50.0)  # exact 2x beta vs ETF
    prices = pd.DataFrame({"AAA": a, "BBB": b, "CCC": c, "EEE": e, "XXX": x}, index=pd.Index(idx))
    last = n - 1

    rows_a, vals_a = ticker_series(prices, "AAA")
    ent = entry_bar(rows_a, vals_a, 10, 5)
    assert ent == (10, 110.0, 0), ent
    lab = forward_label(rows_a, vals_a, entry_row=10, entry_price=110.0, horizon=21, last_panel_row=last, ended=False)
    assert lab["status"] == "ok" and abs(lab["ret"] - (131.0 / 110.0 - 1.0)) < 1e-12, lab

    rows_b, vals_b = ticker_series(prices, "BBB")
    lab = forward_label(rows_b, vals_b, entry_row=10, entry_price=110.0, horizon=21, last_panel_row=last, ended=True)
    assert lab["status"] == "truncated_delisted" and abs(lab["ret"] - (120.0 / 110.0 - 1.0)) < 1e-12, lab

    lab = forward_label(rows_a, vals_a, entry_row=50, entry_price=150.0, horizon=21, last_panel_row=last, ended=False)
    assert lab["status"] == "incomplete_future" and lab["ret"] is None, lab
    lab = forward_label(rows_b, vals_b, entry_row=10, entry_price=110.0, horizon=100, last_panel_row=last, ended=True)
    assert lab["status"] == "truncated_delisted" and abs(lab["ret"] - (120.0 / 110.0 - 1.0)) < 1e-12, lab

    rows_c, vals_c = ticker_series(prices, "CCC")
    ent = entry_bar(rows_c, vals_c, 10, 5)
    assert ent == (12, 212.0, 2), ent
    assert entry_bar(rows_c, vals_c, 0, 5) is None

    dd = drawdown_label(rows_b, vals_b, entry_row=10, entry_price=200.0, horizon=10, last_panel_row=last)
    assert dd["status"] == "ok" and abs(dd["dd"] - (111.0 / 200.0 - 1.0)) < 1e-12, dd
    dd = drawdown_label(rows_a, vals_a, entry_row=10, entry_price=110.0, horizon=10, last_panel_row=last)
    assert dd["dd"] == 0.0, dd  # monotonic riser never draws down below entry

    g = list(100.0 * np.cumprod([1.0] + [1.01] * (n - 1)))  # constant 1% daily growth -> zero vol
    prices["GGG"] = g
    rows_g, vals_g = ticker_series(prices, "GGG")
    rv = realized_vol_label(rows_g, vals_g, entry_row=10, horizon=21, last_panel_row=last, min_obs=10)
    assert rv["status"] == "ok" and abs(rv["vol"]) < 1e-9, rv
    rv = realized_vol_label(rows_g, vals_g, entry_row=50, horizon=21, last_panel_row=last, min_obs=10)
    assert rv["status"] == "partial_window" and abs(rv["vol"]) < 1e-9, rv
    rv = realized_vol_label(rows_g, vals_g, entry_row=55, horizon=21, last_panel_row=last, min_obs=10)
    assert rv["status"] == "insufficient_obs" and rv["vol"] is None, rv
    rv = realized_vol_label(rows_b, vals_b, entry_row=5, horizon=21, last_panel_row=last, min_obs=10)
    assert rv["status"] == "ok" and rv["vol"] is not None and rv["vol"] > 0.0, rv

    betas = trailing_beta(prices, ["XXX", "AAA"], "EEE", entry_row=last, lookback=60, min_obs=20)
    beta_x, n_x = betas["XXX"]
    assert beta_x is not None and abs(beta_x - 2.0) < 1e-9 and n_x >= 20, betas
    lab = forward_label(
        rows_b,
        vals_b,
        entry_row=10,
        entry_price=110.0,
        horizon=100,
        last_panel_row=last,
        ended=True,
        verified_delisted=False,
    )
    assert lab["status"] == "truncated_data_end", lab
    print("calibration-targets self-test: PASS")


# ---------------------------------------------------------------------------
# main build
# ---------------------------------------------------------------------------
def _latest_panel(panel_root: Path, wanted: str | None) -> Path | None:
    if wanted:
        cand = panel_root / wanted
        return cand if (cand / "survivorship_manifest.json").exists() else None
    if not panel_root.exists():
        return None
    builds = sorted(p for p in panel_root.iterdir()
                    if p.is_dir() and DATE_RE.match(p.name) and (p / "survivorship_manifest.json").exists())
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
    if args.lockbox_open and not lockbox["lockbox_opened"]:
        LOGGER.error(
            "--lockbox-open passed but stage11_lockbox.lockbox_opened is false. Opening the lockbox "
            "requires a dated Open Event entry in docs/LOCKBOX_PROTOCOL.md first (protocol violation guard)."
        )
        return 1
    open_mode = bool(args.lockbox_open and lockbox["lockbox_opened"])

    ct = cfg_get(config, "calibration_targets", {}) or {}
    horizons = [int(h) for h in ct.get("horizons_trading_days", [21, 63, 126, 252])]
    dd_horizon = int(ct.get("drawdown_horizon_trading_days", 63))
    beta_lookback = int(ct.get("beta_lookback_trading_days", 252))
    beta_min_obs = int(ct.get("beta_min_observations", 60))
    max_entry_lag = int(ct.get("max_entry_lag_trading_days", 5))
    vol_min_obs = int(ct.get("realized_vol_min_observations", 10))
    spy = str(cfg_get(config, "risk_panel.master_calendar_ticker", "SPY")).upper()
    sector_etf = {str(k): str(v).upper() for k, v in (cfg_get(config, "risk_panel.sector_etf_map", {}) or {}).items()}

    store_dir = paths.output_dir / str(cfg_get(config, "snapshot_store.dir", "snapshot_store"))
    panel_root = paths.output_dir / str(cfg_get(config, "survivorship_panel.dir", "survivorship_panel"))
    panel_dir = _latest_panel(panel_root, args.panel_date)
    if panel_dir is None:
        LOGGER.error("No survivorship panel build found under %s; run backtest/15b first", panel_root)
        return 1
    panel_manifest = json.loads((panel_dir / "survivorship_manifest.json").read_text(encoding="utf-8"))
    if panel_manifest.get("acceptance") != "PASS":
        LOGGER.error("Survivorship panel %s acceptance=%s; refusing", panel_dir.name, panel_manifest.get("acceptance"))
        return 1
    prices = pd.read_csv(panel_dir / "prices_adjclose.csv", index_col=0)
    calendar = [str(d) for d in prices.index]
    last_panel_row = len(calendar) - 1
    coverage = {r["ticker"]: r for r in read_csv(panel_dir / "ticker_coverage.csv")}

    snap_dirs = sorted(
        p for p in store_dir.iterdir()
        if p.is_dir() and DATE_RE.match(p.name) and (p / "stocks_scores.csv").exists()
    ) if store_dir.exists() else []
    if not snap_dirs:
        LOGGER.error("PIT snapshot store is empty (%s); run research/65 first", store_dir)
        return 1

    out_dir = paths.output_dir / str(ct.get("dir", "calibration_targets")) / panel_dir.name
    targets_path = out_dir / "calibration_targets.csv"
    manifest_path = out_dir / "targets_manifest.json"
    if args.force:
        for p in (targets_path, manifest_path):
            if p.exists():
                p.unlink()
    try:
        fail_if_exists([targets_path, manifest_path], force=args.force)
    except FileExistsError as exc:
        LOGGER.error("%s", exc)
        return 1

    fields = [
        "as_of_date", "ticker", *SCORE_CARRY_FIELDS, "sector_etf", "in_lockbox",
        "survivorship_complete", "coverage_status", "entry_date", "entry_lag_days", "entry_price",
        "beta_sector", "beta_obs",
    ]
    for h in horizons:
        fields += [f"fwd_return_{h}d", f"fwd_status_{h}d", f"fwd_end_{h}d",
                   f"excess_sector_{h}d", f"excess_spy_{h}d", f"resid_sector_{h}d"]
    fields += [f"drawdown_{dd_horizon}d", f"drawdown_status_{dd_horizon}d",
               f"realized_vol_{dd_horizon}d", f"realized_vol_status_{dd_horizon}d",
               "usable_for_promoted_training"]

    series_cache: dict[str, tuple[np.ndarray, np.ndarray]] = {}

    def series_of(ticker: str) -> tuple[np.ndarray, np.ndarray]:
        if ticker not in series_cache:
            series_cache[ticker] = ticker_series(prices, ticker) if ticker in prices.columns else (
                np.array([], dtype=int), np.array([], dtype=float))
        return series_cache[ticker]

    sealed_skipped: list[str] = []
    panel_edge_skipped: list[str] = []
    processed: list[str] = []
    no_entry = 0
    out_rows: list[dict[str, Any]] = []
    status_counts: dict[str, int] = {}

    def update_max_end(current: str, end_row: Any | None) -> str:
        if end_row is None:
            return current
        end_date = calendar[int(end_row)]
        return end_date if end_date > current else current

    for snap in snap_dirs:
        as_of = snap.name
        in_lockbox = 1 if as_of >= lockbox["sealed_start"] else 0
        if in_lockbox and not open_mode:
            sealed_skipped.append(as_of)
            continue
        base_row = int(np.searchsorted(np.array(calendar), as_of, side="left"))
        if base_row > last_panel_row:
            LOGGER.warning("Snapshot %s is beyond the panel right edge; skipped", as_of)
            panel_edge_skipped.append(as_of)
            continue
        snap_rows = read_csv(snap / "stocks_scores.csv")
        by_pipe: dict[str, list[dict[str, str]]] = {}
        for r in snap_rows:
            by_pipe.setdefault(str(r.get("source_pipeline", "")).strip(), []).append(r)

        betas: dict[str, tuple[float | None, int]] = {}
        for pipe, rows_p in by_pipe.items():
            etf = sector_etf.get(pipe, "")
            tickers = [str(r.get("ticker", "")).strip().upper() for r in rows_p]
            if etf and etf in prices.columns:
                betas.update(trailing_beta(prices, tickers, etf, entry_row=base_row,
                                           lookback=beta_lookback, min_obs=beta_min_obs))
            else:
                betas.update({t: (None, 0) for t in tickers})

        for r in snap_rows:
            ticker = str(r.get("ticker", "")).strip().upper()
            if not ticker:
                continue
            pipe = str(r.get("source_pipeline", "")).strip()
            etf = sector_etf.get(pipe, "")
            cov = coverage.get(ticker, {})
            coverage_status = str(cov.get("status", ""))
            ended = coverage_status in {"delisted_covered", "ended_uncovered"}
            verified_delisted = coverage_status == "delisted_covered"
            rows_t, vals_t = series_of(ticker)
            row: dict[str, Any] = {
                "as_of_date": as_of, "ticker": ticker,
                **{f: str(r.get(f, "")) for f in SCORE_CARRY_FIELDS},
                "sector_etf": etf, "in_lockbox": in_lockbox,
                "survivorship_complete": str(cov.get("survivorship_complete", "0")),
                "coverage_status": str(cov.get("status", "missing_from_panel")),
            }
            ent = entry_bar(rows_t, vals_t, base_row, max_entry_lag) if len(rows_t) else None
            if ent is None:
                no_entry += 1
                row.update({"entry_date": "", "entry_lag_days": "", "entry_price": "",
                            "beta_sector": "", "beta_obs": 0})
                for h in horizons:
                    row.update({f"fwd_return_{h}d": "", f"fwd_status_{h}d": "no_entry_bar", f"fwd_end_{h}d": "",
                                f"excess_sector_{h}d": "", f"excess_spy_{h}d": "", f"resid_sector_{h}d": ""})
                row.update({f"drawdown_{dd_horizon}d": "", f"drawdown_status_{dd_horizon}d": "no_entry_bar",
                            f"realized_vol_{dd_horizon}d": "", f"realized_vol_status_{dd_horizon}d": "no_entry_bar",
                            "usable_for_promoted_training": 0})
                out_rows.append(row)
                status_counts["no_entry_bar"] = status_counts.get("no_entry_bar", 0) + len(horizons)
                continue
            entry_row, entry_price, entry_lag = ent
            beta, beta_obs = betas.get(ticker, (None, 0))
            row.update({
                "entry_date": calendar[entry_row], "entry_lag_days": entry_lag,
                "entry_price": round(entry_price, 6),
                "beta_sector": round(beta, 6) if beta is not None else "", "beta_obs": beta_obs,
            })
            rows_e, vals_e = series_of(etf) if etf else (np.array([], dtype=int), np.array([], dtype=float))
            rows_s, vals_s = series_of(spy)
            etf_entry = entry_bar(rows_e, vals_e, entry_row, 0) if len(rows_e) else None
            spy_entry = entry_bar(rows_s, vals_s, entry_row, 0) if len(rows_s) else None
            max_end = ""
            for h in horizons:
                lab = forward_label(rows_t, vals_t, entry_row=entry_row, entry_price=entry_price,
                                    horizon=h, last_panel_row=last_panel_row, ended=ended,
                                    verified_delisted=verified_delisted)
                status_counts[lab["status"]] = status_counts.get(lab["status"], 0) + 1
                ret = lab["ret"]
                end_row = lab["end_row"]
                end_date = calendar[end_row] if end_row is not None else ""
                max_end = update_max_end(max_end, end_row)
                excess_etf = excess_spy = resid = ""
                if ret is not None and end_row is not None:
                    if etf_entry is not None:
                        hit = exit_bar(rows_e, vals_e, entry_row, end_row)
                        if hit is not None:
                            etf_ret = hit[1] / etf_entry[1] - 1.0
                            excess_etf = round(ret - etf_ret, 8)
                            if beta is not None:
                                resid = round(ret - beta * etf_ret, 8)
                    if spy_entry is not None:
                        hit = exit_bar(rows_s, vals_s, entry_row, end_row)
                        if hit is not None:
                            excess_spy = round(ret - (hit[1] / spy_entry[1] - 1.0), 8)
                row.update({
                    f"fwd_return_{h}d": round(ret, 8) if ret is not None else "",
                    f"fwd_status_{h}d": lab["status"], f"fwd_end_{h}d": end_date,
                    f"excess_sector_{h}d": excess_etf, f"excess_spy_{h}d": excess_spy,
                    f"resid_sector_{h}d": resid,
                })
            dd = drawdown_label(rows_t, vals_t, entry_row=entry_row, entry_price=entry_price,
                                horizon=dd_horizon, last_panel_row=last_panel_row)
            rv = realized_vol_label(rows_t, vals_t, entry_row=entry_row, horizon=dd_horizon,
                                    last_panel_row=last_panel_row, min_obs=vol_min_obs)
            if dd["dd"] is not None:
                max_end = update_max_end(max_end, dd.get("end_row"))
            if rv["vol"] is not None:
                max_end = update_max_end(max_end, rv.get("end_row"))
            row.update({
                f"drawdown_{dd_horizon}d": round(dd["dd"], 8) if dd["dd"] is not None else "",
                f"drawdown_status_{dd_horizon}d": dd["status"],
                f"realized_vol_{dd_horizon}d": round(rv["vol"], 8) if rv["vol"] is not None else "",
                f"realized_vol_status_{dd_horizon}d": rv["status"],
                "usable_for_promoted_training": 1 if (max_end and max_end <= lockbox["training_label_end_max"]) else 0,
            })
            out_rows.append(row)
        processed.append(as_of)

    # ---- gates ----
    checks: list[dict[str, str]] = []

    def rec(name: str, status: str, detail: str) -> None:
        checks.append({"check": name, "status": status, "detail": detail})

    leaked = [r for r in out_rows if int(r["in_lockbox"]) == 1] if not open_mode else []
    rec("lockbox_enforced", "PASS" if not leaked else "FAIL",
        f"sealed snapshots skipped={len(sealed_skipped)} open_mode={open_mode}"
        if not leaked else f"{len(leaked)} sealed-window label rows emitted without open")
    bad_entry = [r["ticker"] for r in out_rows if r["entry_date"] and str(r["entry_date"]) < str(r["as_of_date"])]
    rec("entry_never_before_asof", "PASS" if not bad_entry else "FAIL",
        "entry bar >= snapshot as-of for all labels" if not bad_entry else f"{bad_entry[:8]}")
    bad_end = []
    for r in out_rows:
        for h in horizons:
            end = str(r.get(f"fwd_end_{h}d", ""))
            if end and (end <= str(r["entry_date"]) or end > calendar[-1]):
                bad_end.append(f"{r['as_of_date']}:{r['ticker']}:{h}d")
    rec("label_windows_within_panel", "PASS" if not bad_end else "FAIL",
        "all label end bars in (entry, panel_end]" if not bad_end else f"{bad_end[:8]}")
    incomplete_names = sum(1 for r in out_rows if str(r["survivorship_complete"]) != "1")
    rec("survivorship_flags_carried", "PASS",
        f"{incomplete_names} label rows flagged survivorship-incomplete (of {len(out_rows)})")

    out_dir.mkdir(parents=True, exist_ok=True)
    write_csv(targets_path, fields, out_rows)
    passed = all(c["status"] == "PASS" for c in checks)
    manifest = {
        "stage": "stage11_calibration_targets",
        "generated_at": utc_now(),
        "acceptance": "PASS" if passed else "FAIL",
        "panel_build": panel_dir.name,
        "panel_manifest_sha256": sha256_file(panel_dir / "survivorship_manifest.json"),
        "protocol_sha256": lockbox["protocol_sha256"],
        "lockbox_open_flag": bool(args.lockbox_open),
        "lockbox_opened_config": lockbox["lockbox_opened"],
        "sealed_snapshots_skipped": sealed_skipped,
        "panel_edge_snapshots_skipped": panel_edge_skipped,
        "snapshots_processed": processed,
        "horizons_trading_days": horizons,
        "training_label_end_max": lockbox["training_label_end_max"],
        "rows": len(out_rows),
        "no_entry_rows": no_entry,
        "label_status_counts": dict(sorted(status_counts.items())),
        "checks": checks,
        "files": {"calibration_targets.csv": {"sha256": sha256_file(targets_path), "rows": len(out_rows)}},
    }
    write_manifest(manifest_path, manifest)
    for c in checks:
        LOGGER.info("[%s] %s -- %s", c["status"], c["check"], c["detail"])
    LOGGER.info(
        "CALIBRATION TARGETS: %s (snapshots=%d, sealed_skipped=%d, rows=%d, statuses=%s) -> %s",
        "PASS" if passed else "FAIL", len(processed), len(sealed_skipped), len(out_rows),
        dict(sorted(status_counts.items())), out_dir,
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
