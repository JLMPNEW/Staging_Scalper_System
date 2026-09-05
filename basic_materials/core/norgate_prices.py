"""Norgate adjusted-price extraction and atomic Stage 3 publication."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
import hashlib
import json
import math
from pathlib import Path
import sqlite3
from typing import Any, Mapping

import pandas as pd

from basic_materials.core.atomic_io import atomic_write_csv, atomic_write_json
from basic_materials.core.db import assert_database_identity, utc_now
from basic_materials.core.market_data_contract import MarketDataManifest, MarketDataPolicy
from basic_materials.core.norgate_runtime import (
    NORGATE_EQUITY_DATABASES,
    norgate_database_fingerprint,
    require_norgate_snapshot,
)


CACHE_COLUMNS = (
    "bar_date",
    "open",
    "high",
    "low",
    "close",
    "adjusted_close",
    "volume",
    "dividend",
    "capital_event",
)


@dataclass(frozen=True)
class ExtractedInstrument:
    instrument_id: int
    instrument_key: str
    provider_symbol: str
    provider_asset_id: str
    requested_start: str
    requested_end: str
    row_count: int
    first_bar_date: str
    last_bar_date: str
    payload_path: Path
    payload_sha256: str


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _iso_index(frame: Any) -> list[str]:
    if frame is None or len(frame) == 0:
        return []
    index = pd.DatetimeIndex(pd.to_datetime(frame.index))
    if index.tz is not None:
        index = index.tz_localize(None)
    return [value.date().isoformat() for value in index]


def _column(frame: pd.DataFrame, name: str) -> Any:
    lookup = {str(column).casefold(): column for column in frame.columns}
    key = lookup.get(name.casefold())
    return frame[key] if key is not None else None


def _value(series: Any, position: int) -> float | None:
    if series is None or position >= len(series):
        return None
    return _safe_float(series.iloc[position])


def _instrument_requests(conn: sqlite3.Connection, as_of: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT i.instrument_id, i.instrument_key, i.provider_source_id,
               i.provider_symbol, i.provider_asset_id, i.provider_database,
               MIN(r.expected_start_date) AS requested_start,
               MAX(CASE WHEN r.expected_end_date IS NULL THEN ? ELSE r.expected_end_date END)
                   AS requested_end
        FROM dim_market_instrument AS i
        JOIN bridge_market_instrument_role AS r ON r.instrument_id = i.instrument_id
        WHERE r.required_for_stage3 = 1
        GROUP BY i.instrument_id, i.instrument_key, i.provider_source_id,
                 i.provider_symbol, i.provider_asset_id, i.provider_database
        ORDER BY CAST(i.provider_asset_id AS INTEGER)
        """,
        (as_of,),
    ).fetchall()
    return [dict(row) for row in rows]


