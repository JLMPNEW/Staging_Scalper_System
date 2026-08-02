from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class InstrumentHydration:
    ticker: str
    row_count: int
    first_date: str
    last_date: str
    extracted_sha256: str


def configured_market_instruments(risk_config: dict[str, Any]) -> list[str]:
    tickers = {
        str(risk_config.get("master_calendar_ticker") or "SPY").strip().upper()
    }
    for key in ("benchmark_tickers", "hedge_rotation_etfs"):
        tickers.update(
            str(value).strip().upper()
            for value in risk_config.get(key, [])
            if str(value).strip()
        )
    tickers.update(
        str(value).strip().upper()
        for value in (risk_config.get("sector_etf_map") or {}).values()
        if str(value).strip()
    )
    return sorted(ticker for ticker in tickers if ticker)


def _required_last_session(end: date) -> date:
    required = end
    while required.weekday() >= 5:
        required = required.fromordinal(required.toordinal() - 1)
    return required


def _series_sha256(rows: list[tuple[str, float]]) -> str:
    digest = hashlib.sha256()
    for bar_date, value in rows:
        digest.update(f"{bar_date},{value:.17g}\n".encode())
    return digest.hexdigest()


def _frame_rows(frame: Any, *, start: date, end: date) -> list[tuple[str, float]]:
    if frame is None or bool(frame.empty):
        return []
    rows: list[tuple[str, float]] = []
    for raw_index, values in frame.iterrows():
        bar_date = raw_index.date().isoformat()
        if bar_date < start.isoformat() or bar_date > end.isoformat():
            continue
        raw_close = values.get("Close")
        if raw_close is None:
            continue
        close = float(raw_close)
        if close > 0:
            rows.append((bar_date, close))
    return sorted(dict(rows).items())


def hydrate_market_instruments(
    provider: Any,
    *,
    database_path: Path,
    tickers: list[str],
    start: date,
    end: date,
    source_id: str,
    price_adjustment: str,
    allow_missing: bool = False,
) -> list[InstrumentHydration]:
    required_last = _required_last_session(end).isoformat()
    fetched: dict[str, list[tuple[str, float]]] = {}
    summaries: list[InstrumentHydration] = []
    for ticker in tickers:
        try:
            frame = provider.price_timeseries(
                ticker,
                stock_price_adjustment_setting=(
                    provider.StockPriceAdjustmentType.TOTALRETURN
                ),
                start_date=start,
                end_date=end,
                timeseriesformat="pandas-dataframe",
            )
            rows = _frame_rows(frame, start=start, end=end)
            if not rows:
                raise RuntimeError(f"Norgate returned no adjusted rows for {ticker}")
            if rows[-1][0] < required_last:
                raise RuntimeError(
                    f"Norgate adjusted history is stale for {ticker}: "
                    f"last={rows[-1][0]} required={required_last}"
                )
        except Exception:
            if allow_missing:
                continue
            raise
        fetched[ticker] = rows
        summaries.append(
            InstrumentHydration(
                ticker=ticker,
                row_count=len(rows),
                first_date=rows[0][0],
                last_date=rows[-1][0],
                extracted_sha256=_series_sha256(rows),
            )
        )

    database_path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS fact_price_ohlcv(
                ticker TEXT NOT NULL,
                bar_date TEXT NOT NULL,
                source_id TEXT NOT NULL,
                adj_close REAL NOT NULL,
                price_adjustment TEXT NOT NULL,
                is_adjusted INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (ticker, bar_date, source_id)
            )
            """
        )
        for ticker, rows in fetched.items():
            connection.executemany(
                """
                INSERT INTO fact_price_ohlcv(
                    ticker, bar_date, source_id, adj_close,
                    price_adjustment, is_adjusted, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(ticker, bar_date, source_id) DO UPDATE SET
                    adj_close = excluded.adj_close,
                    price_adjustment = excluded.price_adjustment,
                    is_adjusted = 1,
                    updated_at = excluded.updated_at
                """,
                [
                    (
                        ticker,
                        bar_date,
                        source_id,
                        value,
                        price_adjustment,
                        now,
                        now,
                    )
                    for bar_date, value in rows
                ],
            )
    return summaries


def hydration_rows(items: list[InstrumentHydration]) -> list[dict[str, Any]]:
    return [asdict(item) for item in items]


def purge_cached_tickers(
    database_path: Path,
    *,
    tickers: set[str],
    source_id: str,
) -> int:
    if not database_path.is_file() or not tickers:
        return 0
    placeholders = ",".join("?" for _ in tickers)
    with sqlite3.connect(database_path) as connection:
        table_exists = connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'fact_price_ohlcv'
            """
        ).fetchone()
        if table_exists is None:
            return 0
        cursor = connection.execute(
            f"""
            DELETE FROM fact_price_ohlcv
            WHERE source_id = ? AND ticker IN ({placeholders})
            """,
            (source_id, *sorted(tickers)),
        )
        return max(0, int(cursor.rowcount))
