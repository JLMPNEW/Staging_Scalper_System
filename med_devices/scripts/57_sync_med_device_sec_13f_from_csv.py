#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import sys
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


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
FIELDNAMES = [
    "accession_nodash",
    "report_date",
    "source_id",
    "manager_cik",
    "manager_name",
    "ticker",
    "company_id",
    "cusip",
    "shares",
    "market_value_usd",
    "manager_count",
    "institutional_ownership_pct",
    "institutional_ownership_delta_pct",
    "put_call",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Load normalized SEC 13F holdings or aggregate exports for med-device tickers.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--input-csv", type=Path, default=None)
    parser.add_argument("--output-csv", type=Path, default=None)
    return parser.parse_args()


def to_float(raw: object) -> float | None:
    try:
        value = float(str(raw).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return value if value == value else None


def ensure_source(conn: Any, source_id: str) -> None:
    now = utc_now()
    conn.execute(
        """
        INSERT INTO source_registry(
            source_id, stage, source_name, source_type, base_url,
            authentication_required, free_key_required, priority, status, created_at, updated_at
        )
        VALUES (?, 'stage_1', 'SEC Form 13F institutional holdings', 'csv_or_edgar',
                'https://www.sec.gov/edgar/search/', 0, 0, 63, 'planned', ?, ?)
        ON CONFLICT(source_id) DO UPDATE SET updated_at = excluded.updated_at
        """,
        (source_id, now, now),
    )


def load_company_map(conn: Any) -> dict[str, dict[str, Any]]:
    rows = conn.execute("SELECT company_id, ticker FROM dim_company WHERE is_active = 1").fetchall()
    return {normalize_ticker(row["ticker"]): dict(row) for row in rows}


def load_rows(path: Path, *, source_id: str, company_by_ticker: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for raw in reader:
            ticker = normalize_ticker(raw.get("ticker") or raw.get("symbol") or raw.get("issuer_ticker"))
            report_date = str(raw.get("report_date") or raw.get("period_date") or raw.get("asof_date") or "").strip()
            if not ticker or ticker not in company_by_ticker or not report_date:
                continue
            accession = str(raw.get("accession_nodash") or raw.get("accession_number") or "").replace("-", "").strip()
            if not accession:
                accession = f"aggregate_{report_date.replace('-', '')}_{ticker}_{source_id}"
            company = company_by_ticker[ticker]
            rows.append(
                {
                    "accession_nodash": accession,
                    "report_date": report_date,
                    "source_id": str(raw.get("source_id") or source_id),
                    "manager_cik": str(raw.get("manager_cik") or raw.get("cik") or ""),
                    "manager_name": str(raw.get("manager_name") or raw.get("institution_name") or ""),
                    "ticker": ticker,
                    "company_id": int(company["company_id"]),
                    "cusip": str(raw.get("cusip") or ""),
                    "shares": to_float(raw.get("shares") or raw.get("share_count") or raw.get("institutional_share_count")),
                    "market_value_usd": to_float(raw.get("market_value_usd") or raw.get("value_usd") or raw.get("value")),
                    "manager_count": to_float(raw.get("manager_count") or raw.get("institutional_manager_count")),
                    "institutional_ownership_pct": to_float(raw.get("institutional_ownership_pct") or raw.get("ownership_pct")),
                    "institutional_ownership_delta_pct": to_float(
                        raw.get("institutional_ownership_delta_pct") or raw.get("ownership_delta_pct")
                    ),
                    "put_call": str(raw.get("put_call") or ""),
                    "investment_discretion": str(raw.get("investment_discretion") or ""),
                    "voting_authority_json": str(raw.get("voting_authority_json") or ""),
                    "payload_json": json.dumps(raw, sort_keys=True, ensure_ascii=True),
                }
            )
    return rows


def upsert_rows(conn: Any, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    now = utc_now()
    conn.executemany(
        """
        INSERT INTO fact_sec_13f_holding(
            accession_nodash, report_date, source_id, manager_cik, manager_name, ticker,
            company_id, cusip, shares, market_value_usd, manager_count, institutional_ownership_pct,
            institutional_ownership_delta_pct, put_call, investment_discretion, voting_authority_json,
            payload_json, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT DO UPDATE SET
            report_date = excluded.report_date,
            manager_cik = excluded.manager_cik,
            manager_name = excluded.manager_name,
            company_id = excluded.company_id,
            shares = excluded.shares,
            market_value_usd = excluded.market_value_usd,
            manager_count = excluded.manager_count,
            institutional_ownership_pct = excluded.institutional_ownership_pct,
            institutional_ownership_delta_pct = excluded.institutional_ownership_delta_pct,
            investment_discretion = excluded.investment_discretion,
            voting_authority_json = excluded.voting_authority_json,
            payload_json = excluded.payload_json,
            updated_at = excluded.updated_at
        """,
        [
            (
                row["accession_nodash"],
                row["report_date"],
                row["source_id"],
                row.get("manager_cik", ""),
                row.get("manager_name", ""),
                row["ticker"],
                row["company_id"],
                row.get("cusip", ""),
                row.get("shares"),
                row.get("market_value_usd"),
                row.get("manager_count"),
                row.get("institutional_ownership_pct"),
                row.get("institutional_ownership_delta_pct"),
                row.get("put_call", ""),
                row.get("investment_discretion", ""),
                row.get("voting_authority_json", ""),
                row.get("payload_json", "{}"),
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
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    input_csv_raw = args.input_csv or (Path(str(cfg_get(config, "sec_13f_ingestion.input_csv", ""))) if cfg_get(config, "sec_13f_ingestion.input_csv", "") else None)
    if input_csv_raw is None:
        raise ValueError("Provide --input-csv or sec_13f_ingestion.input_csv")
    input_csv = input_csv_raw.expanduser().resolve()
    source_id = str(cfg_get(config, "sec_13f_ingestion.source_id", "sec_13f_edgar"))
    output_csv = (
        args.output_csv.expanduser().resolve()
        if args.output_csv
        else resolve_path(cfg_get(config, "sec_13f_ingestion.output_csv"), base_dir=base_dir)
    )
    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        init_db(conn)
        ensure_source(conn, source_id)
        run_id = start_run(conn, run_type="sync_med_device_sec_13f_from_csv", input_path=input_csv)
        try:
            rows = load_rows(input_csv, source_id=source_id, company_by_ticker=load_company_map(conn))
            count = upsert_rows(conn, rows)
            write_csv(output_csv, rows)
            finish_run(conn, run_id=run_id, status="success", row_count=count, message=f"input={input_csv} rows={count}")
        except BaseException as exc:
            finish_run(conn, run_id=run_id, status="failed", row_count=0, message=f"{type(exc).__name__}: {exc}")
            raise


if __name__ == "__main__":
    main()
