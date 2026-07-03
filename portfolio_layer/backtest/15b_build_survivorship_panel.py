#!/usr/bin/env python3
"""Stage 11 - survivorship-complete research price panel (NOT the Stage 2 live panel).

Universe = every ticker that appears in ANY archived PIT snapshot (research/65 store) plus the
benchmark / hedge / sector ETFs. Prices come from Yahoo (primary, threaded + cached) merged with each
sector's PUBLISHED delisted-price exports (docs/delisted_price_export_contract.md) — never a sector-DB
read. Every equity is classified with an explicit `survivorship_complete` flag so Stage 11 labels can
mark, rather than silently inherit, survivor bias:

  active_covered    last bar reaches the panel right edge                     -> complete=1
  delisted_covered  ends early AND a delisting event/hint matches the end     -> complete=1
  ended_uncovered   ends early with no matching delisting evidence            -> complete=0
  no_price_data     nothing fetched and no export rows                        -> complete=0

Writes a sealed per-build-date panel under output/survivorship_panel/<build_date>/.
"""
from __future__ import annotations

import argparse
import glob as globmod
import json
import logging
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_layer.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from portfolio_layer.core.contracts import fail_if_exists, read_csv, sha256_file, write_csv, write_manifest  # noqa: E402
from portfolio_layer.core.db import utc_now  # noqa: E402
from portfolio_layer.core.logging_utils import configure_utc_logging  # noqa: E402
from portfolio_layer.core.paths import resolve_runtime_paths  # noqa: E402
from portfolio_layer.risk.panel import assemble_prices, coverage_stats, to_returns  # noqa: E402
from portfolio_layer.risk.yahoo import fetch_adjclose_with_splits  # noqa: E402


LOGGER = logging.getLogger("build_survivorship_panel")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

COVERAGE_FIELDS = [
    "ticker", "role", "source_pipeline", "first_snapshot", "last_snapshot", "snapshot_count",
    "observation_count", "first_bar", "last_bar", "tail_gap_days", "status", "survivorship_complete",
    "price_sources", "export_rows_used", "overlap_disagreements", "source_seam_status",
    "delist_date", "delist_source",
]
EVENT_FIELDS = ["ticker", "delist_date", "delist_reason", "terminal_value", "source"]
FETCH_FIELDS = ["ticker", "status", "provider", "source_symbol", "rows", "first", "last", "cache_hit"]


def iso_date_arg(raw: str) -> str:
    try:
        date.fromisoformat(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"must be YYYY-MM-DD, got {raw!r}") from exc
    return raw


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build the Stage 11 survivorship-complete price panel.")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--build-date", type=iso_date_arg, default=None, help="Panel build/right-edge date (default today).")
    p.add_argument("--universe-only", action="store_true", help="Report the universe and coverage plan; no fetching.")
    p.add_argument("--max-workers", type=int, default=0, help="Override risk_panel.fetch.max_workers.")
    p.add_argument(
        "--allow-intraday-partial",
        action="store_true",
        help="Dangerous/debug only: allow today's panel before the post-close safety window.",
    )
    p.add_argument("--force", action="store_true")
    return p.parse_args()


def _panel_end_is_final(panel_end: date, *, now: datetime | None = None) -> tuple[bool, str]:
    ny = ZoneInfo("America/New_York")
    now_et = (now or datetime.now(tz=ZoneInfo("UTC"))).astimezone(ny)
    today_et = now_et.date()
    if panel_end > today_et:
        return False, f"panel_end {panel_end} is after current New York date {today_et}"
    if panel_end < today_et:
        return True, "historical date"
    safe_after = time(17, 30)
    if now_et.time() >= safe_after:
        return True, f"same-day after {safe_after.strftime('%H:%M')} ET"
    return False, (
        f"same-day panel {panel_end} before {safe_after.strftime('%H:%M')} ET "
        f"(now {now_et.strftime('%Y-%m-%d %H:%M:%S %Z')})"
    )


