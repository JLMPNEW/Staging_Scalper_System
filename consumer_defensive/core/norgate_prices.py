from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from typing import Any, Iterable

import pandas as pd

from consumer_defensive.core.db import require_lastrowid, utc_now
from consumer_defensive.core.market_data import (
    CorporateAction,
    MarketDataPolicy,
    NORGATE_SOURCE_ID,
    PriceBar,
    safe_float,
    security_rows,
    upsert_corporate_actions,
    upsert_price_bars,
)
from consumer_defensive.core.norgate_runtime import (
    NORGATE_EQUITY_DATABASES,
    NorgateSnapshotChanged,
    norgate_database_fingerprint,
    require_norgate_snapshot,
)


@dataclass(frozen=True)
class NorgateResult:
    ticker: str
    symbol: str
    listing_status: str
    requested_start: str
    requested_end: str
    bars: tuple[PriceBar, ...]
    actions: tuple[CorporateAction, ...]
    error: str = ""


def _iso_index(frame: Any) -> list[str]:
    if frame is None or len(frame) == 0:
        return []
    index = pd.DatetimeIndex(pd.to_datetime(frame.index))
    if index.tz is not None:
        index = index.tz_localize(None)
    return [value.date().isoformat() for value in index]


def _column(frame: pd.DataFrame, name: str) -> Any:
    lookup = {str(column).casefold(): column for column in frame.columns}
    column = lookup.get(name.casefold())
    return frame[column] if column is not None else None


def _value(series: Any, position: int) -> float | None:
    if series is None or position >= len(series):
        return None
    return safe_float(series.iloc[position])


def fetch_norgate_prices(
    provider: Any,
    *,
    ticker: str,
    symbol: str,
    listing_status: str,
    start: str,
    end: str,
) -> NorgateResult:
    try:
        raw = provider.price_timeseries(
            symbol,
            stock_price_adjustment_setting=provider.StockPriceAdjustmentType.NONE,
            start_date=start,
            end_date=end,
            timeseriesformat="pandas-dataframe",
        )
        adjusted = provider.price_timeseries(
            symbol,
            stock_price_adjustment_setting=provider.StockPriceAdjustmentType.TOTALRETURN,
            start_date=start,
            end_date=end,
            timeseriesformat="pandas-dataframe",
        )
    except Exception as exc:
        return NorgateResult(
            ticker, symbol, listing_status, start, end, (), (),
            f"norgate_fetch_error:{type(exc).__name__}:{exc}",
        )
    raw_dates = _iso_index(raw)
    adjusted_dates = _iso_index(adjusted)
    if not raw_dates:
        return NorgateResult(ticker, symbol, listing_status, start, end, (), (), "norgate_no_raw_bars")
    if raw_dates != sorted(set(raw_dates)):
        return NorgateResult(
            ticker,
            symbol,
            listing_status,
            start,
            end,
            (),
            (),
            'norgate_dates_not_strictly_increasing_unique',
        )
    if any(bar_date < start or bar_date > end for bar_date in raw_dates):
        return NorgateResult(
            ticker,
            symbol,
            listing_status,
            start,
            end,
            (),
            (),
            'norgate_bar_outside_requested_window',
        )
    if raw_dates != adjusted_dates:
        return NorgateResult(
            ticker,
            symbol,
            listing_status,
            start,
            end,
            (),
            (),
            f"norgate_raw_adjusted_date_mismatch:{len(raw_dates)}:{len(adjusted_dates)}",
        )

    raw_frame = raw.copy()
    adjusted_frame = adjusted.copy()
    raw_open = _column(raw_frame, "Open")
    raw_high = _column(raw_frame, "High")
    raw_low = _column(raw_frame, "Low")
    raw_close = _column(raw_frame, "Close")
    raw_volume = _column(raw_frame, "Volume")
    raw_dividend = _column(raw_frame, "Dividend")
    adjusted_close = _column(adjusted_frame, "Close")
    if raw_close is None or adjusted_close is None:
        return NorgateResult(ticker, symbol, listing_status, start, end, (), (), "norgate_close_column_missing")

    source_timestamp = utc_now()
    currency = "USD"
    bars: list[PriceBar] = []
    actions: list[CorporateAction] = []
    for position, bar_date in enumerate(raw_dates):
        close = _value(raw_close, position)
        total_return_close = _value(adjusted_close, position)
        if close is None or close <= 0 or total_return_close is None or total_return_close <= 0:
            continue
        dividend = _value(raw_dividend, position)
        bars.append(
            PriceBar(
                ticker=ticker,
                bar_date=bar_date,
                source_id=NORGATE_SOURCE_ID,
                open=_value(raw_open, position),
                high=_value(raw_high, position),
                low=_value(raw_low, position),
                close=close,
                adjusted_close=total_return_close,
                volume=_value(raw_volume, position),
                dividend=dividend,
                split_factor=None,
                total_return_basis="norgate_total_return",
                source_timestamp=source_timestamp,
            )
        )
        if dividend is not None and dividend > 0:
            actions.append(
                CorporateAction(
                    ticker=ticker,
                    action_date=bar_date,
                    source_id=NORGATE_SOURCE_ID,
                    action_type="dividend",
                    action_value=dividend,
                    action_currency=currency,
                    details={"provider_symbol": symbol},
                )
            )
    if not bars:
        return NorgateResult(ticker, symbol, listing_status, start, end, (), (), "norgate_no_usable_bars")
    return NorgateResult(ticker, symbol, listing_status, start, end, tuple(bars), tuple(actions))


