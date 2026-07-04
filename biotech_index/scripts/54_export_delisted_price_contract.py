#!/usr/bin/env python3
"""Publish the biotech delisted price + delisting-events contract for the portfolio layer.

Implements portfolio_layer/docs/delisted_price_export_contract.md: the portfolio layer never reads
a sector database, so the Norgate total-return history imported by script 49 is dumped to flat CSVs
under the report tree. Rows are keyed by the SUFFIXED calibration ticker exactly as it appears in
biotech_daily_scores.csv (e.g. IMMU-202010) — those symbols are unfetchable from Yahoo by
construction, which makes this export the sole price source for delisted names in the Stage 11
survivorship panel.

Outputs (globbed by portfolio_layer survivorship_panel config):
  output/biotech_index_reports/market_data/biotech_delisted_price_export.csv
      ticker, date, adjclose, close, volume, source_symbol
  output/biotech_index_reports/market_data/biotech_delisting_events.csv
      ticker, delist_date, delist_reason, terminal_value

adjclose is the Norgate total-return adjusted close (split + dividend adjusted), matching the
panel's yahoo_adjclose_div_split adjustment policy. delist_date is the final trading day
(price_end_date). Only norgate total-return bars are exported so no mixed-adjustment series can
leak into the panel.
"""
from __future__ import annotations

import argparse
import csv
import logging
import sqlite3
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from biotech_index.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402

LOGGER = logging.getLogger("export_delisted_price_contract")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
PRICE_FIELDS = ["ticker", "date", "adjclose", "close", "volume", "source_symbol"]
EVENT_FIELDS = ["ticker", "delist_date", "delist_reason", "terminal_value"]
NORGATE_SOURCE = "norgate_us_equities_total_return"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export the delisted price/events contract CSVs.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=None,
                        help="Default: <reports>/market_data next to the existing Norgate import report.")
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


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = (args.db.expanduser().resolve() if args.db
               else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir))
    if not Path(db_path).exists():
        LOGGER.error("biotech database not found: %s", db_path)
        return 1
    reports_dir = resolve_path(
        cfg_get(config, "paths.reports_dir", "../output/biotech_index_reports"), base_dir=base_dir
    )
    out_dir = args.output_dir.expanduser().resolve() if args.output_dir else Path(reports_dir) / "market_data"
    price_path = out_dir / "biotech_delisted_price_export.csv"
    events_path = out_dir / "biotech_delisting_events.csv"

    conn = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        universe = conn.execute(
            """
            SELECT ticker, calibration_company_ticker, norgate_symbol, price_end_date,
                   terminal_date, recovery_type, equity_recovery
            FROM delisted_calibration_universe
            ORDER BY calibration_company_ticker
            """
        ).fetchall()
        if not universe:
            LOGGER.error("delisted_calibration_universe is empty; run scripts 48/49/52 first")
            return 1

        price_rows: list[dict] = []
        event_rows: list[dict] = []
        missing_bars: list[str] = []
        for u in universe:
            contract_ticker = str(u["calibration_company_ticker"] or "").strip().upper()
            bars_key = str(u["ticker"] or "").strip().upper()
            end_date = str(u["price_end_date"] or "").strip()
            if not contract_ticker or not bars_key:
                continue
            params = [bars_key, NORGATE_SOURCE]
            date_sql = ""
            if args.min_date:
                date_sql += " AND bar_date >= ?"
                params.append(args.min_date)
            if end_date:
                date_sql += " AND bar_date <= ?"
                params.append(end_date)
            bars = conn.execute(
                f"""
                SELECT bar_date, adj_close, COALESCE(raw_close, close) AS close_px, volume
                FROM market_bars_daily
                WHERE ticker = ? AND source = ? AND adj_close IS NOT NULL AND adj_close > 0
                {date_sql}
                ORDER BY bar_date
                """,
                params,
            ).fetchall()
            if not bars:
                missing_bars.append(contract_ticker)
                continue
            for b in bars:
                price_rows.append({
                    "ticker": contract_ticker,
                    "date": str(b["bar_date"])[:10],
                    "adjclose": round(float(b["adj_close"]), 6),
                    "close": round(float(b["close_px"]), 6) if b["close_px"] is not None else "",
                    "volume": int(b["volume"]) if b["volume"] is not None else "",
                    "source_symbol": str(u["norgate_symbol"] or bars_key),
                })
            delist_date = end_date or str(bars[-1]["bar_date"])[:10]
            recovery = str(u["recovery_type"] or "").strip()
            terminal = u["equity_recovery"]
            event_rows.append({
                "ticker": contract_ticker,
                "delist_date": delist_date,
                "delist_reason": recovery or "delisted",
                "terminal_value": round(float(terminal), 6) if terminal is not None else "",
            })
    finally:
        conn.close()

    write_csv(price_path, PRICE_FIELDS, price_rows)
    write_csv(events_path, EVENT_FIELDS, event_rows)
    LOGGER.info("Delisted price export: %d bars for %d names -> %s", len(price_rows), len(event_rows), price_path)
    LOGGER.info("Delisting events: %d -> %s", len(event_rows), events_path)
    if missing_bars:
        LOGGER.warning("Universe names WITHOUT norgate bars (not exported): %s", missing_bars)
    return 0 if event_rows and not missing_bars else (0 if event_rows else 1)


if __name__ == "__main__":
    raise SystemExit(main())