# ---------------------------------------------------------------------------
# universe from the PIT snapshot store
# ---------------------------------------------------------------------------
def snapshot_universe(store_dir: Path) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Ticker -> {pipeline, first/last snapshot, count} from every archived snapshot."""
    universe: dict[str, dict[str, Any]] = {}
    snap_dates: list[str] = []
    if not store_dir.exists():
        return universe, snap_dates
    for child in sorted(store_dir.iterdir()):
        if not (child.is_dir() and DATE_RE.match(child.name)):
            continue
        scores = child / "stocks_scores.csv"
        if not scores.exists():
            continue
        snap_dates.append(child.name)
        for r in read_csv(scores):
            ticker = str(r.get("ticker", "")).strip().upper()
            if not ticker:
                continue
            item = universe.setdefault(ticker, {"pipelines": {}, "first": child.name, "last": child.name, "count": 0})
            pipe = str(r.get("source_pipeline", "")).strip()
            item["pipelines"][pipe] = item["pipelines"].get(pipe, 0) + 1
            item["last"] = child.name
            item["count"] += 1
    for item in universe.values():
        item["pipeline"] = max(item["pipelines"], key=lambda k: item["pipelines"][k]) if item["pipelines"] else ""
    return universe, snap_dates


def market_instruments(config: dict[str, Any]) -> list[str]:
    rc = cfg_get(config, "risk_panel", {}) or {}
    out = {str(rc.get("master_calendar_ticker", "SPY")).upper()}
    out.update(str(x).upper() for x in rc.get("benchmark_tickers", []) or [])
    out.update(str(x).upper() for x in rc.get("hedge_rotation_etfs", []) or [])
    out.update(str(v).upper() for v in (rc.get("sector_etf_map", {}) or {}).values())
    return sorted(t for t in out if t)


# ---------------------------------------------------------------------------
# delisted exports + events/hints
# ---------------------------------------------------------------------------
def _glob_files(patterns: list[str], base_dir: Path) -> list[Path]:
    files: list[Path] = []
    for pattern in patterns or []:
        root = resolve_path(pattern, base_dir=base_dir)
        files.extend(Path(hit) for hit in sorted(globmod.glob(str(root))))
    return [f for f in files if f.is_file()]


def load_export_prices(files: list[Path]) -> dict[str, dict[str, float]]:
    """ticker -> {date: adjclose} from published delisted-price exports."""
    out: dict[str, dict[str, float]] = {}
    for path in files:
        for r in read_csv(path):
            ticker = str(r.get("ticker", "")).strip().upper()
            day = str(r.get("date", "")).strip()
            try:
                adj = float(str(r.get("adjclose", "")).strip())
            except (TypeError, ValueError):
                continue
            if ticker and DATE_RE.match(day) and adj > 0:
                out.setdefault(ticker, {})[day] = adj
    return out


def load_delisting_events(event_files: list[Path], hint_files: list[Path], *, panel_end: str,
                          stale_tail_days: int) -> dict[str, dict[str, str]]:
    """ticker -> event. Contract events take precedence over Norgate import-report hints."""
    events: dict[str, dict[str, str]] = {}
    for path in hint_files:
        for r in read_csv(path):
            ticker = str(r.get("ticker", "")).strip().upper()
            last_bar = str(r.get("last_bar_date", "")).strip()
            if not ticker or str(r.get("status", "")).strip().lower() != "loaded" or not DATE_RE.match(last_bar):
                continue
            panel_end_day = date.fromisoformat(panel_end)
            last_bar_day = date.fromisoformat(last_bar)
            if last_bar_day >= panel_end_day:
                continue  # still trading through the panel edge; not a delisting hint
            if (panel_end_day - last_bar_day).days <= stale_tail_days:
                continue  # too close to panel edge to distinguish a live stale import from a delisting
            events[ticker] = {"ticker": ticker, "delist_date": last_bar, "delist_reason": "norgate_import_hint",
                              "terminal_value": "", "source": path.name}
    for path in event_files:
        for r in read_csv(path):
            ticker = str(r.get("ticker", "")).strip().upper()
            delist = str(r.get("delist_date", "")).strip()
            if not ticker or not DATE_RE.match(delist):
                continue
            events[ticker] = {"ticker": ticker, "delist_date": delist,
                              "delist_reason": str(r.get("delist_reason", "")).strip(),
                              "terminal_value": str(r.get("terminal_value", "")).strip(), "source": path.name}
    return events


# ---------------------------------------------------------------------------
# fetch with cache
# ---------------------------------------------------------------------------
def _cache_path(cache_dir: Path, ticker: str) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "_" for ch in ticker.upper())
    return cache_dir / f"{safe}.json"


def load_cached(cache_dir: Path, ticker: str, *, start: date, end: date,
                delist_date: str | None) -> tuple[list[tuple[str, float]], str, str] | None:
    path = _cache_path(cache_dir, ticker)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    raw_bars = payload.get("bars", [])
    dates = [
        str(r.get("date", ""))
        for r in raw_bars
        if isinstance(r, dict) and DATE_RE.match(str(r.get("date", "")))
    ]
    if not dates:
        return None
    actual_first = min(dates)
    actual_last = max(dates)
    fetched_from = str(payload.get("fetched_from") or min(dates))
    fetched_through = str(payload.get("fetched_through") or actual_last)
    # Metadata can be stale or optimistic after a cache-format change. Require the actual cached
    # bars to cover the requested left edge so a widened development window cannot silently reuse a
    # shorter historical cache.
    if fetched_from > start.isoformat() or actual_first > start.isoformat():
        return None
    today_et = datetime.now(tz=ZoneInfo("UTC")).astimezone(ZoneInfo("America/New_York")).date()
    if end >= today_et and payload.get("right_edge_final") is not True:
        return None
    # A cache is current when it was fetched through the panel edge — or, for a name with a known
    # delisting comfortably inside the cached window, through delist+30d (its series can never grow).
    current = fetched_through >= end.isoformat()
    if not current and delist_date:
        horizon = (date.fromisoformat(delist_date) + timedelta(days=30)).isoformat()
        current = fetched_through >= horizon
    if not current:
        return None
    bars = [(str(r["date"]), float(r["adjclose"])) for r in raw_bars
            if start.isoformat() <= str(r.get("date", "")) <= end.isoformat()]
    if not bars:
        return None
    return bars, str(payload.get("provider", "cache")), str(payload.get("source_symbol", ticker))


def write_cache(cache_dir: Path, ticker: str, *, bars: list[tuple[str, float]], provider: str,
                source_symbol: str, fetched_through: date) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    first_bar = min((d for d, _ in bars), default=fetched_through.isoformat())
    edge_final, edge_detail = _panel_end_is_final(fetched_through)
    write_manifest(_cache_path(cache_dir, ticker), {
        "ticker": ticker, "provider": provider, "source_symbol": source_symbol,
        "cached_at": utc_now(), "fetched_from": first_bar, "fetched_through": fetched_through.isoformat(),
        "right_edge_final": edge_final, "right_edge_final_detail": edge_detail,
        "bars": [{"date": d, "adjclose": v} for d, v in bars],
    })


def main() -> int:  # noqa: C901
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    paths = resolve_runtime_paths(config, config_path)
    sp = cfg_get(config, "survivorship_panel", {}) or {}
    rc_fetch = cfg_get(config, "risk_panel.fetch", {}) or {}

    build_date = args.build_date or date.today().isoformat()
    panel_end = build_date
    store_dir = paths.output_dir / str(cfg_get(config, "snapshot_store.dir", "snapshot_store"))
    dev_start = str(cfg_get(config, "stage11_lockbox.dev_window_start", "2024-01-02"))
    buffer_days = int(sp.get("history_buffer_calendar_days", 40))
    stale_tail = int(sp.get("stale_tail_trading_days", 5))
    delist_match = int(sp.get("delist_match_trading_days", 10))
    overlap_warn_rel = float(sp.get("overlap_disagreement_warn_rel", 0.02))
    min_complete_warn = float(sp.get("min_complete_fraction_warn", 0.95))

    universe, snap_dates = snapshot_universe(store_dir)
    if not universe:
        LOGGER.error("PIT snapshot store is empty (%s); run research/65 first", store_dir)
        return 1
    instruments = market_instruments(config)
    start = date.fromisoformat(min(dev_start, min(snap_dates))) - timedelta(days=buffer_days)
    end = date.fromisoformat(panel_end)
    right_edge_final, right_edge_detail = _panel_end_is_final(end)
    if not right_edge_final and not args.allow_intraday_partial:
        LOGGER.error(
            "Refusing to build a survivorship panel on a non-final daily close: %s. "
            "Pass --build-date for the last completed session, or rerun after the post-close buffer.",
            right_edge_detail,
        )
        return 1
    if args.allow_intraday_partial and not right_edge_final:
        LOGGER.warning("Building with an explicitly allowed non-final right edge: %s", right_edge_detail)
    master_ticker = str(cfg_get(config, "risk_panel.master_calendar_ticker", "SPY")).upper()

    export_files = _glob_files(list(sp.get("delisted_price_export_globs", []) or []), config_path.parent)
    event_files = _glob_files(list(sp.get("delisting_events_globs", []) or []), config_path.parent)
    hint_files = _glob_files(list(sp.get("delisting_hint_report_globs", []) or []), config_path.parent)
    export_prices = load_export_prices(export_files)
    events = load_delisting_events(event_files, hint_files, panel_end=panel_end, stale_tail_days=stale_tail)

    # Corporate-action ticker aliases (same config Stage 2 uses): fetch the active market-data symbol
    # for a migrated contract ticker and stitch its lineage price-history CSV in as export bars.
    alias_query: dict[str, str] = {}
    for raw_ticker, alias_cfg in (cfg_get(config, "risk_panel.ticker_aliases", {}) or {}).items():
        contract_ticker = str(raw_ticker).strip().upper()
        alias_cfg = alias_cfg or {}
        active = str(alias_cfg.get("active_ticker") or "").strip().upper()
        predecessor = str(alias_cfg.get("predecessor_ticker") or "").strip().upper()
        if active and contract_ticker != active:
            alias_query[contract_ticker] = active
        history_rel = str(alias_cfg.get("price_history_csv") or "").strip()
        if not history_rel:
            continue
        history_path = resolve_path(history_rel, base_dir=config_path.parent)
        if not history_path.exists():
            continue
        allowed = {contract_ticker, active, predecessor} - {""}
        merged = export_prices.setdefault(contract_ticker, {})
        for r in read_csv(history_path):
            row_ticker = str(r.get("ticker", "")).strip().upper()
            day = str(r.get("date") or r.get("bar_date") or "").strip()
            try:
                adj = float(str(r.get("adjclose") or r.get("adj_close") or "").strip())
            except (TypeError, ValueError):
                continue
            if row_ticker in allowed and DATE_RE.match(day) and adj > 0:
                merged.setdefault(day, adj)

    equities = sorted(universe)
    all_tickers = sorted(set(equities) | set(instruments))
    LOGGER.info(
        "Universe: %d snapshot equities (+%d market instruments) from %d snapshots [%s..%s]; "
        "window %s..%s; exports=%d files (%d tickers), events=%d (%d from hints)",
        len(equities), len(instruments), len(snap_dates), snap_dates[0], snap_dates[-1],
        start, end, len(export_files), len(export_prices), len(events),
        sum(1 for e in events.values() if e["delist_reason"] == "norgate_import_hint"),
    )
    if args.universe_only:
        by_pipe: dict[str, int] = {}
        for t in equities:
            by_pipe[universe[t]["pipeline"]] = by_pipe.get(universe[t]["pipeline"], 0) + 1
        LOGGER.info("Equities by pipeline: %s", dict(sorted(by_pipe.items())))
        LOGGER.info("Tickers with delisting events/hints: %d; with export prices: %d",
                    len(set(events) & set(equities)), len(set(export_prices) & set(equities)))
        return 0

    out_dir = paths.output_dir / str(sp.get("dir", "survivorship_panel")) / build_date
    artifacts = {name: out_dir / name for name in (
        "prices_adjclose.csv", "returns_daily.csv", "ticker_coverage.csv",
        "delisting_events.csv", "fetch_results.csv", "survivorship_manifest.json",
    )}
    if args.force:
        for p in artifacts.values():
            if p.exists():
                p.unlink()
    try:
        fail_if_exists(list(artifacts.values()), force=args.force)
    except FileExistsError as exc:
        LOGGER.error("%s", exc)
        return 1

    url_templates = [str(x) for x in rc_fetch.get("chart_url_templates", []) or
                     ["https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"]]
    ua = str(rc_fetch.get("user_agent", "portfolio_layer/0.1"))
    timeout = float(rc_fetch.get("request_timeout_sec", 20))
    retries = int(rc_fetch.get("max_retries", 3))
    workers = args.max_workers or int(rc_fetch.get("max_workers", 10))
    enable_stooq = bool(rc_fetch.get("enable_stooq_fallback", True))
    cache_dir = paths.cache_dir / "survivorship_prices"

    def fetch_one(ticker: str) -> tuple[str, list[tuple[str, float]], str, str, str, bool]:
        delist = events.get(ticker, {}).get("delist_date")
        cached = load_cached(cache_dir, ticker, start=start, end=end, delist_date=delist)
        if cached:
            bars, provider, source_symbol = cached
            return ticker, bars, "ok", f"cache:{provider}", source_symbol, True
        query_symbol = alias_query.get(ticker, ticker)
        bars, _splits, status, provider, source_symbol = fetch_adjclose_with_splits(
            query_symbol, start=start, end=end, url_templates=url_templates, user_agent=ua,
            timeout_sec=timeout, max_retries=retries, enable_stooq_fallback=enable_stooq,
        )
        bars = [(d, v) for d, v in bars if d <= end.isoformat()]
        if status == "ok" and bars:
            write_cache(cache_dir, ticker, bars=bars, provider=provider,
                        source_symbol=source_symbol, fetched_through=end)
        return ticker, bars, status, provider, source_symbol, False

    series: dict[str, dict[str, float]] = {}
    fetch_rows: list[dict[str, Any]] = []
    overlap_flags: dict[str, int] = {}
    source_seams: dict[str, str] = {}
    export_used: dict[str, int] = {}
    LOGGER.info("Fetching %d tickers (%d workers, cache %s)", len(all_tickers), workers, cache_dir)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(fetch_one, t): t for t in all_tickers}
        for fut in as_completed(futures):
            ticker, bars, status, provider, source_symbol, cache_hit = fut.result()
            yahoo = dict(bars)
            merged = dict(yahoo)
            export_for_ticker = {
                day: value for day, value in (export_prices.get(ticker) or {}).items()
                if day <= end.isoformat()
            }
            overlap_days = set(yahoo) & set(export_for_ticker)
            disagreements = 0
            filled = 0
            for day, value in export_for_ticker.items():
                if day in merged:
                    base = merged[day]
                    if base > 0 and abs(value - base) / base > overlap_warn_rel:
                        disagreements += 1
                else:
                    merged[day] = value
                    filled += 1
            if merged:
                series[ticker] = merged
            if disagreements:
                overlap_flags[ticker] = disagreements
            if filled:
                export_used[ticker] = filled
            if yahoo and export_for_ticker:
                if disagreements:
                    source_seams[ticker] = "overlap_disagreement"
                elif filled and not overlap_days:
                    source_seams[ticker] = "unverified_no_overlap"
                elif filled:
                    source_seams[ticker] = "verified_overlap"
            days = sorted(merged)
            fetch_rows.append({
                "ticker": ticker, "status": status if merged else (status or "no_data"),
                "provider": provider + ("+export" if filled else ""), "source_symbol": source_symbol,
                "rows": len(merged), "first": days[0] if days else "", "last": days[-1] if days else "",
                "cache_hit": int(cache_hit),
            })

    if master_ticker not in series:
        LOGGER.error("Master calendar ticker %s failed to fetch; cannot align panel", master_ticker)
        return 1
    calendar = sorted(d for d in series[master_ticker] if start.isoformat() <= d <= panel_end)
    prices = assemble_prices({t: series[t] for t in all_tickers if t in series}, calendar)
    returns = to_returns(prices, "daily")

    # ---- per-ticker survivorship classification ----
    coverage_rows: list[dict[str, Any]] = []
    complete_by_pipe: dict[str, list[int]] = {}
    for ticker in all_tickers:
        role = "market_instrument" if ticker in instruments and ticker not in universe else "scored"
        info = universe.get(ticker, {})
        stats = coverage_stats(prices, ticker, panel_end)
        obs = int(stats["observation_count"])
        tail_gap = int(stats["right_edge_missing_day_count"])
        last_bar = str(stats["end_date"])[:10]
        event = events.get(ticker)
        seam_status = source_seams.get(ticker, "single_source")
        if obs == 0:
            status, complete = "no_price_data", 0
        elif seam_status in {"overlap_disagreement", "unverified_no_overlap"}:
            status, complete = "source_seam_unverified", 0
        elif event:
            delist = event["delist_date"]
            gap = abs((date.fromisoformat(last_bar) - date.fromisoformat(delist)).days) if last_bar else 999
            if gap <= delist_match * 2:  # calendar-day allowance for a trading-day threshold
                status, complete = "delisted_covered", 1
            else:
                status, complete = "ended_uncovered", 0
        elif tail_gap <= stale_tail:
            status, complete = "active_covered", 1
        else:
            status, complete = "ended_uncovered", 0
        pipe = str(info.get("pipeline", ""))
        if role == "scored":
            complete_by_pipe.setdefault(pipe, []).append(complete)
        coverage_rows.append({
            "ticker": ticker, "role": role, "source_pipeline": pipe,
            "first_snapshot": str(info.get("first", "")), "last_snapshot": str(info.get("last", "")),
            "snapshot_count": int(info.get("count", 0)), "observation_count": obs,
            "first_bar": str(stats["start_date"])[:10], "last_bar": last_bar, "tail_gap_days": tail_gap,
            "status": status, "survivorship_complete": complete,
            "price_sources": next((r["provider"] for r in fetch_rows if r["ticker"] == ticker), ""),
            "export_rows_used": export_used.get(ticker, 0),
            "overlap_disagreements": overlap_flags.get(ticker, 0),
            "source_seam_status": seam_status,
            "delist_date": (event or {}).get("delist_date", ""),
            "delist_source": (event or {}).get("source", ""),
        })

    # ---- gates ----
    checks: list[dict[str, str]] = []

    def rec(name: str, status: str, detail: str) -> None:
        checks.append({"check": name, "status": status, "detail": detail})

    future_days = [d for d in calendar if d > panel_end]
    dupes = len(calendar) - len(set(calendar))
    first_ok = bool(calendar and date.fromisoformat(calendar[0]) <= start + timedelta(days=7))
    last_ok = bool(calendar and date.fromisoformat(calendar[-1]) <= end and (end - date.fromisoformat(calendar[-1])).days <= 7)
    calendar_ok = bool(calendar and not future_days and calendar == sorted(calendar) and dupes == 0 and first_ok and last_ok)
    rec("calendar_requested_window_coverage", "PASS" if calendar_ok else "FAIL",
        f"rows={len(calendar)} first={calendar[0] if calendar else '-'} last={calendar[-1] if calendar else '-'} "
        f"requested={start.isoformat()}..{panel_end} future={len(future_days)} dupes={dupes}")
    bad_market = [t for t in instruments
                  if next((c for c in coverage_rows if c["ticker"] == t), {}).get("status") != "active_covered"]
    rec("market_instruments_full_coverage", "PASS" if not bad_market else "FAIL",
        "benchmarks/ETFs active through panel edge" if not bad_market else f"{bad_market[:8]}")
    expected_return_mask = prices.notna() & prices.shift(1).notna()
    fabricated = int(((returns.notna()) & (~expected_return_mask.reindex(returns.index))).to_numpy().sum())
    rec("returns_never_fabricated", "PASS" if fabricated == 0 else "FAIL",
        "returns require current and prior observed prices" if fabricated == 0 else f"{fabricated} fabricated cells")
    coverage_by_ticker = {str(c["ticker"]): c for c in coverage_rows}
    malformed_universe = [
        t for t in equities
        if t not in coverage_by_ticker or not coverage_by_ticker[t].get("status")
        or str(coverage_by_ticker[t].get("status")) not in {
            "active_covered", "delisted_covered", "ended_uncovered", "no_price_data", "source_seam_unverified"
        }
    ]
    no_price_count = sum(1 for t in equities if coverage_by_ticker.get(t, {}).get("status") == "no_price_data")
    rec("snapshot_universe_price_status", "PASS" if not malformed_universe else "FAIL",
        f"{len(equities)} snapshot tickers classified; no_price_data={no_price_count}"
        if not malformed_universe else f"malformed {malformed_universe[:8]}")
    frac_detail = []
    below = []
    for pipe, flags in sorted(complete_by_pipe.items()):
        frac = sum(flags) / len(flags) if flags else 1.0
        frac_detail.append(f"{pipe}={frac:.3f}({sum(flags)}/{len(flags)})")
        if frac < min_complete_warn:
            below.append(pipe)
    rec("survivorship_complete_fraction", "PASS" if not below else "WARN",
        "; ".join(frac_detail) + (f"; below {min_complete_warn}: {below} — publish delisted exports "
                                  f"(docs/delisted_price_export_contract.md)" if below else ""))
    rec("export_overlap_consistency", "PASS" if not overlap_flags else "WARN",
        "no Yahoo-vs-export adjclose disagreements" if not overlap_flags else
        f"{len(overlap_flags)} tickers disagree >{overlap_warn_rel:.0%}: {sorted(overlap_flags)[:8]}")
    bad_seams = {t: s for t, s in source_seams.items() if s in {"overlap_disagreement", "unverified_no_overlap"}}
    rec("source_seams_verified_or_quarantined", "PASS" if not bad_seams else "WARN",
        "all Yahoo/export seams verified by overlap or single-source"
        if not bad_seams else f"{len(bad_seams)} source seams quarantined from survivorship_complete: {sorted(bad_seams)[:8]}")
    rec("right_edge_final_close", "PASS" if right_edge_final else "FAIL",
        right_edge_detail if right_edge_final else
        f"{right_edge_detail}; non-final panels may be built for debugging but cannot pass acceptance")

    # ---- write artifacts + manifest ----
    out_dir.mkdir(parents=True, exist_ok=True)
    out_prices = prices.copy()
    out_prices.index = [d.date().isoformat() for d in out_prices.index]
    out_prices.to_csv(artifacts["prices_adjclose.csv"], lineterminator="\n")
    out_returns = returns.copy()
    out_returns.index = [d.date().isoformat() for d in out_returns.index]
    out_returns.to_csv(artifacts["returns_daily.csv"], lineterminator="\n")
    write_csv(artifacts["ticker_coverage.csv"], COVERAGE_FIELDS, coverage_rows)
    write_csv(artifacts["delisting_events.csv"], EVENT_FIELDS,
              sorted(events.values(), key=lambda e: str(e["ticker"])))
    write_csv(artifacts["fetch_results.csv"], FETCH_FIELDS, sorted(fetch_rows, key=lambda r: str(r["ticker"])))

    passed = all(c["status"] in {"PASS", "WARN"} for c in checks)
    status_counts: dict[str, int] = {}
    for c in coverage_rows:
        if c["role"] == "scored":
            status_counts[str(c["status"])] = status_counts.get(str(c["status"]), 0) + 1
    manifest = {
        "stage": "stage11_survivorship_panel",
        "build_date": build_date,
        "generated_at": utc_now(),
        "acceptance": "PASS" if passed else "FAIL",
        "right_edge_final": {"ok": right_edge_final, "detail": right_edge_detail,
                             "allow_intraday_partial": bool(args.allow_intraday_partial)},
        "window": {"start": start.isoformat(), "end": panel_end, "calendar_days": len(calendar)},
        "universe": {"snapshot_equities": len(equities), "market_instruments": len(instruments),
                     "snapshots": len(snap_dates), "snapshot_range": [snap_dates[0], snap_dates[-1]]},
        "coverage_status_counts": dict(sorted(status_counts.items())),
        "survivorship_complete_fraction_by_pipeline": {
            pipe: round(sum(flags) / len(flags), 4) for pipe, flags in sorted(complete_by_pipe.items()) if flags
        },
        "export_files": [str(p) for p in export_files],
        "event_files": [str(p) for p in event_files],
        "hint_files": [str(p) for p in hint_files],
        "checks": checks,
        "files": {name: {"sha256": sha256_file(path)} for name, path in artifacts.items()
                  if name != "survivorship_manifest.json" and path.exists()},
    }
    write_manifest(artifacts["survivorship_manifest.json"], manifest)

    for c in checks:
        LOGGER.info("[%s] %s -- %s", c["status"], c["check"], c["detail"])
    LOGGER.info("SURVIVORSHIP PANEL: %s (%d tickers x %d days) coverage=%s -> %s",
                "PASS" if passed else "FAIL", prices.shape[1], prices.shape[0],
                dict(sorted(status_counts.items())), out_dir)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
