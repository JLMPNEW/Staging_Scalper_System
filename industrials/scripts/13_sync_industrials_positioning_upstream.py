#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import io
import logging
import re
import sqlite3
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
from market_positioning.core import backfill_short_interest_float_shares  # noqa: E402
from market_positioning.core import init_db as init_market_positioning_db  # noqa: E402
from market_positioning.core import parse_date as mp_parse_date  # noqa: E402
from market_positioning.core import to_float, update_feed_state, utc_now  # noqa: E402
from industrials.core.config import cfg_get, expand_env_vars, load_yaml, resolve_path  # noqa: E402
from industrials.core.logging_utils import configure_utc_logging  # noqa: E402
from industrials.core.reports import write_csv_atomic  # noqa: E402


LOGGER = logging.getLogger("sync_industrials_positioning_upstream")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"


def parse_date_arg(raw: object, *, default: date) -> date:
    """Parse a CLI/config date, raising loudly on malformed non-empty input.

    A typo'd --history-start/--end-date must never silently degrade to the
    default (that turns a historical rebuild into a current-day build).
    """
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    text = str(raw or "").strip()
    if not text:
        return default
    for separator in ("T", " "):
        if separator in text:
            text = text.split(separator, 1)[0]
            break
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"Unparseable date argument {raw!r}; expected YYYY-MM-DD") from exc


def parse_pit_date_strict(raw: object, *, field: str, context: str = "") -> date | None:
    """Parse a point-in-time metadata date from an override CSV; raise on garbage.

    Empty means "not set"; a malformed value must not fail open into
    "always effective" / "never expires".
    """
    text = str(raw or "").strip()
    if not text:
        return None
    parsed = parse_13f_date(text)
    if parsed is None:
        where = f" for {context}" if context else ""
        raise ValueError(f"Unparseable {field} date {text!r}{where}; expected YYYY-MM-DD")
    return parsed


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
            "Populate market_positioning.sqlite for the configured Industrials universe, "
            "then import those rows into industrials.sqlite."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--model-family", default="", help="Industrials model family to sync, e.g. defense.")
    parser.add_argument("--history-start", default="")
    parser.add_argument("--end-date", default=date.today().isoformat())
    parser.add_argument("--tickers-csv", type=Path, default=None)
    parser.add_argument("--market-positioning-db", type=Path, default=None)
    parser.add_argument("--cache-dir", type=Path, default=None)
    parser.add_argument("--user-agent", default="")
    parser.add_argument("--skip-finra-short-interest", action="store_true")
    parser.add_argument("--skip-13f", action="store_true")
    parser.add_argument(
        "--offline-13f-cache-only",
        action="store_true",
        help=(
            "Rematch the requested universe against already-cached SEC 13F "
            "archives without archive discovery or network requests."
        ),
    )
    parser.add_argument("--skip-ibkr-borrow", action="store_true")
    parser.add_argument("--skip-float-proxy", action="store_true")
    parser.add_argument(
        "--daily-refresh",
        action="store_true",
        help=(
            "Daily as-of mode: skip full upstream FINRA/13F/IBKR history sweeps, "
            "refresh diluted-share proxies, and import positioning features as of --end-date."
        ),
    )
    parser.add_argument(
        "--float-proxy-only",
        action="store_true",
        help="Only load diluted-share float proxies and backfill short-interest pct-float.",
    )
    parser.add_argument("--skip-industrials-import", action="store_true")
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
    parser.add_argument(
        "--ibkr-fee-rate-incremental-duration",
        default="45 D",
        help="IB historical FEE_RATE duration for tickers with existing coverage, e.g. '10 D'.",
    )
    parser.add_argument("--ibkr-max-tickers", type=int, default=0)
    parser.add_argument("--ibkr-snapshot-wait-sec", type=float, default=4.0)
    parser.add_argument(
        "--skip-ibkr-shortable-snapshot",
        action="store_true",
        help=(
            "Load dated IBKR fee-rate history without sampling current shortableShares. "
            "Use for historical catch-up runs so a current observation is not backdated."
        ),
    )
    return parser.parse_args()


SEC_13F_ARCHIVE_YEAR_RE = re.compile(r"(?:19|20)\d{2}")


