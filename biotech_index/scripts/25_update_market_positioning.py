#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import logging
import sys
from datetime import date, datetime, timezone
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
    sync_ibkr_borrow_availability,
    sync_sec_13f_data_sets,
)
from market_positioning.core import (  # noqa: E402
    DEFAULT_DB_PATH,
    DEFAULT_HISTORY_START_DATE,
    backfill_short_interest_float_shares,
    connect,
    export_positioning_features,
    ingest_float_shares_csv,
    ingest_company_fact_share_proxies,
    ingest_sec_public_float_proxies,
    init_db,
    load_tickers,
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
    parser.add_argument("--ibkr-only", action="store_true", help="Only run IBKR borrow availability refresh and export.")
    parser.add_argument("--public-float-only", action="store_true", help="Only run SEC public-float proxy extraction and export.")
    parser.add_argument(
        "--force-sec13f-reprocess",
        action="store_true",
        help="Reprocess cached SEC 13F archives. Use after adding CUSIP mappings for delisted calibration names.",
    )
    parser.add_argument(
        "--ibkr-borrow-backfill",
        action="store_true",
        help="Use the configured long IBKR FEE_RATE backfill duration instead of the daily initial-load duration.",
    )
    parser.add_argument("--skip-ibkr", action="store_true", help="Skip IBKR borrow availability even when enabled in config.")
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
    return datetime.now(timezone.utc).date()


def config_path_or_default(config: dict[str, Any], key: str, *, base_dir: Path, default: str = "") -> Path | None:
    raw = cfg_get(config, key, default)
    return resolve_optional_path(raw, base_dir=base_dir)


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return [{str(k): str(v or "") for k, v in row.items()} for row in reader]


def write_csv_rows(path: Path, rows: list[dict[str, str]], *, fieldnames: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = fieldnames or (list(rows[0].keys()) if rows else ["ticker"])
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def normalize_ticker_local(raw: object) -> str:
    return "".join(ch for ch in str(raw or "").strip().upper() if ch.isalnum() or ch in {".", "-"})


def load_effective_retained_ticker_changes(config: dict[str, Any], *, base_dir: Path, asof: date) -> list[dict[str, str]]:
    path = config_path_or_default(config, "paths.company_ticker_actions_csv", base_dir=base_dir)
    if path is None or not path.exists():
        return []
    actions: list[dict[str, str]] = []
    for row in read_csv_rows(path):
        action = str(row.get("action") or "").strip().lower()
        retain = as_bool(row.get("retain_in_biotech"), False)
        old_ticker = normalize_ticker_local(row.get("old_ticker"))
        new_ticker = normalize_ticker_local(row.get("new_ticker"))
        effective = str(row.get("effective_date") or "").strip()
        if action != "ticker_change" or not retain or not old_ticker or not new_ticker:
            continue
        if effective and effective > asof.isoformat():
            continue
        clean = dict(row)
        clean["old_ticker"] = old_ticker
        clean["new_ticker"] = new_ticker
        actions.append(clean)
    return actions


def ticker_value(row: dict[str, str]) -> str:
    return normalize_ticker_local(row.get("ticker") or row.get("symbol"))


def build_positioning_ticker_universe_with_predecessors(
    source_csv: Path | None,
    *,
    output_dir: Path,
    asof: date,
    actions: list[dict[str, str]],
) -> Path | None:
    """Include predecessor tickers so lagged ticker-keyed feeds can bridge changes.

    FINRA, 13F, and borrow feeds are ticker-keyed.  For a true retained ticker
    change such as SNSE->FTH, the current universe must contain FTH, but the most
    recent public short-interest or 13F rows may still be filed under SNSE.  This
    temporary universe lets the shared collector ingest/export both; exports are
    remapped back to current tickers before biotech features consume them.
    """
    if source_csv is None or not source_csv.exists() or not actions:
        return source_csv
    rows = read_csv_rows(source_csv)
    if not rows:
        return source_csv
    fields = list(rows[0].keys())
    by_ticker = {ticker_value(row): row for row in rows if ticker_value(row)}
    added = 0
    for action in actions:
        old_ticker = action["old_ticker"]
        new_ticker = action["new_ticker"]
        if new_ticker not in by_ticker or old_ticker in by_ticker:
            continue
        predecessor = dict(by_ticker[new_ticker])
        if "ticker" in predecessor:
            predecessor["ticker"] = old_ticker
        elif "symbol" in predecessor:
            predecessor["symbol"] = old_ticker
        else:
            predecessor["ticker"] = old_ticker
            fields = [*fields, "ticker"] if "ticker" not in fields else fields
        predecessor["company_name"] = (
            action.get("predecessor_company_name")
            or action.get("old_company_name")
            or predecessor.get("company_name")
            or predecessor.get("name")
            or ""
        )
        rows.append(predecessor)
        added += 1
    if added <= 0:
        return source_csv
    out = output_dir / f"_market_positioning_tickers_with_predecessors_{asof:%Y%m%d}.csv"
    write_csv_rows(out, rows, fieldnames=fields)
    LOGGER.info("Market-positioning ticker bridge added %d predecessor ticker(s): %s", added, out)
    return out


def remap_positioning_export_for_ticker_changes(path: Path, actions: list[dict[str, str]]) -> int:
    if not actions or not path.exists():
        return 0
    rows = read_csv_rows(path)
    if not rows:
        return 0
    fields = list(rows[0].keys())
    by_ticker: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        by_ticker.setdefault(ticker_value(row), []).append(row)
    changed = 0
    bridge_old_tickers = {action["old_ticker"] for action in actions}
    out: list[dict[str, str]] = []
    for row in rows:
        if ticker_value(row) not in bridge_old_tickers:
            out.append(row)
    dropped = len(rows) - len(out)
    existing_current = {ticker_value(row) for row in out}
    for action in actions:
        old_ticker = action["old_ticker"]
        new_ticker = action["new_ticker"]
        if new_ticker in existing_current:
            continue
        predecessor_rows = by_ticker.get(old_ticker, [])
        if not predecessor_rows:
            continue
        for row in predecessor_rows:
            mapped = dict(row)
            if "ticker" in mapped:
                mapped["ticker"] = new_ticker
            elif "symbol" in mapped:
                mapped["symbol"] = new_ticker
            else:
                mapped["ticker"] = new_ticker
                fields = [*fields, "ticker"] if "ticker" not in fields else fields
            basis = str(mapped.get("source") or mapped.get("signal_basis") or "")
            mapped["source"] = basis or mapped.get("source", "")
            out.append(mapped)
            changed += 1
    if changed or dropped:
        write_csv_rows(path, out, fieldnames=fields)
    return changed + dropped


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
    ticker_actions = load_effective_retained_ticker_changes(config, base_dir=base_dir, asof=asof)
    tickers_csv = build_positioning_ticker_universe_with_predecessors(
        tickers_csv,
        output_dir=output_dir,
        asof=asof,
        actions=ticker_actions,
    )
    user_agent = str(cfg_get(config, "market_positioning.user_agent", cfg_get(config, "sec_filings.user_agent", DEFAULT_USER_AGENT)))
    timeout_sec = to_float(cfg_get(config, "market_positioning.timeout_sec", 120.0), 120.0)
    fail_on_error = as_bool(cfg_get(config, "market_positioning.fail_on_error", True), True)

    results: list[str] = []
    with connect(db_path) as conn:
        init_db(conn)
        if not args.skip_download:
            if (
                not args.sec13f_only
                and not args.ibkr_only
                and as_bool(
                    cfg_get(config, "market_positioning.float_shares.sec_public_float_proxy.enabled", True),
                    True,
                )
            ):
                try:
                    biotech_db_path = resolve_path(
                        cfg_get(config, "paths.database_path"),
                        base_dir=base_dir,
                    )
                    rows = ingest_sec_public_float_proxies(
                        conn,
                        biotech_db_path,
                        history_start_date=history_start,
                        end_date=asof,
                        tickers=load_tickers(tickers_csv),
                        max_filings_per_ticker=to_int(
                            cfg_get(config, "market_positioning.float_shares.sec_public_float_proxy.max_filings_per_ticker", 9),
                            9,
                        ),
                    )
                    enriched = backfill_short_interest_float_shares(conn)
                    results.append(f"sec_public_float_proxy rows={rows} short_interest_enriched={enriched}")
                    if as_bool(
                        cfg_get(config, "market_positioning.float_shares.company_fact_shares_proxy.enabled", True),
                        True,
                    ):
                        share_proxy_rows = ingest_company_fact_share_proxies(
                            conn,
                            biotech_db_path=biotech_db_path,
                            history_start_date=history_start,
                            end_date=asof,
                            tickers=load_tickers(tickers_csv),
                        )
                        enriched = backfill_short_interest_float_shares(conn)
                        results.append(
                            f"company_fact_share_proxy rows={share_proxy_rows} short_interest_enriched={enriched}"
                        )
                except Exception as exc:
                    if fail_on_error and as_bool(
                        cfg_get(config, "market_positioning.float_shares.sec_public_float_proxy.required", False),
                        False,
                    ):
                        raise
                    LOGGER.exception("SEC public-float proxy update failed but required=false: %s", exc)
                    results.append(f"sec_public_float_proxy failed={type(exc).__name__}")
            if (
                not args.finra_only
                and
                not args.sec13f_only
                and not args.ibkr_only
                and as_bool(cfg_get(config, "market_positioning.float_shares.enabled", False), False)
            ):
                try:
                    csv_path = config_path_or_default(
                        config,
                        "market_positioning.float_shares.csv_path",
                        base_dir=base_dir,
                    )
                    if csv_path is None or not csv_path.exists():
                        raise FileNotFoundError(
                            f"Configured market_positioning.float_shares.csv_path is missing: {csv_path}"
                        )
                    rows = ingest_float_shares_csv(
                        conn,
                        csv_path,
                        history_start_date=history_start,
                        source=str(cfg_get(config, "market_positioning.float_shares.source", "csv")),
                    )
                    enriched = backfill_short_interest_float_shares(conn)
                    results.append(f"float_shares rows={rows} short_interest_enriched={enriched}")
                except Exception as exc:
                    if fail_on_error and as_bool(cfg_get(config, "market_positioning.float_shares.required", False), False):
                        raise
                    LOGGER.exception("Float-shares update failed but required=false: %s", exc)
                    results.append(f"float_shares failed={type(exc).__name__}")
            if (
                not args.public_float_only
                and
                not args.sec13f_only
                and not args.ibkr_only
                and as_bool(cfg_get(config, "market_positioning.short_interest.enabled", True), True)
            ):
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
            if (
                not args.public_float_only
                and
                not args.finra_only
                and not args.ibkr_only
                and as_bool(cfg_get(config, "market_positioning.institutional_13f.enabled", True), True)
            ):
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
                        force_reprocess_archives=(
                            bool(args.force_sec13f_reprocess)
                            or as_bool(
                                cfg_get(config, "market_positioning.institutional_13f.force_reprocess_archives", False),
                                False,
                            )
                        ),
                    )
                    results.append(f"{result.feed_name} rows={result.rows}")
                except Exception as exc:
                    if fail_on_error:
                        raise
                    LOGGER.exception("SEC 13F update failed but fail_on_error=false: %s", exc)
                    results.append(f"institutional_13f failed={type(exc).__name__}")
            if (
                not args.public_float_only
                and
                not args.finra_only
                and not args.sec13f_only
                and not args.skip_ibkr
                and as_bool(cfg_get(config, "market_positioning.ibkr_borrow.enabled", False), False)
            ):
                try:
                    fee_rate_initial_duration = str(
                        cfg_get(
                            config,
                            "market_positioning.ibkr_borrow.fee_rate_backfill_duration"
                            if args.ibkr_borrow_backfill
                            else "market_positioning.ibkr_borrow.fee_rate_initial_duration",
                            "7 Y" if args.ibkr_borrow_backfill else "2 Y",
                        )
                    )
                    max_tickers_key = (
                        "market_positioning.ibkr_borrow.backfill_max_tickers_per_run"
                        if args.ibkr_borrow_backfill
                        else "market_positioning.ibkr_borrow.max_tickers_per_run"
                    )
                    result = sync_ibkr_borrow_availability(
                        conn,
                        tickers_csv=tickers_csv,
                        history_start_date=history_start,
                        end_date=asof,
                        host=str(cfg_get(config, "market_positioning.ibkr_borrow.host", "127.0.0.1")),
                        port=to_int(cfg_get(config, "market_positioning.ibkr_borrow.port", 7497), 7497),
                        client_id=to_int(cfg_get(config, "market_positioning.ibkr_borrow.client_id", 7822), 7822),
                        market_data_type=to_int(cfg_get(config, "market_positioning.ibkr_borrow.market_data_type", 1), 1),
                        fee_rate_unit=str(cfg_get(config, "market_positioning.ibkr_borrow.fee_rate_unit", "decimal")),
                        fee_rate_initial_duration=fee_rate_initial_duration,
                        fee_rate_incremental_duration=str(
                            cfg_get(config, "market_positioning.ibkr_borrow.fee_rate_incremental_duration", "45 D")
                        ),
                        snapshot_wait_sec=to_float(
                            cfg_get(config, "market_positioning.ibkr_borrow.snapshot_wait_sec", 4.0),
                            4.0,
                        ),
                        shortable_snapshot=as_bool(
                            cfg_get(config, "market_positioning.ibkr_borrow.shortable_snapshot", True),
                            True,
                        ),
                        shortable_coverage_warn_min=to_float(
                            cfg_get(config, "market_positioning.ibkr_borrow.shortable_coverage_warn_min", 50.0),
                            50.0,
                        ),
                        batch_size=to_int(cfg_get(config, "market_positioning.ibkr_borrow.batch_size", 50), 50),
                        sleep_sec=to_float(cfg_get(config, "market_positioning.ibkr_borrow.sleep_sec", 0.2), 0.2),
                        max_tickers=to_int(cfg_get(config, max_tickers_key, 0), 0),
                    )
                    results.append(f"{result.feed_name} rows={result.rows}")
                except Exception as exc:
                    if fail_on_error:
                        raise
                    LOGGER.exception("IBKR borrow availability update failed but fail_on_error=false: %s", exc)
                    results.append(f"ibkr_borrow_availability failed={type(exc).__name__}")
        enriched = backfill_short_interest_float_shares(conn)
        if enriched:
            results.append(f"short_interest_float_backfill rows={enriched}")
        short_path, institutional_path, borrow_path, short_count, institutional_count, borrow_count = export_positioning_features(
            conn,
            asof_date=asof,
            output_dir=output_dir,
            tickers_csv=tickers_csv,
            max_borrow_fee_staleness_days=to_int(
                cfg_get(config, "market_positioning.ibkr_borrow.max_fee_staleness_days", 10),
                10,
            ),
            max_borrow_snapshot_staleness_days=to_int(
                cfg_get(config, "market_positioning.ibkr_borrow.max_snapshot_staleness_days", 7),
                7,
            ),
            hard_to_borrow_shares=to_float(
                cfg_get(config, "market_positioning.ibkr_borrow.hard_to_borrow_shares", 50_000.0),
                50_000.0,
            ),
        )
        remapped_short = remap_positioning_export_for_ticker_changes(short_path, ticker_actions)
        remapped_institutional = remap_positioning_export_for_ticker_changes(institutional_path, ticker_actions)
        remapped_borrow = remap_positioning_export_for_ticker_changes(borrow_path, ticker_actions)
        if remapped_short or remapped_institutional or remapped_borrow:
            results.append(
                "ticker_action_export_bridge "
                f"short={remapped_short} institutional={remapped_institutional} borrow={remapped_borrow}"
            )
    LOGGER.info(
        "Market positioning update complete: asof=%s db=%s %s exports short=%s rows=%d institutional=%s rows=%d borrow=%s rows=%d",
        asof.isoformat(),
        db_path,
        "; ".join(results) if results else "download=skipped",
        short_path,
        short_count,
        institutional_path,
        institutional_count,
        borrow_path,
        borrow_count,
    )


if __name__ == "__main__":
    main()
