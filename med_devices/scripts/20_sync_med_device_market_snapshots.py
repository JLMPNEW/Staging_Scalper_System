#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import re
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import requests  # type: ignore[reportMissingModuleSource]


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from med_devices.core.config import cfg_get, expand_env_vars, load_yaml, resolve_path  # noqa: E402
from med_devices.core.db import connect, finish_run, init_db, start_run, utc_now  # noqa: E402
from med_devices.core.logging_utils import configure_utc_logging  # noqa: E402
from med_devices.core.source_registry import load_source_registry, upsert_source_registry  # noqa: E402
from med_devices.core.text_norm import normalize_ticker  # noqa: E402


LOGGER = logging.getLogger("sync_med_device_market_snapshots")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
SOURCE_REGISTRY = PACKAGE_ROOT / "data" / "free_source_registry.yaml"
FIELDNAMES = [
    "ticker",
    "company_id",
    "company_name",
    "asof_date",
    "status",
    "source_id",
    "shares_outstanding",
    "market_cap",
    "currency",
    "review_reason",
]
IB_SHARE_KEYS = {
    "SHARESOUT",
    "SHARESOUTSTANDING",
    "COMMONSHARESOUTSTANDING",
    "TOTALCOMMONSHARESOUTSTANDING",
    "SHARESOUTSTANDINGCURRENT",
}
IB_MARKET_CAP_KEYS = {"MKTCAP", "MARKETCAP", "MARKETCAPITALIZATION"}


@dataclass(frozen=True)
class Company:
    company_id: int
    ticker: str
    company_name: str
    exchange: str
    currency: str


@dataclass(frozen=True)
class Snapshot:
    ticker: str
    company_id: int
    asof_date: str
    source_id: str
    shares_outstanding: float | None
    market_cap: float | None
    currency: str
    source_timestamp: str
    payload: dict[str, Any]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync market-data share-count snapshots for med-device companies.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--asof", type=str, default="")
    parser.add_argument("--tickers", type=str, default="")
    parser.add_argument("--max-tickers", type=int, default=0)
    parser.add_argument("--skip-ib", action="store_true", help="Skip IB and use Yahoo fallback directly.")
    parser.add_argument("--skip-yahoo", action="store_true", help="Skip Yahoo fallback.")
    parser.add_argument("--allow-partial", action="store_true")
    return parser.parse_args()


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
    if isinstance(raw, (int, float)):
        value = float(raw)
        return value if math.isfinite(value) else None
    text = str(raw).strip().replace(",", "")
    if not text:
        return None
    multiplier = 1.0
    suffix = text[-1:].upper()
    if suffix in {"K", "M", "B", "T"}:
        multiplier = {"K": 1_000.0, "M": 1_000_000.0, "B": 1_000_000_000.0, "T": 1_000_000_000_000.0}[suffix]
        text = text[:-1]
    try:
        value = float(text) * multiplier
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def latest_market_asof(conn: Any) -> str:
    row = conn.execute("SELECT MAX(bar_date) AS asof_date FROM fact_price_ohlcv").fetchone()
    value = str(row["asof_date"] or "") if row is not None else ""
    return value or date.today().isoformat()


def load_companies(conn: Any, *, ticker_filter: set[str], max_tickers: int) -> list[Company]:
    rows = conn.execute(
        """
        SELECT company_id, ticker, company_name, exchange, currency
        FROM dim_company
        WHERE is_active = 1
        ORDER BY ticker
        """
    ).fetchall()
    out: list[Company] = []
    for row in rows:
        ticker = normalize_ticker(row["ticker"])
        if ticker_filter and ticker not in ticker_filter:
            continue
        out.append(
            Company(
                company_id=int(row["company_id"]),
                ticker=ticker,
                company_name=str(row["company_name"] or ""),
                exchange=str(row["exchange"] or ""),
                currency=str(row["currency"] or "USD") or "USD",
            )
        )
        if max_tickers > 0 and len(out) >= max_tickers:
            break
    return out


