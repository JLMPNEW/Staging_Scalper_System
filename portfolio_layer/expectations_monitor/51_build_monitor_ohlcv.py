#!/usr/bin/env python3
"""Build a sealed current adjusted-OHLCV panel for the expectations monitor."""

from __future__ import annotations

import argparse
import logging
import math
import sys
import tempfile
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_layer.core.config import cfg_get, load_yaml  # noqa: E402
from portfolio_layer.core.contracts import (  # noqa: E402
    fail_if_exists,
    sha256_file,
    write_csv,
    write_manifest,
)
from portfolio_layer.core.logging_utils import configure_utc_logging  # noqa: E402
from portfolio_layer.core.paths import ensure_not_prod_path, resolve_runtime_paths  # noqa: E402
from portfolio_layer.expectations_monitor.market_data_common import (  # noqa: E402
    BENCHMARK_OHLCV_FILENAME,
    COVERAGE_FIELDS,
    DISAGREEMENT_FIELDS,
    FETCH_RESULT_FIELDS,
    OBSERVATION_FIELDS,
    SELECTED_FIELDS,
    SELECTED_OHLCV_FILENAME,
    VALIDATION_FIELDS,
    effective_request_end,
    load_sealed_universe,
    market_artifact_dir,
    market_policy_errors,
    normalized_cache_path,
    provider_symbols,
    read_normalized_provider_cache,
    read_gzip_csv,
    row_digest,
    tier0_coverage_status,
    write_gzip_csv,
    write_normalized_provider_cache,
)
from portfolio_layer.risk.ohlcv_sources import (  # noqa: E402
    SOURCE_PRIORITY,
    arbitrate_observations,
    fetch_ib_adjusted_ohlcv,
    fetch_tiingo_adjusted_ohlcv,
    normalize_yahoo_rows,
)
from portfolio_layer.risk.yahoo import fetch_adjusted_ohlcv  # noqa: E402


LOGGER = logging.getLogger("build_monitor_ohlcv")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
ENTITLEMENTS_PATH = Path(__file__).with_name("provider_entitlements.yaml")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--as-of", type=date.fromisoformat)
    parser.add_argument("--universe-as-of", type=date.fromisoformat)
    parser.add_argument("--symbols", nargs="+")
    parser.add_argument("--skip-ib", action="store_true")
    parser.add_argument("--skip-tiingo", action="store_true")
    parser.add_argument("--tiingo-crosscheck", action="store_true")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--selftest", action="store_true")
    return parser.parse_args()


