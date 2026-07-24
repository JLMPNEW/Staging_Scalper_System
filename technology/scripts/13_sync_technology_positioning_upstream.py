#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import io
import logging
import subprocess
import sys
import time
import zipfile
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
    SyncResult,
    discover_sec_13f_archives,
    download_cached,
    load_cusip_ticker_map,
    load_universe_name_map,
    load_universe_tickers,
    match_13f_ticker,
    normalize_cusip,
    read_zip_table,
    sync_finra_equity_short_interest_files,
    sync_ibkr_borrow_availability,
    upsert_13f_records,
)
from market_positioning.core import connect as connect_market_positioning  # noqa: E402
from market_positioning.core import init_db as init_market_positioning_db  # noqa: E402
from market_positioning.core import parse_date as mp_parse_date  # noqa: E402
from market_positioning.core import to_float, update_feed_state, utc_now  # noqa: E402
from technology.core.config import cfg_get, expand_env_vars, load_yaml, resolve_path  # noqa: E402
from technology.core.logging_utils import configure_utc_logging  # noqa: E402


LOGGER = logging.getLogger("sync_technology_positioning_upstream")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"


def parse_date_arg(raw: object, *, default: date) -> date:
    text = str(raw or "").strip()
    if not text:
        return default
    return datetime.strptime(text[:10], "%Y-%m-%d").date()