def _extract_one(
    provider: Any,
    request: Mapping[str, Any],
    *,
    cache_dir: Path,
) -> ExtractedInstrument:
    symbol = str(request["provider_symbol"])
    asset_id = str(request["provider_asset_id"])
    observed_asset = str(provider.assetid(symbol) or "")
    if observed_asset != asset_id:
        raise RuntimeError(
            f"Norgate stable identity mismatch for {symbol}: contract={asset_id}, provider={observed_asset}"
        )
    start = str(request["requested_start"])
    end = str(request["requested_end"])
    if start > end:
        raise RuntimeError(f"Invalid requested market window for {symbol}: {start}>{end}")
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
        capital = provider.capital_event_timeseries(
            symbol,
            start_date=start,
            end_date=end,
            timeseriesformat="pandas-dataframe",
        )
    except Exception as exc:
        raise RuntimeError(f"Norgate extraction failed for {symbol}: {type(exc).__name__}: {exc}") from exc

    raw_dates = _iso_index(raw)
    adjusted_dates = _iso_index(adjusted)
    if not raw_dates:
        raise RuntimeError(f"Norgate returned no raw bars for {symbol}")
    if raw_dates != sorted(set(raw_dates)):
        raise RuntimeError(f"Norgate dates are not unique and increasing for {symbol}")
    if raw_dates != adjusted_dates:
        raise RuntimeError(f"Norgate raw and total-return dates differ for {symbol}")
    if raw_dates[0] < start or raw_dates[-1] > end:
        raise RuntimeError(f"Norgate returned bars outside the requested window for {symbol}")

    raw_open = _column(raw, "Open")
    raw_high = _column(raw, "High")
    raw_low = _column(raw, "Low")
    raw_close = _column(raw, "Close")
    raw_volume = _column(raw, "Volume")
    raw_dividend = _column(raw, "Dividend")
    adjusted_close = _column(adjusted, "Close")
    if raw_close is None or adjusted_close is None:
        raise RuntimeError(f"Norgate close columns are missing for {symbol}")

    capital_dates: set[str] = set()
    if capital is not None and len(capital):
        capital_column = _column(capital, "Capital Event")
        for position, event_date in enumerate(_iso_index(capital)):
            event_value = _value(capital_column, position)
            if start <= event_date <= end and event_value not in {None, 0.0}:
                capital_dates.add(event_date)

    rows: list[dict[str, Any]] = []
    for position, bar_date in enumerate(raw_dates):
        close = _value(raw_close, position)
        total_return = _value(adjusted_close, position)
        if close is None or close <= 0 or total_return is None or total_return <= 0:
            raise RuntimeError(f"Norgate returned an invalid close for {symbol} on {bar_date}")
        open_value = _value(raw_open, position)
        high = _value(raw_high, position)
        low = _value(raw_low, position)
        volume = _value(raw_volume, position)
        dividend = _value(raw_dividend, position)
        if high is not None and (high < close or (open_value is not None and high < open_value)):
            raise RuntimeError(f"Norgate returned an invalid high for {symbol} on {bar_date}")
        if low is not None and (low > close or (open_value is not None and low > open_value)):
            raise RuntimeError(f"Norgate returned an invalid low for {symbol} on {bar_date}")
        if volume is not None and volume < 0:
            raise RuntimeError(f"Norgate returned negative volume for {symbol} on {bar_date}")
        if dividend is not None and dividend < 0:
            raise RuntimeError(f"Norgate returned a negative dividend for {symbol} on {bar_date}")
        rows.append(
            {
                "bar_date": bar_date,
                "open": open_value,
                "high": high,
                "low": low,
                "close": close,
                "adjusted_close": total_return,
                "volume": volume,
                "dividend": dividend,
                "capital_event": int(bar_date in capital_dates),
            }
        )

    payload_path = cache_dir / f"{asset_id}_{symbol.replace('.', '_')}.csv"
    atomic_write_csv(payload_path, rows, CACHE_COLUMNS)
    payload_sha = hashlib.sha256(payload_path.read_bytes()).hexdigest()
    return ExtractedInstrument(
        instrument_id=int(request["instrument_id"]),
        instrument_key=str(request["instrument_key"]),
        provider_symbol=symbol,
        provider_asset_id=asset_id,
        requested_start=start,
        requested_end=end,
        row_count=len(rows),
        first_bar_date=rows[0]["bar_date"],
        last_bar_date=rows[-1]["bar_date"],
        payload_path=payload_path,
        payload_sha256=payload_sha,
    )


def _cache_rows(extraction: ExtractedInstrument) -> list[dict[str, Any]]:
    with extraction.payload_path.open("r", encoding="utf-8", newline="") as handle:
        rows = []
        for raw in csv.DictReader(handle):
            row: dict[str, Any] = {"bar_date": str(raw["bar_date"])}
            for name in ("open", "high", "low", "close", "adjusted_close", "volume", "dividend"):
                row[name] = _safe_float(raw[name])
            row["capital_event"] = int(raw["capital_event"])
            rows.append(row)
    if len(rows) != extraction.row_count:
        raise RuntimeError(f"Canonical cache row count changed for {extraction.provider_symbol}")
    if hashlib.sha256(extraction.payload_path.read_bytes()).hexdigest() != extraction.payload_sha256:
        raise RuntimeError(f"Canonical cache hash changed for {extraction.provider_symbol}")
    return rows


