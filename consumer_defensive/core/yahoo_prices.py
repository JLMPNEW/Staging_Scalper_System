from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from consumer_defensive.core.config import expand_env_vars
from consumer_defensive.core.atomic_io import atomic_write_text
from consumer_defensive.core.db import require_lastrowid, utc_now
from consumer_defensive.core.tickers import validate_investable_ticker
from consumer_defensive.core.market_data import (
    CorporateAction,
    MarketDataPolicy,
    PriceBar,
    YAHOO_SOURCE_ID,
    current_tickers,
    safe_float,
    upsert_corporate_actions,
    upsert_price_bars,
)


@dataclass(frozen=True)
class YahooResult:
    ticker: str
    symbol: str
    endpoint: str
    query: dict[str, Any]
    status_code: int
    payload: str
    bars: tuple[PriceBar, ...]
    actions: tuple[CorporateAction, ...]
    error: str
    cache_status: str


def yahoo_symbol(ticker: str) -> str:
    return validate_investable_ticker(
        ticker, context='Yahoo security ticker'
    ).replace('.', '-')


def _yahoo_cache_path(
    policy: MarketDataPolicy, ticker: str, start: date, end: date,
) -> Path:
    canonical = validate_investable_ticker(
        ticker, context='Yahoo cache ticker'
    )
    root = policy.resolve('yahoo.cache_dir').resolve()
    root.mkdir(parents=True, exist_ok=True)
    component = canonical.replace('.', '_')
    lexical = root / (
        f'ticker_{component}_{start.isoformat()}_{end.isoformat()}.json'
    )
    resolved = lexical.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError('Yahoo cache path escapes its configured directory') from exc
    if lexical.exists() and resolved != lexical:
        raise ValueError('Yahoo cache entry is a symlink or redirected path')
    return lexical


def _legacy_yahoo_cache_path(
    policy: MarketDataPolicy, ticker: str, start: date, end: date,
) -> Path:
    """Resolve the pre-hardening cache name without weakening containment."""
    canonical = validate_investable_ticker(
        ticker, context='legacy Yahoo cache ticker'
    )
    root = policy.resolve('yahoo.cache_dir').resolve()
    root.mkdir(parents=True, exist_ok=True)
    component = canonical.replace('.', '_')
    lexical = root / (
        f'{component}_{start.isoformat()}_{end.isoformat()}.json'
    )
    resolved = lexical.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(
            'Legacy Yahoo cache path escapes its configured directory'
        ) from exc
    if lexical.exists() and resolved != lexical:
        raise ValueError('Legacy Yahoo cache entry is a symlink or redirected path')
    return lexical


def _epoch(day: date) -> int:
    return int(datetime.combine(day, datetime_time.min, tzinfo=timezone.utc).timestamp())


def _date_from_epoch(raw: Any) -> str:
    try:
        return datetime.fromtimestamp(int(raw), tz=timezone.utc).date().isoformat()
    except (TypeError, ValueError, OSError):
        return ""