def latest_price(conn: Any, ticker: str, asof: str, source_ids: list[str]) -> float | None:
    if not source_ids:
        return None
    placeholders = ",".join("?" for _ in source_ids)
    rows = conn.execute(
        f"""
        SELECT source_id, close, adj_close, bar_date
        FROM fact_price_ohlcv
        WHERE ticker = ?
          AND bar_date <= ?
          AND source_id IN ({placeholders})
        ORDER BY bar_date DESC
        """,
        [ticker, asof, *source_ids],
    ).fetchall()
    by_source: dict[str, Any] = {}
    for row in rows:
        by_source.setdefault(str(row["source_id"]), row)
    for source_id in source_ids:
        row = by_source.get(source_id)
        if row is not None:
            return to_float(row["adj_close"]) or to_float(row["close"])
    return None


def normalize_share_count(raw: object, *, million_units_hint: bool = False) -> float | None:
    value = to_float(raw)
    if value is None or value <= 0:
        return None
    # IB fundamental ratios may report shares in millions. Only apply that
    # conversion to fields known to use the IB ratio convention; derived or
    # vendor-reported raw share counts must stay in shares.
    if million_units_hint and value < 1_000_000:
        value *= 1_000_000.0
    return value


def parse_ib_ratio_object(raw: Any) -> tuple[float | None, float | None]:
    if raw is None:
        return None, None
    shares = None
    market_cap = None
    for key in dir(raw):
        if key.startswith("_"):
            continue
        value = getattr(raw, key, None)
        key_norm = re.sub(r"[^A-Z0-9]+", "", key.upper())
        if key_norm in IB_SHARE_KEYS:
            shares = normalize_share_count(value, million_units_hint=True)
        elif key_norm in IB_MARKET_CAP_KEYS:
            market_cap = to_float(value)
    return shares, market_cap


def mapping_get(raw: Any, *keys: str) -> Any:
    if raw is None:
        return None
    for key in keys:
        if isinstance(raw, dict) and key in raw:
            return raw.get(key)
        get = getattr(raw, "get", None)
        if callable(get):
            try:
                value = get(key)
            except Exception:
                value = None
            if value is not None:
                return value
        if hasattr(raw, key):
            value = getattr(raw, key, None)
            if value is not None:
                return value
    return None


def derive_shares_from_market_cap(market_cap: float | None, price: float | None) -> float | None:
    if market_cap is None or market_cap <= 0 or price is None or price <= 0:
        return None
    return normalize_share_count(market_cap / price)


def parse_ib_fundamental_xml(raw: str) -> tuple[float | None, float | None]:
    if not raw.strip():
        return None, None
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return None, None
    shares = None
    market_cap = None
    for elem in root.iter():
        label = ""
        for attr_key in ("FieldName", "fieldName", "ID", "Name", "name"):
            if elem.attrib.get(attr_key):
                label = elem.attrib[attr_key]
                break
        label_norm = re.sub(r"[^A-Z0-9]+", "", label.upper())
        value = to_float(elem.text)
        if value is None:
            continue
        if label_norm in IB_SHARE_KEYS:
            shares = normalize_share_count(value)
        elif label_norm in IB_MARKET_CAP_KEYS:
            market_cap = value
    return shares, market_cap


