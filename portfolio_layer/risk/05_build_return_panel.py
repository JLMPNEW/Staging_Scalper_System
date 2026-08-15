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
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd  # noqa: E402

from portfolio_layer.core.artifacts import invalidate_dependents  # noqa: E402
from portfolio_layer.core.config import cfg_get, load_yaml  # noqa: E402
from portfolio_layer.core.config import resolve_path  # noqa: E402
from portfolio_layer.core.contracts import (  # noqa: E402
    fail_if_exists,
    read_csv,
    sha256_file,
    write_csv,
    write_manifest,
    write_via_temp,
)
from portfolio_layer.core.db import connect, finish_run, start_run  # noqa: E402
from portfolio_layer.core.logging_utils import configure_utc_logging  # noqa: E402
from portfolio_layer.core.paths import resolve_database_path, resolve_runtime_paths  # noqa: E402
from portfolio_layer.risk.local_prices import (  # noqa: E402
    load_local_adjusted_price_fallbacks,
    prefer_strictly_more_complete_local_history,
)
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


def same_day_bar_finality(
    run_as_of: str,
    master_bars: list[tuple[str, float]],
    *,
    final_after_local: str,
    timezone_name: str,
    now: datetime | None = None,
) -> tuple[bool, str]:
    """Reject a current-session daily bar until the configured post-close finality time."""
    try:
        zone = ZoneInfo(timezone_name)
        cutoff_time = datetime.strptime(final_after_local, "%H:%M").time()
    except (ValueError, KeyError) as exc:
        raise ValueError(
            f"Invalid Stage 2 market finality config timezone={timezone_name!r} "
            f"same_day_bar_final_after_et={final_after_local!r}"
        ) from exc
    current = now or datetime.now(zone)
    if current.tzinfo is None:
        current = current.replace(tzinfo=zone)
    local_now = current.astimezone(zone)
    has_same_day_bar = any(str(bar_date)[:10] == run_as_of for bar_date, _ in master_bars)
    if date.fromisoformat(run_as_of) != local_now.date() or not has_same_day_bar:
        return True, f"historical_or_no_same_day_bar now={local_now.isoformat(timespec='seconds')}"
    is_final = local_now.time() >= cutoff_time
    return is_final, (
        f"same_day_bar={run_as_of} now={local_now.isoformat(timespec='seconds')} "
        f"final_after={final_after_local} {timezone_name}"
    )


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
    out = frame.copy()
    out.index = [d.date().isoformat() for d in out.index]
    write_via_temp(path, lambda temp: out.to_csv(temp, lineterminator="\n"))


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
        predecessor = (
            str(
                raw_cfg.get("predecessor_ticker")
                or raw_cfg.get("legacy_ticker")
                or raw_cfg.get("old_ticker")
                or (ticker if ticker != active else "")
            )
            .strip()
            .upper()
        )
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
            segments.append(
                {
                    "query_symbol": predecessor,
                    "start": start,
                    "end": predecessor_end,
                    "segment": "predecessor",
                }
            )
    active_start = max(start, effective)
    if active_start <= end:
        segments.append(
            {
                "query_symbol": active,
                "start": active_start,
                "end": end,
                "segment": "active",
            }
        )
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
    required_end = end
    while required_end.weekday() >= 5:
        required_end -= timedelta(days=1)
    if min(dates) > (start + timedelta(days=7)).isoformat() or max(dates) < required_end.isoformat():
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
    required_end = end
    while required_end.weekday() >= 5:
        required_end -= timedelta(days=1)
    required_end_s = required_end.isoformat()
    for ticker in frame.columns:
        series = frame[ticker].dropna()
        if series.empty or str(series.index[-1]) < required_end_s:
            continue
        seed[str(ticker)] = [(str(idx), float(value)) for idx, value in series.items() if str(idx) <= end_s]
    return seed


def existing_seed_is_adjusted(prices_path: Path, snapshot_path: Path) -> bool:
    """Only reuse a panel whose seal proves a uniform adjusted-price basis."""
    if not prices_path.exists() or not snapshot_path.exists():
        return False
    try:
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    expected = ((snapshot.get("files") or {}).get("prices_adjclose.csv") or {}).get("sha256")
    exceptions = snapshot.get("adjustment_policy_exceptions") or {}
    exception_names = [ticker for values in exceptions.values() if isinstance(values, list) for ticker in values]
    return bool(expected) and expected == sha256_file(prices_path) and not exception_names


