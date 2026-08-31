#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from med_devices.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from med_devices.core.db import connect, finish_run, init_db, start_run, utc_now  # noqa: E402
from med_devices.core.logging_utils import configure_utc_logging  # noqa: E402
from med_devices.core.text_norm import normalize_ticker  # noqa: E402


LOGGER = logging.getLogger("sync_med_device_ibkr_borrow")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
SOURCE_FIELDS = [
    "ticker",
    "asof_date",
    "source_id",
    "company_id",
    "shortable_status",
    "shortable_shares",
    "borrow_fee_rate",
    "source_timestamp",
]
REQUIRED_IBKR_BORROW_GENERIC_TICKS = ("236", "499")
SUPPORTED_IBKR_BORROW_GENERIC_TICKS = frozenset(REQUIRED_IBKR_BORROW_GENERIC_TICKS)
DEFAULT_IBKR_MARKET_DATA_BATCH_SIZE = 75
MAX_IBKR_MARKET_DATA_BATCH_SIZE = 90


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync med-device IBKR borrow availability snapshots.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", default="")
    parser.add_argument("--input-csv", type=Path, default=None, help="Optional normalized CSV import instead of live IBKR.")
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--tickers", default="")
    parser.add_argument("--max-tickers", type=int, default=0)
    parser.add_argument("--use-ib", action="store_true", help="Fetch live snapshots from TWS/IB Gateway.")
    return parser.parse_args()


def to_float(raw: object) -> float | None:
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def borrow_generic_tick_list(config: dict[str, Any]) -> str:
    raw_ticks = str(cfg_get(config, "ibkr_borrow_ingestion.generic_tick_list", "236,499") or "")
    ticks: list[str] = []
    unsupported: list[str] = []
    for raw_tick in raw_ticks.replace(";", ",").split(","):
        tick = raw_tick.strip()
        if tick and tick not in SUPPORTED_IBKR_BORROW_GENERIC_TICKS:
            unsupported.append(tick)
            continue
        if tick and tick not in ticks:
            ticks.append(tick)
    if unsupported:
        LOGGER.warning(
            "Ignoring unsupported IBKR borrow generic ticks: %s",
            ",".join(dict.fromkeys(unsupported)),
        )
    missing = [tick for tick in REQUIRED_IBKR_BORROW_GENERIC_TICKS if tick not in ticks]
    if missing:
        LOGGER.warning(
            "ibkr_borrow_ingestion.generic_tick_list missing required borrow ticks %s; adding them",
            ",".join(missing),
        )
        ticks.extend(missing)
    return ",".join(ticks)


def ibkr_market_data_batch_size(config: dict[str, Any]) -> int:
    raw = cfg_get(
        config,
        "ibkr_borrow_ingestion.batch_size",
        DEFAULT_IBKR_MARKET_DATA_BATCH_SIZE,
    )
    try:
        batch_size = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"ibkr_borrow_ingestion.batch_size must be an integer, got {raw!r}") from exc
    if not 1 <= batch_size <= MAX_IBKR_MARKET_DATA_BATCH_SIZE:
        raise ValueError(
            "ibkr_borrow_ingestion.batch_size must be between 1 and "
            f"{MAX_IBKR_MARKET_DATA_BATCH_SIZE}, got {batch_size}"
        )
    return batch_size


def ensure_source(conn: Any, source_id: str) -> None:
    now = utc_now()
    conn.execute(
        """
        INSERT INTO source_registry(
            source_id, stage, source_name, source_type, base_url,
            authentication_required, free_key_required, priority, status, created_at, updated_at
        )
        VALUES (?, 'stage_1', 'Interactive Brokers shortable shares and borrow fee', 'broker_api', '127.0.0.1:7497',
                1, 0, 61, 'planned', ?, ?)
        ON CONFLICT(source_id) DO UPDATE SET updated_at = excluded.updated_at
        """,
        (source_id, now, now),
    )


