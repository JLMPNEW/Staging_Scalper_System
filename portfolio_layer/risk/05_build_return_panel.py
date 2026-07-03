#!/usr/bin/env python3
"""Stage 2 - build the self-sourced adjusted-close return panel over the current universe.

Fetches Yahoo adjusted close for eligible names + benchmarks/ETFs, aligns to the SPY master calendar
(no future dates beyond run as-of, no fabricated zero returns), and writes the panel plus a
provenance-hashed price snapshot so the run reproduces even if the provider later revises prices.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd  # noqa: E402

from portfolio_layer.core.config import cfg_get, load_yaml  # noqa: E402
from portfolio_layer.core.config import resolve_path  # noqa: E402
from portfolio_layer.core.contracts import fail_if_exists, read_csv, sha256_file, write_csv, write_manifest  # noqa: E402
from portfolio_layer.core.db import connect, finish_run, start_run  # noqa: E402
from portfolio_layer.core.logging_utils import configure_utc_logging  # noqa: E402
from portfolio_layer.core.paths import resolve_database_path, resolve_runtime_paths  # noqa: E402
from portfolio_layer.risk.panel import assemble_prices, build_universe, master_calendar, to_returns  # noqa: E402
from portfolio_layer.risk.readiness import check_stage1_readiness, latest_run_with, readiness_passed  # noqa: E402
from portfolio_layer.risk.yahoo import fetch_adjclose_with_splits  # noqa: E402


LOGGER = logging.getLogger("build_return_panel")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"


def iso_date_arg(raw: str) -> str:
    try:
        date.fromisoformat(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"must be YYYY-MM-DD, got {raw!r}") from exc
    return raw


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build the Stage 2 self-sourced return panel.")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--as-of", type=iso_date_arg, default=None)
    p.add_argument("--db", type=Path, default=None)
    p.add_argument("--force", action="store_true")
    p.add_argument(
        "--reuse-existing-panel",
        action="store_true",
        help="Seed unchanged non-aliased tickers from this run's existing prices_adjclose.csv.",
    )
    p.add_argument(
        "--reuse-price-cache",
        action="store_true",
        help="Use portfolio_layer/output/cache/risk_prices when it is current for the requested as-of.",
    )
    return p.parse_args()


def write_df(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    os.close(fd)
    out = frame.copy()
    out.index = [d.date().isoformat() for d in out.index]
    out.to_csv(tmp, lineterminator="\n")
    os.replace(tmp, path)


def unlink_artifacts(paths: list[Path]) -> None:
    for path in paths:
        if path.exists():
            path.unlink()


def cache_path(cache_dir: Path, ticker: str) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "_" for ch in ticker.upper())
    return cache_dir / f"{safe}.json"


def parse_ticker_aliases(rc: dict) -> dict[str, dict[str, str]]:
    """Load corporate-action ticker aliases used only for market-data fetches."""
    aliases: dict[str, dict[str, str]] = {}
    raw = rc.get("ticker_aliases") or {}
    if not isinstance(raw, dict):
        return aliases
    for raw_ticker, raw_cfg in raw.items():
        ticker = str(raw_ticker).strip().upper()
        if not ticker or not isinstance(raw_cfg, dict):
            continue
        active = str(raw_cfg.get("active_ticker") or raw_cfg.get("source_symbol") or "").strip().upper()
        effective = str(raw_cfg.get("effective_date") or "").strip()
        if not active or not effective:
            raise ValueError(f"risk_panel.ticker_aliases.{ticker} requires active_ticker and effective_date")
        date.fromisoformat(effective)
        predecessor = str(
            raw_cfg.get("predecessor_ticker")
            or raw_cfg.get("legacy_ticker")
            or raw_cfg.get("old_ticker")
            or (ticker if ticker != active else "")
        ).strip().upper()
        aliases[ticker] = {
            "active_ticker": active,
            "predecessor_ticker": predecessor,
            "effective_date": effective,
            "price_history_csv": str(raw_cfg.get("price_history_csv") or "").strip(),
            "issuer_id": str(raw_cfg.get("issuer_id") or "").strip(),
            "reason": str(raw_cfg.get("reason") or "").strip(),
        }
    return aliases


def fetch_symbols_for(ticker: str, aliases: dict[str, dict[str, str]], *, run_date: date) -> list[str]:
    """Return provider query symbols for a contract ticker, active alias first when effective."""
    ticker = ticker.upper()
    alias = aliases.get(ticker)
    if not alias or run_date < date.fromisoformat(alias["effective_date"]):
        return [ticker]
    active = alias["active_ticker"]
    # After a same-issuer ticker migration, the retired symbol can return stale legacy bars. Query only
    # the configured active symbol and floor the fetch at the effective date below.
    return [active]


def fetch_segments_for(
    ticker: str,
    aliases: dict[str, dict[str, str]],
    *,
    start: date,
    end: date,
) -> list[dict[str, date | str]]:
    """Return provider-symbol date segments for same-issuer ticker lineage."""
    ticker = ticker.upper()
    alias = aliases.get(ticker)
    if not alias:
        return [{"query_symbol": ticker, "start": start, "end": end, "segment": "direct"}]
    effective = date.fromisoformat(alias["effective_date"])
    active = alias["active_ticker"]
    predecessor = str(alias.get("predecessor_ticker") or "").upper()
    if end < effective:
        return [{"query_symbol": predecessor or ticker, "start": start, "end": end, "segment": "predecessor"}]

    segments: list[dict[str, date | str]] = []
    if predecessor and start < effective:
        predecessor_end = min(end, effective - timedelta(days=1))
        if start <= predecessor_end:
            segments.append({
                "query_symbol": predecessor,
                "start": start,
                "end": predecessor_end,
                "segment": "predecessor",
            })
    active_start = max(start, effective)
    if active_start <= end:
        segments.append({
            "query_symbol": active,
            "start": active_start,
            "end": end,
            "segment": "active",
        })
    return segments or [{"query_symbol": active, "start": start, "end": end, "segment": "active"}]


def load_cached_bars(
    cache_dir: Path,
    ticker: str,
    *,
    start: date,
    end: date,
    expected_query_symbols: set[str],
) -> tuple[list[tuple[str, float]], list[dict[str, str]], str, str, str] | None:
    path = cache_path(cache_dir, ticker)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    query_symbol = str(payload.get("query_symbol") or payload.get("ticker") or ticker).strip().upper()
    if expected_query_symbols and query_symbol not in expected_query_symbols:
        return None
    bars = [
        (str(row["date"]), float(row["adjclose"]))
        for row in payload.get("bars", [])
        if str(row.get("date", "")) <= end.isoformat()
    ]
    if not bars:
        return None
    dates = [d for d, _ in bars]
    if min(dates) > (start + timedelta(days=7)).isoformat() or max(dates) < end.isoformat():
        return None
    source_symbol = str(payload.get("source_symbol") or query_symbol).strip()
    split_events = [
        {
            "split_date": str(row.get("split_date") or ""),
            "numerator": str(row.get("numerator") or ""),
            "denominator": str(row.get("denominator") or ""),
            "split_ratio": str(row.get("split_ratio") or ""),
        }
        for row in payload.get("split_events", [])
        if row.get("split_date")
    ]
    return bars, split_events, str(payload.get("provider", "cache")), query_symbol, source_symbol


def write_cached_bars(
    cache_dir: Path,
    ticker: str,
    provider: str,
    bars: list[tuple[str, float]],
    split_events: list[dict[str, str]],
    *,
    query_symbol: str,
    source_symbol: str,
) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "ticker": ticker,
        "query_symbol": query_symbol,
        "source_symbol": source_symbol,
        "provider": provider,
        "cached_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "split_events": split_events,
        "bars": [{"date": d, "adjclose": v} for d, v in bars],
    }
    path = cache_path(cache_dir, ticker)
    try:
        write_manifest(path, payload)
    except OSError as exc:
        LOGGER.warning("Skipping price-cache update for %s at %s: %s", ticker, path, exc)


def load_existing_price_seed(path: Path, *, end: date) -> dict[str, list[tuple[str, float]]]:
    if not path.exists():
        return {}
    try:
        frame = pd.read_csv(path, index_col=0)
    except Exception:  # noqa: BLE001 - stale/bad prior artifact should not block a fresh fetch
        return {}
    seed: dict[str, list[tuple[str, float]]] = {}
    end_s = end.isoformat()
    for ticker in frame.columns:
        series = frame[ticker].dropna()
        if series.empty or str(series.index[-1]) < end_s:
            continue
        seed[str(ticker)] = [(str(idx), float(value)) for idx, value in series.items() if str(idx) <= end_s]
    return seed


def load_price_history_csv(
    path: Path,
    ticker: str,
    *,
    start: date,
    end: date,
    alternate_tickers: set[str] | None = None,
) -> list[tuple[str, float]]:
    """Load an explicit same-issuer lineage price-history override."""
    if not path.exists():
        return []
    out: list[tuple[str, float]] = []
    start_s = start.isoformat()
    end_s = end.isoformat()
    allowed_tickers = {ticker.upper()}
    allowed_tickers.update(str(t).upper() for t in (alternate_tickers or set()) if str(t).strip())
    for row in read_csv(path):
        row_ticker = str(row.get("ticker") or "").strip().upper()
        day = str(row.get("date") or row.get("bar_date") or "").strip()
        raw_adj = str(row.get("adjclose") or row.get("adj_close") or "").strip()
        if row_ticker not in allowed_tickers or not day or day < start_s or day > end_s or not raw_adj:
            continue
        try:
            adj = float(raw_adj)
        except ValueError:
            continue
        if adj > 0:
            out.append((day, adj))
    return sorted(out)


def price_history_csv_summary(path: Path) -> dict[str, str | int | bool]:
    if not path.exists():
        return {"exists": False, "rows": 0, "first_date": "", "last_date": ""}
    dates = [
        str(row.get("date") or row.get("bar_date") or "").strip()
        for row in read_csv(path)
        if str(row.get("date") or row.get("bar_date") or "").strip()
    ]
    return {
        "exists": True,
        "rows": len(dates),
        "first_date": min(dates) if dates else "",
        "last_date": max(dates) if dates else "",
    }


def main() -> int:
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
    rc = cfg_get(config, "risk_panel", {})
    runs_root = paths.output_dir / "runs"
    run_as_of = args.as_of or latest_run_with(runs_root, "manifest.json")
    if not run_as_of:
        LOGGER.error("No sealed Stage 1 run found under %s", runs_root)
        return 1

    tolerance = int(cfg_get(config, "score_contract.staleness_tolerance_days", 10))
    expected = []
    per_pipeline_tolerance = {}
    for sector in cfg_get(config, "score_contract.sectors", []):
        if not bool(sector.get("enabled", True)):
            continue
        pipe = str(sector["model_family"])
        expected.append(pipe)
        per_pipeline_tolerance[pipe] = int(sector.get("staleness_tolerance_days", tolerance))
    stale_status = str(cfg_get(config, "risk_panel.readiness_stale_status", "FAIL"))
    readiness = check_stage1_readiness(
        runs_root,
        run_as_of,
        staleness_tolerance=tolerance,
        per_pipeline_staleness_tolerance=per_pipeline_tolerance,
        expected_pipelines=expected,
        stale_status=stale_status,
    )
    for c in readiness:
        LOGGER.info("readiness [%s] %s -- %s", c["status"], c["check"], c["detail"])
    if not readiness_passed(readiness):
        LOGGER.error("Stage 1 readiness FAILED; refusing to build panel for %s", run_as_of)
        return 1

    run_dir = runs_root / run_as_of
    risk_dir = run_dir / "risk"
    prices_path = risk_dir / "prices_adjclose.csv"
    returns_path = risk_dir / "returns_panel.csv"
    fetch_path = risk_dir / "fetch_results.csv"
    split_events_path = risk_dir / "split_events.csv"
    snapshot_path = risk_dir / "price_snapshot.json"
    if args.force:
        unlink_artifacts([
            risk_dir / "risk_coverage.csv",
            risk_dir / "covariance.csv",
            risk_dir / "covariance_period.csv",
            risk_dir / "correlation_clusters.csv",
            risk_dir / "covariance_meta.json",
            risk_dir / "return_outliers.csv",
            risk_dir / "data_quality_review.csv",
            risk_dir / "ib_spread_samples.csv",
            risk_dir / "spread_snapshot.csv",
            risk_dir / "spread_snapshot_meta.json",
            risk_dir / "liquidity_audit.csv",
            risk_dir / "liquidity_audit_by_sector.csv",
            risk_dir / "liquidity_audit_summary.json",
            risk_dir / "validation" / "risk_panel_validation.csv",
            risk_dir / "risk_manifest.json",
        ])
    try:
        fail_if_exists([prices_path, returns_path, fetch_path, split_events_path, snapshot_path], force=args.force)
    except FileExistsError as exc:
        LOGGER.error("%s", exc)
        return 1

    universe = build_universe(run_dir / "stocks_scores.csv", rc)
    master_ticker = str(cfg_get(config, "risk_panel.master_calendar_ticker", "SPY")).upper()
    tickers = sorted({u["ticker"] for u in universe} | {master_ticker})
    lookback = int(cfg_get(config, "risk_panel.lookback_trading_days", 504))
    run_date = date.fromisoformat(run_as_of)
    start = run_date - timedelta(days=int(lookback * 1.6) + 40)

    fetch_cfg = cfg_get(config, "risk_panel.fetch", {})
    raw_templates = fetch_cfg.get("chart_url_templates")
    if isinstance(raw_templates, list) and raw_templates:
        url_templates = [str(x) for x in raw_templates]
    else:
        first = str(fetch_cfg.get("chart_url_template"))
        url_templates = [first]
        if "query1.finance.yahoo.com" in first:
            url_templates.append(first.replace("query1.finance.yahoo.com", "query2.finance.yahoo.com"))
    ua = str(fetch_cfg.get("user_agent", "portfolio_layer/0.1"))
    timeout = float(fetch_cfg.get("request_timeout_sec", 20))
    retries = int(fetch_cfg.get("max_retries", 3))
    master_retry_attempts = max(0, int(fetch_cfg.get("master_calendar_retry_attempts", 3)))
    master_retry_sleep_sec = max(0.0, float(fetch_cfg.get("master_calendar_retry_sleep_sec", 5.0)))
    workers = int(fetch_cfg.get("max_workers", 10))
    enable_stooq = bool(fetch_cfg.get("enable_stooq_fallback", True))
    price_cache_dir = paths.cache_dir / "risk_prices"
    existing_seed = load_existing_price_seed(prices_path, end=run_date) if args.reuse_existing_panel else {}
    try:
        ticker_aliases = parse_ticker_aliases(rc)
    except ValueError as exc:
        LOGGER.error("%s", exc)
        return 1
    price_history_overrides = []
    for ticker, alias in sorted(ticker_aliases.items()):
        raw_history = str(alias.get("price_history_csv") or "").strip()
        if not raw_history:
            continue
        history_path = resolve_path(raw_history, base_dir=config_path.parent)
        summary = price_history_csv_summary(history_path)
        price_history_overrides.append({
            "ticker": ticker,
            "path": str(history_path),
            "exists": summary["exists"],
            "rows": summary["rows"],
            "first_date": summary["first_date"],
            "last_date": summary["last_date"],
            "sha256": sha256_file(history_path) if history_path.exists() else "",
        })

    def fetch_one(
        ticker: str,
    ) -> tuple[list[tuple[str, float]], list[dict[str, str]], str, str, str, str, bool, str, str, str]:
        alias = ticker_aliases.get(ticker)
        alias_applied = bool(alias and run_date >= date.fromisoformat(alias["effective_date"]))
        alias_effective_date = alias["effective_date"] if alias_applied and alias else ""
        alias_issuer_id = alias["issuer_id"] if alias_applied and alias else ""
        alias_reason = alias["reason"] if alias_applied and alias else ""
        segments = fetch_segments_for(ticker, ticker_aliases, start=start, end=run_date)
        expected_query_symbols = {str(segment["query_symbol"]) for segment in segments}

        if ticker in existing_seed and not alias_applied:
            # Seed bars carry no split events; do NOT write them into the price cache — that would
            # overwrite a previously fetched entry's real split_events and blind the split-artifact
            # detector for every later --reuse-price-cache run.
            bars = existing_seed[ticker]
            return bars, [], "ok", "existing_price_snapshot", ticker, ticker, False, "", "", ""

        # Aliased (reused-ticker) names skip the cache so the start-date floor is always re-applied and
        # any pre-effective-date history cached by an earlier run is overwritten.
        cached = None if alias_applied or not args.reuse_price_cache else load_cached_bars(
            price_cache_dir,
            ticker,
            start=start,
            end=run_date,
            expected_query_symbols=expected_query_symbols,
        )
        if cached:
            bars, split_events, provider, query_symbol, source_symbol = cached
            return (
                bars,
                split_events,
                "ok",
                f"cache:{provider}",
                source_symbol,
                query_symbol,
                alias_applied,
                alias_effective_date,
                alias_issuer_id,
                alias_reason,
            )

        combined_bars: dict[str, float] = {}
        combined_splits: list[dict[str, str]] = []
        providers: list[str] = []
        query_symbols_used: list[str] = []
        source_symbols_used: list[str] = []
        tail_notes: list[str] = []
        lineage_used = False
        lineage_last: date | None = None
        if alias_applied and alias and alias.get("price_history_csv"):
            history_path = resolve_path(alias["price_history_csv"], base_dir=config_path.parent)
            bars = load_price_history_csv(
                history_path,
                ticker,
                start=start,
                end=run_date,
                alternate_tickers={str(alias.get("active_ticker") or ""), str(alias.get("predecessor_ticker") or "")},
            )
            if bars:
                dates = [d for d, _ in bars]
                left_ok = min(dates) <= (start + timedelta(days=7)).isoformat()
                if left_ok:
                    for day, value in bars:
                        combined_bars[day] = value
                    lineage_used = True
                    lineage_last = date.fromisoformat(max(dates))
                    providers.append("lineage_price_history_csv")
                    query_symbols_used.append(f"{alias.get('predecessor_ticker') or ticker}|{alias['active_ticker']}")
                    source_symbols_used.append(ticker)
                    statuses = []
                else:
                    statuses = [f"{ticker}:lineage_csv:insufficient_left_edge:{min(dates)}..{max(dates)}"]
            else:
                statuses = [f"{ticker}:lineage_csv:missing_or_empty:{history_path}"]
        else:
            statuses = []

        for segment in segments:
            query_symbol = str(segment["query_symbol"])
            sym_start = segment["start"]
            sym_end = segment["end"]
            if not isinstance(sym_start, date) or not isinstance(sym_end, date):
                statuses.append(f"{query_symbol}:invalid_segment")
                continue
            if lineage_used:
                if str(segment["segment"]) == "predecessor":
                    continue
                if alias and query_symbol == alias["active_ticker"] and lineage_last is not None:
                    sym_start = max(sym_start, lineage_last + timedelta(days=1))
                    if sym_start > sym_end:
                        continue
            bars, split_events, status, provider, source_symbol = fetch_adjclose_with_splits(
                query_symbol,
                start=sym_start,
                end=sym_end,
                url_templates=url_templates,
                user_agent=ua,
                timeout_sec=timeout,
                max_retries=retries,
                enable_stooq_fallback=enable_stooq,
            )
            if status == "ok" and bars:
                bars = [(d, v) for d, v in bars if sym_start.isoformat() <= d <= sym_end.isoformat()]
                split_events = [
                    {
                        **row,
                        "_query_symbol": query_symbol,
                        "_source_symbol": source_symbol,
                        "_provider": provider,
                        "_segment": str(segment["segment"]),
                    }
                    for row in split_events
                    if sym_start.isoformat() <= row["split_date"] <= sym_end.isoformat()
                ]
            if status == "ok" and bars:
                for day, value in bars:
                    combined_bars[day] = value
                combined_splits.extend(split_events)
                providers.append(provider)
                query_symbols_used.append(query_symbol)
                source_symbols_used.append(source_symbol)
                continue
            if lineage_used and alias and query_symbol == alias["active_ticker"]:
                tail_notes.append(
                    f"{query_symbol}:{status if bars or status != 'ok' else 'empty_after_lineage_tail'}"
                )
                continue
            statuses.append(
                f"{query_symbol}:{segment['segment']}:"
                f"{status if bars or status != 'ok' else 'empty_after_segment_floor'}"
            )
        if "stooq_us_daily" in providers and len(set(providers)) > 1:
            # Stooq serves dividend-UNadjusted closes; splicing it against adjusted Yahoo/lineage
            # segments fabricates a level jump at the seam. Refuse the splice: the name routes to
            # coverage as a failed fetch instead of sealing a mixed-basis series.
            statuses.append(f"{ticker}:cross_provider_adjustment_splice_refused:{'+'.join(sorted(set(providers)))}")
        if combined_bars and not statuses:
            bars = sorted(combined_bars.items())
            provider = providers[0] if len(set(providers)) == 1 else "lineage:" + "+".join(providers)
            if tail_notes:
                provider = f"{provider};tail_gap={'|'.join(tail_notes)}"
            query_symbol = query_symbols_used[0] if len(query_symbols_used) == 1 else "|".join(query_symbols_used)
            source_symbol = (
                source_symbols_used[0]
                if len(source_symbols_used) == 1
                else "|".join(source_symbols_used)
            )
            write_cached_bars(
                price_cache_dir,
                ticker,
                provider,
                bars,
                combined_splits,
                query_symbol=query_symbol,
                source_symbol=source_symbol,
            )
            return (
                bars,
                combined_splits,
                "ok",
                provider,
                source_symbol,
                query_symbol,
                alias_applied,
                alias_effective_date,
                alias_issuer_id,
                alias_reason,
            )
        return (
            [],
            [],
            "|".join(statuses) if statuses else "no_providers",
            "",
            str(segments[0]["query_symbol"]) if segments else ticker,
            str(segments[0]["query_symbol"]) if segments else ticker,
            alias_applied,
            alias_effective_date,
            alias_issuer_id,
            alias_reason,
        )

    series_by_ticker: dict[str, dict[str, float]] = {}
    fetch_rows: list[dict] = []
    split_rows: list[dict] = []
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    LOGGER.info("Fetching %d tickers (lookback=%d, start=%s, end=%s)", len(tickers), lookback, start, run_as_of)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(fetch_one, t): t for t in tickers}
        for fut in as_completed(futures):
            t = futures[fut]
            (
                bars,
                split_events,
                status,
                provider,
                source_symbol,
                query_symbol,
                alias_applied,
                alias_effective_date,
                alias_issuer_id,
                alias_reason,
            ) = fut.result()
            bars = [(d, v) for d, v in bars if d <= run_as_of]  # hard right edge = run_as_of
            if status == "ok" and bars:
                series_by_ticker[t] = dict(bars)
                for row in split_events:
                    split_rows.append({
                        "ticker": t,
                        "query_symbol": row.get("_query_symbol", query_symbol),
                        "source_symbol": row.get("_source_symbol", source_symbol),
                        "provider": row.get("_provider", provider),
                        "split_date": row["split_date"],
                        "numerator": row.get("numerator", ""),
                        "denominator": row.get("denominator", ""),
                        "split_ratio": row.get("split_ratio", ""),
                    })
            fetch_rows.append({
                "ticker": t, "status": status, "provider": provider, "source_symbol": source_symbol,
                "query_symbol": query_symbol,
                "alias_applied": int(alias_applied),
                "alias_effective_date": alias_effective_date,
                "alias_issuer_id": alias_issuer_id,
                "alias_reason": alias_reason,
                "rows": len(bars),
                "first": bars[0][0] if bars else "", "last": bars[-1][0] if bars else "",
            })

    if master_ticker not in series_by_ticker and master_retry_attempts > 0:
        LOGGER.warning(
            "Master calendar ticker %s failed in batch; retrying it serially up to %d time(s)",
            master_ticker,
            master_retry_attempts,
        )
        retry_row: dict | None = None
        for attempt in range(1, master_retry_attempts + 1):
            if master_retry_sleep_sec > 0:
                time.sleep(master_retry_sleep_sec)
            (
                bars,
                split_events,
                status,
                provider,
                source_symbol,
                query_symbol,
                alias_applied,
                alias_effective_date,
                alias_issuer_id,
                alias_reason,
            ) = fetch_one(master_ticker)
            bars = [(d, v) for d, v in bars if d <= run_as_of]
            retry_row = {
                "ticker": master_ticker,
                "status": status,
                "provider": f"{provider};master_calendar_retry_attempt={attempt}" if provider else "",
                "source_symbol": source_symbol,
                "query_symbol": query_symbol,
                "alias_applied": int(alias_applied),
                "alias_effective_date": alias_effective_date,
                "alias_issuer_id": alias_issuer_id,
                "alias_reason": alias_reason,
                "rows": len(bars),
                "first": bars[0][0] if bars else "",
                "last": bars[-1][0] if bars else "",
            }
            if status == "ok" and bars:
                series_by_ticker[master_ticker] = dict(bars)
                for row in split_events:
                    split_rows.append({
                        "ticker": master_ticker,
                        "query_symbol": row.get("_query_symbol", query_symbol),
                        "source_symbol": row.get("_source_symbol", source_symbol),
                        "provider": row.get("_provider", provider),
                        "split_date": row["split_date"],
                        "numerator": row.get("numerator", ""),
                        "denominator": row.get("denominator", ""),
                        "split_ratio": row.get("split_ratio", ""),
                    })
                LOGGER.info("Master calendar retry succeeded for %s on attempt %d", master_ticker, attempt)
                break
            LOGGER.warning("Master calendar retry %d/%d failed for %s: %s", attempt, master_retry_attempts, master_ticker, status)
        if retry_row is not None:
            fetch_rows = [row for row in fetch_rows if row["ticker"] != master_ticker]
            fetch_rows.append(retry_row)

    if master_ticker not in series_by_ticker:
        LOGGER.error("Master calendar ticker %s failed to fetch; cannot align panel", master_ticker)
        return 1

    calendar = master_calendar(series_by_ticker[master_ticker], run_as_of, lookback)
    panel_tickers = [u["ticker"] for u in sorted(universe, key=lambda u: u["ticker"])]
    prices = assemble_prices({t: series_by_ticker[t] for t in panel_tickers if t in series_by_ticker}, calendar)
    frequency = str(cfg_get(config, "risk_panel.covariance_frequency", "daily"))
    returns = to_returns(prices, frequency)

    write_df(prices_path, prices)
    write_df(returns_path, returns)
    role_by_ticker = {u["ticker"]: (u["role"], u["source_pipeline"]) for u in universe}
    for row in fetch_rows:
        role, pipe = role_by_ticker.get(
            row["ticker"],
            ("master_calendar" if row["ticker"] == master_ticker else "", ""),
        )
        row["role"], row["source_pipeline"] = role, pipe
    write_csv(
        fetch_path,
        [
            "ticker", "role", "source_pipeline", "status", "provider", "query_symbol", "source_symbol",
            "alias_applied", "alias_effective_date", "alias_issuer_id", "alias_reason",
            "rows", "first", "last",
        ],
        sorted(fetch_rows, key=lambda r: r["ticker"]),
    )
    write_csv(
        split_events_path,
        ["ticker", "query_symbol", "source_symbol", "provider", "split_date", "numerator", "denominator",
         "split_ratio"],
        sorted(split_rows, key=lambda r: (r["ticker"], r["split_date"])),
    )
    provider_counts = pd.Series([
        r["provider"] for r in fetch_rows if r["status"] == "ok" and r["rows"] > 0
    ]).value_counts().to_dict()

    snapshot = {
        "provider": str(cfg_get(config, "risk_panel.price_provider", "yahoo_adjusted")),
        "adjustment_policy": str(cfg_get(config, "risk_panel.adjustment_policy", "yahoo_adjclose_div_split")),
        "fetch_timestamp": fetched_at,
        "run_as_of": run_as_of,
        "master_calendar_ticker": master_ticker,
        "master_calendar_retry_attempts": master_retry_attempts,
        "master_calendar_retry_sleep_sec": master_retry_sleep_sec,
        "covariance_frequency": frequency,
        "lookback_trading_days": lookback,
        "start_date": start.isoformat(),
        "end_date": run_as_of,
        "calendar_days": len(calendar),
        "universe_size": len(panel_tickers),
        "fetched_ok": sum(1 for r in fetch_rows if r["status"] == "ok" and r["rows"] > 0),
        "fetch_failed": sorted(r["ticker"] for r in fetch_rows if not (r["status"] == "ok" and r["rows"] > 0)),
        "provider_counts": provider_counts,
        "ticker_aliases_applied": [
            {
                "ticker": str(r["ticker"]),
                "query_symbol": str(r["query_symbol"]),
                "source_symbol": str(r["source_symbol"]),
                "effective_date": str(r["alias_effective_date"]),
                "issuer_id": str(r["alias_issuer_id"]),
                "reason": str(r["alias_reason"]),
            }
            for r in sorted(fetch_rows, key=lambda row: row["ticker"])
            if int(r.get("alias_applied") or 0) == 1
        ],
        "price_history_overrides": price_history_overrides,
        "fallbacks_enabled": {"stooq_us_daily": enable_stooq},
        # Stooq closes are dividend-unadjusted; the panel-level policy below does not hold for these
        # names. Recorded per ticker so consumers (covariance review, Stage 11 calibration) can see
        # exactly which series carry a different adjustment basis.
        "adjustment_policy_exceptions": {
            "stooq_us_daily_close_unadjusted_dividends": sorted(
                str(r["ticker"]) for r in fetch_rows
                if r["status"] == "ok" and r["rows"] > 0 and "stooq" in str(r["provider"])
            ),
        },
        "files": {
            "prices_adjclose.csv": {"sha256": sha256_file(prices_path), "rows": len(prices)},
            "returns_panel.csv": {"sha256": sha256_file(returns_path), "rows": len(returns)},
            "fetch_results.csv": {"sha256": sha256_file(fetch_path), "rows": len(fetch_rows)},
            "split_events.csv": {"sha256": sha256_file(split_events_path), "rows": len(split_rows)},
        },
    }
    write_manifest(snapshot_path, snapshot)

    with connect(db_path) as conn:
        run_id = start_run(conn, run_type="build_return_panel", input_path=run_dir / "stocks_scores.csv")
        finish_run(conn, run_id=run_id, status="success", row_count=len(panel_tickers),
                   message=(f"as_of={run_as_of} universe={len(panel_tickers)} "
                            f"ok={snapshot['fetched_ok']} cal_days={len(calendar)}"))

    LOGGER.info("Panel built: %d tickers, %d calendar days, %d fetched ok, %d failed -> %s",
                len(panel_tickers), len(calendar), snapshot["fetched_ok"], len(snapshot["fetch_failed"]), risk_dir)
    if snapshot["fetch_failed"]:
        LOGGER.warning("Failed fetches (routed to coverage): %s", snapshot["fetch_failed"][:20])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