def parse_yahoo_payload(
    ticker: str,
    symbol: str,
    payload: str,
    *,
    start: date | None = None,
    end: date | None = None,
) -> tuple[tuple[PriceBar, ...], tuple[CorporateAction, ...], str]:
    try:
        document = json.loads(payload)
        chart = document.get("chart") or {}
        error = chart.get("error")
        if error:
            return (), (), f"yahoo_chart_error:{error}"
        results = chart.get("result") or []
        if not results:
            return (), (), "yahoo_empty_result"
        result = results[0]
        timestamps = result.get("timestamp") or []
        indicators = result.get("indicators") or {}
        quote_rows = indicators.get("quote") or []
        quotes = quote_rows[0] if quote_rows else {}
        adj_rows = indicators.get("adjclose") or []
        adjusted = (adj_rows[0].get("adjclose") or []) if adj_rows else []
        meta = result.get("meta") or {}
        provider_symbol = yahoo_symbol(str(meta.get('symbol') or ''))
        if provider_symbol != yahoo_symbol(symbol):
            return (), (), (
                f'yahoo_symbol_mismatch:requested={symbol}:received={provider_symbol or "<blank>"}'
            )
        source_timestamp = str(meta.get("regularMarketTime") or "")
    except (TypeError, ValueError, KeyError) as exc:
        return (), (), f"yahoo_payload_parse_error:{type(exc).__name__}:{exc}"

    timestamp_dates = [_date_from_epoch(value) for value in timestamps]
    if any(not value for value in timestamp_dates):
        return (), (), 'yahoo_invalid_timestamp'
    if timestamp_dates != sorted(set(timestamp_dates)):
        return (), (), 'yahoo_timestamps_not_strictly_increasing_unique'
    if start is not None and end is not None and any(
        value < start.isoformat() or value > end.isoformat() for value in timestamp_dates
    ):
        return (), (), 'yahoo_bar_outside_requested_window'

    events = result.get("events") or {}
    dividends_by_date: dict[str, float] = {}
    splits_by_date: dict[str, float] = {}
    actions: list[CorporateAction] = []
    currency = str(meta.get("currency") or "")
    for raw in (events.get("dividends") or {}).values():
        action_date = _date_from_epoch(raw.get("date"))
        amount = safe_float(raw.get("amount"))
        if not action_date:
            continue
        if amount is not None:
            dividends_by_date[action_date] = amount
        actions.append(
            CorporateAction(
                ticker=ticker,
                action_date=action_date,
                source_id=YAHOO_SOURCE_ID,
                action_type="dividend",
                action_value=amount,
                action_currency=currency,
                details={"provider_symbol": symbol, "raw": raw},
            )
        )
    for raw in (events.get("splits") or {}).values():
        action_date = _date_from_epoch(raw.get("date"))
        numerator = safe_float(raw.get("numerator"))
        denominator = safe_float(raw.get("denominator"))
        factor = numerator / denominator if numerator is not None and denominator not in (None, 0) else None
        if not action_date:
            continue
        if factor is not None:
            splits_by_date[action_date] = factor
        actions.append(
            CorporateAction(
                ticker=ticker,
                action_date=action_date,
                source_id=YAHOO_SOURCE_ID,
                action_type="split",
                action_value=factor,
                action_currency="",
                details={"provider_symbol": symbol, "raw": raw},
            )
        )

    def item(values: Any, position: int) -> float | None:
        if not isinstance(values, list) or position >= len(values):
            return None
        return safe_float(values[position])

    bars: list[PriceBar] = []
    for position, timestamp in enumerate(timestamps):
        bar_date = _date_from_epoch(timestamp)
        adj = item(adjusted, position)
        close = item(quotes.get("close"), position)
        if not bar_date or adj is None or adj <= 0 or close is None or close <= 0:
            continue
        bars.append(
            PriceBar(
                ticker=ticker,
                bar_date=bar_date,
                source_id=YAHOO_SOURCE_ID,
                open=item(quotes.get("open"), position),
                high=item(quotes.get("high"), position),
                low=item(quotes.get("low"), position),
                close=close,
                adjusted_close=adj,
                volume=item(quotes.get("volume"), position),
                dividend=dividends_by_date.get(bar_date),
                split_factor=splits_by_date.get(bar_date),
                total_return_basis="yahoo_adjusted_close",
                source_timestamp=source_timestamp,
            )
        )
    if start is not None and end is not None:
        outside_actions = [
            action for action in actions
            if action.action_date < start.isoformat()
            or action.action_date > end.isoformat()
        ]
        boundary_distances = [
            (start - date.fromisoformat(action.action_date)).days
            if action.action_date < start.isoformat()
            else (date.fromisoformat(action.action_date) - end).days
            for action in outside_actions
        ]
        if len(outside_actions) > 2 or any(
            distance > 7 for distance in boundary_distances
        ):
            return (), (), 'yahoo_action_outside_requested_window'
        actions = [
            action for action in actions
            if start.isoformat() <= action.action_date <= end.isoformat()
        ]
    return tuple(bars), tuple(actions), "" if bars else "yahoo_no_usable_bars"