def fetch_ib_snapshot(ib: Any, company: Company, *, policy: dict[str, Any], asof: str) -> Snapshot | None:
    from ib_insync import Stock  # type: ignore[reportMissingModuleSource]

    source_id = str(cfg_get(policy, "primary_source_id", "ib_market_data") or "ib_market_data")
    contract = Stock(
        company.ticker,
        str(cfg_get(policy, "ib_default_exchange", "SMART") or "SMART"),
        str(company.currency or cfg_get(policy, "ib_default_currency", "USD") or "USD"),
    )
    qualified = ib.qualifyContracts(contract)
    if qualified:
        contract = qualified[0]
    shares = None
    market_cap = None
    payload: dict[str, Any] = {}
    try:
        ticker = ib.reqMktData(
            contract,
            genericTickList=str(cfg_get(policy, "ib_generic_tick_list", "258") or "258"),
            snapshot=True,
            regulatorySnapshot=False,
        )
        ib.sleep(float(cfg_get(policy, "ib_snapshot_timeout_sec", 8.0)))
        ratios = getattr(ticker, "fundamentalRatios", None)
        shares, market_cap = parse_ib_ratio_object(ratios)
        last_price = (
            to_float(getattr(ticker, "marketPrice", lambda: None)())
            or to_float(getattr(ticker, "last", None))
            or to_float(getattr(ticker, "close", None))
        )
        shares = shares or derive_shares_from_market_cap(market_cap, last_price)
        payload["mkt_data"] = {"fundamentalRatios": str(ratios), "last_price": last_price}
        ib.cancelMktData(contract)
    except Exception as exc:
        payload["mkt_data_error"] = f"{type(exc).__name__}: {exc}"
    if shares is None:
        try:
            xml_text = ib.reqFundamentalData(contract, "ReportSnapshot")
            xml_shares, xml_market_cap = parse_ib_fundamental_xml(str(xml_text or ""))
            shares = shares or xml_shares
            market_cap = market_cap or xml_market_cap
            payload["fundamental_xml_received"] = bool(xml_text)
        except Exception as exc:
            payload["fundamental_xml_error"] = f"{type(exc).__name__}: {exc}"
    if shares is None:
        return None
    return Snapshot(
        ticker=company.ticker,
        company_id=company.company_id,
        asof_date=asof,
        source_id=source_id,
        shares_outstanding=shares,
        market_cap=market_cap,
        currency=company.currency,
        source_timestamp=utc_now(),
        payload=payload,
    )


def yahoo_symbol(ticker: str) -> str:
    return normalize_ticker(ticker).replace(".", "-")


def fetch_yfinance_snapshots(
    companies: list[Company],
    *,
    policy: dict[str, Any],
    asof: str,
) -> dict[str, Snapshot]:
    import yfinance as yf  # type: ignore[reportMissingModuleSource]

    source_id = str(cfg_get(policy, "fallback_source_id", "yahoo_finance_backup") or "yahoo_finance_backup")
    sleep_sec = float(cfg_get(policy, "request_sleep_sec", 0.20))
    share_history_days = max(30, int(cfg_get(policy, "yfinance_share_history_days", 370)))
    asof_date = parse_date(asof) or date.today()
    start_date = asof_date - timedelta(days=share_history_days)
    end_date = asof_date + timedelta(days=1)
    out: dict[str, Snapshot] = {}
    for company in companies:
        symbol = yahoo_symbol(company.ticker)
        payload: dict[str, Any] = {"symbol": symbol}
        shares = None
        market_cap = None
        currency = company.currency or "USD"
        try:
            ticker = yf.Ticker(symbol)
            fast_info = ticker.fast_info
            market_cap = to_float(mapping_get(fast_info, "market_cap", "marketCap"))
            price = to_float(
                mapping_get(
                    fast_info,
                    "last_price",
                    "lastPrice",
                    "regularMarketPrice",
                    "previous_close",
                    "previousClose",
                )
            )
            shares = normalize_share_count(
                mapping_get(fast_info, "shares", "shares_outstanding", "sharesOutstanding")
            )
            currency = str(mapping_get(fast_info, "currency") or currency)
            payload["fast_info"] = {
                "shares": shares,
                "market_cap": market_cap,
                "price": price,
                "currency": currency,
            }
            if shares is None:
                shares = derive_shares_from_market_cap(market_cap, price)
                if shares is not None:
                    payload["share_method"] = "market_cap_div_price_fast_info"
            if shares is None:
                info = ticker.get_info()
                if isinstance(info, dict):
                    shares = normalize_share_count(
                        info.get("sharesOutstanding")
                        or info.get("impliedSharesOutstanding")
                        or info.get("floatShares")
                    )
                    market_cap = market_cap or to_float(info.get("marketCap"))
                    price = price or to_float(
                        info.get("regularMarketPrice")
                        or info.get("currentPrice")
                        or info.get("previousClose")
                    )
                    currency = str(info.get("currency") or currency)
                    payload["info_keys_used"] = {
                        "sharesOutstanding": info.get("sharesOutstanding"),
                        "impliedSharesOutstanding": info.get("impliedSharesOutstanding"),
                        "floatShares": info.get("floatShares"),
                        "marketCap": info.get("marketCap"),
                        "price": price,
                    }
                    if shares is None:
                        shares = derive_shares_from_market_cap(market_cap, price)
                        if shares is not None:
                            payload["share_method"] = "market_cap_div_price_info"
            if shares is None:
                try:
                    series = ticker.get_shares_full(start=start_date, end=end_date)
                except TypeError:
                    series = ticker.get_shares_full(start=start_date.isoformat(), end=end_date.isoformat())
                if series is not None and not getattr(series, "empty", True):
                    latest = series.dropna().iloc[-1]
                    shares = normalize_share_count(latest)
                    payload["share_method"] = "shares_full_latest"
                    payload["shares_full_date"] = str(series.dropna().index[-1])
            if shares is not None:
                out[company.ticker] = Snapshot(
                    ticker=company.ticker,
                    company_id=company.company_id,
                    asof_date=asof,
                    source_id=source_id,
                    shares_outstanding=shares,
                    market_cap=market_cap,
                    currency=currency,
                    source_timestamp=utc_now(),
                    payload=payload,
                )
        except Exception as exc:
            LOGGER.debug("yfinance snapshot failed for %s: %s", company.ticker, exc)
        time.sleep(sleep_sec)
    return out