def load_existing_split_seed(path: Path, *, end: date) -> dict[str, list[dict[str, str]]]:
    if not path.exists():
        return {}
    try:
        rows = read_csv(path)
    except Exception:  # noqa: BLE001 - stale/bad prior artifact should not block a fresh fetch
        return {}
    seed: dict[str, list[dict[str, str]]] = {}
    end_s = end.isoformat()
    for row in rows:
        ticker = str(row.get("ticker") or "").strip().upper()
        split_date = str(row.get("split_date") or "").strip()
        if not ticker or not split_date or split_date > end_s:
            continue
        query_symbol = str(row.get("query_symbol") or ticker).strip().upper()
        source_symbol = str(row.get("source_symbol") or query_symbol).strip().upper()
        provider = str(row.get("provider") or "existing_price_snapshot").strip()
        seed.setdefault(ticker, []).append(
            {
                "split_date": split_date,
                "numerator": str(row.get("numerator") or "").strip(),
                "denominator": str(row.get("denominator") or "").strip(),
                "split_ratio": str(row.get("split_ratio") or "").strip(),
                "_query_symbol": query_symbol,
                "_source_symbol": source_symbol,
                "_provider": provider,
            }
        )
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
    optional_pipelines: set[str] = set()
    for sector in cfg_get(config, "score_contract.sectors", []):
        if not bool(sector.get("enabled", True)):
            continue
        pipe = str(sector["model_family"])
        per_pipeline_tolerance[pipe] = int(sector.get("staleness_tolerance_days", tolerance))
        if bool(sector.get("required", True)):
            expected.append(pipe)
        else:
            optional_pipelines.add(pipe)  # shadow sectors never block the production sleeves
    stale_status = str(cfg_get(config, "risk_panel.readiness_stale_status", "FAIL"))
    readiness = check_stage1_readiness(
        runs_root,
        run_as_of,
        staleness_tolerance=tolerance,
        per_pipeline_staleness_tolerance=per_pipeline_tolerance,
        expected_pipelines=expected,
        optional_pipelines=optional_pipelines,
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
        invalidate_dependents(run_dir, "risk")
        # Liquidity samples/snapshots are produced by 05c/05d, not by this price-panel builder.
        # Keep those independently collected artifacts across a failed price refresh. Gate 08
        # verifies their sealed requested universe against the rebuilt risk universe and will
        # fail closed if they are stale. Deleting them here used to destroy valid IB evidence
        # before a replacement price panel had been fetched successfully.
        unlink_artifacts(
            [
                risk_dir / "risk_coverage.csv",
                risk_dir / "covariance.csv",
                risk_dir / "covariance_period.csv",
                risk_dir / "correlation_clusters.csv",
                risk_dir / "covariance_meta.json",
                risk_dir / "return_outliers.csv",
                risk_dir / "data_quality_review.csv",
                risk_dir / "validation" / "risk_panel_validation.csv",
                risk_dir / "risk_manifest.json",
            ]
        )
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
    local_price_seed, local_price_provenance, local_price_sources = load_local_adjusted_price_fallbacks(
        rc.get("local_adjusted_price_fallbacks"),
        base_dir=config_path.parent,
        universe=universe,
        start=start,
        end=run_date,
    )
    if local_price_seed:
        LOGGER.info(
            "Prepared %d local adjusted-price fallback histories",
            len(local_price_seed),
        )

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
    enable_stooq = bool(fetch_cfg.get("enable_stooq_fallback", False))
    price_cache_dir = paths.cache_dir / "risk_prices"
    seed_allowed = args.reuse_existing_panel and existing_seed_is_adjusted(prices_path, snapshot_path)
    if args.reuse_existing_panel and not seed_allowed:
        LOGGER.warning("Existing panel seed rejected: missing/invalid seal or non-adjusted provider exception")
    existing_seed = load_existing_price_seed(prices_path, end=run_date) if seed_allowed else {}
    existing_split_seed = load_existing_split_seed(split_events_path, end=run_date) if seed_allowed else {}
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
        price_history_overrides.append(
            {
                "ticker": ticker,
                "path": str(history_path),
                "exists": summary["exists"],
                "rows": summary["rows"],
                "first_date": summary["first_date"],
                "last_date": summary["last_date"],
                "sha256": sha256_file(history_path) if history_path.exists() else "",
            }
        )

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
            # Seeded bars preserve the prior split snapshot, but do not update the price cache; a
            # seeded run should not overwrite a provider-fetched cache entry.
            bars = existing_seed[ticker]
            return (
                bars,
                existing_split_seed.get(ticker, []),
                "ok",
                "existing_price_snapshot",
                ticker,
                ticker,
                False,
                "",
                "",
                "",
            )

        # Aliased (reused-ticker) names skip the cache so the start-date floor is always re-applied and
        # any pre-effective-date history cached by an earlier run is overwritten.
        cached = (
            None
            if alias_applied or not args.reuse_price_cache
            else load_cached_bars(
                price_cache_dir,
                ticker,
                start=start,
                end=run_date,
                expected_query_symbols=expected_query_symbols,
            )
        )
        if cached:
            bars, split_events, provider, query_symbol, source_symbol = cached
            if "stooq" in provider.lower():
                LOGGER.warning("Ignoring unadjusted Stooq cache for %s", ticker)
                cached = None
            else:
                local_bars = local_price_seed.get(ticker, [])
                if not alias_applied and prefer_strictly_more_complete_local_history(bars, local_bars):
                    local_provenance = local_price_provenance[ticker]
                    LOGGER.info(
                        "Using strictly more complete local adjusted history for %s "
                        "(%d rows vs cached %d rows)",
                        ticker,
                        len(local_bars),
                        len(bars),
                    )
                    return (
                        local_bars,
                        [
                            {
                                **row,
                                "_provider": f"cache:{provider}",
                                "_query_symbol": query_symbol,
                                "_source_symbol": source_symbol,
                            }
                            for row in split_events
                        ],
                        "ok",
                        local_provenance.provider,
                        ticker,
                        ticker,
                        alias_applied,
                        alias_effective_date,
                        alias_issuer_id,
                        alias_reason,
                    )
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

        local_bars = local_price_seed.get(ticker, [])
        if args.reuse_price_cache and local_bars and not alias_applied:
            required_end = run_date
            while required_end.weekday() >= 5:
                required_end -= timedelta(days=1)
            local_is_complete = (
                local_bars[0][0] <= (start + timedelta(days=7)).isoformat()
                and local_bars[-1][0] >= required_end.isoformat()
            )
            if local_is_complete:
                local_provenance = local_price_provenance[ticker]
                write_cached_bars(
                    price_cache_dir,
                    ticker,
                    local_provenance.provider,
                    local_bars,
                    [],
                    query_symbol=ticker,
                    source_symbol=ticker,
                )
                return (
                    local_bars,
                    [],
                    "ok",
                    local_provenance.provider,
                    ticker,
                    ticker,
                    False,
                    "",
                    "",
                    "",
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
                tail_notes.append(f"{query_symbol}:{status if bars or status != 'ok' else 'empty_after_lineage_tail'}")
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
        if not combined_bars and ticker in local_price_seed and not alias_applied:
            bars = local_price_seed[ticker]
            local_provenance = local_price_provenance[ticker]
            write_cached_bars(
                price_cache_dir,
                ticker,
                local_provenance.provider,
                bars,
                [],
                query_symbol=ticker,
                source_symbol=ticker,
            )
            return (
                bars,
                [],
                "ok",
                local_provenance.provider,
                ticker,
                ticker,
                alias_applied,
                alias_effective_date,
                alias_issuer_id,
                alias_reason,
            )
        if combined_bars and not statuses:
            bars = sorted(combined_bars.items())
            provider = providers[0] if len(set(providers)) == 1 else "lineage:" + "+".join(providers)
            if tail_notes:
                provider = f"{provider};tail_gap={'|'.join(tail_notes)}"
            query_symbol = query_symbols_used[0] if len(query_symbols_used) == 1 else "|".join(query_symbols_used)
            source_symbol = source_symbols_used[0] if len(source_symbols_used) == 1 else "|".join(source_symbols_used)
            local_bars = local_price_seed.get(ticker, [])
            if not alias_applied and prefer_strictly_more_complete_local_history(bars, local_bars):
                local_provenance = local_price_provenance[ticker]
                LOGGER.info(
                    "Using strictly more complete local adjusted history for %s (%d rows vs fetched %d rows)",
                    ticker,
                    len(local_bars),
                    len(bars),
                )
                bars = local_bars
                provider = local_provenance.provider
                query_symbol = ticker
                source_symbol = ticker

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
                    split_rows.append(
                        {
                            "ticker": t,
                            "query_symbol": row.get("_query_symbol", query_symbol),
                            "source_symbol": row.get("_source_symbol", source_symbol),
                            "provider": row.get("_provider", provider),
                            "split_date": row["split_date"],
                            "numerator": row.get("numerator", ""),
                            "denominator": row.get("denominator", ""),
                            "split_ratio": row.get("split_ratio", ""),
                        }
                    )
            fetch_rows.append(
                {
                    "ticker": t,
                    "status": status,
                    "provider": provider,
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
            )

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
                    split_rows.append(
                        {
                            "ticker": master_ticker,
                            "query_symbol": row.get("_query_symbol", query_symbol),
                            "source_symbol": row.get("_source_symbol", source_symbol),
                            "provider": row.get("_provider", provider),
                            "split_date": row["split_date"],
                            "numerator": row.get("numerator", ""),
                            "denominator": row.get("denominator", ""),
                            "split_ratio": row.get("split_ratio", ""),
                        }
                    )
                LOGGER.info("Master calendar retry succeeded for %s on attempt %d", master_ticker, attempt)
                break
            LOGGER.warning(
                "Master calendar retry %d/%d failed for %s: %s", attempt, master_retry_attempts, master_ticker, status
            )
        if retry_row is not None:
            fetch_rows = [row for row in fetch_rows if row["ticker"] != master_ticker]
            fetch_rows.append(retry_row)

    if master_ticker not in series_by_ticker:
        LOGGER.error("Master calendar ticker %s failed to fetch; cannot align panel", master_ticker)
        return 1

    final_after = str(cfg_get(config, "risk_panel.same_day_bar_final_after_et", "17:00"))
    market_timezone = str(cfg_get(config, "risk_panel.market_timezone", "America/New_York"))
    try:
        same_day_final, finality_detail = same_day_bar_finality(
            run_as_of,
            list(series_by_ticker[master_ticker].items()),
            final_after_local=final_after,
            timezone_name=market_timezone,
        )
    except ValueError as exc:
        LOGGER.error("%s", exc)
        return 1
    if not same_day_final:
        LOGGER.error(
            "Refusing to seal an intraday partial Yahoo daily bar: %s. Re-run after the finality cutoff.",
            finality_detail,
        )
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
            "ticker",
            "role",
            "source_pipeline",
            "status",
            "provider",
            "query_symbol",
            "source_symbol",
            "alias_applied",
            "alias_effective_date",
            "alias_issuer_id",
            "alias_reason",
            "rows",
            "first",
            "last",
        ],
        sorted(fetch_rows, key=lambda r: r["ticker"]),
    )
    write_csv(
        split_events_path,
        [
            "ticker",
            "query_symbol",
            "source_symbol",
            "provider",
            "split_date",
            "numerator",
            "denominator",
            "split_ratio",
        ],
        sorted(split_rows, key=lambda r: (r["ticker"], r["split_date"])),
    )
    provider_counts = (
        pd.Series([r["provider"] for r in fetch_rows if r["status"] == "ok" and r["rows"] > 0]).value_counts().to_dict()
    )

    snapshot = {
        "provider": str(cfg_get(config, "risk_panel.price_provider", "yahoo_adjusted")),
        "adjustment_policy": str(cfg_get(config, "risk_panel.adjustment_policy", "yahoo_adjclose_div_split")),
        "fetch_timestamp": fetched_at,
        "run_as_of": run_as_of,
        "master_calendar_ticker": master_ticker,
        "master_calendar_retry_attempts": master_retry_attempts,
        "master_calendar_retry_sleep_sec": master_retry_sleep_sec,
        "same_day_bar_finality": {
            "status": "PASS",
            "detail": finality_detail,
            "final_after_local": final_after,
            "timezone": market_timezone,
        },
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
        "local_adjusted_price_fallbacks": local_price_sources,
        "local_adjusted_price_fallback_tickers": [
            {
                "ticker": ticker,
                "provider": item.provider,
                "source_id": item.source_id,
                "database_path": item.database_path,
                "first_date": item.first_date,
                "last_date": item.last_date,
                "row_count": item.row_count,
                "extracted_sha256": item.extracted_sha256,
            }
            for ticker, item in sorted(local_price_provenance.items())
            if any(
                row["ticker"] == ticker and row["status"] == "ok" and str(row["provider"]).startswith("local_sqlite:")
                for row in fetch_rows
            )
        ],
        "fallbacks_enabled": {"stooq_us_daily": enable_stooq},
        # This must remain empty: dividend-unadjusted sources are never admitted to this panel.
        "adjustment_policy_exceptions": {
            "stooq_us_daily_close_unadjusted_dividends": sorted(
                str(r["ticker"])
                for r in fetch_rows
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
        finish_run(
            conn,
            run_id=run_id,
            status="success",
            row_count=len(panel_tickers),
            message=(
                f"as_of={run_as_of} universe={len(panel_tickers)} ok={snapshot['fetched_ok']} cal_days={len(calendar)}"
            ),
        )

    LOGGER.info(
        "Panel built: %d tickers, %d calendar days, %d fetched ok, %d failed -> %s",
        len(panel_tickers),
        len(calendar),
        snapshot["fetched_ok"],
        len(snapshot["fetch_failed"]),
        risk_dir,
    )
    if snapshot["fetch_failed"]:
        LOGGER.warning("Failed fetches (routed to coverage): %s", snapshot["fetch_failed"][:20])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
