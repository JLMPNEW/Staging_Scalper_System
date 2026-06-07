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
    DEFAULT_FINRA_EQUITY_SHORT_INTEREST_FILES_BASE_URL,
    DEFAULT_FINRA_SHORT_INTEREST_URL,
    DEFAULT_USER_AGENT,
    sync_finra_equity_short_interest_files,
    sync_finra_short_interest,
)
from market_positioning.core import connect as connect_positioning  # noqa: E402
from market_positioning.core import init_db as init_positioning_db  # noqa: E402
from market_positioning.core import parse_date  # noqa: E402
from med_devices.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from med_devices.core.logging_utils import configure_utc_logging  # noqa: E402
from med_devices.core.text_norm import normalize_ticker  # noqa: E402


LOGGER = logging.getLogger("update_med_device_finra_short_interest")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Update FINRA true short-interest snapshots for the med-device universe.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--history-start", default="")
    parser.add_argument("--asof", default="")
    parser.add_argument("--market-positioning-db", type=Path, default=None)
    parser.add_argument("--tickers-csv", type=Path, default=None)
    parser.add_argument("--cache-dir", type=Path, default=None)
    parser.add_argument("--use-api", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--use-files", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--max-files", type=int, default=None)
    parser.add_argument("--max-tickers", type=int, default=None)
    parser.add_argument("--timeout-sec", type=float, default=None)
    parser.add_argument("--sleep-sec", type=float, default=None)
    return parser.parse_args()


def parse_asof(raw: str) -> date:
    parsed = parse_date(raw)
    if parsed is not None:
        return parsed
    return datetime.now().date()


def as_bool(raw: object, default: bool) -> bool:
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


def count_coverage(db_path: Path, tickers_csv: Path) -> tuple[int, int, str, str, dict[str, int]]:
    import csv

    with tickers_csv.open(newline="", encoding="utf-8-sig") as handle:
        tickers = sorted(
            {
                normalize_ticker(row.get("ticker") or row.get("symbol"))
                for row in csv.DictReader(handle)
                if normalize_ticker(row.get("ticker") or row.get("symbol"))
            }
        )
    if not tickers:
        return 0, 0, "", "", {}
    with sqlite3.connect(db_path) as conn:
        placeholders = ",".join("?" for _ in tickers)
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS n,
                   COUNT(DISTINCT UPPER(ticker)) AS ticker_count,
                   MIN(settlement_date) AS min_settlement,
                   MAX(settlement_date) AS max_settlement
            FROM short_interest_snapshots
            WHERE UPPER(ticker) IN ({placeholders})
            """,
            tickers,
        ).fetchone()
        by_source = {
            str(source): int(count or 0)
            for source, count in conn.execute(
                f"""
                SELECT source, COUNT(*)
                FROM short_interest_snapshots
                WHERE UPPER(ticker) IN ({placeholders})
                GROUP BY source
                ORDER BY source
                """,
                tickers,
            ).fetchall()
        }
    return int(row[0] or 0), int(row[1] or 0), str(row[2] or ""), str(row[3] or ""), by_source


def main() -> None:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    asof = parse_asof(args.asof)
    history_start = parse_asof(
        args.history_start
        or str(cfg_get(config, "market_positioning_update.finra_short_interest.history_start", "2019-01-01"))
    )
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
                "market_positioning_update.finra_short_interest.cache_dir",
                "../output/market_positioning_cache/finra_short_interest",
            ),
            base_dir=base_dir,
        )
    )
    use_files = args.use_files
    if use_files is None:
        use_files = as_bool(cfg_get(config, "market_positioning_update.finra_short_interest.use_files", True), True)
    use_api = args.use_api
    if use_api is None:
        use_api = as_bool(cfg_get(config, "market_positioning_update.finra_short_interest.use_api", False), False)
    max_files = args.max_files
    if max_files is None:
        max_files = to_int(cfg_get(config, "market_positioning_update.finra_short_interest.max_files_per_run", 0), 0)
    max_tickers = args.max_tickers
    if max_tickers is None:
        max_tickers = to_int(cfg_get(config, "market_positioning_update.finra_short_interest.max_tickers", 0), 0)
    timeout_sec = args.timeout_sec
    if timeout_sec is None:
        timeout_sec = to_float(cfg_get(config, "market_positioning_update.timeout_sec", 120.0), 120.0)
    sleep_sec = args.sleep_sec
    if sleep_sec is None:
        sleep_sec = to_float(cfg_get(config, "market_positioning_update.finra_short_interest.sleep_sec", 0.05), 0.05)
    user_agent = str(
        cfg_get(config, "market_positioning_update.user_agent", cfg_get(config, "sec_filings.user_agent", DEFAULT_USER_AGENT))
    )

    messages: list[str] = []
    with connect_positioning(db_path) as conn:
        init_positioning_db(conn)
        if use_files:
            result = sync_finra_equity_short_interest_files(
                conn,
                tickers_csv=tickers_csv,
                history_start_date=history_start,
                end_date=asof,
                base_url=str(
                    cfg_get(
                        config,
                        "market_positioning_update.finra_short_interest.files_base_url",
                        DEFAULT_FINRA_EQUITY_SHORT_INTEREST_FILES_BASE_URL,
                    )
                ),
                cache_dir=cache_dir,
                publication_lag_days=to_int(
                    cfg_get(config, "market_positioning_update.finra_short_interest.publication_lag_days", 12),
                    12,
                ),
                sleep_sec=sleep_sec,
                user_agent=user_agent,
                timeout_sec=timeout_sec,
                max_files=max_files,
            )
            messages.append(result.message)
        if use_api:
            result = sync_finra_short_interest(
                conn,
                tickers_csv=tickers_csv,
                history_start_date=history_start,
                end_date=asof,
                api_url=str(
                    cfg_get(
                        config,
                        "market_positioning_update.finra_short_interest.api_url",
                        DEFAULT_FINRA_SHORT_INTEREST_URL,
                    )
                ),
                page_size=to_int(cfg_get(config, "market_positioning_update.finra_short_interest.page_size", 5000), 5000),
                sleep_sec=sleep_sec,
                user_agent=user_agent,
                timeout_sec=timeout_sec,
                max_tickers=max_tickers,
            )
            messages.append(result.message)

    rows, ticker_count, min_settlement, max_settlement, by_source = count_coverage(db_path, tickers_csv)
    for message in messages:
        LOGGER.info("%s", message)
    print(
        "finra_short_interest_update_complete "
        f"rows={rows} med_device_tickers={ticker_count} settlement_range={min_settlement}..{max_settlement} "
        f"by_source={by_source} db={db_path}"
    )


if __name__ == "__main__":
    main()
