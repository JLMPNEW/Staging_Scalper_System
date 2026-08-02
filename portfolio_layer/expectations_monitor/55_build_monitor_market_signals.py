#!/usr/bin/env python3
"""Build sector-relative market corroboration signals from sealed price artifacts."""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_layer.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from portfolio_layer.core.contracts import (  # noqa: E402
    fail_if_exists,
    read_csv,
    read_manifest,
    sha256_file,
    write_csv,
    write_manifest,
)
from portfolio_layer.core.paths import ensure_not_prod_path, resolve_runtime_paths  # noqa: E402
from portfolio_layer.expectations_monitor.market_data_common import (  # noqa: E402
    BENCHMARK_OHLCV_FILENAME,
    SELECTED_OHLCV_FILENAME,
    read_gzip_csv,
)
from portfolio_layer.expectations_monitor.monitor_common import (  # noqa: E402
    connect_monitor_db,
    fetch_universe_snapshot,
    database_writer_lock,
    monitor_output_subdir,
)
from portfolio_layer.expectations_monitor.state_common import (  # noqa: E402
    ensure_state_schema,
)


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
SIGNAL_FIELDS = [
    "ticker", "asof_date", "benchmark_ticker", "market_data_status",
    "abnormal_ret_1d_z", "rel_ret_5d",
    "rel_ret_20d", "volume_z", "realized_vol_ratio", "below_ma50", "below_ma200",
    "new_52w_low", "gap_state", "market_component_points", "input_manifest_sha256",
    "inputs_json",
]
VALIDATION_FIELDS = ["check", "status", "detail"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--as-of", type=date.fromisoformat)
    parser.add_argument("--universe-as-of", type=date.fromisoformat)
    parser.add_argument("--market-data-dir", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--selftest", action="store_true")
    return parser.parse_args()


def _sealed_file(manifest_path: Path, filename: str, *, accepted: set[str]) -> Path:
    manifest = read_manifest(manifest_path)
    if str(manifest.get("acceptance", "")) not in accepted:
        raise ValueError(f"Manifest is not accepted: {manifest_path}")
    path = manifest_path.parent / filename
    expected = dict(manifest.get("outputs_sha256", {})).get(filename)
    if not path.is_file() or expected != sha256_file(path):
        raise ValueError(f"Sealed file mismatch: {path}")
    return path


def _safe_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _numeric_series(values: Any, *, index: Any = None) -> pd.Series:
    resolved_index = getattr(values, "index", index)
    return pd.Series(pd.to_numeric(values, errors="coerce"), index=resolved_index, dtype=float)


def _point(series: pd.Series, periods: int) -> float | None:
    clean = series.dropna()
    if len(clean) <= periods:
        return None
    denominator = float(clean.iloc[-periods - 1])
    if denominator <= 0:
        return None
    return float(clean.iloc[-1] / denominator - 1.0)


def compute_signal_row(
    frame: pd.DataFrame,
    benchmark: pd.Series,
    *,
    ticker: str,
    as_of: str,
    benchmark_ticker: str,
    manifest_sha: str,
    market_component_cap: float = 15.0,
) -> dict[str, Any]:
    frame = frame.sort_index()
    close = _numeric_series(frame["adj_close"]).dropna()
    volume = _numeric_series(frame["volume"])
    if close.empty:
        raise ValueError(f"No adjusted close data for {ticker}")
    aligned = pd.concat(
        [close.rename("name"), _numeric_series(benchmark).rename("bench")],
        axis=1,
        join="inner",
    ).dropna()
    rel_daily = aligned.pct_change(fill_method=None).dropna()
    rel_series = rel_daily["name"] - rel_daily["bench"]
    current_rel = float(rel_series.iloc[-1]) if not rel_series.empty else np.nan
    trailing_rel = rel_series.iloc[-60:]
    rel_std = float(trailing_rel.std(ddof=1)) if len(trailing_rel) >= 20 else np.nan
    abnormal_z = current_rel / rel_std if rel_std > 0 else np.nan
    name_series = _numeric_series(aligned.loc[:, "name"])
    bench_series = _numeric_series(aligned.loc[:, "bench"])
    rel5 = _point(name_series, 5)
    bench5 = _point(bench_series, 5)
    rel20 = _point(name_series, 20)
    bench20 = _point(bench_series, 20)
    rel5 = None if rel5 is None or bench5 is None else rel5 - bench5
    rel20 = None if rel20 is None or bench20 is None else rel20 - bench20
    volume_tail = volume.dropna().iloc[-60:]
    volume_z = np.nan
    if len(volume_tail) >= 20 and float(volume_tail.std(ddof=1)) > 0:
        volume_z = (float(volume_tail.iloc[-1]) - float(volume_tail.mean())) / float(volume_tail.std(ddof=1))
    returns = close.pct_change(fill_method=None).dropna()
    vol20 = float(returns.iloc[-20:].std(ddof=1)) if len(returns) >= 20 else np.nan
    vol60 = float(returns.iloc[-60:].std(ddof=1)) if len(returns) >= 60 else np.nan
    vol_ratio = vol20 / vol60 if vol60 > 0 else np.nan
    latest = float(close.iloc[-1])
    ma50 = float(close.iloc[-50:].mean()) if len(close) >= 50 else np.nan
    ma200 = float(close.iloc[-200:].mean()) if len(close) >= 200 else np.nan
    below50 = int(math.isfinite(ma50) and latest < ma50)
    below200 = int(math.isfinite(ma200) and latest < ma200)
    new_low = int(len(close) >= 60 and latest <= float(close.iloc[-252:].min()) * 1.000001)
    gap_state = "none"
    if len(close) >= 2:
        prior_close = float(close.iloc[-2])
        open_series = _numeric_series(frame["adj_open"])
        adj_open = _safe_float(open_series.reindex([close.index[-1]]).iloc[0])
        if adj_open is not None and prior_close > 0 and adj_open / prior_close - 1.0 <= -0.03:
            gap_state = "down_unrecovered" if latest < prior_close else "down_recovered"
    points = 0.0
    if math.isfinite(abnormal_z) and abs(abnormal_z) >= 2.0:
        points += math.copysign(8.0, abnormal_z)
    if gap_state == "down_unrecovered":
        points -= 8.0
    if rel5 is not None and rel20 is not None and rel5 < 0 and rel20 < 0:
        points -= 6.0
    if new_low:
        points -= 6.0
    if below50 and below200:
        points -= 4.0
    if math.isfinite(vol_ratio) and vol_ratio >= 2.0:
        points -= 4.0
    points = max(-market_component_cap, min(market_component_cap, points))
    inputs = {
        "last_market_date": str(close.index[-1])[:10],
        "latest_session_present": True,
        "latest_adj_close": latest,
        "ma50": _safe_float(ma50),
        "ma200": _safe_float(ma200),
        "relative_observation_count": len(rel_series),
    }
    return {
        "ticker": ticker,
        "asof_date": as_of,
        "benchmark_ticker": benchmark_ticker,
        "market_data_status": "current",
        "abnormal_ret_1d_z": _safe_float(abnormal_z),
        "rel_ret_5d": rel5,
        "rel_ret_20d": rel20,
        "volume_z": _safe_float(volume_z),
        "realized_vol_ratio": _safe_float(vol_ratio),
        "below_ma50": below50,
        "below_ma200": below200,
        "new_52w_low": new_low,
        "gap_state": gap_state,
        "market_component_points": points,
        "input_manifest_sha256": manifest_sha,
        "inputs_json": json.dumps(inputs, sort_keys=True, separators=(",", ":")),
    }


def missing_latest_signal_row(
    frame: pd.DataFrame,
    *,
    ticker: str,
    as_of: str,
    benchmark_ticker: str,
    manifest_sha: str,
    required_market_date: str,
) -> dict[str, Any]:
    close = _numeric_series(frame.get("adj_close", pd.Series(dtype=float))).dropna()
    inputs = {
        "last_market_date": str(close.index[-1])[:10] if not close.empty else "",
        "latest_adj_close": None,
        "latest_session_present": False,
        "required_market_date": required_market_date,
    }
    return {
        "ticker": ticker,
        "asof_date": as_of,
        "benchmark_ticker": benchmark_ticker,
        "market_data_status": "missing_latest",
        "abnormal_ret_1d_z": None,
        "rel_ret_5d": None,
        "rel_ret_20d": None,
        "volume_z": None,
        "realized_vol_ratio": None,
        "below_ma50": 0,
        "below_ma200": 0,
        "new_52w_low": 0,
        "gap_state": "market_data_unavailable",
        "market_component_points": 0.0,
        "input_manifest_sha256": manifest_sha,
        "inputs_json": json.dumps(inputs, sort_keys=True, separators=(",", ":")),
    }


def _validate(
    rows: list[dict[str, Any]],
    expected: int,
    current_tickers: set[str] | None = None,
    *,
    market_component_cap: float = 15.0,
) -> list[dict[str, str]]:
    finite_points = all(math.isfinite(float(row["market_component_points"])) for row in rows)
    bounded = all(
        abs(float(row["market_component_points"])) <= market_component_cap
        for row in rows
    )
    statuses_valid = all(
        row["market_data_status"] in {"current", "missing_latest"} for row in rows
    )
    missing_isolated = all(
        row["market_data_status"] != "missing_latest"
        or (
            float(row["market_component_points"]) == 0.0
            and row["gap_state"] == "market_data_unavailable"
        )
        for row in rows
    )
    status_matches_coverage = current_tickers is None or all(
        (row["ticker"] in current_tickers)
        == (row["market_data_status"] == "current")
        for row in rows
    )
    return [
        {"check": "universe_complete", "status": "PASS" if len(rows) == expected else "FAIL", "detail": f"rows={len(rows)}; expected={expected}"},
        {"check": "market_points_finite", "status": "PASS" if finite_points else "FAIL", "detail": "market component finite"},
        {
            "check": "market_component_cap",
            "status": "PASS" if bounded else "FAIL",
            "detail": f"absolute market component <={market_component_cap}",
        },
        {"check": "benchmark_present", "status": "PASS" if all(row["benchmark_ticker"] for row in rows) else "FAIL", "detail": "every name mapped to a benchmark"},
        {"check": "market_data_status_closed", "status": "PASS" if statuses_valid else "FAIL", "detail": "status is current or missing_latest"},
        {"check": "missing_latest_isolated", "status": "PASS" if missing_isolated else "FAIL", "detail": "missing names contribute zero market points"},
        {"check": "coverage_status_reproduced", "status": "PASS" if status_matches_coverage else "FAIL", "detail": "signal status matches sealed coverage"},
    ]


def run_selftest() -> None:
    dates = pd.bdate_range("2025-01-01", periods=260)
    close = pd.Series(np.linspace(100.0, 130.0, len(dates)), index=dates)
    frame = pd.DataFrame({"adj_close": close, "adj_open": close * 0.999, "volume": 1_000_000.0}, index=dates)
    benchmark = pd.Series(np.linspace(100.0, 110.0, len(dates)), index=dates)
    row = compute_signal_row(frame, benchmark, ticker="AAA", as_of="2026-01-01", benchmark_ticker="SPY", manifest_sha="a" * 64)
    assert row["ticker"] == "AAA" and abs(float(row["market_component_points"])) <= 15.0
    missing = missing_latest_signal_row(
        frame,
        ticker="BBB",
        as_of="2026-01-01",
        benchmark_ticker="SPY",
        manifest_sha="a" * 64,
        required_market_date="2026-01-01",
    )
    checks = _validate([row, missing], 2, {"AAA"})
    assert not [check for check in checks if check["status"] == "FAIL"]
    print("monitor market signals selftest: PASS")


def main() -> int:
    args = parse_args()
    if args.selftest:
        run_selftest()
        return 0
    if args.as_of is None:
        raise ValueError("--as-of is required")
    config_path = args.config.resolve()
    config = load_yaml(config_path)
    paths = resolve_runtime_paths(config, config_path)
    monitor_cfg = cfg_get(config, "expectations_monitor", {})
    state_cfg = cfg_get(config, "expectations_monitor.state_model", {})
    if not isinstance(monitor_cfg, dict) or not isinstance(state_cfg, dict):
        raise ValueError("expectations_monitor config must be a mapping")
    market_component_cap = float(state_cfg.get("market_component_cap", 15.0))
    universe_as_of = (args.universe_as_of or args.as_of).isoformat()
    monitor_subdir = monitor_output_subdir(config)
    market_dir = (
        args.market_data_dir
        or paths.output_dir
        / "runs"
        / args.as_of.isoformat()
        / monitor_subdir
        / "market_data"
    )
    market_manifest_path = market_dir / "monitor_ohlcv_manifest.json"
    market_validation_path = market_dir / "monitor_ohlcv_validation_manifest.json"
    market_manifest = read_manifest(market_manifest_path)
    market_validation = read_manifest(market_validation_path)
    if market_manifest.get("acceptance") not in {"PASS", "PASS_WITH_WARNINGS"}:
        raise ValueError("Monitor OHLCV producer did not pass")
    if market_validation.get("acceptance") not in {"PASS", "PASS_WITH_WARNINGS"}:
        raise ValueError("Monitor OHLCV validator did not pass")
    if market_manifest.get("as_of_date") != args.as_of.isoformat():
        raise ValueError("Monitor OHLCV date mismatch")
    selected_path = _sealed_file(
        market_manifest_path,
        SELECTED_OHLCV_FILENAME,
        accepted={"PASS", "PASS_WITH_WARNINGS"},
    )
    coverage_path = _sealed_file(
        market_manifest_path,
        "monitor_ohlcv_coverage.csv",
        accepted={"PASS", "PASS_WITH_WARNINGS"},
    )
    benchmark_path = _sealed_file(
        market_manifest_path,
        BENCHMARK_OHLCV_FILENAME,
        accepted={"PASS", "PASS_WITH_WARNINGS"},
    )
    db_path = ensure_not_prod_path(
        resolve_path(monitor_cfg.get("database_path", "db/expectations_monitor.sqlite"), base_dir=config_path.parent),
        label="expectations monitor database",
    )
    timeout = float(monitor_cfg.get("writer_lock_timeout_sec", 30.0))
    conn = connect_monitor_db(db_path, timeout_sec=timeout)
    try:
        ensure_state_schema(conn)
        universe = fetch_universe_snapshot(conn, universe_as_of)
    finally:
        conn.close()
    if not universe:
        raise ValueError(f"No monitor universe for {universe_as_of}")
    selected = pd.DataFrame(read_gzip_csv(selected_path))
    selected["date"] = pd.to_datetime(selected["date"], errors="raise")
    coverage_rows = {row["ticker"]: row for row in read_csv(coverage_path)}
    current_tickers = {
        ticker
        for ticker, row in coverage_rows.items()
        if int(float(row["latest_session_present"])) == 1
    }
    required_market_date = str(market_manifest.get("final_market_date", ""))
    benchmark_rows = pd.DataFrame(read_gzip_csv(benchmark_path))
    benchmark_rows["date"] = pd.to_datetime(benchmark_rows["date"], errors="raise")
    benchmark_prices = benchmark_rows.pivot(
        index="date", columns="ticker", values="adj_close"
    ).apply(pd.to_numeric, errors="coerce")
    etf_map = {str(key): str(value).upper() for key, value in dict(cfg_get(config, "risk_panel.sector_etf_map", {})).items()}
    market_sha = sha256_file(market_manifest_path)
    rows: list[dict[str, Any]] = []
    for member in sorted(universe, key=lambda row: str(row["ticker"])):
        ticker = str(member["ticker"])
        frame = selected.loc[selected["ticker"] == ticker].set_index("date")
        benchmark_ticker = etf_map.get(str(member["source_pipeline"]), "SPY")
        if benchmark_ticker not in benchmark_prices.columns:
            raise ValueError(f"Stage 2 prices lack benchmark {benchmark_ticker} for {ticker}")
        coverage = coverage_rows.get(ticker)
        if coverage is None:
            raise ValueError(f"Sealed coverage lacks {ticker}")
        if ticker not in current_tickers:
            rows.append(
                missing_latest_signal_row(
                    frame,
                    ticker=ticker,
                    as_of=args.as_of.isoformat(),
                    benchmark_ticker=benchmark_ticker,
                    manifest_sha=market_sha,
                    required_market_date=required_market_date,
                )
            )
            continue
        rows.append(
            compute_signal_row(
                frame,
                _numeric_series(benchmark_prices.loc[:, benchmark_ticker]),
                ticker=ticker,
                as_of=args.as_of.isoformat(),
                benchmark_ticker=benchmark_ticker,
                manifest_sha=market_sha,
                market_component_cap=market_component_cap,
            )
        )
    checks = _validate(
        rows,
        len(universe),
        current_tickers,
        market_component_cap=market_component_cap,
    )
    failures = [row for row in checks if row["status"] == "FAIL"]
    output_dir = (
        args.output_dir
        or paths.output_dir
        / "runs"
        / args.as_of.isoformat()
        / monitor_subdir
        / "signals"
    )
    signals_path = output_dir / "market_signals.csv"
    checks_path = output_dir / "market_signal_validation.csv"
    manifest_path = output_dir / "market_signals_manifest.json"
    fail_if_exists([signals_path, checks_path, manifest_path], force=args.force)
    write_csv(signals_path, SIGNAL_FIELDS, rows)
    write_csv(checks_path, VALIDATION_FIELDS, checks)
    conn = connect_monitor_db(db_path, timeout_sec=timeout)
    try:
        ensure_state_schema(conn)
        with database_writer_lock(db_path, timeout_sec=timeout), conn:
            conn.execute("DELETE FROM market_signals_daily WHERE asof_date=?", (args.as_of.isoformat(),))
            conn.executemany(
                f"INSERT INTO market_signals_daily({','.join(SIGNAL_FIELDS)}) VALUES ({','.join('?' for _ in SIGNAL_FIELDS)})",
                [tuple(row[field] for field in SIGNAL_FIELDS) for row in rows],
            )
    finally:
        conn.close()
    acceptance = "FAIL" if failures else "PASS"
    input_paths = [
        config_path, Path(__file__).resolve(), Path(__file__).with_name("state_common.py"),
        market_manifest_path, market_validation_path,
    ]
    write_manifest(
        manifest_path,
        {
            "schema_version": "monitor_market_signals_manifest_v1",
            "acceptance": acceptance,
            "as_of_date": args.as_of.isoformat(),
            "universe_as_of": universe_as_of,
            "row_count": len(rows),
            "current_market_data_count": len(current_tickers),
            "missing_latest_tickers": sorted(set(coverage_rows) - current_tickers),
            "market_component_cap": market_component_cap,
            "inputs_sha256": {str(path): sha256_file(path) for path in input_paths},
            "outputs_sha256": {signals_path.name: sha256_file(signals_path), checks_path.name: sha256_file(checks_path)},
        },
    )
    print(f"MONITOR MARKET SIGNALS: {acceptance}")
    print(f"rows={len(rows)}; manifest={manifest_path}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