def cached_sec_13f_archives(
    cache_dir: Path,
    *,
    start_year: int,
    end_year: int,
) -> list[Path]:
    if not cache_dir.is_dir():
        raise FileNotFoundError(
            f"SEC 13F cache directory not found: {cache_dir}"
        )
    selected: list[tuple[int, str, Path]] = []
    for path in cache_dir.glob("*_form13f.zip"):
        years = [
            int(value)
            for value in SEC_13F_ARCHIVE_YEAR_RE.findall(path.name)
        ]
        if not years or max(years) < start_year or min(years) > end_year:
            continue
        if path.stat().st_size <= 0:
            raise ValueError(f"Cached SEC 13F archive is empty: {path}")
        selected.append((min(years), path.name.lower(), path.resolve()))
    archives = [path for _, _, path in sorted(selected)]
    if not archives:
        raise FileNotFoundError(
            "No cached SEC 13F archives overlap "
            f"{start_year}-{end_year} in {cache_dir}"
        )
    return archives


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


def normalize_source_ticker(raw: object) -> str:
    return str(raw or "").strip().upper().replace(".", "-")


def load_positioning_overrides(config: dict[str, Any], *, base_dir: Path, asof: date) -> dict[str, dict[str, str]]:
    """Load positioning overrides effective at the evaluation asof.

    `valid_from` gates effectiveness same-day-inclusive at the evaluation asof
    (med_devices convention); `reviewed_at` is provenance documentation only.
    Rows are never selected against wall-clock today.
    """
    path_value = cfg_get(config, "positioning_import.positioning_overrides_csv", "")
    if not path_value:
        return {}
    path = resolve_path(path_value, base_dir=base_dir)
    overrides: dict[str, dict[str, str]] = {}
    for row in read_csv_rows(path):
        ticker = normalize_source_ticker(row.get("ticker"))
        if not ticker:
            continue
        valid_from = parse_pit_date_strict(row.get("valid_from"), field="valid_from", context=ticker)
        if valid_from is not None and valid_from > asof:
            continue
        overrides[ticker] = {str(key): str(value or "").strip() for key, value in row.items()}
    return overrides


def apply_positioning_override(row: dict[str, str], overrides: dict[str, dict[str, str]]) -> dict[str, str]:
    internal_ticker = normalize_source_ticker(row.get("ticker") or row.get("internal_ticker"))
    override = overrides.get(internal_ticker, {})
    source_ticker = normalize_source_ticker(override.get("source_ticker")) or internal_ticker
    out = dict(row)
    out["ticker"] = source_ticker
    out["internal_ticker"] = internal_ticker
    out["exchange_ticker"] = source_ticker
    out["canonical_ticker"] = internal_ticker
    if override.get("cusip"):
        out["cusip"] = override["cusip"].upper()
    if override.get("short_interest_exempt"):
        out["short_interest_exempt"] = override["short_interest_exempt"]
        out["exemption_reason"] = override.get("exemption_reason", "")
    for key in (
        "institutional_13f_exempt",
        "institutional_13f_exemption_reason",
        "institutional_13f_exempt_until",
        "institutional_13f_issuer_alias",
        "short_pct_float_exempt",
        "short_pct_float_exemption_reason",
        "borrow_exempt",
        "borrow_exemption_reason",
        "form4_exempt",
        "form4_exemption_reason",
        "form4_cik",
        "form4_security_title_regex",
        "form4_security_title_exclude_regex",
    ):
        if override.get(key):
            out[key] = override[key]
    if override.get("ibkr_ticker"):
        out["ibkr_ticker"] = override["ibkr_ticker"].upper()
    if source_ticker != internal_ticker:
        out["source_ticker_alias"] = source_ticker
    return out


