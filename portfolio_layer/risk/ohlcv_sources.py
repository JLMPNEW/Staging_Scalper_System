"""Provider adapters and deterministic arbitration for shared adjusted OHLCV.

The source contract is fixed: Yahoo first, read-only IBKR second, Tiingo third.
Conflicting observations are retained and measured; prices are never averaged.
"""

from __future__ import annotations

import math
import os
import time
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime, time as dt_time, timezone
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests


SOURCE_PRIORITY = ("yahoo", "ibkr", "tiingo")
ET = ZoneInfo("America/New_York")


def _number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _day(value: Any) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value or "").strip()
    return text[:10] if len(text) >= 10 else ""


def normalize_ohlcv_row(
    *,
    ticker: str,
    source: str,
    source_symbol: str,
    raw: Mapping[str, Any],
    retrieved_at_utc: str,
    adjustment_status: str,
) -> dict[str, Any] | None:
    """Normalize one provider row and reject invalid OHLC shapes."""
    day = _day(raw.get("date"))
    raw_open = _number(raw.get("open"))
    raw_high = _number(raw.get("high"))
    raw_low = _number(raw.get("low"))
    raw_close = _number(raw.get("close"))
    adj_open = _number(raw.get("adj_open", raw.get("adjOpen")))
    adj_high = _number(raw.get("adj_high", raw.get("adjHigh")))
    adj_low = _number(raw.get("adj_low", raw.get("adjLow")))
    adj_close = _number(raw.get("adj_close", raw.get("adjClose")))
    required = (
        raw_open,
        raw_high,
        raw_low,
        raw_close,
        adj_open,
        adj_high,
        adj_low,
        adj_close,
    )
    if not day or any(value is None or value <= 0 for value in required):
        return None
    assert raw_open is not None
    assert raw_high is not None
    assert raw_low is not None
    assert raw_close is not None
    assert adj_open is not None
    assert adj_high is not None
    assert adj_low is not None
    assert adj_close is not None
    if raw_high + 1e-10 < max(raw_open, raw_close):
        return None
    if raw_low - 1e-10 > min(raw_open, raw_close):
        return None
    if adj_high + 1e-10 < max(adj_open, adj_close):
        return None
    if adj_low - 1e-10 > min(adj_open, adj_close):
        return None
    implied_factor = adj_close / raw_close
    adjusted_pairs = (
        (raw_open, adj_open),
        (raw_high, adj_high),
        (raw_low, adj_low),
        (raw_close, adj_close),
    )
    if any(
        not math.isclose(
            adjusted_value,
            raw_value * implied_factor,
            rel_tol=1e-6,
            abs_tol=1e-8,
        )
        for raw_value, adjusted_value in adjusted_pairs
    ):
        return None
    raw_volume = max(0.0, _number(raw.get("raw_volume", raw.get("volume"))) or 0.0)
    adjusted_volume = _number(raw.get("adj_volume", raw.get("adjVolume")))
    volume = max(0.0, adjusted_volume if adjusted_volume is not None else raw_volume)
    factor = _number(raw.get("adjustment_factor"))
    if factor is None:
        factor = implied_factor
    elif not math.isclose(factor, implied_factor, rel_tol=1e-6, abs_tol=1e-8):
        return None
    split_factor = _number(raw.get("split_factor", raw.get("splitFactor")))
    dividend_cash = _number(raw.get("dividend_cash", raw.get("divCash")))
    return {
        "date": day,
        "ticker": ticker.strip().upper(),
        "source": source,
        "source_symbol": source_symbol.strip().upper(),
        "retrieved_at_utc": retrieved_at_utc,
        "open": raw_open,
        "high": raw_high,
        "low": raw_low,
        "close": raw_close,
        "adj_open": adj_open,
        "adj_high": adj_high,
        "adj_low": adj_low,
        "adj_close": adj_close,
        "raw_volume": raw_volume,
        "volume": volume,
        "adjustment_factor": factor if factor is not None and factor > 0 else 1.0,
        "split_factor": split_factor if split_factor is not None and split_factor > 0 else 1.0,
        "dividend_cash": max(0.0, dividend_cash or 0.0),
        "adjustment_status": adjustment_status,
    }