def load_companies(conn: Any, *, tickers: set[str], max_tickers: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT company_id, ticker, company_name
        FROM dim_company
        WHERE is_active = 1
        ORDER BY ticker
        """
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        ticker = normalize_ticker(item.get("ticker"))
        if tickers and ticker not in tickers:
            continue
        item["ticker"] = ticker
        out.append(item)
        if max_tickers > 0 and len(out) >= max_tickers:
            break
    return out


def load_csv_rows(path: Path, *, asof: str, source_id: str, company_by_ticker: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for raw in reader:
            ticker = normalize_ticker(raw.get("ticker") or raw.get("symbol"))
            if not ticker or ticker not in company_by_ticker:
                continue
            company = company_by_ticker[ticker]
            rows.append(
                {
                    "ticker": ticker,
                    "asof_date": str(raw.get("asof_date") or raw.get("date") or asof),
                    "source_id": str(raw.get("source_id") or source_id),
                    "company_id": int(company["company_id"]),
                    "shortable_status": to_float(raw.get("shortable_status") or raw.get("shortable")),
                    "shortable_shares": to_float(raw.get("shortable_shares") or raw.get("available_shares")),
                    "borrow_fee_rate": to_float(raw.get("borrow_fee_rate") or raw.get("fee_rate") or raw.get("rebate_rate")),
                    "source_timestamp": str(raw.get("source_timestamp") or raw.get("timestamp") or ""),
                    "payload_json": json.dumps(raw, sort_keys=True, ensure_ascii=True),
                }
            )
    return rows


def _ib_row(
    company: dict[str, Any],
    *,
    asof: str,
    source_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    fee_rate = to_float(payload.get("feeRate"))
    rebate_rate = to_float(payload.get("rebateRate"))
    return {
        "ticker": normalize_ticker(company["ticker"]),
        "asof_date": asof,
        "source_id": source_id,
        "company_id": int(company["company_id"]),
        "shortable_status": to_float(payload.get("shortable")),
        "shortable_shares": to_float(payload.get("shortableShares")),
        "borrow_fee_rate": fee_rate if fee_rate is not None else rebate_rate,
        "source_timestamp": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "payload_json": json.dumps(payload, sort_keys=True, ensure_ascii=True),
    }


def fetch_ib_rows(
    companies: list[dict[str, Any]],
    *,
    asof: str,
    source_id: str,
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    try:
        from ib_insync import IB, Stock  # type: ignore
    except ImportError as exc:
        raise RuntimeError("ib_insync is required for --use-ib borrow snapshots") from exc

    ib = IB()
    host = str(cfg_get(config, "ibkr_borrow_ingestion.host", "127.0.0.1"))
    port = int(cfg_get(config, "ibkr_borrow_ingestion.port", 7497))
    client_id = int(cfg_get(config, "ibkr_borrow_ingestion.client_id", 7741))
    timeout_sec = float(cfg_get(config, "ibkr_borrow_ingestion.snapshot_timeout_sec", 8.0))
    sleep_sec = float(cfg_get(config, "ibkr_borrow_ingestion.sleep_sec", 0.25))
    batch_size = ibkr_market_data_batch_size(config)
    generic_ticks = borrow_generic_tick_list(config)
    exchange = str(cfg_get(config, "ibkr_borrow_ingestion.default_exchange", "SMART"))
    currency = str(cfg_get(config, "ibkr_borrow_ingestion.default_currency", "USD"))
    ib.connect(
        host,
        port,
        clientId=client_id,
        timeout=float(cfg_get(config, "ibkr_borrow_ingestion.connect_timeout_sec", 15.0)),
        readonly=True,
    )
    rows: list[dict[str, Any]] = []
    try:
        for start in range(0, len(companies), batch_size):
            batch = companies[start : start + batch_size]
            requested_contracts = [
                Stock(normalize_ticker(company["ticker"]), exchange, currency)
                for company in batch
            ]
            qualified = list(ib.qualifyContracts(*requested_contracts) or [])
            qualified_by_ticker = {
                normalize_ticker(getattr(contract, "symbol", "")): contract
                for contract in qualified
                if normalize_ticker(getattr(contract, "symbol", ""))
            }
            active: list[tuple[dict[str, Any], Any, Any]] = []
            try:
                for company in batch:
                    ticker = normalize_ticker(company["ticker"])
                    contract = qualified_by_ticker.get(ticker)
                    if contract is None:
                        rows.append(
                            _ib_row(
                                company,
                                asof=asof,
                                source_id=source_id,
                                payload={
                                    "ticker": ticker,
                                    "genericTickList": generic_ticks,
                                    "request_error": "contract_not_qualified",
                                },
                            )
                        )
                        continue
                    try:
                        snapshot = ib.reqMktData(
                            contract,
                            genericTickList=generic_ticks,
                            snapshot=False,
                            regulatorySnapshot=False,
                        )
                    except Exception as exc:  # noqa: BLE001 - preserve per-name coverage
                        rows.append(
                            _ib_row(
                                company,
                                asof=asof,
                                source_id=source_id,
                                payload={
                                    "ticker": ticker,
                                    "genericTickList": generic_ticks,
                                    "request_error": f"{type(exc).__name__}: {exc}",
                                },
                            )
                        )
                        try:
                            ib.cancelMktData(contract)
                        except Exception:  # noqa: BLE001 - disconnect remains final cleanup
                            pass
                        continue
                    active.append((company, contract, snapshot))

                if active:
                    ib.sleep(timeout_sec)
                for company, _contract, snapshot in active:
                    ticker = normalize_ticker(company["ticker"])
                    rows.append(
                        _ib_row(
                            company,
                            asof=asof,
                            source_id=source_id,
                            payload={
                                "ticker": ticker,
                                "shortable": getattr(snapshot, "shortable", None),
                                "shortableShares": getattr(snapshot, "shortableShares", None),
                                "feeRate": getattr(snapshot, "feeRate", None),
                                "rebateRate": getattr(snapshot, "rebateRate", None),
                                "ticks": [str(tick) for tick in getattr(snapshot, "ticks", [])],
                                "genericTickList": generic_ticks,
                            },
                        )
                    )
            finally:
                for _company, contract, _snapshot in active:
                    try:
                        ib.cancelMktData(contract)
                    except Exception as exc:  # noqa: BLE001 - disconnect closes residual subscriptions
                        LOGGER.warning("Unable to cancel IBKR borrow subscription: %s", exc)
            if start + batch_size < len(companies) and sleep_sec > 0:
                ib.sleep(sleep_sec)
    finally:
        ib.disconnect()
    return sorted(rows, key=lambda row: str(row["ticker"]))


def upsert_rows(conn: Any, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    now = utc_now()
    conn.executemany(
        """
        INSERT INTO fact_ibkr_borrow_snapshot(
            ticker, asof_date, source_id, company_id, shortable_status, shortable_shares,
            borrow_fee_rate, source_timestamp, payload_json, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ticker, asof_date, source_id) DO UPDATE SET
            company_id = excluded.company_id,
            shortable_status = excluded.shortable_status,
            shortable_shares = excluded.shortable_shares,
            borrow_fee_rate = excluded.borrow_fee_rate,
            source_timestamp = excluded.source_timestamp,
            payload_json = excluded.payload_json,
            updated_at = excluded.updated_at
        """,
        [
            (
                row["ticker"],
                row["asof_date"],
                row["source_id"],
                row["company_id"],
                row.get("shortable_status"),
                row.get("shortable_shares"),
                row.get("borrow_fee_rate"),
                row.get("source_timestamp") or "",
                row.get("payload_json") or "{}",
                now,
                now,
            )
            for row in rows
        ],
    )
    return len(rows)


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=SOURCE_FIELDS, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    configure_utc_logging()
    logging.getLogger("ib_insync").setLevel(logging.WARNING)
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    asof = args.asof.strip() or date.today().isoformat()
    try:
        date.fromisoformat(asof)
    except ValueError as exc:
        raise ValueError(f"--asof must be YYYY-MM-DD, got {asof!r}") from exc
    source_id = str(cfg_get(config, "ibkr_borrow_ingestion.source_id", "ibkr_borrow"))
    output_csv = (
        args.output_csv.expanduser().resolve()
        if args.output_csv
        else resolve_path(cfg_get(config, "ibkr_borrow_ingestion.output_csv"), base_dir=base_dir)
    )
    ticker_filter = {normalize_ticker(value) for value in str(args.tickers or "").split(",") if normalize_ticker(value)}
    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        init_db(conn)
        ensure_source(conn, source_id)
        run_id = start_run(conn, run_type="sync_med_device_ibkr_borrow", input_path=config_path)
        try:
            companies = load_companies(conn, tickers=ticker_filter, max_tickers=int(args.max_tickers))
            company_by_ticker = {company["ticker"]: company for company in companies}
            if args.input_csv is not None:
                rows = load_csv_rows(args.input_csv.expanduser().resolve(), asof=asof, source_id=source_id, company_by_ticker=company_by_ticker)
            elif args.use_ib or bool(cfg_get(config, "ibkr_borrow_ingestion.enabled", False)):
                rows = fetch_ib_rows(companies, asof=asof, source_id=source_id, config=config)
            else:
                rows = []
                LOGGER.info("IBKR borrow ingestion disabled; no rows fetched.")
            count = upsert_rows(conn, rows)
            write_csv(output_csv, rows)
            finish_run(conn, run_id=run_id, status="success", row_count=count, message=f"asof={asof} rows={count}")
            LOGGER.info("IBKR borrow sync complete: asof=%s rows=%d", asof, count)
        except BaseException as exc:
            finish_run(conn, run_id=run_id, status="failed", row_count=0, message=f"{type(exc).__name__}: {exc}")
            raise


if __name__ == "__main__":
    main()
