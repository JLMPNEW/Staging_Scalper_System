#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from biotech_index.core.config import cfg_get, load_yaml, resolve_path
from biotech_index.core.db import connect, finish_run, init_db, start_run, utc_now


LOGGER = logging.getLogger("sync_market_data_ib")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
SOURCE = "interactive_brokers"


@dataclass(frozen=True)
class Company:
    company_id: int
    ticker: str
    company_name: str
    currency: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync IB market prices/bars into the biotech index database.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", type=str, default="", help="Snapshot date in YYYY-MM-DD. Defaults to UTC today.")
    parser.add_argument("--tickers", type=str, default="", help="Optional comma-separated ticker subset.")
    parser.add_argument("--max-tickers", type=int, default=0, help="Smoke-test limit. 0 means all.")
    return parser.parse_args()


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    for handler in logging.getLogger().handlers:
        if handler.formatter is not None:
            handler.formatter.converter = time.gmtime
    logging.getLogger("ib_insync.wrapper").setLevel(logging.WARNING)
    logging.getLogger("ib_insync.client").setLevel(logging.WARNING)
    logging.getLogger("ib_insync.ib").setLevel(logging.WARNING)


def parse_date(raw: object) -> date | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def to_float(raw: object) -> float | None:
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def as_bool(raw: object) -> bool:
    return str(raw or "").strip().lower() in {"1", "true", "yes", "y"}