def normalize_yahoo_rows(
    ticker: str,
    source_symbol: str,
    rows: Iterable[Mapping[str, Any]],
    *,
    retrieved_at_utc: str,
) -> list[dict[str, Any]]:
    output = [
        normalize_ohlcv_row(
            ticker=ticker,
            source="yahoo",
            source_symbol=source_symbol,
            raw=row,
            retrieved_at_utc=retrieved_at_utc,
            adjustment_status="yahoo_adjclose_div_split",
        )
        for row in rows
    ]
    return [row for row in output if row is not None]


def normalize_tiingo_rows(
    ticker: str,
    source_symbol: str,
    rows: Iterable[Mapping[str, Any]],
    *,
    retrieved_at_utc: str,
) -> list[dict[str, Any]]:
    output = [
        normalize_ohlcv_row(
            ticker=ticker,
            source="tiingo",
            source_symbol=source_symbol,
            raw=row,
            retrieved_at_utc=retrieved_at_utc,
            adjustment_status="tiingo_provider_adjusted",
        )
        for row in rows
    ]
    return [row for row in output if row is not None]


def fetch_tiingo_adjusted_ohlcv(
    ticker: str,
    *,
    start: date,
    end: date,
    timeout_sec: float,
    max_retries: int,
    max_response_bytes: int,
) -> tuple[list[dict[str, Any]], str]:
    """Fetch normalized Tiingo EOD rows without persisting a raw response."""
    key = os.environ.get("TIINGO_API_KEY", "").strip()
    if not key:
        return [], "key_missing"
    symbol = ticker.strip().upper()
    url = f"https://api.tiingo.com/tiingo/daily/{quote(symbol, safe='')}/prices"
    headers = {
        "Accept": "application/json",
        "Authorization": f"Token {key}",
        "User-Agent": "staging-portfolio-market-data/1.0",
    }
    params = {
        "startDate": start.isoformat(),
        "endDate": end.isoformat(),
        "resampleFreq": "daily",
    }
    last_status = "unknown"
    for attempt in range(max(0, max_retries) + 1):
        try:
            response = requests.get(
                url,
                headers=headers,
                params=params,
                timeout=timeout_sec,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            last_status = f"request_error:{type(exc).__name__}"
        else:
            if len(response.content) > max_response_bytes:
                return [], "response_too_large"
            if response.status_code in {401, 402, 403}:
                return [], f"unauthorized_or_plan:{response.status_code}"
            if response.status_code == 429:
                last_status = "rate_limited"
            elif response.status_code < 200 or response.status_code >= 300:
                last_status = f"http_{response.status_code}"
            else:
                try:
                    payload = response.json()
                except ValueError:
                    return [], "non_json"
                if not isinstance(payload, list):
                    return [], "schema_mismatch"
                retrieved = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
                rows = normalize_tiingo_rows(
                    symbol,
                    symbol,
                    (row for row in payload if isinstance(row, dict)),
                    retrieved_at_utc=retrieved,
                )
                return rows, "ok" if rows else "empty"
        if attempt < max_retries:
            time.sleep(0.5 * (attempt + 1))
    return [], last_status


def _ib_day(value: Any) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value or "").strip()
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return text[:10]