def fetch_yahoo_snapshots(
    session: requests.Session,
    companies: list[Company],
    *,
    policy: dict[str, Any],
    asof: str,
) -> dict[str, Snapshot]:
    source_id = str(cfg_get(policy, "fallback_source_id", "yahoo_finance_backup") or "yahoo_finance_backup")
    url = str(cfg_get(policy, "yahoo_quote_url_template", "https://query1.finance.yahoo.com/v7/finance/quote"))
    timeout = float(cfg_get(policy, "http_timeout_sec", 30.0))
    batch_size = max(1, int(cfg_get(policy, "yahoo_batch_size", 50)))
    out: dict[str, Snapshot] = {}
    for start in range(0, len(companies), batch_size):
        batch = companies[start : start + batch_size]
        symbols = ",".join(yahoo_symbol(company.ticker) for company in batch)
        response = session.get(url, params={"symbols": symbols}, timeout=timeout)
        response.raise_for_status()
        payload = response.json()
        by_symbol = {
            normalize_ticker(str(item.get("symbol") or "").replace("-", ".")): item
            for item in payload.get("quoteResponse", {}).get("result", [])
            if isinstance(item, dict)
        }
        for company in batch:
            item = by_symbol.get(company.ticker)
            if not item:
                continue
            shares = normalize_share_count(item.get("sharesOutstanding") or item.get("shares_outstanding"))
            market_cap = to_float(item.get("marketCap") or item.get("market_cap"))
            price = to_float(
                item.get("regularMarketPrice")
                or item.get("postMarketPrice")
                or item.get("preMarketPrice")
                or item.get("regularMarketPreviousClose")
            )
            shares = shares or derive_shares_from_market_cap(market_cap, price)
            if shares is None:
                continue
            out[company.ticker] = Snapshot(
                ticker=company.ticker,
                company_id=company.company_id,
                asof_date=asof,
                source_id=source_id,
                shares_outstanding=shares,
                market_cap=market_cap,
                currency=str(item.get("currency") or company.currency or "USD"),
                source_timestamp=utc_now(),
                payload={"quote": item},
            )
        time.sleep(float(cfg_get(policy, "request_sleep_sec", 0.20)))
    return out


