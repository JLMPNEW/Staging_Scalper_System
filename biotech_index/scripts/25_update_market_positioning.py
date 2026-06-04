#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from biotech_index.core.config import cfg_get, load_yaml, resolve_optional_path, resolve_path  # noqa: E402
from biotech_index.core.logging_utils import configure_utc_logging  # noqa: E402
from market_positioning.api_collectors import (  # noqa: E402
    DEFAULT_FINRA_EQUITY_SHORT_INTEREST_FILES_BASE_URL,
    DEFAULT_SEC_13F_DATASETS_URL,
    DEFAULT_USER_AGENT,
    sync_finra_equity_short_interest_files,
    sync_sec_13f_data_sets,
)
from market_positioning.core import (  # noqa: E402
    DEFAULT_DB_PATH,
    DEFAULT_HISTORY_START_DATE,
    connect,
    export_positioning_features,
    init_db,
    parse_history_start,
    parse_date,
)


LOGGER = logging.getLogger("update_market_positioning")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Update shared FINRA short-interest and SEC 13F positioning data.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None, help="Ignored biotech DB path; accepted for orchestrator compatibility.")
    parser.add_argument("--asof", type=str, default="", help="Pipeline as-of date in YYYY-MM-DD.")
    parser.add_argument("--skip-download", action="store_true", help="Only initialize DB and export already-loaded features.")
    parser.add_argument("--finra-only", action="store_true", help="Only run FINRA short-interest refresh and export.")
    parser.add_argument("--sec13f-only", action="store_true", help="Only run SEC 13F refresh and export.")
    return parser.parse_args()


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


def parse_asof(raw: str) -> date:
    parsed = parse_date(raw)
    if parsed is not None:
        return parsed
    return datetime.utcnow().date()


def config_path_or_default(config: dict[str, Any], key: str, *, base_dir: Path, default: str = "") -> Path | None:
    raw = cfg_get(config, key, default)
    return resolve_optional_path(raw, base_dir=base_dir)


def main() -> None:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    asof = parse_asof(args.asof)

    enabled = as_bool(cfg_get(config, "market_positioning.enabled", True), True)
    if not enabled:
        LOGGER.warning("market_positioning.enabled=false; skipping update.")
        return

    db_path = resolve_path(cfg_get(config, "market_positioning.database_path", str(DEFAULT_DB_PATH)), base_dir=base_dir)
    history_start = parse_history_start(
        str(cfg_get(config, "market_positioning.history_start_date", DEFAULT_HISTORY_START_DATE.isoformat()))
    )
    output_dir = resolve_path(
        cfg_get(config, "market_positioning.exports.biotech_output_dir", "../output/biotech_index_reports"),
        base_dir=base_dir,
    )
    tickers_csv = config_path_or_default(
        config,
        "biotech_features.final_scoring_universe_csv",
        base_dir=base_dir,
        default="../output/biotech_index_reports/ctgov_final_scoring_universe.csv",
    )
    user_agent = str(cfg_get(config, "market_positioning.user_agent", cfg_get(config, "sec_filings.user_agent", DEFAULT_USER_AGENT)))
    timeout_sec = to_float(cfg_get(config, "market_positioning.timeout_sec", 120.0), 120.0)
    fail_on_error = as_bool(cfg_get(config, "market_positioning.fail_on_error", True), True)

    results: list[str] = []
    with connect(db_path) as conn:
        init_db(conn)
        if not args.skip_download:
            if not args.sec13f_only and as_bool(cfg_get(config, "market_positioning.short_interest.enabled", True), True):
                try:
                    cache_dir = resolve_path(
                        cfg_get(config, "market_positioning.short_interest.cache_dir", "../output/market_positioning_cache/finra_short_interest"),
                        base_dir=base_dir,
                    )
                    result = sync_finra_equity_short_interest_files(
                        conn,
                        tickers_csv=tickers_csv,
                        history_start_date=history_start,
                        end_date=asof,
                        base_url=str(
                            cfg_get(
                                config,
                                "market_positioning.short_interest.files_base_url",
                                DEFAULT_FINRA_EQUITY_SHORT_INTEREST_FILES_BASE_URL,
                            )
                        ),
                        cache_dir=cache_dir,
                        publication_lag_days=to_int(cfg_get(config, "market_positioning.short_interest.publication_lag_days", 12), 12),
                        sleep_sec=to_float(cfg_get(config, "market_positioning.short_interest.sleep_sec", 0.15), 0.15),
                        user_agent=user_agent,
                        timeout_sec=timeout_sec,
                        max_files=to_int(cfg_get(config, "market_positioning.short_interest.max_files_per_run", 0), 0),
                    )
                    results.append(f"{result.feed_name} rows={result.rows}")
                except Exception as exc:
                    if fail_on_error:
                        raise
                    LOGGER.exception("FINRA short-interest update failed but fail_on_error=false: %s", exc)
                    results.append(f"short_interest failed={type(exc).__name__}")
            if not args.finra_only and as_bool(cfg_get(config, "market_positioning.institutional_13f.enabled", True), True):
                try:
                    cache_dir = resolve_path(
                        cfg_get(config, "market_positioning.institutional_13f.cache_dir", "../output/market_positioning_cache/sec_13f"),
                        base_dir=base_dir,
                    )
                    result = sync_sec_13f_data_sets(
                        conn,
                        tickers_csv=tickers_csv,
                        cusip_ticker_map_csv=config_path_or_default(
                            config,
                            "market_positioning.institutional_13f.cusip_ticker_map_csv",
                            base_dir=base_dir,
                        ),
                        history_start_date=history_start,
                        end_date=asof,
                        cache_dir=cache_dir,
                        index_url=str(
                            cfg_get(
                                config,
                                "market_positioning.institutional_13f.data_sets_url",
                                DEFAULT_SEC_13F_DATASETS_URL,
                            )
                        ),
                        user_agent=user_agent,
                        timeout_sec=timeout_sec,
                        sleep_sec=to_float(cfg_get(config, "market_positioning.institutional_13f.sleep_sec", 0.2), 0.2),
                        max_archives=to_int(cfg_get(config, "market_positioning.institutional_13f.max_archives_per_run", 0), 0),
                    )
                    results.append(f"{result.feed_name} rows={result.rows}")
                except Exception as exc:
                    if fail_on_error:
                        raise
                    LOGGER.exception("SEC 13F update failed but fail_on_error=false: %s", exc)
                    results.append(f"institutional_13f failed={type(exc).__name__}")
        short_path, institutional_path, short_count, institutional_count = export_positioning_features(
            conn,
            asof_date=asof,
            output_dir=output_dir,
            tickers_csv=tickers_csv,
        )
    LOGGER.info(
        "Market positioning update complete: asof=%s db=%s %s exports short=%s rows=%d institutional=%s rows=%d",
        asof.isoformat(),
        db_path,
        "; ".join(results) if results else "download=skipped",
        short_path,
        short_count,
        institutional_path,
        institutional_count,
    )


if __name__ == "__main__":
    main()