def fetch_ib_adjusted_ohlcv(
    tickers: Sequence[tuple[str, str]],
    *,
    start: date,
    end: date,
    host: str,
    port: int,
    client_id: int,
    timeout_sec: float,
    batch_size: int,
    request_pause_sec: float,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, str]]:
    """Read adjusted daily bars from IBKR sequentially in bounded batches."""
    if batch_size < 1 or batch_size >= 100:
        raise ValueError("IB OHLCV batch_size must be between 1 and 99")
    try:
        from ib_insync import IB, Stock  # type: ignore
    except ImportError as exc:
        raise RuntimeError("ib_insync is required for IB OHLCV confirmation") from exc
    years = max(1, math.ceil(((end - start).days + 1) / 365.25))
    duration = f"{years} Y"
    end_dt = datetime.combine(end, dt_time(23, 59, 59), tzinfo=ET)
    rows_by_ticker: dict[str, list[dict[str, Any]]] = {}
    statuses: dict[str, str] = {}
    ib = IB()
    try:
        ib.connect(host, port, clientId=client_id, timeout=timeout_sec, readonly=True)
        for offset in range(0, len(tickers), batch_size):
            batch = tickers[offset : offset + batch_size]
            for ticker, source_symbol in batch:
                contract = Stock(source_symbol.replace(".", " "), "SMART", "USD")
                try:
                    qualified = ib.qualifyContracts(contract)
                    selected = qualified[0] if qualified else contract
                    bars = ib.reqHistoricalData(
                        selected,
                        endDateTime=end_dt,
                        durationStr=duration,
                        barSizeSetting="1 day",
                        whatToShow="ADJUSTED_LAST",
                        useRTH=True,
                        formatDate=2,
                        keepUpToDate=False,
                    )
                except Exception as exc:  # noqa: BLE001 - IB errors vary by gateway version.
                    rows_by_ticker[ticker] = []
                    statuses[ticker] = f"ib_error:{type(exc).__name__}"
                    if request_pause_sec > 0:
                        ib.sleep(request_pause_sec)
                    continue
                retrieved = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
                normalized: list[dict[str, Any]] = []
                for bar in bars:
                    raw = {
                        "date": _ib_day(getattr(bar, "date", "")),
                        "open": getattr(bar, "open", None),
                        "high": getattr(bar, "high", None),
                        "low": getattr(bar, "low", None),
                        "close": getattr(bar, "close", None),
                        "adj_open": getattr(bar, "open", None),
                        "adj_high": getattr(bar, "high", None),
                        "adj_low": getattr(bar, "low", None),
                        "adj_close": getattr(bar, "close", None),
                        "volume": getattr(bar, "volume", 0.0),
                        "adjustment_factor": 1.0,
                    }
                    row = normalize_ohlcv_row(
                        ticker=ticker,
                        source="ibkr",
                        source_symbol=source_symbol,
                        raw=raw,
                        retrieved_at_utc=retrieved,
                        adjustment_status="ibkr_adjusted_last_raw_unavailable",
                    )
                    if row is not None and start.isoformat() <= row["date"] <= end.isoformat():
                        normalized.append(row)
                rows_by_ticker[ticker] = normalized
                statuses[ticker] = "ok" if normalized else "empty"
                if request_pause_sec > 0:
                    ib.sleep(request_pause_sec)
    finally:
        if ib.isConnected():
            ib.disconnect()
    return rows_by_ticker, statuses


def arbitrate_observations(
    rows: Iterable[Mapping[str, Any]],
    *,
    source_priority: Sequence[str] = SOURCE_PRIORITY,
    disagreement_warn_bps: float = 25.0,
    disagreement_fail_bps: float = 100.0,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Select one row by source priority and retain disagreement diagnostics."""
    if tuple(source_priority) != SOURCE_PRIORITY:
        raise ValueError(f"Source priority must be {SOURCE_PRIORITY}")
    if not 0 <= disagreement_warn_bps <= disagreement_fail_bps:
        raise ValueError("Invalid OHLCV disagreement thresholds")
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for raw in rows:
        row = dict(raw)
        grouped[(str(row.get("ticker", "")), str(row.get("date", "")))].append(row)
    selected_rows: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    rank = {source: index for index, source in enumerate(source_priority)}
    for (ticker, day), candidates in sorted(grouped.items()):
        candidates.sort(key=lambda row: rank.get(str(row.get("source", "")), 999))
        selected = dict(candidates[0])
        closes = [float(row["adj_close"]) for row in candidates]
        base = float(selected["adj_close"])
        max_bps = max((abs(value - base) / base * 10000.0 for value in closes), default=0.0)
        status = (
            "FAIL"
            if max_bps > disagreement_fail_bps
            else "WARN"
            if max_bps > disagreement_warn_bps
            else "PASS"
        )
        sources = sorted(
            {str(row["source"]) for row in candidates},
            key=lambda source: rank.get(source, 999),
        )
        selected.update(
            {
                "source_count": len(sources),
                "sources_observed": ";".join(sources),
                "max_adj_close_disagreement_bps": round(max_bps, 8),
                "disagreement_status": status,
            }
        )
        selected_rows.append(selected)
        diagnostics.append(
            {
                "date": day,
                "ticker": ticker,
                "selected_source": selected["source"],
                "sources_observed": ";".join(sources),
                "source_count": len(sources),
                "max_adj_close_disagreement_bps": round(max_bps, 8),
                "status": status,
            }
        )
    return selected_rows, diagnostics
