#!/usr/bin/env python3
"""Publish the med-devices delisted price + delisting-events contract for the portfolio layer.

Implements portfolio_layer/docs/delisted_price_export_contract.md (med twin of biotech script 54):
the portfolio layer never reads a sector database, so the curated calibration-only historical
members (dim_universe_membership, basis=calibration_only_historical_delisted) and their adjusted
price history (fact_price_ohlcv) are dumped to flat CSVs under the report tree. Names like ASXC
whose symbols Yahoo no longer serves become coverable in the Stage 11 survivorship panel only
through this export.

Outputs (globbed by portfolio_layer survivorship_panel config):
  output/med_devices_reports/market_data/med_devices_delisted_price_export.csv
      ticker, date, adj_open, adj_high, adj_low, adjclose, close, volume, source_symbol
  output/med_devices_reports/market_data/med_devices_delisting_events.csv
      ticker, delist_date, delist_reason, terminal_value

delist_date is the name's actual final bar (its last trading day). Members whose series still
trades at the panel edge (policy-delisted, e.g. pending acquisitions) are exported with their
metadata; the survivorship panel classifies a live-at-edge series as active regardless.
Members with membership_status='removed_from_source' are skipped.
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

from med_devices.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402

LOGGER = logging.getLogger("export_med_delisted_price_contract")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
PRICE_FIELDS = [
    "ticker",
    "date",
    "adj_open",
    "adj_high",
    "adj_low",
    "adjclose",
    "close",
    "volume",
    "source_symbol",
]
EVENT_FIELDS = ["ticker", "delist_date", "delist_reason", "terminal_value"]
MEMBERSHIP_BASIS = "calibration_only_historical_delisted"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export the med-devices delisted price/events contract CSVs.")
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


def delist_reason_from(reason: str) -> str:
    """First two annotated segments of the curated reason ('acquired;JNJ;desc;...' -> 'acquired;JNJ')."""
    parts = [p.strip() for p in str(reason or "").split(";") if p.strip()]
    keep = parts[:2] if len(parts) >= 2 and len(parts[1]) <= 8 else parts[:1]
    return ";".join(keep) or "delisted"


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = (args.db.expanduser().resolve() if args.db
               else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir))
    if not Path(db_path).exists():
        LOGGER.error("med_devices database not found: %s", db_path)
        return 1
    reports_dir = resolve_path(
        cfg_get(config, "paths.output_dir", "../output/med_devices_reports"), base_dir=base_dir
    )
    out_dir = args.output_dir.expanduser().resolve() if args.output_dir else Path(reports_dir) / "market_data"
    price_path = out_dir / "med_devices_delisted_price_export.csv"
    events_path = out_dir / "med_devices_delisting_events.csv"

    conn = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        members = conn.execute(
            """
            SELECT m.ticker, m.end_date, m.reason
            FROM dim_universe_membership m
            WHERE m.membership_basis = ? AND m.membership_status = 'historical'
            ORDER BY m.ticker
            """,
            (MEMBERSHIP_BASIS,),
        ).fetchall()
        if not members:
            LOGGER.error("No %s members found; load the historical membership seed first (script 01b)", MEMBERSHIP_BASIS)
            return 1

        price_rows: list[dict] = []
        event_rows: list[dict] = []
        missing_bars: list[str] = []
        for m in members:
            ticker = str(m["ticker"] or "").strip().upper()
            if not ticker:
                continue
            params: list = [ticker]
            date_sql = ""
            if args.min_date:
                date_sql = " AND bar_date >= ?"
                params.append(args.min_date)
            bars = conn.execute(
                f"""
                SELECT bar_date, open, high, low, adj_close, close, volume
                FROM fact_price_ohlcv
                WHERE UPPER(ticker) = ? AND adj_close IS NOT NULL AND adj_close > 0
                {date_sql}
                ORDER BY bar_date
                """,
                params,
            ).fetchall()
            if not bars:
                missing_bars.append(ticker)
                continue
            for b in bars:
                close_px = float(b["close"]) if b["close"] is not None else 0.0
                factor = float(b["adj_close"]) / close_px if close_px > 0 else 0.0
                price_rows.append({
                    "ticker": ticker,
                    "date": str(b["bar_date"])[:10],
                    "adj_open": (
                        round(float(b["open"]) * factor, 6)
                        if b["open"] is not None and factor > 0
                        else ""
                    ),
                    "adj_high": (
                        round(float(b["high"]) * factor, 6)
                        if b["high"] is not None and factor > 0
                        else ""
                    ),
                    "adj_low": (
                        round(float(b["low"]) * factor, 6)
                        if b["low"] is not None and factor > 0
                        else ""
                    ),
                    "adjclose": round(float(b["adj_close"]), 6),
                    "close": round(float(b["close"]), 6) if b["close"] is not None else "",
                    "volume": int(b["volume"]) if b["volume"] is not None else "",
                    "source_symbol": ticker,
                })
            event_rows.append({
                "ticker": ticker,
                "delist_date": str(bars[-1]["bar_date"])[:10],  # actual final trading bar
                "delist_reason": delist_reason_from(m["reason"]),
                "terminal_value": "",
            })
    finally:
        conn.close()

    write_csv(price_path, PRICE_FIELDS, price_rows)
    write_csv(events_path, EVENT_FIELDS, event_rows)
    LOGGER.info("Delisted price export: %d bars for %d names -> %s", len(price_rows), len(event_rows), price_path)
    LOGGER.info("Delisting events: %d -> %s", len(event_rows), events_path)
    if missing_bars:
        LOGGER.warning("Historical members WITHOUT price bars (not exported): %s", missing_bars)
    return 0 if event_rows else 1


if __name__ == "__main__":
    raise SystemExit(main())