def http_fetch(
    endpoint: str,
    query: dict[str, Any],
    *,
    user_agent: str,
    timeout_sec: float,
    max_retries: int,
) -> tuple[int, str]:
    url = endpoint + "?" + urllib.parse.urlencode(query)
    last_status = 0
    last_payload = ""
    for attempt in range(max_retries):
        request = urllib.request.Request(
            url,
            headers={"User-Agent": user_agent, "Accept": "application/json,text/plain,*/*"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_sec) as response:
                return int(response.status), response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            last_status = int(exc.code)
            last_payload = exc.read().decode("utf-8", errors="replace")
            if last_status not in {429, 500, 502, 503, 504}:
                break
        except (urllib.error.URLError, TimeoutError) as exc:
            last_status = 0
            last_payload = f"{type(exc).__name__}: {exc}"
        if attempt + 1 < max_retries:
            time.sleep(0.5 * (attempt + 1))
    return last_status, last_payload


def fetch_yahoo_job(
    ticker: str,
    *,
    policy: MarketDataPolicy,
    start: date,
    end: date,
    force_refresh: bool,
    fetcher: Callable[..., tuple[int, str]] | None = None,
) -> YahooResult:
    symbol = yahoo_symbol(ticker)
    settings = policy.payload["yahoo"]
    endpoint = str(settings["chart_url_template"]).format(
        ticker=urllib.parse.quote(symbol, safe='')
    )
    query = {
        "period1": _epoch(start),
        "period2": _epoch(end + timedelta(days=1)),
        "interval": str(settings["interval"]),
        "events": str(settings["events"]),
        "includeAdjustedClose": "true",
    }
    cache_path = _yahoo_cache_path(policy, ticker, start, end)
    legacy_cache_path = _legacy_yahoo_cache_path(policy, ticker, start, end)
    cache_only = os.environ.get(
        'CONSUMER_DEFENSIVE_CACHE_ONLY', ''
    ).strip().casefold() in {'1', 'true', 'yes', 'on'}
    selected_cache_path = cache_path
    selected_cache_status = 'cache'
    if not cache_path.exists() and legacy_cache_path.exists():
        selected_cache_path = legacy_cache_path
        selected_cache_status = 'legacy_cache'
    if selected_cache_path.exists() and not force_refresh:
        cached_payload = selected_cache_path.read_text(
            encoding='utf-8', errors='replace'
        )
        cached_bars, cached_actions, cached_error = parse_yahoo_payload(
            ticker,
            symbol,
            cached_payload,
            start=start,
            end=end,
        )
        if not cached_error or cache_only:
            return YahooResult(
                ticker,
                symbol,
                endpoint,
                query,
                200,
                cached_payload,
                cached_bars,
                cached_actions,
                cached_error,
                selected_cache_status,
            )
        cache_status = 'live_repair'
    else:
        cache_status = 'live'
        if cache_only:
            reason = 'force-refresh requested' if force_refresh else 'cache entry missing'
            raise FileNotFoundError(
                f'Consumer Defensive cache-only replay: {reason}: {cache_path}'
            )
    callback = fetcher or http_fetch
    status, payload = callback(
        endpoint,
        query,
        user_agent=expand_env_vars(settings['user_agent']),
        timeout_sec=float(settings['timeout_sec']),
        max_retries=int(settings['max_retries']),
    )
    bars, actions, error = parse_yahoo_payload(
        ticker,
        symbol,
        payload,
        start=start,
        end=end,
    )
    if status != 200 and not error:
        error = f"yahoo_http_status_{status}"
    if status == 200 and not error:
        atomic_write_text(cache_path, payload, encoding='utf-8')
    return YahooResult(ticker, symbol, endpoint, query, status, payload, bars, actions, error, cache_status)


def load_yahoo_prices(
    conn: Any,
    policy: MarketDataPolicy,
    *,
    start: str,
    end: str,
    tickers: list[str] | None = None,
    force_refresh: bool = False,
    fetcher: Callable[..., tuple[int, str]] | None = None,
) -> dict[str, Any]:
    start_date, end_date = date.fromisoformat(start), date.fromisoformat(end)
    if start_date > end_date:
        raise ValueError(f"Invalid Yahoo date window: start {start} is after end {end}.")
    universe = tickers or current_tickers(conn)
    universe = sorted({
        validate_investable_ticker(value, context='Yahoo ingestion ticker')
        for value in [*universe, 'XLP', 'SPY']
    })
    now = utc_now()
    cursor = conn.execute(
        "INSERT INTO ingestion_runs(source_id, started_at, status, created_at) VALUES (?, ?, 'running', ?)",
        (YAHOO_SOURCE_ID, now, now),
    )
    ingestion_run_id = require_lastrowid(cursor, context="create Yahoo price ingestion run")
    results: list[YahooResult] = []
    worker_failures: list[dict[str, str]] = []
    workers = int(policy.payload["yahoo"]["parallel_workers"])
    try:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
            futures = {
                executor.submit(
                    fetch_yahoo_job,
                    ticker,
                    policy=policy,
                    start=start_date,
                    end=end_date,
                    force_refresh=force_refresh,
                    fetcher=fetcher,
                ): ticker
                for ticker in universe
            }
            for future in as_completed(futures):
                ticker = futures[future]
                try:
                    results.append(future.result())
                except Exception as exc:
                    worker_failures.append(
                        {"ticker": ticker, "error": f"yahoo_worker_error:{type(exc).__name__}:{exc}"}
                    )
    except BaseException as exc:
        with conn:
            conn.execute(
                """UPDATE ingestion_runs SET completed_at=?, status='failed', request_count=?,
                          row_count=0, message=? WHERE ingestion_run_id=?""",
                (
                    utc_now(),
                    len(universe),
                    json.dumps({"fatal_error": f"{type(exc).__name__}: {exc}"}, sort_keys=True),
                    ingestion_run_id,
                ),
            )
        raise
    bars_written = 0
    actions_written = 0
    failures: list[dict[str, str]] = list(worker_failures)
    with conn:
        for result in results:
            response_hash = hashlib.sha256(result.payload.encode("utf-8", errors="replace")).hexdigest()
            conn.execute(
                """
                INSERT INTO raw_api_responses(
                    source_id, endpoint, query_params_json, request_time_utc,
                    response_status, response_hash, asof_date, payload_text,
                    ingestion_run_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    YAHOO_SOURCE_ID,
                    result.endpoint,
                    json.dumps(result.query, sort_keys=True),
                    now,
                    result.status_code,
                    response_hash,
                    end,
                    result.payload,
                    ingestion_run_id,
                    now,
                ),
            )
            if result.error:
                failures.append({"ticker": result.ticker, "error": result.error})
                continue
            conn.execute(
                "DELETE FROM fact_price_ohlcv WHERE ticker=? AND source_id=? AND bar_date BETWEEN ? AND ?",
                (result.ticker, YAHOO_SOURCE_ID, start, end),
            )
            conn.execute(
                '''DELETE FROM fact_corporate_action
                   WHERE ticker=? AND source_id=? AND action_date BETWEEN ? AND ?''',
                (result.ticker, YAHOO_SOURCE_ID, start, end),
            )
            bars_written += upsert_price_bars(conn, result.bars)
            actions_written += upsert_corporate_actions(conn, result.actions)
        conn.execute(
            """
            UPDATE ingestion_runs SET completed_at=?, status=?, request_count=?,
                row_count=?, message=? WHERE ingestion_run_id=?
            """,
            (
                utc_now(),
                "success" if not failures else "partial",
                len(universe),
                bars_written,
                json.dumps({"failures": failures}, sort_keys=True),
                ingestion_run_id,
            ),
        )
    manifest_entries = sorted(
        (
            {
                "ticker": result.ticker,
                "symbol": result.symbol,
                "bytes": len(result.payload.encode("utf-8", errors="replace")),
                "sha256": hashlib.sha256(result.payload.encode("utf-8", errors="replace")).hexdigest(),
                "cache_status": result.cache_status,
                "status_code": result.status_code,
            }
            for result in results
        ),
        key=lambda row: str(row["ticker"]),
    )
    manifest_payload = json.dumps(manifest_entries, sort_keys=True, separators=(",", ":"))
    return {
        "source_id": YAHOO_SOURCE_ID,
        "tickers_requested": len(universe),
        "tickers_loaded": len(universe) - len(failures),
        "bars_written": bars_written,
        "actions_written": actions_written,
        "payload_manifest": {
            "files": len(manifest_entries),
            "bytes": sum(int(row["bytes"]) for row in manifest_entries),
            "sha256": hashlib.sha256(manifest_payload.encode()).hexdigest(),
            "entries": manifest_entries,
        },
        "failures": failures,
    }