def load_norgate_market_data(
    conn: sqlite3.Connection,
    *,
    policy: MarketDataPolicy,
    manifest: MarketDataManifest,
    provider: Any,
    cache_root: str | Path,
    as_of: str,
) -> dict[str, Any]:
    """Extract a fenced provider snapshot and publish all required bars atomically."""

    assert_database_identity(conn)
    date.fromisoformat(as_of)
    if provider.status() is not True:
        raise RuntimeError("Local Norgate Data Updater is unavailable")
    if conn.in_transaction:
        raise RuntimeError("load_norgate_market_data requires a clean connection")
    requests = _instrument_requests(conn, as_of)
    if len(requests) != policy.expected_unique_instruments:
        raise RuntimeError(
            f"Stage 3 contract must be loaded first; expected {policy.expected_unique_instruments} "
            f"instrument requests and found {len(requests)}"
        )
    expected_source = str(policy.payload["provider"]["source_id"])
    if any(str(row["provider_source_id"]) != expected_source for row in requests):
        raise RuntimeError("Loaded market instruments use an unexpected provider source")

    cache_dir = Path(cache_root).resolve(strict=False) / "norgate" / as_of
    cache_dir.mkdir(parents=True, exist_ok=True)
    fingerprint_start = norgate_database_fingerprint(provider, NORGATE_EQUITY_DATABASES)
    extracted: list[ExtractedInstrument] = []
    for position, request in enumerate(requests, start=1):
        extracted.append(_extract_one(provider, request, cache_dir=cache_dir))
        if position % 25 == 0:
            require_norgate_snapshot(
                provider,
                fingerprint_start,
                context=f"after {position} instrument extractions",
            )
    fingerprint_end = require_norgate_snapshot(
        provider,
        fingerprint_start,
        context="before atomic market-data publication",
    )

    cache_manifest = {
        "manifest_version": 1,
        "source_id": expected_source,
        "extraction_asof_date": as_of,
        "contract_manifest_sha256": manifest.checksum,
        "provider_database_fingerprint": fingerprint_end,
        "instruments": [
            {
                "instrument_key": item.instrument_key,
                "provider_symbol": item.provider_symbol,
                "provider_asset_id": item.provider_asset_id,
                "requested_start": item.requested_start,
                "requested_end": item.requested_end,
                "first_bar_date": item.first_bar_date,
                "last_bar_date": item.last_bar_date,
                "row_count": item.row_count,
                "cache_path": str(item.payload_path),
                "sha256": item.payload_sha256,
            }
            for item in extracted
        ],
    }
    cache_manifest_path = atomic_write_json(cache_dir / "norgate_cache_manifest.json", cache_manifest)
    cache_manifest_payload = cache_manifest_path.read_bytes()
    raw_manifest_sha = hashlib.sha256(cache_manifest_payload).hexdigest()
    snapshot_key = f"norgate:{as_of}:{raw_manifest_sha[:20]}"
    now = utc_now()
    total_bars = sum(item.row_count for item in extracted)

    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """
            INSERT INTO fact_market_provider_snapshot (
                snapshot_key, provider_source_id, extraction_asof_date,
                database_fingerprint_json, contract_manifest_sha256,
                raw_manifest_sha256, instrument_count, bar_count, cache_root,
                status, created_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'loaded', ?)
            ON CONFLICT(snapshot_key) DO UPDATE SET
                database_fingerprint_json = excluded.database_fingerprint_json,
                contract_manifest_sha256 = excluded.contract_manifest_sha256,
                raw_manifest_sha256 = excluded.raw_manifest_sha256,
                instrument_count = excluded.instrument_count,
                bar_count = excluded.bar_count,
                cache_root = excluded.cache_root,
                status = 'loaded'
            """,
            (
                snapshot_key,
                expected_source,
                as_of,
                json.dumps(fingerprint_end, sort_keys=True),
                manifest.checksum,
                raw_manifest_sha,
                len(extracted),
                total_bars,
                str(cache_dir),
                now,
            ),
        )
        conn.execute(
            """
            INSERT INTO raw_source_payloads (
                snapshot_id, source_id, source_snapshot_date, source_path, sha256,
                byte_size, row_count, media_type, payload, manifest_version, ingested_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'application/json', ?, ?, ?)
            ON CONFLICT(snapshot_id) DO NOTHING
            """,
            (
                f"{expected_source}:{raw_manifest_sha}",
                expected_source,
                as_of,
                str(cache_manifest_path),
                raw_manifest_sha,
                len(cache_manifest_payload),
                total_bars,
                cache_manifest_payload,
                policy.policy_version,
                now,
            ),
        )

        action_count = 0
        extracted_by_instrument: dict[int, ExtractedInstrument] = {}
        for item in extracted:
            extracted_by_instrument[item.instrument_id] = item
            rows = _cache_rows(item)
            conn.execute(
                """
                DELETE FROM fact_adjusted_price_bar
                WHERE instrument_id = ? AND provider_source_id = ?
                  AND bar_date BETWEEN ? AND ?
                """,
                (item.instrument_id, expected_source, item.requested_start, item.requested_end),
            )
            conn.execute(
                """
                DELETE FROM fact_corporate_action
                WHERE instrument_id = ? AND provider_source_id = ?
                  AND action_date BETWEEN ? AND ?
                """,
                (item.instrument_id, expected_source, item.requested_start, item.requested_end),
            )
            conn.executemany(
                """
                INSERT INTO fact_adjusted_price_bar (
                    instrument_id, bar_date, provider_source_id, open, high, low, close,
                    adjusted_close, volume, dividend, capital_event, adjustment_basis,
                    snapshot_key, payload_sha256, source_timestamp_utc,
                    created_at_utc, updated_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'norgate_total_return', ?, ?, ?, ?, ?)
                """,
                [
                    (
                        item.instrument_id,
                        row["bar_date"],
                        expected_source,
                        row["open"],
                        row["high"],
                        row["low"],
                        row["close"],
                        row["adjusted_close"],
                        row["volume"],
                        row["dividend"],
                        row["capital_event"],
                        snapshot_key,
                        item.payload_sha256,
                        now,
                        now,
                        now,
                    )
                    for row in rows
                ],
            )
            actions: list[tuple[Any, ...]] = []
            for row in rows:
                if row["dividend"] is not None and row["dividend"] > 0:
                    actions.append(
                        (
                            item.instrument_id,
                            row["bar_date"],
                            expected_source,
                            "cash_dividend",
                            row["dividend"],
                            "USD",
                            snapshot_key,
                            json.dumps({"provider_symbol": item.provider_symbol}, sort_keys=True),
                            now,
                            now,
                        )
                    )
                if row["capital_event"]:
                    actions.append(
                        (
                            item.instrument_id,
                            row["bar_date"],
                            expected_source,
                            "capital_event",
                            1.0,
                            None,
                            snapshot_key,
                            json.dumps({"provider_symbol": item.provider_symbol}, sort_keys=True),
                            now,
                            now,
                        )
                    )
            if actions:
                conn.executemany(
                    """
                    INSERT INTO fact_corporate_action (
                        instrument_id, action_date, provider_source_id, action_type,
                        action_value, action_currency, snapshot_key, details_json,
                        created_at_utc, updated_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    actions,
                )
                action_count += len(actions)

        broad = conn.execute(
            """
            SELECT instrument_id FROM bridge_market_instrument_role
            WHERE role_type = 'broad_benchmark'
            """
        ).fetchone()
        if broad is None:
            raise RuntimeError("Broad benchmark role is missing")
        broad_id = int(broad["instrument_id"])
        broad_rows = _cache_rows(extracted_by_instrument[broad_id])
        conn.executemany(
            """
            INSERT INTO dim_trading_calendar_session (
                calendar_code, session_date, source_instrument_id, provider_source_id,
                snapshot_key, created_at_utc, updated_at_utc
            ) VALUES ('XNYS_PROXY_SPY', ?, ?, ?, ?, ?, ?)
            ON CONFLICT(calendar_code, session_date) DO UPDATE SET
                source_instrument_id = excluded.source_instrument_id,
                provider_source_id = excluded.provider_source_id,
                snapshot_key = excluded.snapshot_key,
                updated_at_utc = excluded.updated_at_utc
            """,
            [
                (row["bar_date"], broad_id, expected_source, snapshot_key, now, now)
                for row in broad_rows
            ],
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return {
        "source_id": expected_source,
        "snapshot_key": snapshot_key,
        "extraction_asof_date": as_of,
        "provider_database_fingerprint_start": fingerprint_start,
        "provider_database_fingerprint_end": fingerprint_end,
        "instrument_count": len(extracted),
        "bar_count": total_bars,
        "corporate_action_count": action_count,
        "calendar_session_count": len(broad_rows),
        "cache_manifest_path": str(cache_manifest_path),
        "cache_manifest_sha256": raw_manifest_sha,
    }