def parse_13f_date(raw: object) -> date | None:
    text = str(raw or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%d-%b-%Y", "%d-%B-%Y"):
        try:
            return datetime.strptime(text.upper(), fmt).date()
        except ValueError:
            continue
    return mp_parse_date(text)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Populate market_positioning.sqlite for the configured technology universe, "
            "then import those rows into technology.sqlite."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--history-start", default="")
    parser.add_argument("--end-date", default=date.today().isoformat())
    parser.add_argument("--tickers-csv", type=Path, default=None)
    parser.add_argument("--market-positioning-db", type=Path, default=None)
    parser.add_argument("--cache-dir", type=Path, default=None)
    parser.add_argument("--user-agent", default="")
    parser.add_argument("--skip-finra-short-interest", action="store_true")
    parser.add_argument("--skip-13f", action="store_true")
    parser.add_argument("--skip-ibkr-borrow", action="store_true")
    parser.add_argument(
        "--allow-stale-ibkr-borrow-on-error",
        action="store_true",
        help=(
            "On IB connection/timeout errors, retain prior borrow observations and let "
            "the downstream staleness gate decide acceptance. Data/logic errors still fail."
        ),
    )
    parser.add_argument("--skip-technology-import", action="store_true")
    parser.add_argument(
        "--reaggregate-13f-only",
        action="store_true",
        help="Rebuild institutional_13f_ownership_snapshots from already-loaded holdings without fetching any source.",
    )
    parser.add_argument("--finra-max-files", type=int, default=0)
    parser.add_argument("--sec-13f-max-archives", type=int, default=0)
    parser.add_argument("--ibkr-host", default="127.0.0.1")
    parser.add_argument("--ibkr-port", type=int, default=7497)
    parser.add_argument("--ibkr-client-id", type=int, default=7822)
    parser.add_argument("--ibkr-market-data-type", type=int, default=1)
    parser.add_argument("--ibkr-max-tickers", type=int, default=0)
    parser.add_argument("--ibkr-snapshot-wait-sec", type=float, default=4.0)
    return parser.parse_args()


def cfg_bool(config: dict[str, Any], key: str, default: bool = False) -> bool:
    raw = cfg_get(config, key, default)
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in {"1", "true", "yes", "y"}


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def build_positioning_universe_csv(config: dict[str, Any], *, base_dir: Path, output_path: Path) -> Path:
    """Build a current+historical ticker map for free FINRA/SEC positioning syncs."""
    seed_path = resolve_path(cfg_get(config, "technology_universe.seed_csv"), base_dir=base_dir)
    historical_path = resolve_path(cfg_get(config, "technology_universe.historical_membership_csv"), base_dir=base_dir)
    rows_by_ticker: dict[str, dict[str, str]] = {}

    for row in read_csv_rows(seed_path):
        ticker = str(row.get("ticker") or row.get("symbol") or "").strip().upper()
        if not ticker:
            continue
        out = {str(key): str(value or "") for key, value in row.items()}
        out.setdefault("listing_status", "active")
        out.setdefault("source_membership", "current_source_of_truth")
        rows_by_ticker[ticker] = out

    if cfg_bool(config, "positioning_import.include_historical_members", False):
        for row in read_csv_rows(historical_path):
            internal_ticker = str(row.get("ticker") or row.get("internal_ticker") or "").strip().upper()
            exchange_ticker = str(row.get("exchange_ticker") or internal_ticker).strip().upper()
            if not internal_ticker or not exchange_ticker or exchange_ticker in rows_by_ticker:
                continue
            rows_by_ticker[exchange_ticker] = {
                "ticker": exchange_ticker,
                "internal_ticker": internal_ticker,
                "exchange_ticker": exchange_ticker,
                "company_name": str(row.get("company_name") or ""),
                "cik": str(row.get("cik") or ""),
                "exchange": str(row.get("exchange") or ""),
                "sector": "Technology",
                "industry": "Semiconductors",
                "subsector": str(row.get("calibration_cohort") or ""),
                "country": str(row.get("country") or ""),
                "currency": str(row.get("currency") or "USD"),
                "security_type": str(row.get("security_type") or "Common Stock"),
                "listing_status": str(row.get("membership_status") or "historical"),
                "is_primary_listing": "FALSE",
                "cusip": str(row.get("cusip") or ""),
                "membership_start_date": str(row.get("start_date") or ""),
                "membership_end_date": str(row.get("end_date") or ""),
                "successor_ticker": str(row.get("successor_ticker") or ""),
                "source_membership": "historical_point_in_time",
            }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames: list[str] = []
    preferred = [
        "ticker", "internal_ticker", "exchange_ticker", "company_name", "cik", "cusip", "exchange", "sector", "industry", "subsector",
        "country", "currency", "security_type", "listing_status", "is_primary_listing",
        "membership_start_date", "membership_end_date", "successor_ticker", "source_membership",
    ]
    for field in preferred:
        if any(field in row for row in rows_by_ticker.values()):
            fieldnames.append(field)
    for row in rows_by_ticker.values():
        for field in row:
            if field not in fieldnames:
                fieldnames.append(field)
    with output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        for ticker in sorted(rows_by_ticker):
            writer.writerow(rows_by_ticker[ticker])
    LOGGER.info("Built positioning universe CSV: %s rows=%d", output_path, len(rows_by_ticker))
    return output_path


def run_technology_import(config_path: Path) -> None:
    cmd = [
        sys.executable,
        str(PACKAGE_ROOT / "scripts" / "09_import_technology_positioning.py"),
        "--config",
        str(config_path),
    ]
    LOGGER.info("Running technology positioning import: %s", " ".join(cmd))
    subprocess.run(cmd, cwd=str(PROJECT_ROOT), check=True)


def zip_member_name(zip_file: zipfile.ZipFile, name_hint: str) -> str:
    candidates = [name for name in zip_file.namelist() if name_hint.upper() in name.upper() and not name.endswith("/")]
    if not candidates:
        raise RuntimeError(f"No {name_hint} table found in {zip_file.filename}")
    return candidates[0]


def iter_zip_table_rows(zip_file: zipfile.ZipFile, name_hint: str) -> Any:
    member_name = zip_member_name(zip_file, name_hint)
    delimiter = "\t" if member_name.lower().endswith(".tsv") else "|"
    with zip_file.open(member_name, "r") as handle:
        text = io.TextIOWrapper(handle, encoding="utf-8-sig", errors="replace", newline="")
        yield from csv.DictReader(text, delimiter=delimiter)


def aggregate_13f_ownership_for_tickers(conn: Any, tickers: list[str], *, source: str = "sec_13f_data_sets") -> int:
    """Aggregate 13F holdings into one snapshot per (ticker, period_of_report).

    Managers file across a 45-day window, so bucketing must be per reporting
    period, not per filing date. Each manager contributes its latest filing for
    the period (amendments supersede originals); the snapshot asof_date is the
    last filing date seen so downstream point-in-time joins remain conservative.
    """
    if not tickers:
        return 0
    qmarks = ",".join("?" for _ in tickers)
    rows = conn.execute(
        f"""
        SELECT ticker, filing_date, period_of_report,
               COALESCE(NULLIF(manager_cik, ''), NULLIF(manager_name, ''), filing_key) AS manager_key,
               COALESCE(NULLIF(filing_key, ''), filing_date) AS accession_key,
               COALESCE(shares, 0.0) AS shares,
               COALESCE(market_value, 0.0) AS market_value
        FROM institutional_13f_holdings
        WHERE UPPER(ticker) IN ({qmarks})
          AND UPPER(COALESCE(share_type, '')) IN ('', 'SH')
          AND COALESCE(put_call, '') = ''
          AND COALESCE(period_of_report, '') <> ''
        ORDER BY ticker, period_of_report, manager_key, filing_date
        """,
        tickers,
    )
    now = utc_now()
    # per (ticker, period): manager -> (latest filing_date, latest accession, shares, value)
    buckets: dict[tuple[str, str], dict[str, tuple[str, str, float, float]]] = {}
    latest_filing: dict[tuple[str, str], str] = {}
    for row in rows:
        key = (str(row["ticker"]), str(row["period_of_report"]))
        manager = str(row["manager_key"] or "").strip()
        if not manager:
            continue
        filing_date = str(row["filing_date"] or "")
        accession = str(row["accession_key"] or "")
        shares = to_float(row["shares"], 0.0) or 0.0
        value = to_float(row["market_value"], 0.0) or 0.0
        bucket = buckets.setdefault(key, {})
        current = bucket.get(manager)
        # Compare (filing_date, accession) so a distinct accession filed the same
        # day (e.g. an amendment) replaces the earlier one instead of being dropped.
        if current is None or (filing_date, accession) > (current[0], current[1]):
            bucket[manager] = (filing_date, accession, shares, value)
        elif (filing_date, accession) == (current[0], current[1]):
            bucket[manager] = (filing_date, accession, current[2] + shares, current[3] + value)
        latest_filing[key] = max(latest_filing.get(key, ""), filing_date)
    records: list[tuple[Any, ...]] = []
    prior_by_ticker: dict[str, tuple[float, set[str]]] = {}
    for (ticker, period) in sorted(buckets, key=lambda item: (item[0], item[1])):
        bucket = buckets[(ticker, period)]
        total_shares = sum(entry[2] for entry in bucket.values())
        total_value = sum(entry[3] for entry in bucket.values())
        managers = set(bucket)
        prior = prior_by_ticker.get(ticker)
        if prior is None:
            delta = None
            new_buyer_count = 0
            exiting_holder_count = 0
        else:
            prior_shares, prior_managers = prior
            delta = (total_shares - prior_shares) / prior_shares if prior_shares > 0 else None
            new_buyer_count = len(managers - prior_managers)
            exiting_holder_count = len(prior_managers - managers)
        prior_by_ticker[ticker] = (total_shares, managers)
        records.append(
            (
                ticker,
                latest_filing.get((ticker, period)) or period,
                period,
                total_shares,
                total_value,
                len(managers),
                new_buyer_count,
                exiting_holder_count,
                new_buyer_count - exiting_holder_count,
                delta,
                source,
                now,
                now,
            )
        )
    # Replace any legacy filing-day-slice snapshots wholesale for these tickers.
    conn.execute(
        f"DELETE FROM institutional_13f_ownership_snapshots WHERE source = ? AND UPPER(ticker) IN ({qmarks})",
        (source, *tickers),
    )
    conn.executemany(
        """
        INSERT INTO institutional_13f_ownership_snapshots(
            ticker, asof_date, period_of_report, institutional_shares, institutional_value,
            manager_count, new_buyer_count, exiting_holder_count, net_buyer_count,
            institutional_ownership_delta_pct, source, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ticker, asof_date, source) DO UPDATE SET
            period_of_report = excluded.period_of_report,
            institutional_shares = excluded.institutional_shares,
            institutional_value = excluded.institutional_value,
            manager_count = excluded.manager_count,
            new_buyer_count = excluded.new_buyer_count,
            exiting_holder_count = excluded.exiting_holder_count,
            net_buyer_count = excluded.net_buyer_count,
            institutional_ownership_delta_pct = excluded.institutional_ownership_delta_pct,
            updated_at = excluded.updated_at
        """,
        records,
    )
    return len(records)


def sync_sec_13f_data_sets_streaming(
    conn: Any,
    *,
    tickers_csv: Path,
    cusip_ticker_map_csv: Path,
    history_start_date: date,
    end_date: date,
    cache_dir: Path,
    user_agent: str,
    index_url: str = DEFAULT_SEC_13F_DATASETS_URL,
    timeout_sec: float = 120.0,
    sleep_sec: float = 0.2,
    max_archives: int = 0,
    batch_size: int = 5000,
) -> SyncResult:
    tickers = load_universe_tickers(tickers_csv)
    ticker_set = set(tickers)
    name_map = load_universe_name_map(tickers_csv)
    cusip_map = load_cusip_ticker_map(cusip_ticker_map_csv)
    if not name_map and not cusip_map:
        raise RuntimeError("SEC 13F sync requires ticker/company-name or ticker/CUSIP mapping")

    archives = discover_sec_13f_archives(
        index_url=index_url,
        start_year=history_start_date.year,
        end_year=end_date.year,
        user_agent=user_agent,
        timeout_sec=timeout_sec,
    )
    if max_archives and max_archives > 0:
        archives = archives[:max_archives]

    processed_archives = 0
    total_holdings = 0
    ticker_hits: set[str] = set()
    for url in archives:
        archive_path = download_cached(url, cache_dir=cache_dir, user_agent=user_agent, timeout_sec=timeout_sec)
        now = utc_now()
        filing_rows: dict[str, tuple[Any, ...]] = {}
        holding_rows: list[tuple[Any, ...]] = []
        archive_holdings = 0
        archive_ticker_hits: set[str] = set()
        with zipfile.ZipFile(archive_path) as zf:
            submissions = read_zip_table(zf, "SUBMISSION")
            submission_by_accession: dict[str, dict[str, str]] = {
                str(row.get("ACCESSION_NUMBER") or row.get("accession_number") or "").strip(): row
                for row in submissions
            }
            for row in iter_zip_table_rows(zf, "INFOTABLE"):
                ticker = match_13f_ticker(row, cusip_map=cusip_map, name_map=name_map)
                if ticker not in ticker_set:
                    continue
                accession = str(row.get("ACCESSION_NUMBER") or row.get("accession_number") or "").strip()
                if not accession:
                    continue
                submission = submission_by_accession.get(accession, {})
                filing_date = parse_13f_date(
                    submission.get("FILING_DATE")
                    or submission.get("filing_date")
                    or submission.get("FILEDASOFDATE")
                    or submission.get("filedAsOfDate")
                )
                period = parse_13f_date(
                    submission.get("REPORTCALENDARORQUARTER")
                    or submission.get("PERIODOFREPORT")
                    or submission.get("periodOfReport")
                )
                if filing_date is None or filing_date < history_start_date or filing_date > end_date:
                    continue
                manager_cik = str(
                    submission.get("CIK")
                    or submission.get("cik")
                    or submission.get("FILERCIK")
                    or submission.get("filerCik")
                    or ""
                ).strip()
                manager_name = str(
                    submission.get("NAME")
                    or submission.get("name")
                    or submission.get("FILERNAME")
                    or submission.get("filerName")
                    or ""
                ).strip()
                filing_rows[accession] = (
                    accession,
                    accession,
                    manager_cik,
                    manager_name,
                    period.isoformat() if period else "",
                    filing_date.isoformat(),
                    str(submission.get("ACCEPTANCE_DATETIME") or submission.get("acceptedAt") or filing_date.isoformat()),
                    "sec_13f_data_sets",
                    str(archive_path),
                    now,
                    now,
                )
                holding_rows.append(
                    (
                        accession,
                        manager_cik,
                        manager_name,
                        ticker,
                        normalize_cusip(row.get("CUSIP") or row.get("cusip")),
                        period.isoformat() if period else "",
                        filing_date.isoformat(),
                        str(submission.get("ACCEPTANCE_DATETIME") or submission.get("acceptedAt") or filing_date.isoformat()),
                        to_float(row.get("SSHPRNAMT") or row.get("sshPrnamt") or row.get("shares")),
                        to_float(row.get("VALUE") or row.get("value")),
                        str(row.get("TITLEOFCLASS") or row.get("titleOfClass") or ""),
                        str(row.get("SSHPRNAMTTYPE") or row.get("sshPrnamtType") or ""),
                        str(row.get("PUTCALL") or row.get("putCall") or ""),
                        "sec_13f_data_sets",
                        str(archive_path),
                        now,
                        now,
                    )
                )
                archive_holdings += 1
                archive_ticker_hits.add(ticker)
                if len(holding_rows) >= batch_size:
                    upsert_13f_records(conn, filing_rows=list(filing_rows.values()), holding_rows=holding_rows)
                    total_holdings += len(holding_rows)
                    holding_rows = []
                    filing_rows = {}
        if holding_rows:
            upsert_13f_records(conn, filing_rows=list(filing_rows.values()), holding_rows=holding_rows)
            total_holdings += len(holding_rows)
        processed_archives += 1
        ticker_hits.update(archive_ticker_hits)
        LOGGER.info(
            "Processed 13F archive %s matched_holdings=%d matched_tickers=%d",
            archive_path.name,
            archive_holdings,
            len(archive_ticker_hits),
        )
        if sleep_sec > 0:
            time.sleep(sleep_sec)

    snapshot_rows = aggregate_13f_ownership_for_tickers(conn, tickers, source="sec_13f_data_sets")
    total_table_holdings = int(conn.execute("SELECT COUNT(*) FROM institutional_13f_holdings").fetchone()[0])
    message = (
        f"SEC Form 13F data-set archives processed={processed_archives} "
        f"new_or_refreshed_matched_holdings={total_holdings} matched_tickers={len(ticker_hits)} "
        f"technology_snapshot_rows={snapshot_rows} total_holdings={total_table_holdings}"
    )
    update_feed_state(
        conn,
        feed_name="institutional_13f",
        history_start_date=history_start_date,
        source="sec_13f_data_sets",
        source_file=None,
        row_count=total_table_holdings,
        message=message,
    )
    return SyncResult("institutional_13f", total_table_holdings, message)


def main() -> None:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent

    history_start = parse_date_arg(
        args.history_start or cfg_get(config, "positioning_import.start_date", "2016-01-01"),
        default=date(2016, 1, 1),
    )
    end_date = parse_date_arg(args.end_date, default=date.today())
    mp_db = (
        args.market_positioning_db.expanduser().resolve()
        if args.market_positioning_db
        else Path(expand_env_vars(cfg_get(config, "upstream_databases.market_positioning.db_path"))).expanduser()
    )
    cache_dir = (
        args.cache_dir.expanduser().resolve()
        if args.cache_dir
        else (PROJECT_ROOT / "output" / "market_positioning_cache").resolve()
    )
    if args.tickers_csv:
        tickers_csv = args.tickers_csv.expanduser().resolve()
    elif cfg_bool(config, "positioning_import.include_historical_members", False):
        tickers_csv = build_positioning_universe_csv(
            config,
            base_dir=base_dir,
            output_path=resolve_path(
                cfg_get(config, "positioning_import.positioning_universe_csv", "../output/technology_cache/positioning/semiconductor_positioning_universe.csv"),
                base_dir=base_dir,
            ),
        )
    else:
        tickers_csv = resolve_path(cfg_get(config, "technology_universe.seed_csv"), base_dir=base_dir)
    user_agent = expand_env_vars(args.user_agent or str(cfg_get(config, "yahoo_price_ingestion.user_agent", DEFAULT_USER_AGENT)))

    LOGGER.info("Universe CSV: %s", tickers_csv)
    LOGGER.info("Market positioning DB: %s", mp_db)
    LOGGER.info("History window: %s to %s", history_start, end_date)

    with connect_market_positioning(mp_db) as conn:
        init_market_positioning_db(conn)
        if args.reaggregate_13f_only:
            tickers = load_universe_tickers(tickers_csv)
            with conn:
                snapshot_rows = aggregate_13f_ownership_for_tickers(conn, tickers, source="sec_13f_data_sets")
            LOGGER.info("Reaggregated 13F ownership snapshots: rows=%d tickers=%d", snapshot_rows, len(tickers))
            if not args.skip_technology_import:
                run_technology_import(config_path)
            return
        if not args.skip_finra_short_interest:
            result = sync_finra_equity_short_interest_files(
                conn,
                tickers_csv=tickers_csv,
                history_start_date=history_start,
                end_date=end_date,
                cache_dir=cache_dir / "finra_short_interest",
                user_agent=user_agent,
                max_files=args.finra_max_files,
            )
            LOGGER.info("%s rows=%d message=%s", result.feed_name, result.rows, result.message)
        if not args.skip_13f:
            result = sync_sec_13f_data_sets_streaming(
                conn,
                tickers_csv=tickers_csv,
                cusip_ticker_map_csv=tickers_csv,
                history_start_date=history_start,
                end_date=end_date,
                cache_dir=cache_dir / "sec_13f",
                user_agent=user_agent,
                max_archives=args.sec_13f_max_archives,
            )
            LOGGER.info("%s rows=%d message=%s", result.feed_name, result.rows, result.message)
        if not args.skip_ibkr_borrow:
            try:
                result = sync_ibkr_borrow_availability(
                    conn,
                    tickers_csv=tickers_csv,
                    history_start_date=history_start,
                    end_date=end_date,
                    host=args.ibkr_host,
                    port=args.ibkr_port,
                    client_id=args.ibkr_client_id,
                    market_data_type=args.ibkr_market_data_type,
                    snapshot_wait_sec=args.ibkr_snapshot_wait_sec,
                    max_tickers=args.ibkr_max_tickers,
                )
                LOGGER.info("%s rows=%d message=%s", result.feed_name, result.rows, result.message)
            except (ConnectionError, TimeoutError, OSError) as exc:
                if not args.allow_stale_ibkr_borrow_on_error:
                    raise
                LOGGER.warning(
                    "IBKR borrow refresh unavailable (%s: %s); retaining prior sealed observations. "
                    "The positioning validator will fail if they exceed its staleness tolerance.",
                    type(exc).__name__,
                    exc,
                )

    if not args.skip_technology_import:
        run_technology_import(config_path)


if __name__ == "__main__":
    main()