def upsert_snapshots(conn: Any, snapshots: list[Snapshot]) -> int:
    if not snapshots:
        return 0
    now = utc_now()
    conn.executemany(
        """
        INSERT INTO fact_market_snapshot(
            ticker, asof_date, source_id, shares_outstanding, market_cap, currency,
            source_timestamp, payload_json, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ticker, asof_date, source_id) DO UPDATE SET
            shares_outstanding = excluded.shares_outstanding,
            market_cap = excluded.market_cap,
            currency = excluded.currency,
            source_timestamp = excluded.source_timestamp,
            payload_json = excluded.payload_json,
            updated_at = excluded.updated_at
        """,
        [
            (
                item.ticker,
                item.asof_date,
                item.source_id,
                item.shares_outstanding,
                item.market_cap,
                item.currency,
                item.source_timestamp,
                json.dumps(item.payload, ensure_ascii=True, sort_keys=True),
                now,
                now,
            )
            for item in snapshots
        ],
    )
    return len(snapshots)


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows([{field: row.get(field, "") for field in FIELDNAMES} for row in rows])


def main() -> None:
    configure_utc_logging()
    logging.getLogger("ib_insync.wrapper").setLevel(logging.WARNING)
    logging.getLogger("ib_insync.client").setLevel(logging.WARNING)
    logging.getLogger("ib_insync.ib").setLevel(logging.WARNING)
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    policy = cfg_get(config, "market_snapshot_ingestion", {}) or {}
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    output_csv = (
        args.output_csv.expanduser().resolve()
        if args.output_csv
        else resolve_path(
            cfg_get(policy, "output_csv", "../output/med_devices_reports/med_device_market_snapshot_coverage.csv"),
            base_dir=base_dir,
        )
    )
    ticker_filter = {normalize_ticker(value) for value in str(args.tickers or "").split(",") if normalize_ticker(value)}

    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        init_db(conn)
        upsert_source_registry(conn, load_source_registry(SOURCE_REGISTRY))
        asof = args.asof.strip() or latest_market_asof(conn)
        if parse_date(asof) is None:
            raise ValueError(f"Invalid as-of date: {asof}")
        companies = load_companies(conn, ticker_filter=ticker_filter, max_tickers=int(args.max_tickers))
        run_id = start_run(conn, run_type="sync_med_device_market_snapshots", input_path=config_path)
        rows: list[dict[str, Any]] = []
        snapshots: list[Snapshot] = []
        try:
            ib: Any = None
            if not args.skip_ib:
                try:
                    from ib_insync import IB  # type: ignore[reportMissingModuleSource]

                    ib = IB()
                    ib.connect(
                        str(cfg_get(policy, "ib_host", "127.0.0.1")),
                        int(cfg_get(policy, "ib_port", 7497)),
                        clientId=int(cfg_get(policy, "ib_client_id", 7731)),
                    )
                except Exception as exc:
                    LOGGER.warning("IB market snapshot source unavailable; falling back to Yahoo/SEC: %s", exc)
                    ib = None
            pending_yahoo: list[Company] = []
            ib_consecutive_misses = 0
            ib_max_consecutive_misses = max(1, int(cfg_get(policy, "ib_max_consecutive_misses", 5)))
            for company in companies:
                snapshot = None
                status = "missing"
                review_reason = "no_market_share_snapshot"
                if ib is not None:
                    try:
                        snapshot = fetch_ib_snapshot(ib, company, policy=policy, asof=asof)
                    except Exception as exc:
                        review_reason = f"ib_failed:{type(exc).__name__}"
                    if snapshot is not None:
                        ib_consecutive_misses = 0
                        status = "success"
                        review_reason = "ib_market_data"
                    else:
                        ib_consecutive_misses += 1
                        pending_yahoo.append(company)
                        if ib_consecutive_misses >= ib_max_consecutive_misses:
                            LOGGER.warning(
                                "IB market snapshot source produced %d consecutive misses; using Yahoo/SEC fallback for remaining tickers.",
                                ib_consecutive_misses,
                            )
                            try:
                                ib.disconnect()
                            except Exception:
                                LOGGER.debug("Ignoring IB disconnect error", exc_info=True)
                            ib = None
                else:
                    pending_yahoo.append(company)
                if snapshot is not None:
                    snapshots.append(snapshot)
                    rows.append(
                        {
                            "ticker": company.ticker,
                            "company_id": company.company_id,
                            "company_name": company.company_name,
                            "asof_date": asof,
                            "status": status,
                            "source_id": snapshot.source_id,
                            "shares_outstanding": snapshot.shares_outstanding,
                            "market_cap": snapshot.market_cap,
                            "currency": snapshot.currency,
                            "review_reason": review_reason,
                        }
                    )
            if ib is not None:
                try:
                    ib.disconnect()
                except Exception:
                    LOGGER.debug("Ignoring IB disconnect error", exc_info=True)
            if pending_yahoo and not args.skip_yahoo:
                session = requests.Session()
                session.headers.update(
                    {
                        "User-Agent": expand_env_vars(cfg_get(policy, "user_agent", "StagingMedDeviceScalper/1.0")),
                        "Accept": "application/json,text/plain,*/*",
                    }
                )
                try:
                    yahoo = fetch_yfinance_snapshots(pending_yahoo, policy=policy, asof=asof)
                except Exception as exc:
                    LOGGER.warning("yfinance market snapshot source unavailable; trying raw Yahoo quote endpoint: %s", exc)
                    yahoo = {}
                pending_raw_yahoo = [company for company in pending_yahoo if company.ticker not in yahoo]
                try:
                    raw_yahoo = fetch_yahoo_snapshots(session, pending_raw_yahoo, policy=policy, asof=asof)
                    yahoo.update(raw_yahoo)
                except Exception as exc:
                    LOGGER.warning("Yahoo market snapshot source unavailable; falling back to SEC: %s", exc)
                for company in pending_yahoo:
                    snapshot = yahoo.get(company.ticker)
                    if snapshot is None:
                        rows.append(
                            {
                                "ticker": company.ticker,
                                "company_id": company.company_id,
                                "company_name": company.company_name,
                                "asof_date": asof,
                                "status": "fallback_to_sec",
                                "review_reason": "ib_yahoo_missing",
                            }
                        )
                        continue
                    snapshots.append(snapshot)
                    rows.append(
                        {
                            "ticker": company.ticker,
                            "company_id": company.company_id,
                            "company_name": company.company_name,
                            "asof_date": asof,
                            "status": "success",
                            "source_id": snapshot.source_id,
                            "shares_outstanding": snapshot.shares_outstanding,
                            "market_cap": snapshot.market_cap,
                            "currency": snapshot.currency,
                            "review_reason": "yahoo_fallback",
                        }
                    )
            elif pending_yahoo:
                for company in pending_yahoo:
                    rows.append(
                        {
                            "ticker": company.ticker,
                            "company_id": company.company_id,
                            "company_name": company.company_name,
                            "asof_date": asof,
                            "status": "fallback_to_sec",
                            "review_reason": "ib_missing_yahoo_skipped",
                        }
                    )
            upserted = upsert_snapshots(conn, snapshots)
            write_csv(output_csv, rows)
            failures = sum(1 for row in rows if row.get("status") != "success")
            status = "success" if failures == 0 or args.allow_partial else "failed"
            message = f"asof={asof} rows={len(rows)} snapshots={upserted} failures={failures} output={output_csv}"
            finish_run(conn, run_id=run_id, status=status, row_count=upserted, message=message)
            if status == "failed":
                raise RuntimeError(message)
            LOGGER.info("Market snapshot sync complete: %s", message)
        except BaseException as exc:
            finish_run(conn, run_id=run_id, status="failed", row_count=0, message=f"{type(exc).__name__}: {exc}")
            raise


if __name__ == "__main__":
    main()
