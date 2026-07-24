#!/usr/bin/env python3
"""Publish transportation delisted prices/events for portfolio-layer survivorship tests."""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sqlite3
import sys
from pathlib import Path


TRANSPORTATION_ROOT = Path(__file__).resolve().parents[1]
INDUSTRIALS_ROOT = TRANSPORTATION_ROOT.parent
PROJECT_ROOT = INDUSTRIALS_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, family_config, load_yaml, resolve_path  # noqa: E402
from industrials.core.csv_utils import read_csv_flexible  # noqa: E402


LOGGER = logging.getLogger("export_transportation_delisted_price_contract")
DEFAULT_CONFIG = INDUSTRIALS_ROOT / "config.yaml"
MODEL_FAMILY = "transportation"
PRICE_FIELDS = ["ticker", "date", "adjclose", "close", "volume", "source_symbol"]
EVENT_FIELDS = ["ticker", "delist_date", "delist_reason", "terminal_value"]
PREFERRED_SOURCES = ["norgate_us_equities_total_return", "yahoo_finance_adjusted"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export transportation delisted price/event CSV contracts.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--min-date", default="")
    parser.add_argument("--allow-partial", action="store_true")
    return parser.parse_args()


def write_csv(path: Path, fields: list[str], rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    temp.replace(path)


def delist_reason(exit_type: object) -> str:
    return {
        "bankruptcy": "bankrupt",
        "strategic": "acquired",
        "pe_buyout": "acquired",
        "merger": "merged",
    }.get(str(exit_type or "").strip().lower(), "delisted")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    family = family_config(config, MODEL_FAMILY)
    universe = family.get("universe")
    market = family.get("market")
    if not isinstance(universe, dict) or not isinstance(market, dict):
        raise KeyError("Transportation universe and market configuration are required")
    db_path = (
        args.db.expanduser().resolve()
        if args.db
        else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    )
    if not db_path.exists():
        raise FileNotFoundError(f"Industrials database not found: {db_path}")
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else resolve_path(market["delisted_export_dir"], base_dir=base_dir)
    )
    mapping_path = resolve_path(universe["norgate_symbol_map_csv"], base_dir=base_dir)
    source_symbols = {
        str(row.get("internal_ticker") or "").strip().upper(): str(row.get("norgate_symbol") or "").strip()
        for row in read_csv_flexible(mapping_path)
        if str(row.get("calibration_usable_flag") or "").strip() == "1"
    }
    price_path = output_dir / "transportation_delisted_price_export.csv"
    event_path = output_dir / "transportation_delisting_events.csv"
    source_rank = " ".join(f"WHEN ? THEN {rank}" for rank in range(len(PREFERRED_SOURCES)))
    price_rows: list[dict[str, object]] = []
    event_rows: list[dict[str, object]] = []
    missing: list[str] = []
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        members = conn.execute(
            """
            SELECT m.ticker, m.end_date, s.exit_type, s.terminal_type
            FROM dim_universe_membership AS m
            JOIN dim_delisted_calibration_seed AS s
              ON s.model_family = m.model_family AND s.internal_ticker = m.ticker
            WHERE m.model_family = ?
              AND m.membership_source_id = ?
              AND m.membership_status = 'delisted'
              AND m.point_in_time_flag = 1
              AND m.end_date IS NOT NULL
            ORDER BY m.ticker
            """,
            (MODEL_FAMILY, str(universe["historical_membership_source_id"])),
        ).fetchall()
        if not members:
            raise ValueError("No resolved transportation delisted memberships are loaded")
        for member in members:
            ticker = str(member["ticker"] or "").strip().upper()
            params: list[object] = [*PREFERRED_SOURCES, ticker, *PREFERRED_SOURCES]
            date_filter = ""
            if args.min_date:
                date_filter = " AND bar_date >= ?"
                params.append(args.min_date)
            bars = conn.execute(
                f"""
                WITH ranked AS (
                    SELECT bar_date, adj_close, close, volume, source_id,
                           ROW_NUMBER() OVER (
                               PARTITION BY bar_date
                               ORDER BY CASE source_id {source_rank} ELSE 99 END
                           ) AS source_order
                    FROM fact_price_ohlcv
                    WHERE UPPER(ticker) = ?
                      AND source_id IN ({','.join('?' for _ in PREFERRED_SOURCES)})
                      AND adj_close IS NOT NULL AND adj_close > 0
                      {date_filter}
                )
                SELECT bar_date, adj_close, close, volume, source_id
                FROM ranked WHERE source_order = 1 ORDER BY bar_date
                """,
                params,
            ).fetchall()
            if not bars:
                missing.append(ticker)
                continue
            for bar in bars:
                price_rows.append(
                    {
                        "ticker": ticker,
                        "date": str(bar["bar_date"])[:10],
                        "adjclose": round(float(bar["adj_close"]), 8),
                        "close": round(float(bar["close"]), 8) if bar["close"] is not None else "",
                        "volume": int(bar["volume"]) if bar["volume"] is not None else "",
                        "source_symbol": source_symbols.get(ticker, ticker),
                    }
                )
            event_rows.append(
                {
                    "ticker": ticker,
                    "delist_date": str(bars[-1]["bar_date"])[:10],
                    "delist_reason": delist_reason(member["exit_type"]),
                    "terminal_value": "0" if str(member["terminal_type"]).lower() == "wipeout" else "",
                }
            )
    finally:
        conn.close()
    write_csv(price_path, PRICE_FIELDS, price_rows)
    write_csv(event_path, EVENT_FIELDS, event_rows)
    status = "PASS" if event_rows and (not missing or args.allow_partial) else "FAIL"
    summary = {
        "acceptance": status,
        "resolved_members": len(event_rows) + len(missing),
        "exported_members": len(event_rows),
        "price_rows": len(price_rows),
        "missing_price_tickers": missing,
        "price_path": str(price_path),
        "event_path": str(event_path),
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if status == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
