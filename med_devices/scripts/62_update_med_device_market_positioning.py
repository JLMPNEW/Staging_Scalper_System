#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from market_positioning.api_collectors import (  # noqa: E402
    DEFAULT_SEC_13F_DATASETS_URL,
    DEFAULT_USER_AGENT,
    sync_sec_13f_data_sets,
)
from market_positioning.core import connect as connect_positioning  # noqa: E402
from market_positioning.core import init_db as init_positioning_db  # noqa: E402
from market_positioning.core import parse_date  # noqa: E402
from med_devices.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from med_devices.core.logging_utils import configure_utc_logging  # noqa: E402


LOGGER = logging.getLogger("update_med_device_market_positioning")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Update shared market-positioning data for the med-device universe.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--asof", default="", help="End/as-of date in YYYY-MM-DD. Defaults to today.")
    parser.add_argument("--history-start", default="", help="History start date in YYYY-MM-DD.")
    parser.add_argument("--market-positioning-db", type=Path, default=None)
    parser.add_argument("--tickers-csv", type=Path, default=None)
    parser.add_argument("--cache-dir", type=Path, default=None)
    parser.add_argument("--cusip-ticker-map-csv", type=Path, default=None)
    parser.add_argument("--user-agent", default="")
    parser.add_argument("--timeout-sec", type=float, default=None)
    parser.add_argument("--sleep-sec", type=float, default=None)
    parser.add_argument("--max-archives", type=int, default=None)
    parser.add_argument(
        "--force-reprocess-archives",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Reparse cached/downloaded SEC 13F archives even if previously processed for another universe.",
    )
    return parser.parse_args()


def parse_asof(raw: str) -> date:
    parsed = parse_date(raw)
    if parsed is not None:
        return parsed
    return datetime.now().date()


def as_bool(raw: object, default: bool = False) -> bool:
    if raw is None:
        return default
    text = str(raw).strip().lower()
    if text in {"1", "true", "t", "yes", "y", "enabled", "on"}:
        return True
    if text in {"0", "false", "f", "no", "n", "disabled", "off"}:
        return False
    return default


def to_int(raw: object, default: int) -> int:
    try:
        return int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def to_float(raw: object, default: float) -> float:
    try:
        return float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def count_coverage(db_path: Path, tickers_csv: Path) -> tuple[int, int, str, str]:
    import csv

    with tickers_csv.open(newline="", encoding="utf-8-sig") as handle:
        tickers = sorted({str(row.get("ticker") or row.get("symbol") or "").strip().upper() for row in csv.DictReader(handle)})
    tickers = [ticker for ticker in tickers if ticker]
    if not tickers:
        return 0, 0, "", ""
    with sqlite3.connect(db_path) as conn:
        q = ",".join("?" for _ in tickers)
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS n,
                   COUNT(DISTINCT UPPER(ticker)) AS ticker_count,
                   MIN(period_of_report) AS min_period,
                   MAX(period_of_report) AS max_period
            FROM institutional_13f_ownership_snapshots
            WHERE UPPER(ticker) IN ({q})
            """,
            tickers,
        ).fetchone()
    return int(row[0] or 0), int(row[1] or 0), str(row[2] or ""), str(row[3] or "")


def main() -> None:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    asof = parse_asof(args.asof)
    history_start = parse_asof(args.history_start or str(cfg_get(config, "external_positioning_import.history_start", "2019-01-01")))
    db_path = (
        args.market_positioning_db.expanduser().resolve()
        if args.market_positioning_db
        else Path(str(cfg_get(config, "external_positioning_import.market_positioning_db_path"))).expanduser().resolve()
    )
    tickers_csv = (
        args.tickers_csv.expanduser().resolve()
        if args.tickers_csv
        else resolve_path(
            cfg_get(config, "med_devices_universe.seed_csv", "../ticker_mapping/med_dev_tickers_clean_keep.csv"),
            base_dir=base_dir,
        )
    )
    cache_dir = (
        args.cache_dir.expanduser().resolve()
        if args.cache_dir
        else resolve_path(
            cfg_get(
                config,
                "market_positioning_update.sec_13f.cache_dir",
                "../output/market_positioning_cache/sec_13f",
            ),
            base_dir=base_dir,
        )
    )
    cusip_ticker_map_csv = (
        args.cusip_ticker_map_csv.expanduser().resolve()
        if args.cusip_ticker_map_csv
        else None
    )
    user_agent = args.user_agent or str(
        cfg_get(config, "market_positioning_update.user_agent", cfg_get(config, "sec_ingestion.user_agent", DEFAULT_USER_AGENT))
    )
    timeout_sec = args.timeout_sec
    if timeout_sec is None:
        timeout_sec = to_float(cfg_get(config, "market_positioning_update.timeout_sec", 120.0), 120.0)
    sleep_sec = args.sleep_sec
    if sleep_sec is None:
        sleep_sec = to_float(cfg_get(config, "market_positioning_update.sec_13f.sleep_sec", 0.2), 0.2)
    max_archives = args.max_archives
    if max_archives is None:
        max_archives = to_int(cfg_get(config, "market_positioning_update.sec_13f.max_archives_per_run", 0), 0)
    force_reprocess = args.force_reprocess_archives
    if force_reprocess is None:
        force_reprocess = as_bool(cfg_get(config, "market_positioning_update.sec_13f.force_reprocess_archives", True), True)

    with connect_positioning(db_path) as conn:
        init_positioning_db(conn)
        result = sync_sec_13f_data_sets(
            conn,
            tickers_csv=tickers_csv,
            cusip_ticker_map_csv=cusip_ticker_map_csv,
            history_start_date=history_start,
            end_date=asof,
            cache_dir=cache_dir,
            index_url=str(
                cfg_get(
                    config,
                    "market_positioning_update.sec_13f.data_sets_url",
                    DEFAULT_SEC_13F_DATASETS_URL,
                )
            ),
            user_agent=user_agent,
            timeout_sec=timeout_sec,
            sleep_sec=sleep_sec,
            max_archives=max_archives,
            force_reprocess_archives=force_reprocess,
        )
    rows, ticker_count, min_period, max_period = count_coverage(db_path, tickers_csv)
    LOGGER.info("%s", result.message)
    print(
        "sec13f_update_complete "
        f"rows={result.rows} med_device_snapshot_rows={rows} med_device_tickers={ticker_count} "
        f"period_range={min_period}..{max_period} db={db_path}"
    )


if __name__ == "__main__":
    main()
