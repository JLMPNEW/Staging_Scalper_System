#!/usr/bin/env python3
"""Optional Stage 2.5 - collect IBKR historical BID_ASK samples for Stage 4 spread costs.

This script is intentionally disabled by config by default. When enabled, it is
expected to run in the overnight portfolio process while TWS or IB Gateway is
available. It stores:

  runs/<as_of>/risk/ib_spread_samples.csv
  runs/<as_of>/risk/spread_snapshot.csv
  runs/<as_of>/risk/spread_snapshot_meta.json

and upserts the same information into the portfolio-owned SQLite database.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import sys
import time
from collections import Counter
from datetime import date, datetime, time as dt_time, timezone
from pathlib import Path
from typing import Any, Sequence
from zoneinfo import ZoneInfo


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_layer.core.config import cfg_get, load_yaml  # noqa: E402
from portfolio_layer.core.artifacts import (  # noqa: E402
    invalidate_cost_outputs_after_spread_change,
    invalidate_risk_outputs_after_spread_change,
)
from portfolio_layer.core.contracts import fail_if_exists, read_csv, sha256_file, write_csv, write_manifest  # noqa: E402
from portfolio_layer.core.db import connect, finish_run, start_run  # noqa: E402
from portfolio_layer.core.logging_utils import configure_utc_logging  # noqa: E402
from portfolio_layer.core.paths import resolve_database_path, resolve_runtime_paths  # noqa: E402
from portfolio_layer.risk.liquidity import (  # noqa: E402
    IB_SPREAD_SAMPLE_FIELDS,
    SPREAD_SNAPSHOT_FIELDS,
    active_symbol_for_ticker,
    configured_fallback_half_spread_bps,
    finite_float,
    init_liquidity_tables,
    liquidity_half_spread_fail_bps,
    liquidity_panel_active,
    liquidity_config,
    parse_sample_times,
    summarize_spread_samples,
    upsert_spread_samples,
    upsert_spread_snapshot,
    upsert_spread_snapshot_run,
)
from portfolio_layer.risk.readiness import latest_run_with  # noqa: E402


LOGGER = logging.getLogger("collect_ib_spread_samples")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
ET = ZoneInfo("America/New_York")


def iso_date_arg(raw: str) -> str:
    try:
        date.fromisoformat(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"must be YYYY-MM-DD, got {raw!r}") from exc
    return raw


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Collect optional IBKR historical BID_ASK spread samples.")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--as-of", type=iso_date_arg, default=None)
    p.add_argument("--db", type=Path, default=None)
    p.add_argument("--force", action="store_true")
    p.add_argument(
        "--input-samples",
        type=Path,
        default=None,
        help="Use an existing ib_spread_samples-format CSV instead of connecting to IB (test/recovery mode).",
    )
    p.add_argument(
        "--universe-source",
        choices=["risk_eligible_scores", "investable_scores", "target_weights", "trade_list", "auto"],
        default=None,
        help="Override liquidity_panel.universe_source for this run only.",
    )
    return p.parse_args()


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _finite_or_blank(value: Any) -> str | float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return ""
    if not math.isfinite(parsed):
        return ""
    return round(parsed, 6)


def _bar_datetime_et(raw: Any) -> datetime | None:
    if isinstance(raw, datetime):
        dt = raw
    else:
        text = str(raw).strip()
        for fmt in ("%Y%m%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                dt = datetime.strptime(text, fmt)
                break
            except ValueError:
                dt = None  # type: ignore[assignment]
        if dt is None:
            try:
                dt = datetime.fromisoformat(text)
            except ValueError:
                return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ET)
    return dt.astimezone(ET)


def _bar_bid_ask(bar: Any) -> tuple[float, float]:
    # IB historical BID_ASK bars are represented by ib_insync as bid=open and ask=close.
    bid = finite_float(getattr(bar, "open", None), name="bar.open")
    ask = finite_float(getattr(bar, "close", None), name="bar.close")
    return bid, ask


def _sample_rows_for_ticker(
    *,
    ticker: str,
    query_symbol: str,
    bars: Sequence[Any],
    as_of: str,
    sample_times: Sequence[str],
    bar_size: str,
    max_lag_minutes: float,
    max_half_spread_bps: float,
) -> list[dict[str, Any]]:
    run_date = date.fromisoformat(as_of)
    parsed: list[dict[str, Any]] = []
    for bar in bars:
        ts = _bar_datetime_et(getattr(bar, "date", None))
        if ts is None or ts.date() > run_date:
            continue
        try:
            bid, ask = _bar_bid_ask(bar)
        except ValueError:
            continue
        if bid <= 0 or ask <= 0:
            continue
        if ask < bid:
            bid, ask = ask, bid
        mid = (bid + ask) / 2.0
        spread_bps = (ask - bid) / mid * 1e4 if mid > 0 else math.nan
        if not math.isfinite(spread_bps) or spread_bps < 0:
            continue
        parsed.append({
            "timestamp": ts,
            "bid": bid,
            "ask": ask,
            "midpoint": mid,
            "spread_bps": spread_bps,
            "half_spread_bps": spread_bps / 2.0,
        })

    if parsed:
        sample_date = max(row["timestamp"].date() for row in parsed)
    else:
        sample_date = None

    out: list[dict[str, Any]] = []
    for target in sample_times:
        base = {
            "as_of_date": as_of,
            "ticker": ticker,
            "query_symbol": query_symbol,
            "target_time_et": target,
            "bar_date_et": "",
            "bar_timestamp_et": "",
            "bar_size": bar_size,
            "bid": "",
            "ask": "",
            "midpoint": "",
            "spread_bps": "",
            "half_spread_bps": "",
            "source": "ibkr_historical_bid_ask",
            "status": "missing",
            "reason": "no_ib_bars",
        }
        if sample_date is None:
            out.append(base)
            continue
        target_dt = datetime.combine(sample_date, dt_time.fromisoformat(target), tzinfo=ET)
        candidates = [row for row in parsed if row["timestamp"].date() == sample_date]
        if not candidates:
            out.append(base | {"reason": "no_bars_on_sample_date"})
            continue
        nearest = min(candidates, key=lambda row: abs((row["timestamp"] - target_dt).total_seconds()))
        lag_minutes = abs((nearest["timestamp"] - target_dt).total_seconds()) / 60.0
        if lag_minutes > max_lag_minutes:
            out.append(base | {"bar_date_et": sample_date.isoformat(), "reason": f"nearest_bar_lag>{max_lag_minutes:g}m"})
            continue
        status = "ok"
        reason = ""
        if float(nearest["half_spread_bps"]) >= max_half_spread_bps:
            status = "invalid"
            reason = f"half_spread_bps>={max_half_spread_bps:g}"
        out.append({
            **base,
            "bar_date_et": sample_date.isoformat(),
            "bar_timestamp_et": nearest["timestamp"].isoformat(timespec="seconds"),
            "bid": _finite_or_blank(nearest["bid"]),
            "ask": _finite_or_blank(nearest["ask"]),
            "midpoint": _finite_or_blank(nearest["midpoint"]),
            "spread_bps": _finite_or_blank(nearest["spread_bps"]),
            "half_spread_bps": _finite_or_blank(nearest["half_spread_bps"]),
            "status": status,
            "reason": reason,
        })
    return out


def _tickers_from_trade_list(run_dir: Path) -> list[str]:
    trade_path = run_dir / "costs" / "trade_list.csv"
    if not trade_path.exists():
        return []
    return sorted({
        str(r.get("ticker", "")).strip().upper()
        for r in read_csv(trade_path)
        if str(r.get("ticker", "")).strip() and finite_float(r.get("trade_notional") or 0.0, name="trade_notional") > 0
    })


def _tickers_from_target_weights(run_dir: Path) -> list[str]:
    target_path = run_dir / "optimizer" / "target_weights.csv"
    if not target_path.exists():
        return []
    return sorted({
        str(r.get("ticker", "")).strip().upper()
        for r in read_csv(target_path)
        if str(r.get("ticker", "")).strip() and finite_float(r.get("weight") or 0.0, name="weight") > 0
    })


def _tickers_from_scores(run_dir: Path, *, risk_eligible_only: bool) -> list[str]:
    scores_path = run_dir / "stocks_scores.csv"
    score_tickers = {
        str(r.get("ticker", "")).strip().upper()
        for r in read_csv(scores_path)
        if str(r.get("ticker", "")).strip() and str(r.get("investable_eligible", "")).strip() == "1"
    }
    if not risk_eligible_only:
        return sorted(score_tickers)

    coverage_path = run_dir / "risk" / "risk_coverage.csv"
    risk_tickers = {
        str(r.get("ticker", "")).strip().upper()
        for r in read_csv(coverage_path)
        if (
            str(r.get("ticker", "")).strip()
            and str(r.get("role", "")).strip() == "scored"
            and str(r.get("risk_eligible", "")).strip() == "1"
            and str(r.get("score_eligible", "")).strip() == "1"
        )
    }
    return sorted(score_tickers & risk_tickers)


def _load_universe(run_dir: Path, config: dict[str, Any], override: str | None = None) -> tuple[list[str], str]:
    mode = str(override or cfg_get(config, "liquidity_panel.universe_source", "risk_eligible_scores")).strip().lower()
    allowed = {"risk_eligible_scores", "investable_scores", "target_weights", "trade_list", "auto"}
    if mode not in allowed:
        raise ValueError(f"liquidity_panel.universe_source must be one of {sorted(allowed)}, got {mode!r}")

    if mode == "trade_list":
        return _tickers_from_trade_list(run_dir), "costs/trade_list.csv"
    if mode == "target_weights":
        return _tickers_from_target_weights(run_dir), "optimizer/target_weights.csv"
    if mode == "investable_scores":
        return _tickers_from_scores(run_dir, risk_eligible_only=False), "stocks_scores.csv:investable_eligible"
    if mode == "risk_eligible_scores":
        return _tickers_from_scores(run_dir, risk_eligible_only=True), "risk_coverage.csv:risk_eligible_scored"

    for loader, source in (
        (_tickers_from_trade_list, "costs/trade_list.csv"),
        (_tickers_from_target_weights, "optimizer/target_weights.csv"),
        (lambda rd: _tickers_from_scores(rd, risk_eligible_only=True), "risk_coverage.csv:risk_eligible_scored"),
    ):
        tickers = loader(run_dir)
        if tickers:
            return tickers, f"auto:{source}"
    return _tickers_from_scores(run_dir, risk_eligible_only=False), "auto:stocks_scores.csv:investable_eligible"


def _load_input_samples(path: Path, *, as_of: str, tickers: Sequence[str]) -> list[dict[str, Any]]:
    wanted = set(tickers)
    out = []
    for row in read_csv(path):
        ticker = str(row.get("ticker", "")).strip().upper()
        if ticker not in wanted:
            continue
        row = {field: row.get(field, "") for field in IB_SPREAD_SAMPLE_FIELDS}
        row["as_of_date"] = as_of
        row["ticker"] = ticker
        out.append(row)
    return out


def _collect_from_ib(
    *,
    config: dict[str, Any],
    as_of: str,
    tickers: Sequence[str],
    sample_times: Sequence[str],
) -> list[dict[str, Any]]:
    try:
        from ib_insync import IB, Stock  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "liquidity_panel.enhanced_intraday_enabled=true requires ib_insync in the portfolio environment"
        ) from exc

    lc = liquidity_config(config)
    ib_cfg = lc.get("ib", {}) if isinstance(lc.get("ib"), dict) else {}
    host = str(ib_cfg.get("host", "127.0.0.1"))
    port = int(ib_cfg.get("port", 7497))
    client_id = int(ib_cfg.get("client_id", 41))
    duration_days = max(1, int(lc.get("duration_days", 5)))
    duration = f"{duration_days} D"
    bar_size = str(lc.get("bar_size", "5 mins"))
    use_rth = bool(lc.get("use_rth", True))
    pause = max(0.0, float(lc.get("request_pause_sec", 2.0)))
    retries = max(1, int(lc.get("max_retries", 3)))
    max_lag = max(0.0, float(lc.get("max_sample_lag_minutes", 12)))
    max_half_spread = liquidity_half_spread_fail_bps(config)
    end_dt = datetime.combine(date.fromisoformat(as_of), dt_time(23, 59, 59), tzinfo=ET)

    ib = IB()
    rows: list[dict[str, Any]] = []
    try:
        logging.getLogger("ib_insync").setLevel(logging.WARNING)
        ib.connect(host, port, clientId=client_id, timeout=20)
        total = len(tickers)
        for index, ticker in enumerate(tickers, start=1):
            if index == 1 or index % 25 == 0 or index == total:
                LOGGER.info("IB BID_ASK collection progress: %d/%d (%s)", index, total, ticker)
            query_symbol, _alias = active_symbol_for_ticker(config, ticker, as_of)
            ib_symbol = query_symbol.replace(".", " ")
            contract = Stock(ib_symbol, "SMART", "USD")
            bars: Sequence[Any] = []
            reason = ""
            for attempt in range(1, retries + 1):
                try:
                    qualified = ib.qualifyContracts(contract)
                    contract_to_use = qualified[0] if qualified else contract
                    bars = ib.reqHistoricalData(
                        contract_to_use,
                        endDateTime=end_dt,
                        durationStr=duration,
                        barSizeSetting=bar_size,
                        whatToShow="BID_ASK",
                        useRTH=use_rth,
                        formatDate=2,
                        keepUpToDate=False,
                    )
                    reason = ""
                    break
                except Exception as exc:  # noqa: BLE001 - IB errors vary by client/library version.
                    reason = f"ib_error:{type(exc).__name__}:{exc}"
                    if attempt < retries:
                        time.sleep(pause)
            if bars:
                rows.extend(_sample_rows_for_ticker(
                    ticker=ticker,
                    query_symbol=query_symbol,
                    bars=bars,
                    as_of=as_of,
                    sample_times=sample_times,
                    bar_size=bar_size,
                    max_lag_minutes=max_lag,
                    max_half_spread_bps=max_half_spread,
                ))
            else:
                for target in sample_times:
                    rows.append({
                        "as_of_date": as_of,
                        "ticker": ticker,
                        "query_symbol": query_symbol,
                        "target_time_et": target,
                        "bar_date_et": "",
                        "bar_timestamp_et": "",
                        "bar_size": bar_size,
                        "bid": "",
                        "ask": "",
                        "midpoint": "",
                        "spread_bps": "",
                        "half_spread_bps": "",
                        "source": "ibkr_historical_bid_ask",
                        "status": "missing",
                        "reason": reason or "no_ib_bars",
                    })
            if pause > 0:
                time.sleep(pause)
    finally:
        if ib.isConnected():
            ib.disconnect()
    return rows


def main() -> int:  # noqa: C901
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    paths = resolve_runtime_paths(config, config_path)
    try:
        db_path = resolve_database_path(paths, args.db)
    except ValueError as exc:
        LOGGER.error("%s", exc)
        return 1

    runs_root = paths.output_dir / "runs"
    run_as_of = args.as_of or latest_run_with(runs_root, "manifest.json")
    if not run_as_of:
        LOGGER.error("No run found under %s", runs_root)
        return 1
    if not liquidity_panel_active(config) and args.input_samples is None:
        LOGGER.info("Enhanced intraday liquidity panel inactive; Stage 4 will use config/default spread.")
        return 0

    run_dir = runs_root / run_as_of
    risk_dir = run_dir / "risk"
    samples_path = risk_dir / "ib_spread_samples.csv"
    snapshot_path = risk_dir / "spread_snapshot.csv"
    meta_path = risk_dir / "spread_snapshot_meta.json"
    if args.force:
        for path in (samples_path, snapshot_path, meta_path):
            if path.exists() and path.is_file():
                path.unlink()
        invalidate_risk_outputs_after_spread_change(risk_dir)
        invalidate_cost_outputs_after_spread_change(run_dir)
    try:
        fail_if_exists([samples_path, snapshot_path, meta_path], force=args.force)
    except FileExistsError as exc:
        LOGGER.error("%s", exc)
        return 1

    try:
        sample_times = parse_sample_times(config)
        fallback_bps = configured_fallback_half_spread_bps(config)
        min_samples = int(cfg_get(config, "liquidity_panel.min_valid_samples", 2))
        max_stale_days = int(cfg_get(config, "liquidity_panel.max_stale_liquidity_days", 5))
        max_half_spread = liquidity_half_spread_fail_bps(config)
        allow_fallback = bool(cfg_get(config, "liquidity_panel.allow_fallback_to_default", True))
        tickers, universe_source = _load_universe(run_dir, config, args.universe_source)
    except (ValueError, FileNotFoundError) as exc:
        LOGGER.error("%s", exc)
        return 1
    if not tickers:
        LOGGER.error("No tickers found for liquidity collection in %s", run_dir)
        return 1

    try:
        if args.input_samples is not None:
            input_path = args.input_samples.expanduser().resolve()
            sample_rows = _load_input_samples(input_path, as_of=run_as_of, tickers=tickers)
            provider = f"input_samples:{input_path}"
        else:
            sample_rows = _collect_from_ib(config=config, as_of=run_as_of, tickers=tickers, sample_times=sample_times)
            provider = str(cfg_get(config, "liquidity_panel.provider", "ibkr_historical_bid_ask"))
    except Exception as exc:  # noqa: BLE001 - collector should fail closed with a clear log line.
        LOGGER.error("Liquidity collection failed: %s", exc)
        return 1

    try:
        snapshot_rows = summarize_spread_samples(
            sample_rows,
            as_of=run_as_of,
            tickers=tickers,
            sample_times=sample_times,
            min_valid_samples=min_samples,
            max_stale_days=max_stale_days,
            max_half_spread_bps=max_half_spread,
            fallback_half_spread_bps=fallback_bps,
            allow_fallback=allow_fallback,
        )
    except ValueError as exc:
        LOGGER.error("%s", exc)
        return 1

    write_csv(samples_path, IB_SPREAD_SAMPLE_FIELDS, sample_rows)
    write_csv(snapshot_path, SPREAD_SNAPSHOT_FIELDS, snapshot_rows)
    status_counts = Counter(str(r.get("spread_status", "")) for r in snapshot_rows)
    source_counts = Counter(str(r.get("spread_source", "")) for r in snapshot_rows)
    counts = {
        "requested_tickers": len(tickers),
        "ok_tickers": int(status_counts.get("ok", 0)) + int(status_counts.get("ok_latest_available", 0)),
        "fallback_tickers": int(status_counts.get("fallback", 0)),
        "failed_tickers": int(status_counts.get("failed", 0)),
        "sample_rows": len(sample_rows),
        "snapshot_rows": len(snapshot_rows),
    }
    meta = {
        "run_as_of": run_as_of,
        "stage": "stage2_5_intraday_liquidity",
        "provider": provider,
        "generated_at": _timestamp(),
        "enabled": True,
        "panel_active": True,
        "enhanced_intraday_enabled": cfg_get(config, "liquidity_panel.enhanced_intraday_enabled", False),
        "spread_source": cfg_get(config, "transaction_costs.spread_source", "auto"),
        "universe_source": universe_source,
        "sample_times_et": sample_times,
        "bar_size": str(cfg_get(config, "liquidity_panel.bar_size", "5 mins")),
        "duration_days": int(cfg_get(config, "liquidity_panel.duration_days", 5)),
        "min_valid_samples": min_samples,
        "max_stale_liquidity_days": max_stale_days,
        "max_half_spread_bps": max_half_spread,
        "fallback_half_spread_bps": fallback_bps,
        "allow_fallback_to_default": allow_fallback,
        "max_universe_fallback_fraction": cfg_get(config, "liquidity_panel.max_universe_fallback_fraction", 0.10),
        "max_fallback_fraction": cfg_get(config, "liquidity_panel.max_fallback_fraction", 0.10),
        "counts": counts,
        "spread_status_counts": dict(status_counts),
        "spread_source_counts": dict(source_counts),
        "files": {
            "ib_spread_samples.csv": {"sha256": sha256_file(samples_path), "rows": len(sample_rows)},
            "spread_snapshot.csv": {"sha256": sha256_file(snapshot_path), "rows": len(snapshot_rows)},
        },
    }
    write_manifest(meta_path, meta)

    with connect(db_path) as conn:
        init_liquidity_tables(conn)
        run_id = start_run(conn, run_type="collect_ib_spread_samples", input_path=run_dir)
        upsert_spread_samples(conn, sample_rows)
        upsert_spread_snapshot(conn, snapshot_rows)
        db_meta = dict(meta)
        db_meta["metadata_json"] = json.dumps(meta, sort_keys=True)
        upsert_spread_snapshot_run(
            conn,
            as_of=run_as_of,
            metadata=db_meta,
            samples_sha256=sha256_file(samples_path),
            snapshot_sha256=sha256_file(snapshot_path),
        )
        finish_run(
            conn,
            run_id=run_id,
            status="success" if counts["failed_tickers"] == 0 else "warning",
            row_count=len(snapshot_rows),
            message=f"as_of={run_as_of} ok={counts['ok_tickers']} fallback={counts['fallback_tickers']} failed={counts['failed_tickers']}",
        )

    LOGGER.info(
        "Liquidity snapshot: %d tickers (%d ok / %d fallback / %d failed) -> %s",
        len(snapshot_rows), counts["ok_tickers"], counts["fallback_tickers"], counts["failed_tickers"], snapshot_path,
    )
    return 0 if counts["failed_tickers"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
