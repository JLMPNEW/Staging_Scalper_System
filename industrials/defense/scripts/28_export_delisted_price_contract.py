#!/usr/bin/env python3
"""Publish the defense delisted price + delisting-events contract for the portfolio layer.

Implements portfolio_layer/docs/delisted_price_export_contract.md (defense twin of biotech
script 54 / med script 77 / technology script 21): the portfolio layer never reads a sector
database, so the point-in-time delisted membership (dim_universe_membership, model_family
defense, point_in_time_flag=1, membership_status delisted) and its adjusted price history
(fact_price_ohlcv) are dumped to flat CSVs. Ticker spellings are the membership keys by
construction — the same spellings scripts/17 publishes in the dated rank tables — including
disambiguated forms like DRS-DEL2008.

Outputs (globbed by portfolio_layer survivorship_panel config, output/*_reports/market_data):
  output/industrials_reports/market_data/defense_delisted_price_export.csv
      ticker, date, adjclose, close, volume, source_symbol
  output/industrials_reports/market_data/defense_delisting_events.csv
      ticker, delist_date, delist_reason, terminal_value

Bars prefer the Norgate total-return import (scripts/15) and fall back to Yahoo adjusted rows
per (ticker, bar_date). delist_date is each name's actual final bar (its last trading day).
"""
from __future__ import annotations

import argparse
import csv
import logging
import sqlite3
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402

LOGGER = logging.getLogger("export_defense_delisted_price_contract")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
MODEL_FAMILY = "defense"
PRICE_FIELDS = ["ticker", "date", "adjclose", "close", "volume", "source_symbol"]
EVENT_FIELDS = ["ticker", "delist_date", "delist_reason", "terminal_value"]
PREFERRED_SOURCES = ["norgate_us_equities_total_return", "yahoo_finance_adjusted"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export the defense delisted price/events contract CSVs.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Default: <project>/output/industrials_reports/market_data (matches the portfolio glob).",
    )
    parser.add_argument("--min-date", default="", help="Optional YYYY-MM-DD floor for exported bars.")
    return parser.parse_args()


def write_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(path)


def delist_reason_from(reason: str) -> str:
    """First annotated segments of the curated membership reason ('acquired;LMT;desc' -> 'acquired;LMT')."""
    parts = [p.strip() for p in str(reason or "").split(";") if p.strip()]
    keep = parts[:2] if len(parts) >= 2 and len(parts[1]) <= 10 else parts[:1]
    return ";".join(keep) or "delisted"


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = (
        args.db.expanduser().resolve()
        if args.db
        else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    )
    if not Path(db_path).exists():
        LOGGER.error("industrials database not found: %s", db_path)
        return 1
    out_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else PROJECT_ROOT / "output" / "industrials_reports" / "market_data"
    )
    price_path = out_dir / "defense_delisted_price_export.csv"
    events_path = out_dir / "defense_delisting_events.csv"

    source_rank = " ".join(f"WHEN ? THEN {rank}" for rank in range(len(PREFERRED_SOURCES)))
    conn = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        members = conn.execute(
            """
            SELECT ticker, end_date, reason
            FROM dim_universe_membership
            WHERE model_family = ?
              AND point_in_time_flag = 1
              AND membership_status = 'delisted'
            ORDER BY ticker
            """,
            (MODEL_FAMILY,),
        ).fetchall()
        if not members:
            LOGGER.error("No delisted point-in-time members found for model_family=%s", MODEL_FAMILY)
            return 1

        price_rows: list[dict] = []
        event_rows: list[dict] = []
        missing_bars: list[str] = []
        seen: set[str] = set()
        for m in members:
            ticker = str(m["ticker"] or "").strip().upper()
            if not ticker or ticker in seen:
                continue
            seen.add(ticker)
            params: list = [*PREFERRED_SOURCES, ticker, *PREFERRED_SOURCES]
            date_sql = ""
            if args.min_date:
                date_sql = " AND bar_date >= ?"
                params.append(args.min_date)
            bars = conn.execute(
                f"""
                WITH ranked AS (
                    SELECT bar_date, adj_close, close, volume, source_id,
                           ROW_NUMBER() OVER (
                               PARTITION BY bar_date
                               ORDER BY CASE source_id {source_rank} ELSE 99 END ASC
                           ) AS rn
                    FROM fact_price_ohlcv
                    WHERE UPPER(ticker) = ?
                      AND source_id IN ({",".join("?" for _ in PREFERRED_SOURCES)})
                      AND adj_close IS NOT NULL AND adj_close > 0
                      {date_sql}
                )
                SELECT bar_date, adj_close, close, volume, source_id
                FROM ranked WHERE rn = 1
                ORDER BY bar_date
                """,
                params,
            ).fetchall()
            if not bars:
                missing_bars.append(ticker)
                continue
            for b in bars:
                price_rows.append(
                    {
                        "ticker": ticker,
                        "date": str(b["bar_date"])[:10],
                        "adjclose": round(float(b["adj_close"]), 6),
                        "close": round(float(b["close"]), 6) if b["close"] is not None else "",
                        "volume": int(b["volume"]) if b["volume"] is not None else "",
                        "source_symbol": ticker,
                    }
                )
            event_rows.append(
                {
                    "ticker": ticker,
                    "delist_date": str(bars[-1]["bar_date"])[:10],  # actual final trading bar
                    "delist_reason": delist_reason_from(m["reason"]),
                    "terminal_value": "",
                }
            )
    finally:
        conn.close()

    write_csv(price_path, PRICE_FIELDS, price_rows)
    write_csv(events_path, EVENT_FIELDS, event_rows)
    LOGGER.info("Delisted price export: %d bars for %d names -> %s", len(price_rows), len(event_rows), price_path)
    LOGGER.info("Delisting events: %d -> %s", len(event_rows), events_path)
    if missing_bars:
        LOGGER.warning("Delisted members WITHOUT price bars (not exported): %s", missing_bars)
    return 0 if event_rows else 1


if __name__ == "__main__":
    raise SystemExit(main())