def build_positioning_universe_csv(config: dict[str, Any], *, base_dir: Path, output_path: Path, asof: date) -> Path:
    """Build a current+historical ticker map for free FINRA/SEC positioning syncs."""
    seed_path = resolve_path(cfg_get(config, "industrials_universe.seed_csv"), base_dir=base_dir)
    historical_path = resolve_path(cfg_get(config, "industrials_universe.historical_membership_csv"), base_dir=base_dir)
    delisted_path = resolve_path(cfg_get(config, "industrials_universe.delisted_seed_csv"), base_dir=base_dir)
    overrides = load_positioning_overrides(config, base_dir=base_dir, asof=asof)
    family_sector = str(cfg_get(config, "industrials_universe.sector", "Industrials") or "Industrials")
    family_industry = str(cfg_get(config, "industrials_universe.industry", "") or "")
    family_subsector = str(cfg_get(config, "industrials_universe.subsector", family_industry) or family_industry)
    rows_by_ticker: dict[str, dict[str, str]] = {}

    for row in read_csv_rows(seed_path):
        ticker = normalize_source_ticker(row.get("ticker") or row.get("symbol"))
        if not ticker:
            continue
        out = {str(key): str(value or "") for key, value in row.items()}
        out.setdefault("listing_status", "active")
        out.setdefault("source_membership", "current_source_of_truth")
        out = apply_positioning_override(out, overrides)
        rows_by_ticker[out["ticker"]] = out

    if cfg_bool(config, "positioning_import.include_historical_members", False):
        for row in read_csv_rows(historical_path):
            internal_ticker = normalize_source_ticker(row.get("ticker") or row.get("internal_ticker"))
            exchange_ticker = normalize_source_ticker(row.get("exchange_ticker") or internal_ticker)
            if not internal_ticker or not exchange_ticker or exchange_ticker in rows_by_ticker:
                continue
            out = {
                "ticker": exchange_ticker,
                "internal_ticker": internal_ticker,
                "exchange_ticker": exchange_ticker,
                "company_name": str(row.get("company_name") or ""),
                "cik": str(row.get("cik") or ""),
                "exchange": str(row.get("exchange") or ""),
                "sector": family_sector,
                "industry": family_industry,
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
            out = apply_positioning_override(out, overrides)
            rows_by_ticker[out["ticker"]] = out
        for row in read_csv_rows(delisted_path):
            source_ticker = normalize_source_ticker(row.get("ticker"))
            if not source_ticker or source_ticker in rows_by_ticker:
                continue
            out = {
                "ticker": source_ticker,
                "internal_ticker": source_ticker,
                "exchange_ticker": source_ticker,
                "company_name": str(row.get("company") or ""),
                "cik": str(row.get("cik") or ""),
                "exchange": str(row.get("exchange") or ""),
                "sector": family_sector,
                "industry": family_industry,
                "subsector": family_subsector,
                "country": str(row.get("country") or "United States"),
                "currency": str(row.get("currency") or "USD"),
                "security_type": str(row.get("security_type") or "Common Stock"),
                "listing_status": "delisted",
                "is_primary_listing": "FALSE",
                "cusip": str(row.get("cusip") or ""),
                "membership_start_date": "",
                "membership_end_date": str(row.get("exit_year") or ""),
                "successor_ticker": str(row.get("acquirer") or ""),
                "source_membership": "delisted_calibration_seed",
            }
            out = apply_positioning_override(out, overrides)
            rows_by_ticker[out["ticker"]] = out

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames: list[str] = []
    preferred = [
        "ticker", "internal_ticker", "exchange_ticker", "company_name", "cik", "cusip", "exchange", "sector", "industry", "subsector",
        "country", "currency", "security_type", "listing_status", "is_primary_listing",
        "membership_start_date", "membership_end_date", "successor_ticker", "source_membership",
        "canonical_ticker", "source_ticker_alias", "ibkr_ticker", "short_interest_exempt", "exemption_reason",
        "institutional_13f_issuer_alias", "institutional_13f_exempt",
        "institutional_13f_exemption_reason", "institutional_13f_exempt_until",
        "short_pct_float_exempt", "short_pct_float_exemption_reason",
        "borrow_exempt", "borrow_exemption_reason",
        "form4_exempt", "form4_exemption_reason", "form4_cik",
        "form4_security_title_regex", "form4_security_title_exclude_regex",
    ]
    for field in preferred:
        if any(field in row for row in rows_by_ticker.values()):
            fieldnames.append(field)
    for row in rows_by_ticker.values():
        for field in row:
            if field not in fieldnames:
                fieldnames.append(field)
    # This CSV is a functional input to the FINRA/13F/IBKR syncs: a truncated
    # partial write would silently shrink the sync universe, so write atomically.
    write_csv_atomic(
        output_path,
        fieldnames,
        [rows_by_ticker[ticker] for ticker in sorted(rows_by_ticker)],
    )
    LOGGER.info("Built positioning universe CSV: %s rows=%d", output_path, len(rows_by_ticker))
    return output_path


def build_active_borrow_universe_csv(source_path: Path, *, output_path: Path, asof: date) -> Path:
    rows = read_csv_rows(source_path)
    active_rows: list[dict[str, str]] = []
    for row in rows:
        listing_status = str(row.get("listing_status") or "").strip().lower()
        membership_end = str(row.get("membership_end_date") or "").strip()[:10]
        if listing_status != "active" or (membership_end and membership_end < asof.isoformat()):
            continue
        active_rows.append(row)
    if not active_rows:
        raise ValueError(f"No active securities available for IBKR borrow sync in {source_path}")
    fieldnames = list(active_rows[0])
    write_csv_atomic(output_path, fieldnames, active_rows)
    LOGGER.info("Built active IBKR borrow universe CSV: %s rows=%d", output_path, len(active_rows))
    return output_path


def load_source_to_internal_map(tickers_csv: Path) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for row in read_csv_rows(tickers_csv):
        source = normalize_source_ticker(row.get("ticker") or row.get("symbol"))
        internal = normalize_source_ticker(row.get("internal_ticker") or row.get("canonical_ticker") or source)
        if source and internal:
            mapping[source] = internal
    return mapping


DILUTED_SHARES_PROXY_SOURCE = "industrials_sec_diluted_shares_proxy"
SHARE_COUNT_PROXY_SOURCE = "industrials_sec_share_count_proxy"


def ingest_industrials_diluted_share_proxies(
    conn: Any,
    *,
    industrials_db_path: Path,
    source_to_internal: dict[str, str],
    history_start_date: date,
    end_date: date,
    model_family: str,
) -> int:
    """Load canonical diluted shares as a transparent short-interest denominator proxy."""
    if not industrials_db_path.exists():
        raise FileNotFoundError(industrials_db_path)
    internal_to_sources: dict[str, list[str]] = {}
    for source_ticker, internal_ticker in source_to_internal.items():
        internal_to_sources.setdefault(internal_ticker, []).append(source_ticker)
    if not internal_to_sources:
        return 0
    now = utc_now()
    rows_to_upsert: list[tuple[Any, ...]] = []
    # Keyed by (source_ticker, asof) only: the diluted-shares and share-count
    # feeds are folded into ONE winner per key with an explicit rank resolved in
    # Python, so the pct-float denominator never depends on SQLite row order.
    proxy_rows_by_key: dict[tuple[str, str], tuple[tuple[int, str, float, str], tuple[Any, ...]]] = {}

    def add_proxy_row(
        *,
        internal_ticker: str,
        asof: date,
        source_asof: date,
        shares: float,
        source: str,
        accession_number: str,
        priority: int,
        proxy_flag: bool = True,
    ) -> None:
        if shares < 100_000.0:
            return
        for source_ticker in internal_to_sources.get(internal_ticker, []):
            key = (source_ticker, asof.isoformat())
            # Deterministic total order: lower priority wins; ties broken by
            # source name, then larger share count, then accession number.
            rank = (priority, source, -shares, accession_number)
            row = (
                source_ticker,
                asof.isoformat(),
                shares,
                source,
                str(industrials_db_path),
                source_asof.isoformat(),
                asof.isoformat(),
                accession_number,
                None,
                source_asof.isoformat(),
                None,
                "",
                1.0 if proxy_flag else 0.0,
                now,
                now,
            )
            current = proxy_rows_by_key.get(key)
            if current is None or rank < current[0]:
                proxy_rows_by_key[key] = (rank, row)

    industrials_conn = None
    try:
        industrials_conn = sqlite3.connect(f"file:{industrials_db_path.as_posix()}?mode=ro", uri=True)
        industrials_conn.row_factory = sqlite3.Row
        internal_tickers = sorted(internal_to_sources)
        placeholders = ",".join("?" for _ in internal_tickers)
        rows = industrials_conn.execute(
            f"""
            SELECT ticker, period_end, filing_date, accepted_at, accession_number, value
            FROM fact_financial_statement_canonical
            WHERE model_family = ?
              AND canonical_metric = 'diluted_shares'
              AND ticker IN ({placeholders})
              AND COALESCE(value, 0.0) > 0.0
              AND COALESCE(NULLIF(accepted_at, ''), filing_date, period_end) <= ?
            ORDER BY ticker, period_end, filing_date
            """,
            (model_family, *internal_tickers, end_date.isoformat()),
        ).fetchall()
        raw_rows = industrials_conn.execute(
            f"""
            SELECT ticker, period_end, filing_date, accepted_at, accession_number,
                   taxonomy, concept_name, raw_value
            FROM fact_sec_xbrl_fact_raw
            WHERE ticker IN ({placeholders})
              AND UPPER(unit) = 'SHARES'
              AND COALESCE(NULLIF(accepted_at, ''), filing_date, period_end) <= ?
              AND concept_name IN (
                  'AdjustedWeightedAverageShares',
                  'WeightedAverageShares',
                  'NumberOfSharesIssuedAndFullyPaid',
                  'EntityCommonStockSharesOutstanding',
                  'WeightedAverageNumberOfShareOutstandingBasicAndDiluted',
                  'WeightedAverageNumberOfSharesOutstandingBasic',
                  'CommonStockSharesOutstanding'
              )
            ORDER BY ticker, period_end, filing_date
            """,
            (*internal_tickers, end_date.isoformat()),
        ).fetchall()
        try:
            share_rows = industrials_conn.execute(
                f"""
                SELECT ticker, asof_date, source_asof_date, source_id, float_shares,
                       float_method, float_proxy_flag
                FROM fact_share_snapshot
                WHERE model_family = ?
                  AND ticker IN ({placeholders})
                  AND asof_date >= ? AND asof_date <= ?
                  AND COALESCE(float_shares, 0.0) > 0.0
                  AND quality_status = 'accepted'
                ORDER BY ticker, asof_date, source_id
                """,
                (
                    model_family,
                    *internal_tickers,
                    history_start_date.isoformat(),
                    end_date.isoformat(),
                ),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc).lower():
                raise
            # Backward compatibility for isolated pre-v6 fixture databases.
            # Production init_db creates the table before this script runs.
            share_rows = []
    finally:
        if industrials_conn is not None:
            industrials_conn.close()
    # True/public-float observations lead. SEC public-float conversions remain
    # marked proxy; shares-outstanding/diluted-share fallbacks are loaded only
    # after this family-scoped source has been considered.
    for row in share_rows:
        internal_ticker = normalize_source_ticker(row["ticker"])
        shares = to_float(row["float_shares"])
        asof = parse_13f_date(row["asof_date"])
        source_asof = parse_13f_date(row["source_asof_date"]) or asof
        if (
            not internal_ticker
            or shares is None
            or shares <= 0.0
            or asof is None
            or source_asof is None
        ):
            continue
        proxy_flag = bool(row["float_proxy_flag"] or 0)
        add_proxy_row(
            internal_ticker=internal_ticker,
            asof=asof,
            source_asof=source_asof,
            shares=shares,
            source=f"industrials_{str(row['source_id'])}_float",
            accession_number=str(row["float_method"] or ""),
            priority=5 if proxy_flag else 0,
            proxy_flag=proxy_flag,
        )
    for row in rows:
        internal_ticker = normalize_source_ticker(row["ticker"])
        shares = to_float(row["value"])
        if not internal_ticker or shares is None or shares <= 0.0:
            continue
        asof_raw = str(row["accepted_at"] or row["filing_date"] or row["period_end"] or "")
        asof = parse_13f_date(asof_raw)
        source_asof = parse_13f_date(row["period_end"])
        if asof is None or source_asof is None or asof < history_start_date or asof > end_date:
            continue
        add_proxy_row(
            internal_ticker=internal_ticker,
            asof=asof,
            source_asof=source_asof,
            shares=shares,
            source=DILUTED_SHARES_PROXY_SOURCE,
            accession_number=str(row["accession_number"] or ""),
            priority=10,
        )
    concept_priority = {
        "AdjustedWeightedAverageShares": 20,
        "WeightedAverageNumberOfShareOutstandingBasicAndDiluted": 25,
        "WeightedAverageShares": 30,
        "EntityCommonStockSharesOutstanding": 40,
        "NumberOfSharesIssuedAndFullyPaid": 50,
        "CommonStockSharesOutstanding": 60,
        "WeightedAverageNumberOfSharesOutstandingBasic": 70,
    }
    for row in raw_rows:
        internal_ticker = normalize_source_ticker(row["ticker"])
        shares = to_float(row["raw_value"])
        if not internal_ticker or shares is None or shares <= 0.0:
            continue
        asof_raw = str(row["accepted_at"] or row["filing_date"] or row["period_end"] or "")
        asof = parse_13f_date(asof_raw)
        source_asof = parse_13f_date(row["period_end"])
        if asof is None or source_asof is None or asof < history_start_date or asof > end_date:
            continue
        concept = str(row["concept_name"] or "")
        add_proxy_row(
            internal_ticker=internal_ticker,
            asof=asof,
            source_asof=source_asof,
            shares=shares,
            source=SHARE_COUNT_PROXY_SOURCE,
            accession_number=str(row["accession_number"] or ""),
            priority=concept_priority.get(concept, 100),
        )
    rows_to_upsert = [item[1] for item in proxy_rows_by_key.values()]
    if not rows_to_upsert:
        LOGGER.warning(
            "No diluted-share/share-count proxy rows produced from %s for model_family=%s; "
            "existing float_shares_snapshots rows left untouched.",
            industrials_db_path,
            model_family,
        )
        return 0
    all_source_tickers = sorted({st for sources in internal_to_sources.values() for st in sources})
    with conn:
        # Rebuild both proxy feeds for the window atomically: without this, a
        # loser-source row from a prior run coexists with the winner at the same
        # (ticker, asof_date) and the pct-float backfill picks between them by
        # SQLite row order (all its tie-breakers tie within a run).
        ticker_qmarks = ",".join("?" for _ in all_source_tickers)
        conn.execute(
            f"""
            DELETE FROM float_shares_snapshots
            WHERE (source IN (?, ?) OR source LIKE 'industrials_%_float')
              AND asof_date >= ?
              AND asof_date <= ?
              AND ticker IN ({ticker_qmarks})
            """,
            (
                DILUTED_SHARES_PROXY_SOURCE,
                SHARE_COUNT_PROXY_SOURCE,
                history_start_date.isoformat(),
                end_date.isoformat(),
                *all_source_tickers,
            ),
        )
        conn.executemany(
            """
            INSERT INTO float_shares_snapshots(
                ticker, asof_date, float_shares, source, source_file, source_asof_date,
                source_filing_date, source_accession_nodash, public_float_usd,
                public_float_measurement_date, close_price, price_date, proxy_flag,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker, asof_date, source) DO UPDATE SET
                float_shares = excluded.float_shares,
                source_file = excluded.source_file,
                source_asof_date = excluded.source_asof_date,
                source_filing_date = excluded.source_filing_date,
                source_accession_nodash = excluded.source_accession_nodash,
                public_float_measurement_date = excluded.public_float_measurement_date,
                proxy_flag = excluded.proxy_flag,
                updated_at = excluded.updated_at
            """,
            rows_to_upsert,
        )
    update_feed_state(
        conn,
        feed_name="industrials_diluted_share_proxy",
        history_start_date=history_start_date,
        source=DILUTED_SHARES_PROXY_SOURCE,
        source_file=industrials_db_path,
        row_count=len(rows_to_upsert),
        message=f"Industrials diluted-share float proxies rows={len(rows_to_upsert)}",
    )
    return len(rows_to_upsert)


def run_industrials_import(
    config_path: Path,
    *,
    model_family: str,
    asof: date | None = None,
) -> None:
    cmd = [
        sys.executable,
        str(PACKAGE_ROOT / "scripts" / "09_import_industrials_positioning.py"),
        "--config",
        str(config_path),
        "--model-family",
        model_family,
    ]
    if asof is not None:
        cmd.extend(["--asof", asof.isoformat()])
    LOGGER.info("Running industrials positioning import: %s", " ".join(cmd))
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
    skipped_no_filing_date = 0
    for (ticker, period) in sorted(buckets, key=lambda item: (item[0], item[1])):
        bucket = buckets[(ticker, period)]
        if not latest_filing.get((ticker, period)):
            # No knowledge date: substituting period_of_report (quarter-end) would
            # make the aggregate visible up to ~45 days before it was filed. Skip
            # the period entirely rather than leak future data into PIT panels.
            skipped_no_filing_date += 1
            continue
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
                latest_filing[(ticker, period)],
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
    if skipped_no_filing_date:
        LOGGER.warning(
            "Skipped %d 13F ticker-periods with no filing date (would leak quarter-end visibility into PIT panels).",
            skipped_no_filing_date,
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
    archive_paths: list[Path] | None = None,
) -> SyncResult:
    tickers = load_universe_tickers(tickers_csv)
    ticker_set = set(tickers)
    name_map = load_universe_name_map(tickers_csv)
    cusip_map = load_cusip_ticker_map(cusip_ticker_map_csv)
    if not name_map and not cusip_map:
        raise RuntimeError("SEC 13F sync requires ticker/company-name or ticker/CUSIP mapping")

    cache_only = archive_paths is not None
    archives: list[str | Path]
    if archive_paths is None:
        archives = discover_sec_13f_archives(
            index_url=index_url,
            start_year=history_start_date.year,
            end_year=end_date.year,
            user_agent=user_agent,
            timeout_sec=timeout_sec,
        )
    else:
        archives = [path.expanduser().resolve() for path in archive_paths]
    if max_archives and max_archives > 0:
        archives = archives[:max_archives]

    processed_archives = 0
    total_holdings = 0
    ticker_hits: set[str] = set()
    for archive in archives:
        archive_path = (
            archive
            if isinstance(archive, Path)
            else download_cached(
                archive,
                cache_dir=cache_dir,
                user_agent=user_agent,
                timeout_sec=timeout_sec,
            )
        )
        if not archive_path.is_file():
            raise FileNotFoundError(archive_path)
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
        f"industrials_snapshot_rows={snapshot_rows} total_holdings={total_table_holdings} "
        f"cache_only={str(cache_only).lower()} network_requests={0 if cache_only else 'archive_discovery_and_cache_misses'}"
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
    industrials_db = resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    cache_dir = (
        args.cache_dir.expanduser().resolve()
        if args.cache_dir
        else (PROJECT_ROOT / "output" / "market_positioning_cache").resolve()
    )
    model_family = str(
        args.model_family
        or cfg_get(config, "industrials_universe.initial_subsector", "defense")
        or "defense"
    ).strip()
    if args.tickers_csv:
        tickers_csv = args.tickers_csv.expanduser().resolve()
    else:
        # Build the override-applied universe CSV unconditionally: overrides
        # (CUSIP/source-ticker/IBKR mappings, exemptions) must reach the
        # FINRA/13F/IBKR syncs even when historical members are excluded.
        universe_csv_value = str(
            cfg_get(
                config,
                "positioning_import.positioning_universe_csv",
                "../output/industrials_cache/positioning/{model_family}_positioning_universe.csv",
            )
        )
        if "{model_family}" in universe_csv_value:
            universe_csv_value = universe_csv_value.replace("{model_family}", model_family)
        elif model_family != "defense" and "defense" in universe_csv_value.lower():
            raise ValueError(
                "positioning_import.positioning_universe_csv points at a defense-scoped path "
                f"({universe_csv_value!r}) but model_family={model_family!r}; "
                "template {model_family} into the config value to avoid clobbering defense artifacts."
            )
        tickers_csv = build_positioning_universe_csv(
            config,
            base_dir=base_dir,
            output_path=resolve_path(universe_csv_value, base_dir=base_dir),
            asof=end_date,
        )
    user_agent = expand_env_vars(args.user_agent or str(cfg_get(config, "yahoo_price_ingestion.user_agent", DEFAULT_USER_AGENT)))

    LOGGER.info("Universe CSV: %s", tickers_csv)
    LOGGER.info("Market positioning DB: %s", mp_db)
    LOGGER.info("History window: %s to %s", history_start, end_date)
    source_to_internal = load_source_to_internal_map(tickers_csv)

    with connect_market_positioning(mp_db) as conn:
        init_market_positioning_db(conn)
        if args.daily_refresh:
            LOGGER.info("Daily refresh mode: skipping FINRA/13F/IBKR upstream sweeps; feature asof=%s", end_date)
            if not args.skip_float_proxy:
                proxy_rows = ingest_industrials_diluted_share_proxies(
                    conn,
                    industrials_db_path=industrials_db,
                    source_to_internal=source_to_internal,
                    history_start_date=history_start,
                    end_date=end_date,
                    model_family=model_family,
                )
                backfilled = backfill_short_interest_float_shares(conn)
                LOGGER.info("Loaded diluted-share float proxies rows=%d backfilled_short_interest_rows=%d", proxy_rows, backfilled)
            if not args.skip_industrials_import:
                run_industrials_import(
                    config_path,
                    model_family=model_family,
                    asof=end_date,
                )
            return
        if args.float_proxy_only:
            proxy_rows = ingest_industrials_diluted_share_proxies(
                conn,
                industrials_db_path=industrials_db,
                source_to_internal=source_to_internal,
                history_start_date=history_start,
                end_date=end_date,
                model_family=model_family,
            )
            backfilled = backfill_short_interest_float_shares(conn)
            LOGGER.info("Loaded diluted-share float proxies rows=%d backfilled_short_interest_rows=%d", proxy_rows, backfilled)
            if not args.skip_industrials_import:
                run_industrials_import(
                    config_path,
                    model_family=model_family,
                    asof=end_date,
                )
            return
        if args.reaggregate_13f_only:
            tickers = load_universe_tickers(tickers_csv)
            with conn:
                snapshot_rows = aggregate_13f_ownership_for_tickers(conn, tickers, source="sec_13f_data_sets")
            LOGGER.info("Reaggregated 13F ownership snapshots: rows=%d tickers=%d", snapshot_rows, len(tickers))
            if not args.skip_float_proxy:
                proxy_rows = ingest_industrials_diluted_share_proxies(
                    conn,
                    industrials_db_path=industrials_db,
                    source_to_internal=source_to_internal,
                    history_start_date=history_start,
                    end_date=end_date,
                    model_family=model_family,
                )
                backfilled = backfill_short_interest_float_shares(conn)
                LOGGER.info("Loaded diluted-share float proxies rows=%d backfilled_short_interest_rows=%d", proxy_rows, backfilled)
            if not args.skip_industrials_import:
                run_industrials_import(
                    config_path,
                    model_family=model_family,
                    asof=end_date,
                )
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
            offline_archives = (
                cached_sec_13f_archives(
                    cache_dir / "sec_13f",
                    start_year=history_start.year,
                    end_year=end_date.year,
                )
                if args.offline_13f_cache_only
                else None
            )
            result = sync_sec_13f_data_sets_streaming(
                conn,
                tickers_csv=tickers_csv,
                cusip_ticker_map_csv=tickers_csv,
                history_start_date=history_start,
                end_date=end_date,
                cache_dir=cache_dir / "sec_13f",
                user_agent=user_agent,
                max_archives=args.sec_13f_max_archives,
                archive_paths=offline_archives,
            )
            LOGGER.info("%s rows=%d message=%s", result.feed_name, result.rows, result.message)
        if not args.skip_ibkr_borrow:
            ibkr_tickers_csv = build_active_borrow_universe_csv(
                tickers_csv,
                output_path=tickers_csv.with_name(f"{tickers_csv.stem}_ibkr_active.csv"),
                asof=end_date,
            )
            result = sync_ibkr_borrow_availability(
                conn,
                tickers_csv=ibkr_tickers_csv,
                history_start_date=history_start,
                end_date=end_date,
                host=args.ibkr_host,
                port=args.ibkr_port,
                client_id=args.ibkr_client_id,
                market_data_type=args.ibkr_market_data_type,
                fee_rate_incremental_duration=args.ibkr_fee_rate_incremental_duration,
                snapshot_wait_sec=args.ibkr_snapshot_wait_sec,
                max_tickers=args.ibkr_max_tickers,
                shortable_snapshot=not args.skip_ibkr_shortable_snapshot,
            )
            LOGGER.info("%s rows=%d message=%s", result.feed_name, result.rows, result.message)
        if not args.skip_float_proxy:
            proxy_rows = ingest_industrials_diluted_share_proxies(
                conn,
                industrials_db_path=industrials_db,
                source_to_internal=source_to_internal,
                history_start_date=history_start,
                end_date=end_date,
                model_family=model_family,
            )
            backfilled = backfill_short_interest_float_shares(conn)
            LOGGER.info("Loaded diluted-share float proxies rows=%d backfilled_short_interest_rows=%d", proxy_rows, backfilled)

    if not args.skip_industrials_import:
        run_industrials_import(
            config_path,
            model_family=model_family,
            asof=end_date,
        )


if __name__ == "__main__":
    main()