def _fetch_summary(
    *,
    ticker: str,
    tier: str,
    provider: str,
    source_symbol: str,
    status: str,
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    days = sorted(str(row["date"]) for row in rows)
    return {
        "ticker": ticker,
        "tier": tier,
        "provider": provider,
        "source_symbol": source_symbol,
        "status": status,
        "row_count": len(rows),
        "first_date": days[0] if days else "",
        "last_date": days[-1] if days else "",
    }


def _coverage(
    selected: list[dict[str, Any]],
    universe: list[dict[str, Any]],
    master_dates: list[str],
    *,
    maximum_missing_fraction: float,
) -> list[dict[str, Any]]:
    selected_by_ticker: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in selected:
        selected_by_ticker[str(row["ticker"])].append(row)
    output: list[dict[str, Any]] = []
    for member in sorted(universe, key=lambda row: str(row["ticker"])):
        ticker = str(member["ticker"])
        tier = str(member["tier"])
        rows = selected_by_ticker.get(ticker, [])
        observed = {str(row["date"]) for row in rows}
        first_observed = min(observed) if observed else master_dates[0]
        required = [day for day in master_dates if day >= first_observed]
        missing = [day for day in required if day not in observed]
        fraction = len(missing) / len(required) if required else 1.0
        latest_present = bool(master_dates and master_dates[-1] in observed)
        status = (
            "PASS"
            if latest_present and fraction <= maximum_missing_fraction
            else "WARN"
            if rows and latest_present
            else "FAIL"
        )
        sources = sorted(
            {str(row["source"]) for row in rows},
            key=lambda source: SOURCE_PRIORITY.index(source),
        )
        output.append(
            {
                "ticker": ticker,
                "tier": tier,
                "first_required_date": required[0] if required else "",
                "latest_required_date": master_dates[-1] if master_dates else "",
                "required_sessions": len(required),
                "observed_sessions": len(required) - len(missing),
                "missing_sessions": len(missing),
                "missing_fraction": round(fraction, 8),
                "latest_session_present": int(latest_present),
                "sources_observed": ";".join(sources),
                "status": status,
            }
        )
    return output


def _check(
    rows: list[dict[str, str]],
    check: str,
    status: str,
    detail: str,
) -> None:
    rows.append({"check": check, "status": status, "detail": detail})


def _validate_build(
    *,
    selected: list[dict[str, Any]],
    observations: list[dict[str, Any]],
    coverage: list[dict[str, Any]],
    disagreements: list[dict[str, Any]],
    universe: list[dict[str, Any]],
    master_dates: list[str],
    tier0_floor: float,
    tier0_hard_floor: float,
    tier1_warn: float,
) -> list[dict[str, str]]:
    checks: list[dict[str, str]] = []
    keys = [(str(row["ticker"]), str(row["date"])) for row in selected]
    source_keys = [
        (str(row["ticker"]), str(row["date"]), str(row["source"]))
        for row in observations
    ]
    _check(
        checks,
        "selected_rows_unique",
        "PASS" if len(keys) == len(set(keys)) else "FAIL",
        f"rows={len(keys)}; unique={len(set(keys))}",
    )
    _check(
        checks,
        "source_rows_unique",
        "PASS" if len(source_keys) == len(set(source_keys)) else "FAIL",
        f"rows={len(source_keys)}; unique={len(set(source_keys))}",
    )
    master_set = set(master_dates)
    out_of_calendar = sum(str(row["date"]) not in master_set for row in selected)
    _check(
        checks,
        "final_master_calendar_only",
        "PASS" if not out_of_calendar else "FAIL",
        f"out_of_calendar={out_of_calendar}; latest={master_dates[-1] if master_dates else ''}",
    )
    rank = {source: index for index, source in enumerate(SOURCE_PRIORITY)}
    by_key: dict[tuple[str, str], set[str]] = defaultdict(set)
    for row in observations:
        by_key[(str(row["ticker"]), str(row["date"]))].add(str(row["source"]))
    priority_errors = sum(
        str(row["source"])
        != min(by_key[(str(row["ticker"]), str(row["date"]))], key=rank.__getitem__)
        for row in selected
    )
    _check(
        checks,
        "fixed_source_priority",
        "PASS" if not priority_errors else "FAIL",
        f"priority_errors={priority_errors}; order={SOURCE_PRIORITY}",
    )
    invalid_shapes = sum(
        float(row["high"]) + 1e-10 < max(float(row["open"]), float(row["close"]))
        or float(row["low"]) - 1e-10 > min(float(row["open"]), float(row["close"]))
        or float(row["adj_high"]) + 1e-10
        < max(float(row["adj_open"]), float(row["adj_close"]))
        or float(row["adj_low"]) - 1e-10
        > min(float(row["adj_open"]), float(row["adj_close"]))
        or float(row["adjustment_factor"]) <= 0
        or not math.isclose(
            float(row["adj_close"]),
            float(row["close"]) * float(row["adjustment_factor"]),
            rel_tol=1e-6,
            abs_tol=1e-8,
        )
        or float(row["split_factor"]) <= 0
        or float(row["dividend_cash"]) < 0
        for row in selected
    )
    _check(
        checks,
        "ohlcv_and_adjustment_shape",
        "PASS" if not invalid_shapes else "FAIL",
        f"invalid_rows={invalid_shapes}",
    )
    tier_counts = {
        tier: [row for row in coverage if row["tier"] == tier]
        for tier in ("tier0", "tier1", "tier2")
    }
    tier0 = tier_counts["tier0"]
    tier0_latest = (
        sum(int(row["latest_session_present"]) for row in tier0) / len(tier0)
        if tier0
        else 1.0
    )
    _check(
        checks,
        "tier0_latest_coverage",
        tier0_coverage_status(
            tier0_latest, target=tier0_floor, hard_floor=tier0_hard_floor
        ),
        (
            f"coverage={tier0_latest:.4f}; target={tier0_floor:.4f}; "
            f"hard_floor={tier0_hard_floor:.4f}; names={len(tier0)}"
        ),
    )
    tier1 = tier_counts["tier1"]
    tier1_latest = (
        sum(int(row["latest_session_present"]) for row in tier1) / len(tier1)
        if tier1
        else 1.0
    )
    _check(
        checks,
        "tier1_latest_coverage",
        "PASS" if tier1_latest + 1e-12 >= tier1_warn else "WARN",
        f"coverage={tier1_latest:.4f}; warn_floor={tier1_warn:.4f}; names={len(tier1)}",
    )
    final_date = master_dates[-1] if master_dates else ""
    failed_conflicts = sum(
        row["status"] == "FAIL" and row["date"] == final_date
        for row in disagreements
    )
    historical_conflicts = sum(
        row["status"] == "FAIL" and row["date"] != final_date
        for row in disagreements
    )
    _check(
        checks,
        "latest_provider_disagreements_bounded",
        "PASS" if not failed_conflicts else "FAIL",
        f"latest_failed_conflicts={failed_conflicts}; latest={final_date}",
    )
    _check(
        checks,
        "historical_adjustment_disagreements_disclosed",
        "WARN" if historical_conflicts else "PASS",
        (
            f"historical_failed_threshold_rows={historical_conflicts}; "
            "retained for provider/corporate-action reconciliation"
        ),
    )
    observed_names = {str(row["ticker"]) for row in selected}
    expected_names = {str(row["ticker"]) for row in universe}
    missing_names = sorted(expected_names - observed_names)
    _check(
        checks,
        "universe_name_coverage",
        "PASS" if not missing_names else "WARN",
        f"observed={len(observed_names)}; expected={len(expected_names)}; missing={missing_names[:20]}",
    )
    return checks


def run_selftest() -> None:
    assert not market_policy_errors(
        {
            "expectations_monitor": {
                "market_data": {
                    "policy_version": "monitor_market_data_v1",
                    "source_priority": ["yahoo", "ibkr", "tiingo"],
                    "primary_eod_source": "yahoo",
                    "current_confirmation_source": "ibkr",
                    "recovery_source": "tiingo",
                    "average_conflicting_prices": False,
                    "retain_source_disagreements": True,
                    "ib_max_batch_size": 90,
                    "require_final_daily_bar": True,
                    "require_corporate_action_validation": True,
                }
            }
        }
    )
    assert tier0_coverage_status(60 / 62, target=0.98, hard_floor=0.90) == "WARN"
    assert tier0_coverage_status(55 / 62, target=0.98, hard_floor=0.90) == "FAIL"
    noon = datetime(2026, 7, 31, 12, tzinfo=timezone.utc)
    assert effective_request_end(
        date(2026, 7, 31),
        timezone_name="America/New_York",
        same_day_final_after="18:00",
        now=noon,
    ) == date(2026, 7, 30)
    observations = [
        {
            "date": "2026-07-30",
            "ticker": "TEST",
            "source": source,
            "open": 10.0,
            "high": 11.0,
            "low": 9.0,
            "close": close,
            "adj_open": 10.0,
            "adj_high": 11.0,
            "adj_low": 9.0,
            "adj_close": close,
            "adjustment_factor": 1.0,
        }
        for source, close in (("tiingo", 10.01), ("yahoo", 10.0), ("ibkr", 10.005))
    ]
    selected, disagreements = arbitrate_observations(observations)
    assert selected[0]["source"] == "yahoo"
    assert selected[0]["adj_close"] == 10.0
    assert disagreements[0]["source_count"] == 3
    with tempfile.TemporaryDirectory() as tmp:
        cache_path = normalized_cache_path(Path(tmp), provider="tiingo", ticker="TEST")
        cache_rows = [{"date": "2026-07-30", "ticker": "TEST", "source": "tiingo"}]
        write_normalized_provider_cache(
            cache_path,
            provider="tiingo",
            ticker="TEST",
            source_symbol="TEST",
            rows=cache_rows,
        )
        assert read_normalized_provider_cache(
            cache_path,
            provider="tiingo",
            ticker="TEST",
            source_symbol="TEST",
        ) == cache_rows
        cache_path.write_text("{}", encoding="utf-8")
        assert not read_normalized_provider_cache(
            cache_path,
            provider="tiingo",
            ticker="TEST",
            source_symbol="TEST",
        )
    print("monitor OHLCV builder selftest: PASS")


def main() -> int:
    args = parse_args()
    if args.selftest:
        run_selftest()
        return 0
    if args.as_of is None:
        raise ValueError("--as-of is required")
    configure_utc_logging()
    config_path = args.config.resolve()
    config = load_yaml(config_path)
    runtime_paths = resolve_runtime_paths(config, config_path)
    policy_errors = market_policy_errors(config)
    if policy_errors:
        raise ValueError(f"Invalid monitor market-data policy: {policy_errors}")
    market = cfg_get(config, "expectations_monitor.market_data", {})
    assert isinstance(market, dict)
    universe_as_of = (args.universe_as_of or args.as_of).isoformat()
    universe, universe_manifest, universe_sources, _ = load_sealed_universe(
        config,
        config_path,
        universe_as_of=universe_as_of,
    )
    if args.symbols:
        requested = {symbol.strip().upper() for symbol in args.symbols}
        known = {str(row["ticker"]) for row in universe}
        unknown = sorted(requested - known)
        if unknown:
            raise ValueError(f"Canary symbols are not in the sealed monitor universe: {unknown}")
        universe = [row for row in universe if str(row["ticker"]) in requested]
    if not universe:
        raise ValueError("Selected monitor universe is empty")
    requested_end = effective_request_end(
        args.as_of,
        timezone_name=str(market.get("market_timezone", "America/New_York")),
        same_day_final_after=str(market.get("same_day_final_after", "18:00")),
    )
    start = requested_end - timedelta(days=int(market.get("calendar_buffer_days", 760)))
    output_dir = ensure_not_prod_path(
        args.output_dir.resolve()
        if args.output_dir
        else market_artifact_dir(config, config_path, as_of=args.as_of.isoformat()),
        label="monitor market-data output",
    )
    selected_path = output_dir / SELECTED_OHLCV_FILENAME
    benchmark_path = output_dir / BENCHMARK_OHLCV_FILENAME
    observations_path = output_dir / "monitor_ohlcv_source_observations.csv.gz"
    coverage_path = output_dir / "monitor_ohlcv_coverage.csv"
    disagreement_path = output_dir / "monitor_ohlcv_disagreements.csv"
    fetch_path = output_dir / "monitor_ohlcv_fetch_results.csv"
    validation_path = output_dir / "monitor_ohlcv_validation.csv"
    manifest_path = output_dir / "monitor_ohlcv_manifest.json"
    artifacts = [
        selected_path,
        benchmark_path,
        observations_path,
        coverage_path,
        disagreement_path,
        fetch_path,
        validation_path,
        manifest_path,
    ]
    fail_if_exists(artifacts, force=args.force)

    fetch_cfg = cfg_get(config, "risk_panel.fetch", {}) or {}
    templates = [str(value) for value in fetch_cfg.get("chart_url_templates", [])]
    if not templates:
        templates = ["https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"]
    user_agent = str(fetch_cfg.get("user_agent", "portfolio_layer/0.1"))
    timeout = float(market.get("request_timeout_sec", 20))
    retries = int(market.get("max_retries", 2))
    symbols = {
        str(row["ticker"]): provider_symbols(
            config,
            str(row["ticker"]),
            as_of=args.as_of.isoformat(),
        )
        for row in universe
    }
    tiers = {str(row["ticker"]): str(row["tier"]) for row in universe}
    observations: list[dict[str, Any]] = []
    benchmark_observations: list[dict[str, Any]] = []
    fetch_results: list[dict[str, Any]] = []
    master_rows: list[dict[str, Any]] = []

    def fetch_yahoo(ticker: str, source_symbol: str) -> tuple[str, list[dict[str, Any]], str]:
        rows, status, _provider, _symbol = fetch_adjusted_ohlcv(
            source_symbol,
            start=start,
            end=requested_end,
            url_templates=templates,
            user_agent=user_agent,
            timeout_sec=timeout,
            max_retries=retries,
        )
        retrieved = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        normalized = normalize_yahoo_rows(
            ticker,
            source_symbol,
            rows,
            retrieved_at_utc=retrieved,
        )
        return ticker, normalized, status

    futures = {}
    with ThreadPoolExecutor(max_workers=int(market.get("yahoo_max_workers", 8))) as pool:
        master_future = None
        for ticker in sorted(symbols):
            future = pool.submit(fetch_yahoo, ticker, symbols[ticker]["yahoo"])
            futures[future] = ticker
            if ticker == "SPY" and symbols[ticker]["yahoo"] == "SPY":
                master_future = future
        if master_future is None:
            master_future = pool.submit(fetch_yahoo, "SPY", "SPY")
        for future in as_completed(set(futures) | {master_future}):
            is_master = future is master_future
            is_universe = future in futures
            ticker = futures[future] if is_universe else "SPY"
            try:
                _ticker, rows, status = future.result()
            except Exception as exc:  # noqa: BLE001 - audited provider boundary.
                rows = []
                status = f"exception:{type(exc).__name__}"
            if is_master:
                master_rows = rows
            if is_universe:
                observations.extend(rows)
                fetch_results.append(
                    _fetch_summary(
                        ticker=ticker,
                        tier=tiers[ticker],
                        provider="yahoo",
                        source_symbol=symbols[ticker]["yahoo"],
                        status=status,
                        rows=rows,
                    )
                )
                continue
            fetch_results.append(
                _fetch_summary(
                    ticker="SPY",
                    tier="master",
                    provider="yahoo",
                    source_symbol="SPY",
                    status=status,
                    rows=rows,
                )
            )
    if not master_rows:
        raise RuntimeError("Yahoo SPY master calendar fetch failed")
    master_dates = sorted(
        {str(row["date"]) for row in master_rows if str(row["date"]) <= requested_end.isoformat()}
    )
    lookback = int(market.get("lookback_trading_days", 504))
    master_dates = master_dates[-lookback:]
    if not master_dates:
        raise RuntimeError("Yahoo SPY master calendar is empty")
    master_set = set(master_dates)
    final_date = master_dates[-1]
    observations = [row for row in observations if str(row["date"]) in master_set]
    benchmark_tickers = sorted(
        {
            "SPY",
            *(
                str(value).strip().upper()
                for value in dict(
                    cfg_get(config, "risk_panel.sector_etf_map", {}) or {}
                ).values()
                if str(value).strip()
            ),
        }
    )
    benchmark_observations.extend(
        row for row in master_rows if str(row["date"]) in master_set
    )
    benchmark_names = [ticker for ticker in benchmark_tickers if ticker != "SPY"]
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(benchmark_names)))) as pool:
        benchmark_futures = {
            pool.submit(fetch_yahoo, ticker, ticker): ticker
            for ticker in benchmark_names
        }
        for future in as_completed(benchmark_futures):
            ticker = benchmark_futures[future]
            try:
                _ticker, rows, status = future.result()
            except Exception as exc:  # noqa: BLE001 - audited provider boundary.
                rows = []
                status = f"exception:{type(exc).__name__}"
            rows = [row for row in rows if str(row["date"]) in master_set]
            benchmark_observations.extend(rows)
            fetch_results.append(
                _fetch_summary(
                    ticker=ticker,
                    tier="benchmark",
                    provider="yahoo",
                    source_symbol=ticker,
                    status=status,
                    rows=rows,
                )
            )

    ib_cfg = market.get("ibkr", {})
    if not isinstance(ib_cfg, dict):
        raise ValueError("market_data.ibkr must be a mapping")
    ib_tiers = {str(value) for value in ib_cfg.get("confirmation_tiers", ["tier0"])}
    ib_names = [
        (ticker, symbols[ticker]["ibkr"])
        for ticker in sorted(symbols)
        if tiers[ticker] in ib_tiers
    ]
    if bool(ib_cfg.get("enabled", True)) and not args.skip_ib and ib_names:
        ib_start = max(
            date.fromisoformat(master_dates[0]),
            date.fromisoformat(final_date)
            - timedelta(days=int(ib_cfg.get("confirmation_lookback_calendar_days", 10))),
        )
        try:
            ib_rows, ib_statuses = fetch_ib_adjusted_ohlcv(
                ib_names,
                start=ib_start,
                end=date.fromisoformat(final_date),
                host=str(ib_cfg.get("host", "127.0.0.1")),
                port=int(ib_cfg.get("port", 7496)),
                client_id=int(ib_cfg.get("client_id", 53)),
                timeout_sec=float(ib_cfg.get("timeout_sec", 20)),
                batch_size=int(market.get("ib_max_batch_size", 90)),
                request_pause_sec=float(ib_cfg.get("request_pause_sec", 0.2)),
            )
        except Exception as exc:  # noqa: BLE001 - Yahoo remains primary if IB is offline.
            ib_rows = {ticker: [] for ticker, _symbol in ib_names}
            ib_statuses = {
                ticker: f"connection_error:{type(exc).__name__}" for ticker, _symbol in ib_names
            }
        for ticker, source_symbol in ib_names:
            rows = [row for row in ib_rows.get(ticker, []) if str(row["date"]) in master_set]
            observations.extend(rows)
            fetch_results.append(
                _fetch_summary(
                    ticker=ticker,
                    tier=tiers[ticker],
                    provider="ibkr",
                    source_symbol=source_symbol,
                    status=ib_statuses.get(ticker, "missing_status"),
                    rows=rows,
                )
            )
    else:
        for ticker, source_symbol in ib_names:
            fetch_results.append(
                _fetch_summary(
                    ticker=ticker,
                    tier=tiers[ticker],
                    provider="ibkr",
                    source_symbol=source_symbol,
                    status="disabled_or_skipped",
                    rows=[],
                )
            )

    tiingo_cfg = market.get("tiingo", {})
    if not isinstance(tiingo_cfg, dict):
        raise ValueError("market_data.tiingo must be a mapping")
    yahoo_days: dict[str, set[str]] = defaultdict(set)
    for row in observations:
        if row["source"] == "yahoo":
            yahoo_days[str(row["ticker"])].add(str(row["date"]))
    crosscheck_tiers = {str(value) for value in tiingo_cfg.get("crosscheck_tiers", [])}
    fallback_only = bool(tiingo_cfg.get("fallback_only", True))
    tiingo_names = [
        ticker
        for ticker in sorted(symbols)
        if args.tiingo_crosscheck
        or (not fallback_only and tiers[ticker] in crosscheck_tiers)
        or final_date not in yahoo_days[ticker]
    ]
    entitlements = load_yaml(ENTITLEMENTS_PATH)
    max_symbols = int(
        dict(
            cfg_get(
                entitlements,
                "probe.max_symbols_by_provider",
                {},
            )
            or {}
        ).get("tiingo", 0)
    )
    if max_symbols < 1:
        raise ValueError("Tiingo entitlement must allow at least one recovery symbol")
    request_pause = float(
        tiingo_cfg.get(
            "request_pause_sec",
            cfg_get(entitlements, "probe.request_pause_sec", 0.35),
        )
    )
    tier_rank = {"tier0": 0, "tier1": 1, "tier2": 2}
    tiingo_names = sorted(
        tiingo_names, key=lambda ticker: (tier_rank.get(tiers[ticker], 9), ticker)
    )
    if bool(tiingo_cfg.get("enabled", True)) and not args.skip_tiingo:
        network_requests = 0
        for ticker in tiingo_names:
            source_symbol = symbols[ticker]["tiingo"]
            cache_path = normalized_cache_path(
                runtime_paths.cache_dir,
                provider="tiingo",
                ticker=ticker,
            )
            cached = read_normalized_provider_cache(
                cache_path,
                provider="tiingo",
                ticker=ticker,
                source_symbol=source_symbol,
            )
            cached_by_date = {
                str(row["date"]): row
                for row in cached
                if str(row.get("date", "")) in master_set
            }
            if final_date in cached_by_date:
                rows = []
                status = "cache_fresh"
            elif network_requests >= max_symbols:
                rows = []
                status = "deferred_entitlement_symbol_cap"
            else:
                rows, status = fetch_tiingo_adjusted_ohlcv(
                    source_symbol,
                    start=date.fromisoformat(master_dates[0]),
                    end=date.fromisoformat(final_date),
                    timeout_sec=timeout,
                    max_retries=retries,
                    max_response_bytes=int(market.get("max_response_bytes", 10_000_000)),
                )
                network_requests += 1
                if request_pause > 0:
                    time.sleep(request_pause)
            fetched = [
                {**row, "ticker": ticker}
                for row in rows
                if str(row["date"]) in master_set
            ]
            normalized_by_date = dict(cached_by_date)
            normalized_by_date.update({str(row["date"]): row for row in fetched})
            normalized = [normalized_by_date[day] for day in sorted(normalized_by_date)]
            if fetched:
                write_normalized_provider_cache(
                    cache_path,
                    provider="tiingo",
                    ticker=ticker,
                    source_symbol=source_symbol,
                    rows=normalized,
                )
                status = "ok" if not cached_by_date else "ok_with_cache"
            elif normalized and status != "cache_fresh":
                status = f"{status}_using_cache"
            observations.extend(normalized)
            fetch_results.append(
                _fetch_summary(
                    ticker=ticker,
                    tier=tiers[ticker],
                    provider="tiingo",
                    source_symbol=source_symbol,
                    status=status,
                    rows=normalized,
                )
            )
    else:
        for ticker in tiingo_names:
            fetch_results.append(
                _fetch_summary(
                    ticker=ticker,
                    tier=tiers[ticker],
                    provider="tiingo",
                    source_symbol=symbols[ticker]["tiingo"],
                    status="disabled_or_skipped",
                    rows=[],
                )
            )

    unique: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in observations:
        key = (str(row["ticker"]), str(row["date"]), str(row["source"]))
        if key in unique:
            raise RuntimeError(f"Duplicate provider observation: {key}")
        unique[key] = row
    observations = [unique[key] for key in sorted(unique)]
    selected, disagreements = arbitrate_observations(
        observations,
        disagreement_warn_bps=float(market.get("disagreement_warn_bps", 25.0)),
        disagreement_fail_bps=float(market.get("disagreement_fail_bps", 100.0)),
    )
    for row in selected:
        row["session_final"] = 1
    benchmark_selected, _benchmark_disagreements = arbitrate_observations(
        benchmark_observations,
        disagreement_warn_bps=float(market.get("disagreement_warn_bps", 25.0)),
        disagreement_fail_bps=float(market.get("disagreement_fail_bps", 100.0)),
    )
    for row in benchmark_selected:
        row["session_final"] = 1
    coverage = _coverage(
        selected,
        universe,
        master_dates,
        maximum_missing_fraction=float(market.get("maximum_missing_fraction", 0.02)),
    )
    checks = _validate_build(
        selected=selected,
        observations=observations,
        coverage=coverage,
        disagreements=disagreements,
        universe=universe,
        master_dates=master_dates,
        tier0_floor=float(market.get("tier0_latest_coverage_floor", 0.98)),
        tier0_hard_floor=float(market.get("tier0_latest_coverage_hard_floor", 0.90)),
        tier1_warn=float(market.get("tier1_latest_coverage_warn", 0.90)),
    )
    benchmark_latest = {
        str(row["ticker"])
        for row in benchmark_selected
        if str(row["date"]) == final_date
    }
    missing_benchmarks = sorted(set(benchmark_tickers) - benchmark_latest)
    _check(
        checks,
        "benchmark_latest_session_complete",
        "PASS" if not missing_benchmarks else "FAIL",
        f"required={benchmark_tickers}; missing={missing_benchmarks}",
    )
    failures = [row for row in checks if row["status"] == "FAIL"]
    warnings = [row for row in checks if row["status"] == "WARN"]
    acceptance = "FAIL" if failures else "PASS_WITH_WARNINGS" if warnings else "PASS"
    write_gzip_csv(selected_path, SELECTED_FIELDS, selected)
    write_gzip_csv(benchmark_path, SELECTED_FIELDS, benchmark_selected)
    write_gzip_csv(observations_path, OBSERVATION_FIELDS, observations)
    write_csv(coverage_path, COVERAGE_FIELDS, coverage)
    write_csv(disagreement_path, DISAGREEMENT_FIELDS, disagreements)
    write_csv(fetch_path, FETCH_RESULT_FIELDS, sorted(fetch_results, key=lambda row: (row["ticker"], row["provider"])))
    write_csv(validation_path, VALIDATION_FIELDS, checks)
    generated = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    input_paths = [
        config_path,
        Path(__file__).resolve(),
        Path(__file__).with_name("market_data_common.py").resolve(),
        PACKAGE_ROOT / "risk" / "ohlcv_sources.py",
        PACKAGE_ROOT / "risk" / "yahoo.py",
        universe_manifest,
    ]
    write_manifest(
        manifest_path,
        {
            "schema_version": "monitor_ohlcv_manifest_v1",
            "acceptance": acceptance,
            "as_of_date": args.as_of.isoformat(),
            "universe_as_of": universe_as_of,
            "requested_end_date": requested_end.isoformat(),
            "final_market_date": final_date,
            "generated_at_utc": generated,
            "policy_version": market["policy_version"],
            "source_priority": list(SOURCE_PRIORITY),
            "tiingo_fallback_only": fallback_only,
            "tiingo_entitlement_symbol_cap": max_symbols,
            "tiingo_network_request_count": (
                network_requests
                if bool(tiingo_cfg.get("enabled", True)) and not args.skip_tiingo
                else 0
            ),
            "prices_averaged": False,
            "session_finality_required": True,
            "universe_count": len(universe),
            "master_session_count": len(master_dates),
            "selected_row_count": len(selected),
            "source_observation_count": len(observations),
            "benchmark_tickers": benchmark_tickers,
            "benchmark_row_count": len(benchmark_selected),
            "latest_deferred_tickers": sorted(
                str(row["ticker"])
                for row in coverage
                if not int(row["latest_session_present"])
            ),
            "selected_row_digest": row_digest(read_gzip_csv(selected_path)),
            "source_observation_digest": row_digest(read_gzip_csv(observations_path)),
            "benchmark_row_digest": row_digest(read_gzip_csv(benchmark_path)),
            "universe_sources": universe_sources,
            "shadow_only": True,
            "inputs_sha256": {str(path): sha256_file(path) for path in input_paths},
            "outputs_sha256": {
                path.name: sha256_file(path)
                for path in artifacts
                if path != manifest_path
            },
        },
    )
    print(f"MONITOR OHLCV: {acceptance}")
    print(
        f"names={len(universe)}; selected_rows={len(selected)}; "
        f"source_rows={len(observations)}; final_market_date={final_date}"
    )
    print(f"manifest={manifest_path}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