def _requested_securities(
    conn: Any,
    policy: MarketDataPolicy,
    *,
    end: str,
    tickers: Iterable[str] | None,
) -> list[dict[str, Any]]:
    requested = {str(value).strip().upper() for value in tickers or [] if str(value).strip()}
    history_start = str(policy.payload["history_start"])
    load_active = bool(policy.payload["norgate"]["load_active_fallback"])
    load_delisted = bool(policy.payload["norgate"]["load_delisted"])
    rows: list[dict[str, Any]] = []
    for security in security_rows(conn):
        ticker = str(security["ticker"]).upper()
        if requested and ticker not in requested:
            continue
        is_active = bool(int(security.get("is_active") or 0))
        if (is_active and not load_active) or (not is_active and not load_delisted):
            continue
        symbol = str(security.get("provider_price_symbol") or "").strip()
        start = max(history_start, str(security.get("listing_start_date") or history_start))
        requested_end = min(end, str(security.get("listing_end_date") or end))
        rows.append({**security, "provider_price_symbol": symbol, "requested_start": start, "requested_end": requested_end})
    return rows


def load_norgate_prices(
    conn: Any,
    policy: MarketDataPolicy,
    *,
    provider: Any,
    end: str,
    tickers: Iterable[str] | None = None,
) -> dict[str, Any]:
    date.fromisoformat(end)
    provider_fingerprint_start = norgate_database_fingerprint(
        provider,
        NORGATE_EQUITY_DATABASES,
    )
    securities = _requested_securities(conn, policy, end=end, tickers=tickers)
    now = utc_now()
    cursor = conn.execute(
        "INSERT INTO ingestion_runs(source_id, started_at, status, created_at) VALUES (?, ?, 'running', ?)",
        (NORGATE_SOURCE_ID, now, now),
    )
    ingestion_run_id = require_lastrowid(cursor, context="create Norgate price ingestion run")
    results: list[NorgateResult] = []

    def fail_provider_change(exc: NorgateSnapshotChanged) -> None:
        message = {
            "error": "norgate_provider_changed_midrun",
            "changed_databases": list(exc.changed_databases),
            "provider_updated_at_start": provider_fingerprint_start,
            "provider_updated_at_end": exc.observed,
            "context": exc.context,
        }
        with conn:
            conn.execute(
                """UPDATE ingestion_runs SET completed_at=?,status='failed',request_count=?,
                          row_count=0,message=? WHERE ingestion_run_id=?""",
                (
                    utc_now(),
                    len(results) * 2,
                    json.dumps(message, sort_keys=True),
                    ingestion_run_id,
                ),
            )
        raise RuntimeError(
            str(exc)
        ) from exc

    def fence(context: str) -> dict[str, str]:
        try:
            return require_norgate_snapshot(
                provider,
                provider_fingerprint_start,
                context=context,
            )
        except NorgateSnapshotChanged as exc:
            fail_provider_change(exc)
            raise AssertionError("unreachable")

    for security in securities:
        ticker = str(security["ticker"])
        symbol = str(security["provider_price_symbol"])
        listing_status = str(security.get("listing_status") or "")
        start = str(security["requested_start"])
        requested_end = str(security["requested_end"])
        if not symbol:
            results.append(
                NorgateResult(ticker, symbol, listing_status, start, requested_end, (), (), "provider_price_symbol_missing")
            )
            continue
        if start > requested_end:
            results.append(
                NorgateResult(ticker, symbol, listing_status, start, requested_end, (), (), "listing_outside_requested_window")
            )
            continue
        results.append(
            fetch_norgate_prices(
                provider,
                ticker=ticker,
                symbol=symbol,
                listing_status=listing_status,
                start=start,
                end=requested_end,
            )
        )
        fence("during price extraction")

    provider_fingerprint_end = fence("before price publication")

    bars_written = 0
    actions_written = 0
    failures: list[dict[str, str]] = []
    delisted_failures: list[dict[str, str]] = []
    rows: list[dict[str, Any]] = []
    with conn:
        for result in results:
            row = {
                "ticker": result.ticker,
                "provider_symbol": result.symbol,
                "listing_status": result.listing_status,
                "requested_start": result.requested_start,
                "requested_end": result.requested_end,
                "first_bar_date": result.bars[0].bar_date if result.bars else "",
                "last_bar_date": result.bars[-1].bar_date if result.bars else "",
                "bars_written": len(result.bars),
                "status": "PASS" if not result.error else "FAIL",
                "error": result.error,
            }
            rows.append(row)
            if result.error:
                failure = {"ticker": result.ticker, "symbol": result.symbol, "error": result.error}
                failures.append(failure)
                if result.listing_status != "active":
                    delisted_failures.append(failure)
                continue
            conn.execute(
                "DELETE FROM fact_price_ohlcv WHERE ticker=? AND source_id=? AND bar_date BETWEEN ? AND ?",
                (result.ticker, NORGATE_SOURCE_ID, result.requested_start, result.requested_end),
            )
            conn.execute(
                '''DELETE FROM fact_corporate_action
                   WHERE ticker=? AND source_id=? AND action_date BETWEEN ? AND ?''',
                (
                    result.ticker,
                    NORGATE_SOURCE_ID,
                    result.requested_start,
                    result.requested_end,
                ),
            )
            bars_written += upsert_price_bars(conn, result.bars)
            actions_written += upsert_corporate_actions(conn, result.actions)
        status = "failed" if delisted_failures else ("partial" if failures else "success")
        conn.execute(
            """
            UPDATE ingestion_runs SET completed_at=?, status=?, request_count=?,
                row_count=?, message=? WHERE ingestion_run_id=?
            """,
            (
                utc_now(),
                status,
                len(results) * 2,
                bars_written,
                json.dumps({"failures": failures, "delisted_failures": delisted_failures}, sort_keys=True),
                ingestion_run_id,
            ),
        )
    return {
        "source_id": NORGATE_SOURCE_ID,
        "status": "FAIL" if delisted_failures else ("WARN" if failures else "PASS"),
        "provider_database_updated_at_start": provider_fingerprint_start,
        "provider_database_updated_at_end": provider_fingerprint_end,
        "tickers_requested": len(securities),
        "tickers_loaded": len(results) - len(failures),
        "bars_written": bars_written,
        "actions_written": actions_written,
        "failures": failures,
        "delisted_failures": delisted_failures,
        "rows": rows,
    }