def read_scoring_tickers(path: Path) -> set[str]:
    if not path.exists():
        raise FileNotFoundError(f"Final scoring universe CSV not found: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        out: set[str] = set()
        for row in reader:
            ticker = str(row.get("ticker") or "").strip().upper()
            if ticker and str(row.get("final_status") or "").strip().lower() == "keep" and as_bool(row.get("scoring_include")):
                out.add(ticker)
    if not out:
        raise ValueError(f"Final scoring universe CSV contains no scoring tickers: {path}")
    return out


def load_companies(conn: sqlite3.Connection, *, scoring_tickers: set[str], ticker_filter: set[str], max_tickers: int) -> list[Company]:
    rows = conn.execute(
        """
        SELECT company_id, ticker, company_name, currency
        FROM companies
        WHERE is_active = 1
        ORDER BY ticker
        """
    ).fetchall()
    out: list[Company] = []
    for row in rows:
        ticker = str(row["ticker"] or "").upper().replace(".", "-")
        if scoring_tickers and ticker not in scoring_tickers:
            continue
        if ticker_filter and ticker not in ticker_filter:
            continue
        out.append(Company(int(row["company_id"]), ticker, str(row["company_name"] or ""), str(row["currency"] or "USD") or "USD"))
        if max_tickers > 0 and len(out) >= max_tickers:
            break
    return out


def load_latest_shares(conn: sqlite3.Connection, company_id: int, asof_date: date) -> float | None:
    row = conn.execute(
        """
        SELECT shares_outstanding
        FROM company_facts_quarterly
        WHERE company_id = ?
          AND period_end <= ?
          AND (filed_date IS NULL OR filed_date = '' OR filed_date <= ?)
          AND shares_outstanding IS NOT NULL
        ORDER BY period_end DESC, filed_date DESC
        LIMIT 1
        """,
        (company_id, asof_date.isoformat(), asof_date.isoformat()),
    ).fetchone()
    return to_float(row["shares_outstanding"]) if row else None


def pct_return(values: list[float], days: int) -> float | None:
    if len(values) <= days or values[-days - 1] == 0:
        return None
    return (values[-1] / values[-days - 1]) - 1.0


def score_liquidity(avg_dollar_volume_20d: float | None) -> float:
    if avg_dollar_volume_20d is None:
        return 0.0
    if avg_dollar_volume_20d >= 20_000_000:
        return 100.0
    if avg_dollar_volume_20d >= 10_000_000:
        return 85.0
    if avg_dollar_volume_20d >= 2_000_000:
        return 65.0
    if avg_dollar_volume_20d >= 1_000_000:
        return 45.0
    return 15.0


def ib_end_datetime(asof_date: date) -> str:
    return f"{asof_date.strftime('%Y%m%d')} 23:59:59 US/Eastern"


def fetch_bars(ib: Any, ticker: str, *, currency: str, duration: str, sleep_sec: float, asof_date: date) -> list[dict[str, Any]]:
    from ib_insync import Stock  # type: ignore

    contract = Stock(ticker, "SMART", currency or "USD")
    qualified = ib.qualifyContracts(contract)
    if not qualified:
        raise ValueError(f"IB could not qualify contract for {ticker}")
    bars = ib.reqHistoricalData(
        qualified[0],
        endDateTime=ib_end_datetime(asof_date),
        durationStr=duration,
        barSizeSetting="1 day",
        whatToShow="TRADES",
        useRTH=True,
        formatDate=1,
        keepUpToDate=False,
    )
    ib.sleep(sleep_sec)
    out: list[dict[str, Any]] = []
    for bar in bars:
        bar_date = bar.date.isoformat() if hasattr(bar.date, "isoformat") else str(bar.date)
        out.append(
            {
                "ticker": ticker,
                "bar_date": bar_date[:10],
                "source": SOURCE,
                "open": to_float(bar.open),
                "high": to_float(bar.high),
                "low": to_float(bar.low),
                "close": to_float(bar.close),
                "volume": to_float(bar.volume),
                "wap": to_float(getattr(bar, "average", None)),
                "data_quality": "high",
            }
        )
    return out


def build_market_rows(company: Company, bars: list[dict[str, Any]], xbi_closes: list[float], shares: float | None, asof_date: date) -> tuple[dict[str, Any], dict[str, Any]]:
    closes = [value for value in (to_float(row.get("close")) for row in bars) if value is not None and value > 0]
    volumes = [to_float(row.get("volume")) or 0.0 for row in bars if to_float(row.get("close")) is not None]
    if not closes:
        raise ValueError(f"No usable close prices for {company.ticker}")
    close = closes[-1]
    dollar_volumes = [closes[idx] * volumes[idx] for idx in range(min(len(closes), len(volumes)))]
    avg_volume_20d = sum(volumes[-20:]) / min(20, len(volumes)) if volumes else None
    avg_dollar_volume_20d = sum(dollar_volumes[-20:]) / min(20, len(dollar_volumes)) if dollar_volumes else None
    high_52w = max((to_float(row.get("high")) or 0.0 for row in bars[-260:]), default=None)
    low_52w = min((to_float(row.get("low")) or close for row in bars[-260:]), default=None)
    market_cap = close * shares if shares and shares > 0 else None
    sma_200 = sum(closes[-200:]) / min(200, len(closes)) if closes else None
    return_1m = pct_return(closes, 21)
    return_3m = pct_return(closes, 63)
    xbi_return_3m = pct_return(xbi_closes, 63) if xbi_closes else None
    relative_strength = return_3m - xbi_return_3m if return_3m is not None and xbi_return_3m is not None else None
    price_vs_200d = (close / sma_200 - 1.0) if sma_200 else None
    distance_52w = (close / high_52w - 1.0) if high_52w else None
    quality = "high" if len(closes) >= 200 else "medium" if len(closes) >= 63 else "low"
    snapshot = {
        "asof_date": asof_date.isoformat(),
        "company_id": company.company_id,
        "ticker": company.ticker,
        "source": SOURCE,
        "last_price": close,
        "close_price": close,
        "market_cap": market_cap,
        "shares_outstanding": shares,
        "avg_volume_20d": avg_volume_20d,
        "avg_dollar_volume_20d": avg_dollar_volume_20d,
        "fifty_two_week_high": high_52w,
        "fifty_two_week_low": low_52w,
        "currency": company.currency,
        "data_quality": quality,
        "payload_json": json.dumps({"bar_count": len(closes), "market_cap_source": "ib_close_x_sec_shares" if market_cap else "missing_sec_shares"}, sort_keys=True),
    }
    features = {
        "asof_date": asof_date.isoformat(),
        "company_id": company.company_id,
        "ticker": company.ticker,
        "source": SOURCE,
        "close_price": close,
        "market_cap": market_cap,
        "shares_outstanding": shares,
        "price_vs_200d_pct": price_vs_200d,
        "return_1m_pct": return_1m,
        "return_3m_pct": return_3m,
        "xbi_return_3m_pct": xbi_return_3m,
        "relative_strength_3m_vs_xbi": relative_strength,
        "distance_from_52w_high_pct": distance_52w,
        "avg_dollar_volume_20d": avg_dollar_volume_20d,
        "liquidity_score": score_liquidity(avg_dollar_volume_20d),
        "market_data_quality": quality,
        "payload_json": snapshot["payload_json"],
    }
    return snapshot, features


def upsert_market_rows(conn: sqlite3.Connection, *, bars: list[dict[str, Any]], snapshots: list[dict[str, Any]], features: list[dict[str, Any]]) -> None:
    now = utc_now()
    with conn:
        for row in bars:
            conn.execute(
                """
                INSERT INTO market_bars_daily(ticker, bar_date, source, open, high, low, close, volume, wap, data_quality, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(ticker, bar_date, source) DO UPDATE SET
                    open = excluded.open, high = excluded.high, low = excluded.low, close = excluded.close,
                    volume = excluded.volume, wap = excluded.wap, data_quality = excluded.data_quality
                """,
                (row["ticker"], row["bar_date"], row["source"], row["open"], row["high"], row["low"], row["close"], row["volume"], row["wap"], row["data_quality"], now),
            )
        for row in snapshots:
            conn.execute(
                """
                INSERT INTO market_snapshots_daily(
                    asof_date, company_id, ticker, source, last_price, close_price, market_cap, shares_outstanding,
                    avg_volume_20d, avg_dollar_volume_20d, fifty_two_week_high, fifty_two_week_low, currency,
                    data_quality, payload_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(asof_date, company_id, source) DO UPDATE SET
                    last_price = excluded.last_price, close_price = excluded.close_price, market_cap = excluded.market_cap,
                    shares_outstanding = excluded.shares_outstanding, avg_volume_20d = excluded.avg_volume_20d,
                    avg_dollar_volume_20d = excluded.avg_dollar_volume_20d, fifty_two_week_high = excluded.fifty_two_week_high,
                    fifty_two_week_low = excluded.fifty_two_week_low, currency = excluded.currency, data_quality = excluded.data_quality,
                    payload_json = excluded.payload_json, updated_at = excluded.updated_at
                """,
                tuple(row.get(field) for field in ["asof_date", "company_id", "ticker", "source", "last_price", "close_price", "market_cap", "shares_outstanding", "avg_volume_20d", "avg_dollar_volume_20d", "fifty_two_week_high", "fifty_two_week_low", "currency", "data_quality", "payload_json"]) + (now, now),
            )
        for row in features:
            conn.execute(
                """
                INSERT INTO market_features_daily(
                    asof_date, company_id, ticker, source, close_price, market_cap, shares_outstanding, price_vs_200d_pct,
                    return_1m_pct, return_3m_pct, xbi_return_3m_pct, relative_strength_3m_vs_xbi,
                    distance_from_52w_high_pct, avg_dollar_volume_20d, liquidity_score, market_data_quality,
                    payload_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(asof_date, company_id, source) DO UPDATE SET
                    close_price = excluded.close_price, market_cap = excluded.market_cap, shares_outstanding = excluded.shares_outstanding,
                    price_vs_200d_pct = excluded.price_vs_200d_pct, return_1m_pct = excluded.return_1m_pct,
                    return_3m_pct = excluded.return_3m_pct, xbi_return_3m_pct = excluded.xbi_return_3m_pct,
                    relative_strength_3m_vs_xbi = excluded.relative_strength_3m_vs_xbi,
                    distance_from_52w_high_pct = excluded.distance_from_52w_high_pct,
                    avg_dollar_volume_20d = excluded.avg_dollar_volume_20d, liquidity_score = excluded.liquidity_score,
                    market_data_quality = excluded.market_data_quality, payload_json = excluded.payload_json, updated_at = excluded.updated_at
                """,
                tuple(row.get(field) for field in ["asof_date", "company_id", "ticker", "source", "close_price", "market_cap", "shares_outstanding", "price_vs_200d_pct", "return_1m_pct", "return_3m_pct", "xbi_return_3m_pct", "relative_strength_3m_vs_xbi", "distance_from_52w_high_pct", "avg_dollar_volume_20d", "liquidity_score", "market_data_quality", "payload_json"]) + (now, now),
            )


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = ["asof_date", "ticker", "company_name", "close_price", "market_cap", "shares_outstanding", "avg_dollar_volume_20d", "return_3m_pct", "relative_strength_3m_vs_xbi", "price_vs_200d_pct", "distance_from_52w_high_pct", "market_data_quality"]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows([{field: row.get(field, "") for field in fieldnames} for row in rows])


def main() -> None:
    configure_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    output_csv = resolve_path(cfg_get(config, "ib_market_data.output_csv"), base_dir=base_dir)
    final_universe_csv = resolve_path(cfg_get(config, "ib_market_data.final_scoring_universe_csv"), base_dir=base_dir)
    asof_date = parse_date(args.asof) if args.asof else datetime.now(timezone.utc).date()
    if asof_date is None:
        raise ValueError(f"Invalid --asof date: {args.asof}")
    ticker_filter = {value.strip().upper().replace(".", "-") for value in args.tickers.split(",") if value.strip()}

    host = str(cfg_get(config, "ib_market_data.host", "127.0.0.1"))
    port = int(cfg_get(config, "ib_market_data.port", 7497))
    client_id = int(cfg_get(config, "ib_market_data.client_id", 7717))
    duration = str(cfg_get(config, "ib_market_data.duration", "1 Y"))
    sleep_sec = float(cfg_get(config, "ib_market_data.sleep_sec", 0.15))
    sqlite_timeout_sec = float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))

    try:
        from ib_insync import IB  # type: ignore
    except Exception as exc:
        raise RuntimeError("ib_insync is required for IB market data sync. Install package 'ib_insync'.") from exc

    with connect(db_path, timeout_sec=sqlite_timeout_sec) as conn:
        init_db(conn)
        run_id = start_run(conn, run_type="sync_market_data_ib", input_path=db_path)
        ib = IB()
        try:
            scoring_tickers = read_scoring_tickers(final_universe_csv)
            companies = load_companies(conn, scoring_tickers=scoring_tickers, ticker_filter=ticker_filter, max_tickers=args.max_tickers)
            LOGGER.info("Loaded %d company job(s) for IB market sync", len(companies))
            ib.connect(host, port, clientId=client_id, timeout=float(cfg_get(config, "ib_market_data.connect_timeout_sec", 15.0)))
            xbi_bars = fetch_bars(ib, "XBI", currency="USD", duration=duration, sleep_sec=sleep_sec, asof_date=asof_date)
            xbi_closes = [value for value in (to_float(row.get("close")) for row in xbi_bars) if value is not None and value > 0]
            all_bars: list[dict[str, Any]] = list(xbi_bars)
            snapshots: list[dict[str, Any]] = []
            features: list[dict[str, Any]] = []
            csv_rows: list[dict[str, Any]] = []
            failed_tickers: list[str] = []
            for idx, company in enumerate(companies, start=1):
                try:
                    bars = fetch_bars(
                        ib,
                        company.ticker,
                        currency=company.currency or "USD",
                        duration=duration,
                        sleep_sec=sleep_sec,
                        asof_date=asof_date,
                    )
                    shares = load_latest_shares(conn, company.company_id, asof_date)
                    snapshot, feature = build_market_rows(company, bars, xbi_closes, shares, asof_date)
                    all_bars.extend(bars)
                    snapshots.append(snapshot)
                    features.append(feature)
                    csv_rows.append({"company_name": company.company_name, **feature})
                    LOGGER.info("[%d/%d] %s bars=%d quality=%s", idx, len(companies), company.ticker, len(bars), feature.get("market_data_quality"))
                except Exception as exc:
                    failed_tickers.append(company.ticker)
                    LOGGER.warning("IB market sync failed for %s: %s", company.ticker, exc)
            if companies and not features:
                raise RuntimeError(f"IB market sync produced no company feature rows; failed_tickers={','.join(failed_tickers)}")
            upsert_market_rows(conn, bars=all_bars, snapshots=snapshots, features=features)
            write_csv(output_csv, csv_rows)
            status = "partial" if failed_tickers else "success"
            message = f"asof={asof_date.isoformat()} output={output_csv}"
            if failed_tickers:
                message += f" failed_tickers={','.join(failed_tickers)}"
            finish_run(conn, run_id=run_id, status=status, row_count=len(features), message=message)
        except Exception as exc:
            finish_run(conn, run_id=run_id, status="failed", row_count=0, message=f"{type(exc).__name__}: {exc}")
            raise
        finally:
            if ib.isConnected():
                ib.disconnect()
    LOGGER.info("IB market sync complete: rows=%d output=%s", len(features), output_csv)


if __name__ == "__main__":
    main()
