#!/usr/bin/env python3
"""Populate market_positioning.sqlite for an Industrials family, then import it.

Full mode sweeps FINRA equity short interest, SEC Form 13F data-set archives,
and IBKR borrow availability (dated FEE_RATE history plus a current
shortableShares snapshot), loads diluted-share float proxies, and re-imports
positioning features into industrials.sqlite for the requested --model-family.

--daily-refresh mode is the nightly fast path used by the family refresh
wrappers. It NEVER samples the IBKR shortable snapshot (a daily run must not
backdate a current shortable observation), but every other upstream feed has
an automated daily producer so no gate depends on operator memory. Without
these sweeps ibkr_borrow_fee_rate_daily, FINRA short interest, and 13F had no
automated producer and went stale once manual full-mode runs lapsed.

* IBKR borrow fee-rate: incremental sweep (same build_active_borrow_universe_csv
  + sync_ibkr_borrow_availability machinery as full mode, with
  --ibkr-fee-rate-incremental-duration, default '45 D').
* FINRA short interest: cycle-aware catch-up. The bi-monthly settlement
  calendar plus the 12-day dissemination lag define the newest cycle that can
  already be published; when the family's max loaded settlement_date covers it
  the check is a zero-network no-op, otherwise only the missing cycles are
  swept (cache-friendly incremental window).
* SEC 13F: DERA-window-aware catch-up. SEC DERA publishes the "Form 13F data
  sets" archives bucketed by 3-month FILING-date windows (mar-may, jun-aug,
  sep-nov, dec-feb) a few weeks after each window closes. When the family's
  max loaded filing_date already falls inside the newest completed window the
  check is a zero-network no-op; otherwise ONE index-page probe looks for the
  expected archive name and, once published, ingests exactly that archive.
  While the archive is pending the outcome log counts down the days until
  14_validate's 13F staleness gate (max_13f_staleness_days) arms. The
  fail-closed race here is closed from BOTH sides: this sweep consumes the
  archive the day it appears, and script 09's staleness clock is
  publication-calendar-capped (A13-7, industrials/core/sec_13f_calendar.py) so
  the gate arms only once the archive that must carry the next filing round
  has (worst case) been publishable for a few grace days — it never demands a
  filing DERA cannot yet have published.

All sweeps run BEFORE the positioning re-import so freshly pulled rows flow
into the same run's features.

Daily sweep controls (one pair per feed; each pair is mutually exclusive):
  --skip-daily-ibkr-borrow            / --require-daily-ibkr-borrow
  --skip-daily-finra-short-interest   / --require-daily-finra-short-interest
  --skip-daily-13f                    / --require-daily-13f
By default a sweep failure (source down, IB Gateway unreachable, ...) logs an
ERROR naming the staleness consequence and the daily sync continues; the
fail-closed authority remains 14_validate. --require-* makes that feed's
failure fatal; --skip-* opts the feed out. The full-mode --skip-ibkr-borrow /
--skip-finra-short-interest / --skip-13f flags also suppress the
corresponding daily sweep, and --offline-13f-cache-only additionally
suppresses the daily 13F check (its availability probe is a network GET,
which would violate the flag's zero-network contract).

--selftest validates the daily-mode plan/degradation logic in-process with no
database or network access.
"""
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
import urllib.parse
import zipfile
from datetime import date, datetime, timedelta
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
    finra_settlement_dates,
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
from industrials.core.sec_13f_calendar import (  # noqa: E402
    next_13f_publishable_date,
    sec_13f_staleness_arming_date,
    sec_13f_snapshot_is_stale,
)


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


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
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
            "archives without archive discovery or network requests. With "
            "--daily-refresh this also suppresses the daily 13F availability "
            "check (its probe is a network GET); conflicts with "
            "--require-daily-13f."
        ),
    )
    parser.add_argument("--skip-ibkr-borrow", action="store_true")
    parser.add_argument("--skip-float-proxy", action="store_true")
    parser.add_argument(
        "--daily-refresh",
        action="store_true",
        help=(
            "Daily as-of mode: never sample the IBKR shortable snapshot; run the "
            "incremental IBKR borrow fee-rate sweep, the cycle-aware FINRA "
            "short-interest catch-up, and the DERA-window-aware 13F catch-up "
            "(each only when new source data can exist), refresh diluted-share "
            "proxies, and import positioning features as of --end-date."
        ),
    )
    daily_borrow_group = parser.add_mutually_exclusive_group()
    daily_borrow_group.add_argument(
        "--skip-daily-ibkr-borrow",
        action="store_true",
        help=(
            "With --daily-refresh: skip the incremental IBKR borrow fee-rate sweep "
            "(legacy skip-everything daily behavior)."
        ),
    )
    daily_borrow_group.add_argument(
        "--require-daily-ibkr-borrow",
        action="store_true",
        help=(
            "With --daily-refresh: fail the run if the IBKR borrow fee-rate sweep "
            "fails, instead of logging an ERROR and continuing."
        ),
    )
    daily_finra_group = parser.add_mutually_exclusive_group()
    daily_finra_group.add_argument(
        "--skip-daily-finra-short-interest",
        action="store_true",
        help=(
            "With --daily-refresh: skip the cycle-aware FINRA short-interest "
            "availability check and catch-up sweep."
        ),
    )
    daily_finra_group.add_argument(
        "--require-daily-finra-short-interest",
        action="store_true",
        help=(
            "With --daily-refresh: fail the run if the FINRA short-interest "
            "availability check or catch-up sweep fails, instead of logging an "
            "ERROR and continuing."
        ),
    )
    daily_13f_group = parser.add_mutually_exclusive_group()
    daily_13f_group.add_argument(
        "--skip-daily-13f",
        action="store_true",
        help=(
            "With --daily-refresh: skip the DERA-window-aware SEC 13F "
            "availability check and catch-up ingest."
        ),
    )
    daily_13f_group.add_argument(
        "--require-daily-13f",
        action="store_true",
        help=(
            "With --daily-refresh: fail the run if the SEC 13F availability "
            "check or catch-up ingest fails, instead of logging an ERROR and "
            "continuing."
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
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="Validate daily-mode sweep planning and degradation logic in-process; no DB/network access.",
    )
    return parser.parse_args(argv)


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


def plan_daily_upstream_sweeps(
    *,
    skip_daily_ibkr_borrow: bool,
    skip_ibkr_borrow: bool,
    require_daily_ibkr_borrow: bool,
    skip_daily_finra_short_interest: bool = False,
    skip_finra_short_interest: bool = False,
    require_daily_finra_short_interest: bool = False,
    skip_daily_13f: bool = False,
    skip_13f: bool = False,
    require_daily_13f: bool = False,
    offline_13f_cache_only: bool = False,
) -> dict[str, str]:
    """Decide which upstream sweeps the --daily-refresh path runs.

    The IBKR shortable snapshot is ALWAYS skipped in daily mode (a daily run
    must never backdate a current shortable observation). The incremental IBKR
    borrow fee-rate sweep runs by default, and FINRA short interest / SEC 13F
    are planned as availability CHECKS: cheap cycle-/DERA-window-aware probes
    that ingest incrementally only when the source can hold data the DB does
    not. Without automated producers all three feeds went stale once manual
    full-mode runs lapsed (borrow fails closed via 14_validate's staleness
    gate; FINRA degrades silently; 13F fails closed after
    max_13f_staleness_days). Family-agnostic: the caller scopes the universe
    CSV per family.

    --offline-13f-cache-only declares zero-network intent for 13F, and the
    daily 13F availability check makes an index-page GET once the newest DERA
    window is not yet ingested — so the offline flag suppresses the daily 13F
    check entirely (and conflicts with --require-daily-13f).
    """
    borrow_skipped = skip_daily_ibkr_borrow or skip_ibkr_borrow
    if require_daily_ibkr_borrow and borrow_skipped:
        raise ValueError(
            "--require-daily-ibkr-borrow conflicts with "
            "--skip-daily-ibkr-borrow/--skip-ibkr-borrow"
        )
    finra_skipped = skip_daily_finra_short_interest or skip_finra_short_interest
    if require_daily_finra_short_interest and finra_skipped:
        raise ValueError(
            "--require-daily-finra-short-interest conflicts with "
            "--skip-daily-finra-short-interest/--skip-finra-short-interest"
        )
    sec_13f_skipped = skip_daily_13f or skip_13f or offline_13f_cache_only
    if require_daily_13f and sec_13f_skipped:
        raise ValueError(
            "--require-daily-13f conflicts with "
            "--skip-daily-13f/--skip-13f/--offline-13f-cache-only"
        )
    return {
        "finra_short_interest": "skip" if finra_skipped else "check",
        "sec_13f": "skip" if sec_13f_skipped else "check",
        "ibkr_shortable_snapshot": "skip",
        "ibkr_borrow_fee_rate": "skip" if borrow_skipped else "run",
        "borrow_failure_mode": "fatal" if require_daily_ibkr_borrow else "degrade",
        "finra_failure_mode": "fatal" if require_daily_finra_short_interest else "degrade",
        "sec_13f_failure_mode": "fatal" if require_daily_13f else "degrade",
    }


def borrow_staleness_consequence(conn: Any, config: dict[str, Any], end_date: date) -> str:
    """Describe what a missed daily borrow sweep costs, for the degraded-mode ERROR log."""
    try:
        max_staleness = int(cfg_get(config, "positioning_import.max_borrow_staleness_days", 10) or 10)
    except (TypeError, ValueError):
        max_staleness = 10
    last_asof: date | None = None
    try:
        row = conn.execute("SELECT MAX(asof_date) FROM ibkr_borrow_fee_rate_daily").fetchone()
        last_asof = parse_13f_date(row[0]) if row else None
    except Exception:  # noqa: BLE001 - a missing table must not mask the primary sweep error
        last_asof = None
    if last_asof is None:
        return (
            "borrow staleness gate in 14_validate will fail closed once borrow data "
            f"exceeds max_borrow_staleness_days={max_staleness}"
        )
    remaining = max(max_staleness - (end_date - last_asof).days, 0)
    return (
        f"borrow staleness gate in 14_validate will fail closed in {remaining} day(s) "
        f"(last borrow asof={last_asof.isoformat()}, max_borrow_staleness_days={max_staleness})"
    )


def execute_daily_ibkr_borrow_sweep(
    conn: Any,
    *,
    tickers_csv: Path,
    history_start: date,
    end_date: date,
    require_success: bool,
    staleness_consequence: str,
    sweep_kwargs: dict[str, Any],
    universe_builder: Any = build_active_borrow_universe_csv,
    sweep_fn: Any = sync_ibkr_borrow_availability,
) -> dict[str, Any]:
    """Run the incremental IBKR borrow fee-rate sweep for the daily path.

    Always passes shortable_snapshot=False: a daily run must never backdate a
    current shortableShares observation. It also disables left-edge history
    repair; daily mode only extends the latest fee-rate tail, while explicit
    full mode retains the historical backfill contract. By default a failure (IB Gateway
    unreachable, empty active universe, ...) degrades gracefully: an ERROR
    names the staleness consequence and the daily sync continues, because the
    fail-closed authority is 14_validate's staleness gate. With
    require_success=True (--require-daily-ibkr-borrow) the failure re-raises.
    """
    try:
        ibkr_tickers_csv = universe_builder(
            tickers_csv,
            output_path=tickers_csv.with_name(f"{tickers_csv.stem}_ibkr_active.csv"),
            asof=end_date,
        )
        result = sweep_fn(
            conn,
            tickers_csv=ibkr_tickers_csv,
            history_start_date=history_start,
            end_date=end_date,
            shortable_snapshot=False,
            backfill_fee_history_left_edge=False,
            **sweep_kwargs,
        )
    except Exception as exc:  # noqa: BLE001 - degraded mode must survive any sweep failure
        if require_success:
            raise
        LOGGER.error(
            "Daily IBKR borrow fee-rate sweep FAILED (%s: %s); continuing daily sync "
            "without fresh borrow rows: %s.",
            type(exc).__name__,
            exc,
            staleness_consequence,
        )
        return {
            "feed": "ibkr_borrow_fee_rate",
            "status": "degraded",
            "rows": 0,
            "message": f"{type(exc).__name__}: {exc}",
        }
    return {
        "feed": result.feed_name,
        "status": "ran",
        "rows": result.rows,
        "message": result.message,
    }


def load_active_universe_tickers(tickers_csv: Path, *, asof: date) -> list[str]:
    """Active-listing tickers from the positioning universe CSV (same filter as the borrow sweep)."""
    tickers: set[str] = set()
    for row in read_csv_rows(tickers_csv):
        listing_status = str(row.get("listing_status") or "").strip().lower()
        membership_end = str(row.get("membership_end_date") or "").strip()[:10]
        if listing_status != "active" or (membership_end and membership_end < asof.isoformat()):
            continue
        ticker = normalize_source_ticker(row.get("ticker") or row.get("symbol"))
        if ticker:
            tickers.add(ticker)
    return sorted(tickers)


# --- daily FINRA short-interest cycle awareness ------------------------------

FINRA_PUBLICATION_LAG_DAYS = 12  # dissemination lag used by the file sync's PIT stamps


def latest_published_finra_settlement(
    end_date: date,
    *,
    publication_lag_days: int = FINRA_PUBLICATION_LAG_DAYS,
    cycle_dates_fn: Any = finra_settlement_dates,
) -> date | None:
    """Newest bi-monthly FINRA settlement whose file can already be published.

    Pure date math (settlement calendar + dissemination lag); no network.
    """
    candidates = cycle_dates_fn(end_date - timedelta(days=180), end_date)
    published = [
        settlement
        for settlement in candidates
        if settlement + timedelta(days=max(0, publication_lag_days)) <= end_date
    ]
    return max(published) if published else None


def family_max_finra_settlement(conn: Any, tickers: list[str]) -> date | None:
    """Newest loaded FINRA settlement for this family's universe (family-scoped:
    the shared DB is also written by other packages for THEIR tickers)."""
    if not tickers:
        return None
    qmarks = ",".join("?" for _ in tickers)
    row = conn.execute(
        f"""
        SELECT MAX(settlement_date) FROM short_interest_snapshots
        WHERE source = 'finra_equity_short_interest_files'
          AND UPPER(ticker) IN ({qmarks})
        """,
        [ticker.upper() for ticker in tickers],
    ).fetchone()
    return parse_13f_date(row[0]) if row else None


def finra_staleness_consequence(conn: Any, config: dict[str, Any], *, tickers: list[str], end_date: date) -> str:
    """Describe what a missed FINRA cycle costs, for the degraded-mode ERROR log.

    Unlike borrow (10d) and 13F (120d) there is NO fail-closed staleness gate
    for short interest in 14_validate (presence-only), so the honest
    consequence is silent feature degradation.
    """
    try:
        lookback = int(cfg_get(config, "positioning_import.lookback_days.short_change", 92) or 92)
    except (TypeError, ValueError):
        lookback = 92
    loaded: date | None = None
    try:
        loaded = family_max_finra_settlement(conn, tickers)
    except Exception:  # noqa: BLE001 - a missing table must not mask the primary sweep error
        loaded = None
    if loaded is None:
        return (
            "14_validate has NO short-interest staleness gate (presence-only), so missed "
            "FINRA cycles degrade features silently; no family cycles loaded yet"
        )
    age_days = (end_date - loaded).days
    return (
        "14_validate has NO short-interest staleness gate (presence-only), so features go "
        f"quietly stale: newest loaded settlement={loaded.isoformat()} ({age_days} day(s) old); "
        f"short_interest_change_3m loses its prior once the settlement gap exceeds the "
        f"{lookback}-day short_change lookback"
    )


def execute_daily_finra_short_interest_sweep(
    conn: Any,
    *,
    tickers: list[str],
    tickers_csv: Path,
    history_start: date,
    end_date: date,
    require_success: bool,
    staleness_consequence: str,
    sweep_kwargs: dict[str, Any],
    publication_lag_days: int = FINRA_PUBLICATION_LAG_DAYS,
    sweep_fn: Any = sync_finra_equity_short_interest_files,
    cycle_dates_fn: Any = finra_settlement_dates,
) -> dict[str, Any]:
    """Cycle-aware daily FINRA catch-up: ingest only when a newer cycle exists.

    Availability is a zero-network check (settlement calendar vs the family's
    max loaded settlement_date); only when the source can hold a newer cycle
    does the incremental sweep run, bounded to the missing settlement window so
    it stays cache-friendly. Failure degrades non-fatally by default (ERROR
    naming the consequence); require_success (--require-daily-finra-short-
    interest) makes it fatal.
    """
    feed = "finra_short_interest"
    try:
        expected = latest_published_finra_settlement(
            end_date,
            publication_lag_days=publication_lag_days,
            cycle_dates_fn=cycle_dates_fn,
        )
        if expected is None:
            return {
                "feed": feed,
                "status": "no_new_data",
                "rows": 0,
                "message": f"no FINRA cycle publishable on or before {end_date.isoformat()}; no network request made",
            }
        loaded = family_max_finra_settlement(conn, tickers)
        cycle_was_current = loaded is not None and loaded >= expected
        catchup_start = (
            max(history_start, loaded) if loaded is not None else history_start
        )
        result = sweep_fn(
            conn,
            tickers_csv=tickers_csv,
            history_start_date=catchup_start,
            end_date=end_date,
            **sweep_kwargs,
        )
        loaded_after = family_max_finra_settlement(conn, tickers)
    except Exception as exc:  # noqa: BLE001 - degraded mode must survive any sweep failure
        if require_success:
            raise
        LOGGER.error(
            "Daily FINRA short-interest sweep FAILED (%s: %s); continuing daily sync "
            "without fresh cycles: %s.",
            type(exc).__name__,
            exc,
            staleness_consequence,
        )
        return {
            "feed": feed,
            "status": "degraded",
            "rows": 0,
            "message": f"{type(exc).__name__}: {exc}",
        }
    if loaded_after is None or loaded_after < expected:
        # The sweep ran but the family's newest settlement did not advance to
        # the expected cycle: the file is not on the CDN yet (publication later
        # than the dissemination-lag heuristic, or a holiday-shifted file name)
        # or it held no family symbols. Reporting 'ran' here would overstate
        # activity; the bounded retry repeats nightly at cache-hit cost.
        return {
            "feed": feed,
            "status": "no_new_data",
            "rows": 0,
            "message": (
                f"expected cycle settlement={expected.isoformat()} still absent after sweeping "
                f"{catchup_start.isoformat()}..{end_date.isoformat()} (file not yet published or "
                f"no family symbols in file; nightly retry is cache-friendly): {result.message}"
            ),
        }
    if cycle_was_current:
        return {
            "feed": feed,
            "status": "no_new_data",
            "rows": 0,
            "message": (
                f"newest publishable cycle settlement={expected.isoformat()} was already loaded; "
                "replayed that cycle inclusively to repair partial-family coverage: "
                f"{result.message}"
            ),
        }
    return {
        "feed": result.feed_name,
        "status": "ran",
        "rows": result.rows,
        "message": (
            f"caught up cycles from {catchup_start.isoformat()} toward "
            f"settlement={expected.isoformat()}: {result.message}"
        ),
    }


# --- daily SEC 13F DERA-window awareness -------------------------------------

SEC_13F_MONTH_ABBREVIATIONS = (
    "jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec",
)
SEC_13F_WINDOW_START_MONTHS = (3, 6, 9, 12)  # mar-may, jun-aug, sep-nov, dec-feb


def latest_completed_sec_13f_window(asof: date) -> tuple[date, date]:
    """(start, end) of the newest closed DERA 13F filing-date window at asof.

    SEC DERA buckets "Form 13F data sets" archives by 3-month FILING-date
    windows (mar-may, jun-aug, sep-nov, dec-feb) and can only publish an
    archive after its window closes (observed lag <= ~2.5 weeks).
    """
    month = asof.month
    if month >= 12:
        current_start = date(asof.year, 12, 1)
    elif month >= 9:
        current_start = date(asof.year, 9, 1)
    elif month >= 6:
        current_start = date(asof.year, 6, 1)
    elif month >= 3:
        current_start = date(asof.year, 3, 1)
    else:
        current_start = date(asof.year - 1, 12, 1)
    if current_start.month == 3:
        completed_start = date(current_start.year - 1, 12, 1)
    else:
        completed_start = date(current_start.year, current_start.month - 3, 1)
    return completed_start, current_start - timedelta(days=1)


def sec_13f_archive_name(window_start: date, window_end: date) -> str:
    """Deterministic DERA archive basename, e.g. 01mar2026-31may2026_form13f.zip (locale-safe)."""

    def fmt(day: date) -> str:
        return f"{day.day:02d}{SEC_13F_MONTH_ABBREVIATIONS[day.month - 1]}{day.year:04d}"

    return f"{fmt(window_start)}-{fmt(window_end)}_form13f.zip"


def family_max_13f_filing_date(conn: Any, tickers: list[str]) -> date | None:
    """Newest loaded 13F filing_date for this family's universe (family-scoped:
    other packages ingest the same archives for THEIR tickers only)."""
    if not tickers:
        return None
    qmarks = ",".join("?" for _ in tickers)
    row = conn.execute(
        f"""
        SELECT MAX(filing_date) FROM institutional_13f_holdings
        WHERE source = 'sec_13f_data_sets'
          AND UPPER(ticker) IN ({qmarks})
        """,
        [ticker.upper() for ticker in tickers],
    ).fetchone()
    return parse_13f_date(row[0]) if row else None


def daily_13f_marker_covers(conn: Any, feed_name: str, archive_name: str) -> bool:
    """True when the per-family daily marker says archive_name was already processed.

    Needed only when an archive yields ZERO family matches (family max
    filing_date cannot advance), so the daily check would otherwise re-download
    and re-parse the archive every night until the next window closes.
    """
    try:
        row = conn.execute(
            "SELECT message FROM market_positioning_feed_state WHERE feed_name = ?",
            (feed_name,),
        ).fetchone()
    except sqlite3.Error:
        return False
    return bool(row and archive_name in str(row[0] or ""))


def sec_13f_staleness_consequence(conn: Any, config: dict[str, Any], *, tickers: list[str], end_date: date) -> str:
    """Describe when 14_validate's 13F staleness gate arms, for daily log lines.

    Uses the first per-ticker ARMING date among the given (active) tickers:
    script 09 NULLs a ticker's 13F features once its latest fact asof breaches
    the publication-calendar-capped staleness clock (A13-7, industrials/core/
    sec_13f_calendar.py — arming = max(last_filing + max_13f_staleness_days + 1,
    worst-case publication of the next DERA archive + grace)), and 14_validate
    then fails closed. Mirroring the capped clock, period_of_report included,
    keeps the countdown from announcing a failure date the gate cannot actually
    fire on (or from overshooting past the real arming date).
    """
    # int(raw) directly (no `or 120`): an explicit 0 means the gate is
    # disabled in script 09, and this message must not claim otherwise.
    try:
        max_staleness = int(cfg_get(config, "positioning_import.max_13f_staleness_days", 120))
    except (TypeError, ValueError):
        max_staleness = 120
    if max_staleness <= 0:
        return (
            f"13F staleness gate disabled (max_13f_staleness_days={max_staleness}); "
            "missed archives degrade features silently"
        )
    earliest: date | None = None
    armed_on: date | None = None
    try:
        if tickers:
            qmarks = ",".join("?" for _ in tickers)
            rows = conn.execute(
                f"""
                SELECT MAX(filing_date) AS latest_filing,
                       MAX(period_of_report) AS latest_period
                FROM institutional_13f_holdings
                WHERE source = 'sec_13f_data_sets'
                  AND UPPER(ticker) IN ({qmarks})
                GROUP BY UPPER(ticker)
                """,
                [ticker.upper() for ticker in tickers],
            ).fetchall()
            for row in rows:
                latest_filing = parse_13f_date(row[0])
                if latest_filing is None:
                    continue
                candidate = sec_13f_staleness_arming_date(
                    last_filing=latest_filing,
                    period_of_report=parse_13f_date(row[1]),
                    max_staleness_days=max_staleness,
                )
                if candidate is not None and (armed_on is None or candidate < armed_on):
                    armed_on = candidate
                    earliest = latest_filing
    except Exception:  # noqa: BLE001 - a missing table must not mask the primary sweep error
        earliest = None
        armed_on = None
    if earliest is None or armed_on is None:
        return (
            "13F staleness gate in 14_validate will fail closed once family 13F coverage "
            f"exceeds max_13f_staleness_days={max_staleness}"
        )
    remaining = max((armed_on - end_date).days, 0)
    return (
        f"13F staleness gate in 14_validate begins failing closed in {remaining} day(s) "
        f"on {armed_on.isoformat()} (earliest family last-filing asof={earliest.isoformat()}, "
        f"max_13f_staleness_days={max_staleness}, publication-calendar-capped so the gate "
        "never arms before the next DERA archive can publish)"
    )


def purge_partial_daily_13f_archive_rows(conn: Any, *, tickers: list[str], archive_path: Path) -> int:
    """Best-effort cleanup after a FAILED daily archive ingest.

    upsert_13f_records commits per 5000-row batch, so a crash mid-parse can
    leave partial family holdings whose MAX(filing_date) already falls inside
    the newest DERA window; the availability short-circuit would then read the
    archive as ingested and silently never load the remaining managers (no
    scheduled full-mode run exists to repair it). Deleting this family's rows
    for the failed archive keeps the nightly check retrying until a clean
    ingest writes the success marker. Scoped to (source, source_file, family
    tickers) so other packages' rows from the same shared archive survive. A
    hard process kill still strands partial rows (no exception path runs);
    any manual full-mode run repairs those.
    """
    if not tickers:
        return 0
    resolved = str(Path(archive_path).expanduser().resolve())
    qmarks = ",".join("?" for _ in tickers)
    cursor = conn.execute(
        f"""
        DELETE FROM institutional_13f_holdings
        WHERE source = 'sec_13f_data_sets'
          AND source_file = ?
          AND UPPER(ticker) IN ({qmarks})
        """,
        [resolved, *[ticker.upper() for ticker in tickers]],
    )
    conn.commit()
    return int(cursor.rowcount or 0)


def execute_daily_sec_13f_sweep(
    conn: Any,
    *,
    tickers: list[str],
    tickers_csv: Path,
    history_start: date,
    end_date: date,
    require_success: bool,
    staleness_consequence: str,
    marker_feed_name: str,
    cache_dir: Path,
    user_agent: str,
    index_url: str = DEFAULT_SEC_13F_DATASETS_URL,
    timeout_sec: float = 120.0,
    discover_fn: Any = discover_sec_13f_archives,
    download_fn: Any = download_cached,
    sweep_fn: Any = None,  # defaults to sync_sec_13f_data_sets_streaming (defined below)
) -> dict[str, Any]:
    """DERA-window-aware daily 13F catch-up: ingest exactly the newest archive.

    Zero network while the family's max loaded filing_date already falls inside
    the newest completed DERA window (or the per-family marker says the archive
    was processed with zero matches). Otherwise ONE index-page probe looks for
    the expected archive name; while unpublished the outcome is no_new_data
    with the staleness countdown, and once published only that archive is
    downloaded and streamed (single-threaded GETs with the repo User-Agent -
    far below SEC fair-access limits). Failure degrades non-fatally by default;
    require_success (--require-daily-13f) makes it fatal.
    """
    feed = "sec_13f"
    if sweep_fn is None:
        sweep_fn = sync_sec_13f_data_sets_streaming
    window_start, window_end = latest_completed_sec_13f_window(end_date)
    expected_archive = sec_13f_archive_name(window_start, window_end)
    try:
        family_max = family_max_13f_filing_date(conn, tickers)
        if family_max is not None and family_max >= window_start:
            return {
                "feed": feed,
                "status": "no_new_data",
                "rows": 0,
                "message": (
                    f"newest completed DERA window {window_start.isoformat()}..{window_end.isoformat()} "
                    f"already ingested (family max filing_date={family_max.isoformat()}); "
                    "no network request made"
                ),
            }
        if daily_13f_marker_covers(conn, marker_feed_name, expected_archive):
            return {
                "feed": feed,
                "status": "no_new_data",
                "rows": 0,
                "message": (
                    f"archive {expected_archive} already processed with zero family matches "
                    "(per-family marker); no network request made"
                ),
            }
        archive_urls = discover_fn(
            index_url=index_url,
            start_year=window_start.year,
            end_year=end_date.year,
            user_agent=user_agent,
            timeout_sec=timeout_sec,
        )
        target_url = next(
            (
                url
                for url in archive_urls
                if Path(urllib.parse.urlparse(str(url)).path).name.lower() == expected_archive
            ),
            None,
        )
        if target_url is None:
            return {
                "feed": feed,
                "status": "no_new_data",
                "rows": 0,
                "message": (
                    f"expected DERA archive {expected_archive} not yet published "
                    f"(filing window closed {window_end.isoformat()}, observed lag <= ~2.5 weeks); "
                    f"{staleness_consequence}"
                ),
            }
        archive_path = download_fn(
            target_url,
            cache_dir=cache_dir,
            user_agent=user_agent,
            timeout_sec=timeout_sec,
        )
        try:
            result = sweep_fn(
                conn,
                tickers_csv=tickers_csv,
                cusip_ticker_map_csv=tickers_csv,
                history_start_date=history_start,
                end_date=end_date,
                cache_dir=cache_dir,
                user_agent=user_agent,
                archive_paths=[archive_path],
                # The archive WAS fetched over the network this run; keeps the
                # shared feed-state message from claiming cache_only/zero
                # network requests for a night that downloaded it.
                network_fetched_archives=True,
            )
        except BaseException:
            # Partial-batch commits must not poison the availability check
            # (see purge_partial_daily_13f_archive_rows); purge before the
            # degraded/fatal handling so the next nightly run retries cleanly.
            try:
                purged = purge_partial_daily_13f_archive_rows(
                    conn, tickers=tickers, archive_path=archive_path
                )
                if purged:
                    LOGGER.warning(
                        "Purged %d partial family 13F rows after failed ingest of %s; "
                        "the nightly check will retry the archive.",
                        purged,
                        expected_archive,
                    )
            except Exception as cleanup_exc:  # noqa: BLE001 - cleanup must not mask the ingest error
                LOGGER.error(
                    "Could not purge partial 13F rows for %s after failed ingest: %s",
                    expected_archive,
                    cleanup_exc,
                )
            raise
        update_feed_state(
            conn,
            feed_name=marker_feed_name,
            history_start_date=history_start,
            source="sec_13f_data_sets",
            source_file=archive_path,
            row_count=result.rows,
            message=f"daily 13F sweep processed archive={expected_archive}; {result.message}",
        )
    except Exception as exc:  # noqa: BLE001 - degraded mode must survive any sweep failure
        if require_success:
            raise
        LOGGER.error(
            "Daily SEC 13F sweep FAILED (%s: %s) while targeting archive %s; continuing "
            "daily sync without fresh 13F rows: %s.",
            type(exc).__name__,
            exc,
            expected_archive,
            staleness_consequence,
        )
        return {
            "feed": feed,
            "status": "degraded",
            "rows": 0,
            "message": f"{type(exc).__name__}: {exc}",
        }
    return {
        "feed": result.feed_name,
        "status": "ran",
        "rows": result.rows,
        "message": f"ingested {expected_archive}: {result.message}",
    }


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
    network_fetched_archives: bool = False,
) -> SyncResult:
    tickers = load_universe_tickers(tickers_csv)
    ticker_set = set(tickers)
    name_map = load_universe_name_map(tickers_csv)
    cusip_map = load_cusip_ticker_map(cusip_ticker_map_csv)
    if not name_map and not cusip_map:
        raise RuntimeError("SEC 13F sync requires ticker/company-name or ticker/CUSIP mapping")

    # archive_paths alone does NOT imply an offline run: the daily sweep passes
    # the archive it just downloaded. network_fetched_archives keeps the feed
    # state message honest about that night's network activity.
    cache_only = archive_paths is not None and not network_fetched_archives
    archives: list[str] | list[Path]
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
    if cache_only:
        network_note = "0"
    elif archive_paths is not None:
        network_note = "targeted_archive_download"
    else:
        network_note = "archive_discovery_and_cache_misses"
    message = (
        f"SEC Form 13F data-set archives processed={processed_archives} "
        f"new_or_refreshed_matched_holdings={total_holdings} matched_tickers={len(ticker_hits)} "
        f"industrials_snapshot_rows={snapshot_rows} total_holdings={total_table_holdings} "
        f"cache_only={str(cache_only).lower()} network_requests={network_note}"
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


def run_selftest() -> int:
    """In-process checks for the daily-mode sweep plan and degradation contract.

    No real database, config, or network access; fakes are injected for the
    borrow universe builder, the IBKR/FINRA/13F sweeps, and the SEC archive
    discovery/download, with throwaway in-memory SQLite fixtures for the
    availability checks and staleness-consequence messages.
    """
    # --- argparse surface: defaults and mutual exclusion -------------------
    args = parse_args(["--daily-refresh"])
    assert args.daily_refresh
    assert not args.skip_daily_ibkr_borrow and not args.require_daily_ibkr_borrow
    assert not args.skip_daily_finra_short_interest and not args.require_daily_finra_short_interest
    assert not args.skip_daily_13f and not args.require_daily_13f
    assert args.ibkr_fee_rate_incremental_duration == "45 D", args.ibkr_fee_rate_incremental_duration
    for exclusive_pair in (
        ["--skip-daily-ibkr-borrow", "--require-daily-ibkr-borrow"],
        ["--skip-daily-finra-short-interest", "--require-daily-finra-short-interest"],
        ["--skip-daily-13f", "--require-daily-13f"],
    ):
        try:
            parse_args(["--daily-refresh", *exclusive_pair])
            raise AssertionError(f"mutually exclusive daily flags unexpectedly accepted: {exclusive_pair}")
        except SystemExit:
            pass

    # --- plan: daily path runs the borrow sweep and the FINRA/13F availability
    # CHECKS by default; the shortable snapshot is never planned --------------
    default_plan = plan_daily_upstream_sweeps(
        skip_daily_ibkr_borrow=False, skip_ibkr_borrow=False, require_daily_ibkr_borrow=False
    )
    assert default_plan["ibkr_borrow_fee_rate"] == "run", default_plan
    assert default_plan["borrow_failure_mode"] == "degrade", default_plan
    assert default_plan["finra_short_interest"] == "check", default_plan
    assert default_plan["sec_13f"] == "check", default_plan
    assert default_plan["finra_failure_mode"] == "degrade", default_plan
    assert default_plan["sec_13f_failure_mode"] == "degrade", default_plan
    fatal_plan = plan_daily_upstream_sweeps(
        skip_daily_ibkr_borrow=False,
        skip_ibkr_borrow=False,
        require_daily_ibkr_borrow=True,
        require_daily_finra_short_interest=True,
        require_daily_13f=True,
    )
    assert fatal_plan["borrow_failure_mode"] == "fatal", fatal_plan
    assert fatal_plan["finra_failure_mode"] == "fatal", fatal_plan
    assert fatal_plan["sec_13f_failure_mode"] == "fatal", fatal_plan
    skip_plan = plan_daily_upstream_sweeps(
        skip_daily_ibkr_borrow=True,
        skip_ibkr_borrow=False,
        require_daily_ibkr_borrow=False,
        skip_daily_finra_short_interest=True,
        skip_daily_13f=True,
    )
    assert skip_plan["ibkr_borrow_fee_rate"] == "skip", skip_plan
    assert skip_plan["finra_short_interest"] == "skip", skip_plan
    assert skip_plan["sec_13f"] == "skip", skip_plan
    legacy_skip_plan = plan_daily_upstream_sweeps(
        skip_daily_ibkr_borrow=False,
        skip_ibkr_borrow=True,
        require_daily_ibkr_borrow=False,
        skip_finra_short_interest=True,
        skip_13f=True,
    )
    assert legacy_skip_plan["ibkr_borrow_fee_rate"] == "skip", legacy_skip_plan
    assert legacy_skip_plan["finra_short_interest"] == "skip", (
        "full-mode --skip-finra-short-interest must suppress the daily check"
    )
    assert legacy_skip_plan["sec_13f"] == "skip", (
        "full-mode --skip-13f must suppress the daily check"
    )
    offline_plan = plan_daily_upstream_sweeps(
        skip_daily_ibkr_borrow=False,
        skip_ibkr_borrow=False,
        require_daily_ibkr_borrow=False,
        offline_13f_cache_only=True,
    )
    assert offline_plan["sec_13f"] == "skip", (
        "--offline-13f-cache-only declares zero-network 13F intent; the daily 13F "
        f"availability probe is a network GET and must be suppressed: {offline_plan}"
    )
    assert offline_plan["finra_short_interest"] == "check", offline_plan
    assert offline_plan["ibkr_borrow_fee_rate"] == "run", offline_plan
    for plan in (default_plan, fatal_plan, skip_plan, legacy_skip_plan, offline_plan):
        assert plan["ibkr_shortable_snapshot"] == "skip", (
            f"daily mode must never plan the shortable snapshot: {plan}"
        )
        for feed in ("finra_short_interest", "sec_13f"):
            assert plan[feed] in {"check", "skip"}, (
                f"daily mode must never unconditionally run {feed}: {plan}"
            )
    for conflicting_kwargs in (
        {"skip_ibkr_borrow": True, "require_daily_ibkr_borrow": True},
        {"skip_daily_finra_short_interest": True, "require_daily_finra_short_interest": True},
        {"skip_finra_short_interest": True, "require_daily_finra_short_interest": True},
        {"skip_daily_13f": True, "require_daily_13f": True},
        {"skip_13f": True, "require_daily_13f": True},
        {"offline_13f_cache_only": True, "require_daily_13f": True},
    ):
        try:
            plan_daily_upstream_sweeps(
                **{
                    "skip_daily_ibkr_borrow": False,
                    "skip_ibkr_borrow": False,
                    "require_daily_ibkr_borrow": False,
                    **conflicting_kwargs,
                }
            )
            raise AssertionError(f"require+skip combination unexpectedly accepted: {conflicting_kwargs}")
        except ValueError:
            pass

    # --- execution: success path forwards shortable_snapshot=False ---------
    recorded: dict[str, Any] = {}

    def fake_universe(source: Path, *, output_path: Path, asof: date) -> Path:
        recorded["universe"] = (source, output_path, asof)
        return output_path

    def fake_sweep(conn: Any, **kwargs: Any) -> SyncResult:
        recorded["sweep"] = kwargs
        return SyncResult("ibkr_borrow", 123, "ok")

    outcome = execute_daily_ibkr_borrow_sweep(
        None,
        tickers_csv=Path("selftest/positioning_universe.csv"),
        history_start=date(2018, 1, 1),
        end_date=date(2026, 8, 4),
        require_success=False,
        staleness_consequence="unused",
        sweep_kwargs={"fee_rate_incremental_duration": "45 D"},
        universe_builder=fake_universe,
        sweep_fn=fake_sweep,
    )
    assert outcome["status"] == "ran" and outcome["rows"] == 123, outcome
    assert recorded["sweep"]["shortable_snapshot"] is False, (
        "daily sweep must never sample the shortable snapshot"
    )
    assert recorded["sweep"]["backfill_fee_history_left_edge"] is False, (
        "daily sweep must not repeat full-history left-edge repair"
    )
    assert recorded["sweep"]["fee_rate_incremental_duration"] == "45 D"
    assert recorded["universe"][1].name == "positioning_universe_ibkr_active.csv"

    # --- execution: degraded (non-fatal) by default, fatal on request ------
    def failing_sweep(conn: Any, **kwargs: Any) -> SyncResult:
        raise ConnectionRefusedError("IB Gateway unreachable")

    degraded = execute_daily_ibkr_borrow_sweep(
        None,
        tickers_csv=Path("selftest/positioning_universe.csv"),
        history_start=date(2018, 1, 1),
        end_date=date(2026, 8, 4),
        require_success=False,
        staleness_consequence="borrow staleness gate in 14_validate will fail closed in 5 day(s)",
        sweep_kwargs={},
        universe_builder=fake_universe,
        sweep_fn=failing_sweep,
    )
    assert degraded["status"] == "degraded" and degraded["rows"] == 0, degraded
    assert "ConnectionRefusedError" in degraded["message"], degraded
    try:
        execute_daily_ibkr_borrow_sweep(
            None,
            tickers_csv=Path("selftest/positioning_universe.csv"),
            history_start=date(2018, 1, 1),
            end_date=date(2026, 8, 4),
            require_success=True,
            staleness_consequence="unused",
            sweep_kwargs={},
            universe_builder=fake_universe,
            sweep_fn=failing_sweep,
        )
        raise AssertionError("--require-daily-ibkr-borrow failure unexpectedly swallowed")
    except ConnectionRefusedError:
        pass

    # --- staleness-consequence message -------------------------------------
    mem = sqlite3.connect(":memory:")
    try:
        mem.execute("CREATE TABLE ibkr_borrow_fee_rate_daily (ticker TEXT, asof_date TEXT)")
        mem.execute("INSERT INTO ibkr_borrow_fee_rate_daily VALUES ('AAA', '2026-07-30')")
        config = {"positioning_import": {"max_borrow_staleness_days": 10}}
        message = borrow_staleness_consequence(mem, config, date(2026, 8, 4))
        assert "fail closed in 5 day(s)" in message, message
        assert "last borrow asof=2026-07-30" in message, message
        overdue = borrow_staleness_consequence(mem, config, date(2026, 8, 20))
        assert "fail closed in 0 day(s)" in overdue, overdue
        empty = borrow_staleness_consequence(sqlite3.connect(":memory:"), config, date(2026, 8, 4))
        assert "max_borrow_staleness_days=10" in empty, empty
    finally:
        mem.close()

    # --- window awareness: DERA 13F filing windows and archive names --------
    assert latest_completed_sec_13f_window(date(2026, 8, 5)) == (date(2026, 3, 1), date(2026, 5, 31))
    assert latest_completed_sec_13f_window(date(2026, 9, 1)) == (date(2026, 6, 1), date(2026, 8, 31))
    assert latest_completed_sec_13f_window(date(2026, 2, 10)) == (date(2025, 9, 1), date(2025, 11, 30))
    assert latest_completed_sec_13f_window(date(2026, 12, 15)) == (date(2026, 9, 1), date(2026, 11, 30))
    assert latest_completed_sec_13f_window(date(2026, 3, 1)) == (date(2025, 12, 1), date(2026, 2, 28))
    assert sec_13f_archive_name(date(2026, 3, 1), date(2026, 5, 31)) == "01mar2026-31may2026_form13f.zip"
    assert sec_13f_archive_name(date(2023, 12, 1), date(2024, 2, 29)) == "01dec2023-29feb2024_form13f.zip"

    # --- A13-7: publication-calendar-capped 13F staleness clock -------------
    # Q1 filer (last filing 2026-05-08, period 2026-03-31): the next round is
    # Q2, due 08-14, carried by the jun-aug archive publishable worst-case
    # 08-31 + 17d = 09-17. The plain 120d clock would arm 09-06 — BEFORE the
    # source can publish; the capped clock arms 09-20 (worst-case + 3d grace).
    assert next_13f_publishable_date(
        last_filing=date(2026, 5, 8), period_of_report=date(2026, 3, 31)
    ) == date(2026, 9, 17)
    assert next_13f_publishable_date(last_filing=date(2026, 5, 8)) == date(2026, 9, 17), (
        "filing-date-only inference must agree with the period for an in-window filing"
    )
    assert date(2026, 5, 8) + timedelta(days=121) == date(2026, 9, 6)  # the old, broken arming
    assert sec_13f_staleness_arming_date(
        last_filing=date(2026, 5, 8), period_of_report=date(2026, 3, 31), max_staleness_days=120
    ) == date(2026, 9, 20)
    assert not sec_13f_snapshot_is_stale(
        asof=date(2026, 9, 19),
        last_filing=date(2026, 5, 8),
        period_of_report=date(2026, 3, 31),
        max_staleness_days=120,
    ), "the gate must never demand a filing DERA cannot yet have published"
    assert sec_13f_snapshot_is_stale(
        asof=date(2026, 9, 20),
        last_filing=date(2026, 5, 8),
        period_of_report=date(2026, 3, 31),
        max_staleness_days=120,
    ), "once the archive has been publishable + grace, staleness must fail closed"
    # Cap is inert when publication was long since possible: the age clock governs.
    assert sec_13f_staleness_arming_date(
        last_filing=date(2026, 1, 5), period_of_report=date(2025, 9, 30), max_staleness_days=120
    ) == date(2026, 5, 6)
    # Year boundary: Q3-2025 round -> Q4 due 2026-02-14 -> dec-feb archive
    # publishable worst-case 2026-03-17 (+3d grace) beats the 03-13 age clock.
    assert sec_13f_staleness_arming_date(
        last_filing=date(2025, 11, 12), period_of_report=date(2025, 9, 30), max_staleness_days=120
    ) == date(2026, 3, 20)
    # Late amendment without period knowledge: filing-only inference defers by
    # one round (conservative — never demands unpublishable data); the period
    # keeps it exact.
    assert next_13f_publishable_date(last_filing=date(2026, 5, 20)) == date(2026, 12, 17)
    assert next_13f_publishable_date(
        last_filing=date(2026, 5, 20), period_of_report=date(2026, 3, 31)
    ) == date(2026, 9, 17)
    # max_staleness_days <= 0 keeps the existing disabled semantics.
    assert sec_13f_staleness_arming_date(last_filing=date(2026, 5, 8), max_staleness_days=0) is None
    assert not sec_13f_snapshot_is_stale(
        asof=date(2027, 1, 1), last_filing=date(2026, 5, 8), max_staleness_days=0
    )

    # --- window awareness: FINRA bi-monthly cycle + 12-day dissemination lag -
    assert latest_published_finra_settlement(date(2026, 8, 5)) == date(2026, 7, 15)
    assert latest_published_finra_settlement(date(2026, 8, 11)) == date(2026, 7, 15)
    assert latest_published_finra_settlement(date(2026, 8, 12)) == date(2026, 7, 31)
    assert latest_published_finra_settlement(date(2026, 8, 5), cycle_dates_fn=lambda a, b: []) is None

    # --- daily FINRA sweep: no_new_data / incremental run / degraded / fatal -
    finra_mem = sqlite3.connect(":memory:")
    try:
        finra_mem.execute(
            "CREATE TABLE short_interest_snapshots (ticker TEXT, settlement_date TEXT, source TEXT)"
        )
        finra_mem.execute(
            "INSERT INTO short_interest_snapshots VALUES ('LMT', '2026-06-15', 'finra_equity_short_interest_files')"
        )
        finra_calls: dict[str, Any] = {}

        def fake_finra_sweep_file_missing(conn: Any, **kwargs: Any) -> SyncResult:
            finra_calls["kwargs"] = kwargs
            return SyncResult("short_interest", 41, "files_found=0 files_missing=1 new_matched_rows=0")

        # Expected cycle publishable but the file is not on the CDN yet (later
        # publication than the lag heuristic): the sweep runs, nothing new
        # lands, and the outcome must say no_new_data — not 'ran'.
        pending_cycle = execute_daily_finra_short_interest_sweep(
            finra_mem,
            tickers=["LMT"],
            tickers_csv=Path("selftest/positioning_universe.csv"),
            history_start=date(2018, 1, 1),
            end_date=date(2026, 8, 5),
            require_success=False,
            staleness_consequence="unused",
            sweep_kwargs={},
            sweep_fn=fake_finra_sweep_file_missing,
        )
        assert pending_cycle["status"] == "no_new_data" and pending_cycle["rows"] == 0, pending_cycle
        assert "settlement=2026-07-15 still absent" in pending_cycle["message"], pending_cycle
        assert "files_found=0" in pending_cycle["message"], pending_cycle
        assert finra_calls["kwargs"]["history_start_date"] == date(2026, 6, 15), (
            "catch-up sweep must replay the last loaded settlement for partial-family repair"
        )

        def fake_finra_sweep(conn: Any, **kwargs: Any) -> SyncResult:
            finra_calls["kwargs"] = kwargs
            conn.execute(
                "INSERT INTO short_interest_snapshots VALUES ('LMT', '2026-07-15', 'finra_equity_short_interest_files')"
            )
            return SyncResult("short_interest", 42, "cycles loaded")

        finra_calls.clear()
        ran = execute_daily_finra_short_interest_sweep(
            finra_mem,
            tickers=["LMT"],
            tickers_csv=Path("selftest/positioning_universe.csv"),
            history_start=date(2018, 1, 1),
            end_date=date(2026, 8, 5),
            require_success=False,
            staleness_consequence="unused",
            sweep_kwargs={"cache_dir": Path("selftest/cache"), "user_agent": "ua", "max_files": 0},
            sweep_fn=fake_finra_sweep,
        )
        assert ran["status"] == "ran" and ran["rows"] == 42, ran
        assert finra_calls["kwargs"]["history_start_date"] == date(2026, 6, 15), (
            "catch-up sweep must replay the last loaded settlement for partial-family repair"
        )
        finra_calls.clear()
        current = execute_daily_finra_short_interest_sweep(
            finra_mem,
            tickers=["LMT"],
            tickers_csv=Path("selftest/positioning_universe.csv"),
            history_start=date(2018, 1, 1),
            end_date=date(2026, 8, 5),
            require_success=False,
            staleness_consequence="unused",
            sweep_kwargs={},
            sweep_fn=fake_finra_sweep,
        )
        assert current["status"] == "no_new_data", current
        assert "replayed that cycle inclusively" in current["message"], current
        assert finra_calls["kwargs"]["history_start_date"] == date(2026, 7, 15), (
            "a current cycle must still be replayed to repair missing family symbols"
        )

        def failing_finra_sweep(conn: Any, **kwargs: Any) -> SyncResult:
            raise TimeoutError("FINRA CDN unreachable")

        finra_mem.execute("DELETE FROM short_interest_snapshots WHERE settlement_date = '2026-07-15'")
        finra_degraded = execute_daily_finra_short_interest_sweep(
            finra_mem,
            tickers=["LMT"],
            tickers_csv=Path("selftest/positioning_universe.csv"),
            history_start=date(2018, 1, 1),
            end_date=date(2026, 8, 5),
            require_success=False,
            staleness_consequence="no short-interest staleness gate; silent degradation",
            sweep_kwargs={},
            sweep_fn=failing_finra_sweep,
        )
        assert finra_degraded["status"] == "degraded" and finra_degraded["rows"] == 0, finra_degraded
        assert "TimeoutError" in finra_degraded["message"], finra_degraded
        try:
            execute_daily_finra_short_interest_sweep(
                finra_mem,
                tickers=["LMT"],
                tickers_csv=Path("selftest/positioning_universe.csv"),
                history_start=date(2018, 1, 1),
                end_date=date(2026, 8, 5),
                require_success=True,
                staleness_consequence="unused",
                sweep_kwargs={},
                sweep_fn=failing_finra_sweep,
            )
            raise AssertionError("--require-daily-finra-short-interest failure unexpectedly swallowed")
        except TimeoutError:
            pass

        # --- FINRA consequence: names the missing gate and the silent decay --
        finra_config = {"positioning_import": {"lookback_days": {"short_change": 92}}}
        finra_msg = finra_staleness_consequence(
            finra_mem, finra_config, tickers=["LMT"], end_date=date(2026, 8, 5)
        )
        assert "NO short-interest staleness gate" in finra_msg, finra_msg
        assert "settlement=2026-06-15 (51 day(s) old)" in finra_msg, finra_msg
        assert "92-day short_change lookback" in finra_msg, finra_msg
    finally:
        finra_mem.close()

    # --- daily 13F sweep: no_new_data (DB) / pending / ingest / marker /
    # degraded / fatal -------------------------------------------------------
    sec_mem = sqlite3.connect(":memory:")
    try:
        sec_mem.execute(
            """
            CREATE TABLE institutional_13f_holdings (
                ticker TEXT, filing_date TEXT, source TEXT,
                source_file TEXT, period_of_report TEXT
            )
            """
        )
        sec_mem.execute(
            """
            CREATE TABLE market_positioning_feed_state (
                feed_name TEXT PRIMARY KEY, last_success_at TEXT NOT NULL,
                history_start_date TEXT, source TEXT, source_file TEXT,
                row_count INTEGER NOT NULL DEFAULT 0, message TEXT
            )
            """
        )
        sec_mem.execute(
            "INSERT INTO institutional_13f_holdings VALUES ('LMT', '2026-05-29', 'sec_13f_data_sets', '', '2026-03-31')"
        )
        network_calls: list[str] = []

        def fake_discover(**kwargs: Any) -> list[str]:
            network_calls.append("discover")
            return [
                "https://www.sec.gov/files/structureddata/data/form-13f-data-sets/01mar2026-31may2026_form13f.zip",
            ]

        def fake_download(url: str, **kwargs: Any) -> Path:
            network_calls.append("download")
            return Path("selftest/cache") / Path(urllib.parse.urlparse(url).path).name

        def fake_13f_sweep(conn: Any, **kwargs: Any) -> SyncResult:
            assert kwargs.get("network_fetched_archives") is True, (
                "daily ingest must flag its archive as network-fetched so the shared "
                "feed-state message never claims cache_only/zero network requests"
            )
            network_calls.append(f"sweep:{[p.name for p in kwargs['archive_paths']]}")
            return SyncResult("institutional_13f", 5130, "archive streamed")

        common_13f_kwargs: dict[str, Any] = {
            "tickers": ["LMT"],
            "tickers_csv": Path("selftest/positioning_universe.csv"),
            "history_start": date(2018, 1, 1),
            "require_success": False,
            "staleness_consequence": "13F staleness gate begins failing closed in 15 day(s) on 2026-09-20",
            "marker_feed_name": "industrials_daily_13f_defense",
            "cache_dir": Path("selftest/cache"),
            "user_agent": "ua",
            "discover_fn": fake_discover,
            "download_fn": fake_download,
            "sweep_fn": fake_13f_sweep,
        }
        # Window mar-may already ingested (family max filing 2026-05-29): zero network.
        idle = execute_daily_sec_13f_sweep(sec_mem, end_date=date(2026, 8, 5), **common_13f_kwargs)
        assert idle["status"] == "no_new_data", idle
        assert "no network request made" in idle["message"], idle
        assert not network_calls, "already-ingested window must cost zero network"
        # Window jun-aug closed but archive not yet on the index page: pending,
        # exactly one discovery probe, no download, countdown in the message.
        pending = execute_daily_sec_13f_sweep(sec_mem, end_date=date(2026, 9, 5), **common_13f_kwargs)
        assert pending["status"] == "no_new_data", pending
        assert "not yet published" in pending["message"], pending
        assert "failing closed in 15 day(s) on 2026-09-20" in pending["message"], pending
        assert network_calls == ["discover"], network_calls
        # Archive published: exactly that archive is downloaded and streamed,
        # and the per-family marker is recorded.
        network_calls.clear()

        def fake_discover_published(**kwargs: Any) -> list[str]:
            network_calls.append("discover")
            return [
                "https://www.sec.gov/files/structureddata/data/form-13f-data-sets/01jun2026-31aug2026_form13f.zip",
                "https://www.sec.gov/files/structureddata/data/form-13f-data-sets/01mar2026-31may2026_form13f.zip",
            ]

        ingested = execute_daily_sec_13f_sweep(
            sec_mem,
            end_date=date(2026, 9, 5),
            **{**common_13f_kwargs, "discover_fn": fake_discover_published},
        )
        assert ingested["status"] == "ran" and ingested["rows"] == 5130, ingested
        assert "01jun2026-31aug2026_form13f.zip" in ingested["message"], ingested
        assert network_calls == ["discover", "download", "sweep:['01jun2026-31aug2026_form13f.zip']"], network_calls
        marker_row = sec_mem.execute(
            "SELECT message FROM market_positioning_feed_state WHERE feed_name = 'industrials_daily_13f_defense'"
        ).fetchone()
        assert marker_row and "01jun2026-31aug2026_form13f.zip" in marker_row[0], marker_row
        # Marker short-circuit: archive processed but zero family matches (family
        # max filing_date unchanged) must not re-download nightly.
        network_calls.clear()
        marked = execute_daily_sec_13f_sweep(
            sec_mem,
            end_date=date(2026, 9, 6),
            **{**common_13f_kwargs, "discover_fn": fake_discover_published},
        )
        assert marked["status"] == "no_new_data", marked
        assert "zero family matches" in marked["message"], marked
        assert not network_calls, "marker-covered archive must cost zero network"

        # Crash mid-parse: upsert batches commit progressively, so partial
        # family rows would otherwise advance MAX(filing_date) into the newest
        # window and make every later nightly check short-circuit to
        # no_new_data with the archive forever half-ingested. The failed
        # ingest must purge its own partial rows (and must not write the
        # success marker) so the next nightly run retries.
        def crashing_13f_sweep(conn: Any, **kwargs: Any) -> SyncResult:
            partial_source_file = str(Path(kwargs["archive_paths"][0]).expanduser().resolve())
            conn.execute(
                "INSERT INTO institutional_13f_holdings VALUES "
                "('LMT', '2026-07-02', 'sec_13f_data_sets', ?, '2026-06-30')",
                (partial_source_file,),
            )
            conn.commit()
            raise RuntimeError("simulated crash after a committed 5000-row batch")

        network_calls.clear()
        crash_degraded = execute_daily_sec_13f_sweep(
            sec_mem,
            end_date=date(2026, 9, 5),
            **{
                **common_13f_kwargs,
                "marker_feed_name": "industrials_daily_13f_crash",
                "discover_fn": fake_discover_published,
                "sweep_fn": crashing_13f_sweep,
            },
        )
        assert crash_degraded["status"] == "degraded", crash_degraded
        assert "RuntimeError" in crash_degraded["message"], crash_degraded
        max_after_crash = sec_mem.execute(
            "SELECT MAX(filing_date) FROM institutional_13f_holdings WHERE UPPER(ticker) = 'LMT'"
        ).fetchone()[0]
        assert max_after_crash == "2026-05-29", (
            "partial rows from a failed archive ingest must be purged so the nightly "
            f"availability check retries instead of short-circuiting: {max_after_crash}"
        )
        crash_marker = sec_mem.execute(
            "SELECT message FROM market_positioning_feed_state WHERE feed_name = 'industrials_daily_13f_crash'"
        ).fetchone()
        assert crash_marker is None, "failed ingest must not write the success marker"

        def failing_discover(**kwargs: Any) -> list[str]:
            raise ConnectionResetError("SEC index unreachable")

        sec_degraded = execute_daily_sec_13f_sweep(
            sec_mem,
            end_date=date(2026, 9, 5),
            **{
                **common_13f_kwargs,
                "marker_feed_name": "industrials_daily_13f_other",
                "discover_fn": failing_discover,
            },
        )
        assert sec_degraded["status"] == "degraded" and sec_degraded["rows"] == 0, sec_degraded
        assert "ConnectionResetError" in sec_degraded["message"], sec_degraded
        try:
            execute_daily_sec_13f_sweep(
                sec_mem,
                end_date=date(2026, 9, 5),
                **{
                    **common_13f_kwargs,
                    "marker_feed_name": "industrials_daily_13f_other",
                    "discover_fn": failing_discover,
                    "require_success": True,
                },
            )
            raise AssertionError("--require-daily-13f failure unexpectedly swallowed")
        except ConnectionResetError:
            pass

        # --- 13F consequence: countdown to the publication-capped gate --------
        # BA (last filing 2026-05-08, period 2026-03-31): plain 120d would arm
        # 2026-09-06, but the jun-aug archive carrying the next round is only
        # worst-case publishable 2026-09-17 (+3d grace) => the capped gate and
        # therefore the countdown arm 2026-09-20 (46 days from 2026-08-05).
        sec_mem.execute(
            "INSERT INTO institutional_13f_holdings VALUES ('BA', '2026-05-08', 'sec_13f_data_sets', '', '2026-03-31')"
        )
        sec_config = {"positioning_import": {"max_13f_staleness_days": 120}}
        sec_msg = sec_13f_staleness_consequence(
            sec_mem, sec_config, tickers=["LMT", "BA"], end_date=date(2026, 8, 5)
        )
        assert "failing closed in 46 day(s) on 2026-09-20" in sec_msg, sec_msg
        assert "earliest family last-filing asof=2026-05-08" in sec_msg, sec_msg
        assert "publication-calendar-capped" in sec_msg, sec_msg
        overdue_sec_msg = sec_13f_staleness_consequence(
            sec_mem, sec_config, tickers=["LMT", "BA"], end_date=date(2026, 9, 25)
        )
        assert "failing closed in 0 day(s) on 2026-09-20" in overdue_sec_msg, overdue_sec_msg
        empty_sec_msg = sec_13f_staleness_consequence(
            sqlite3.connect(":memory:"), sec_config, tickers=["LMT"], end_date=date(2026, 8, 5)
        )
        assert "max_13f_staleness_days=120" in empty_sec_msg, empty_sec_msg
        disabled_sec_msg = sec_13f_staleness_consequence(
            sec_mem,
            {"positioning_import": {"max_13f_staleness_days": 0}},
            tickers=["LMT", "BA"],
            end_date=date(2026, 8, 5),
        )
        assert "13F staleness gate disabled" in disabled_sec_msg, disabled_sec_msg
    finally:
        sec_mem.close()

    print(
        "SELFTEST PASS: daily plan runs incremental IBKR borrow sweep plus cycle-aware "
        "FINRA and DERA-window-aware 13F availability checks by default (shortable "
        "snapshot never planned; --offline-13f-cache-only suppresses the daily 13F "
        "probe); checks are zero-network when the DB already covers the newest "
        "publishable cycle/window; pending archives/cycles report no_new_data with the "
        "publication-calendar-capped staleness countdown (A13-7: the gate never arms "
        "before the next DERA archive can publish); a crashed archive ingest purges its "
        "partial family rows so the nightly check retries; skip/require flags honored "
        "and mutually exclusive per feed; all sweeps degrade non-fatally by default and "
        "are fatal with --require-*; staleness consequence messages OK"
    )
    return 0


def main() -> None:
    configure_utc_logging()
    args = parse_args()
    if args.selftest:
        raise SystemExit(run_selftest())
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
            daily_plan = plan_daily_upstream_sweeps(
                skip_daily_ibkr_borrow=args.skip_daily_ibkr_borrow,
                skip_ibkr_borrow=args.skip_ibkr_borrow,
                require_daily_ibkr_borrow=args.require_daily_ibkr_borrow,
                skip_daily_finra_short_interest=args.skip_daily_finra_short_interest,
                skip_finra_short_interest=args.skip_finra_short_interest,
                require_daily_finra_short_interest=args.require_daily_finra_short_interest,
                skip_daily_13f=args.skip_daily_13f,
                skip_13f=args.skip_13f,
                require_daily_13f=args.require_daily_13f,
                offline_13f_cache_only=args.offline_13f_cache_only,
            )
            LOGGER.info(
                "Daily refresh mode: FINRA cycle check=%s (failure_mode=%s); 13F DERA-window "
                "check=%s (failure_mode=%s); incremental IBKR borrow fee-rate sweep=%s "
                "(failure_mode=%s); IBKR shortable snapshot never sampled; feature asof=%s",
                daily_plan["finra_short_interest"],
                daily_plan["finra_failure_mode"],
                daily_plan["sec_13f"],
                daily_plan["sec_13f_failure_mode"],
                daily_plan["ibkr_borrow_fee_rate"],
                daily_plan["borrow_failure_mode"],
                end_date,
            )
            # All sweeps must complete BEFORE the positioning re-import below so
            # freshly pulled rows flow into the same run's features.
            universe_tickers = load_universe_tickers(tickers_csv)
            active_tickers = load_active_universe_tickers(tickers_csv, asof=end_date) or universe_tickers
            if daily_plan["finra_short_interest"] == "check":
                finra_outcome = execute_daily_finra_short_interest_sweep(
                    conn,
                    tickers=universe_tickers,
                    tickers_csv=tickers_csv,
                    history_start=history_start,
                    end_date=end_date,
                    require_success=daily_plan["finra_failure_mode"] == "fatal",
                    staleness_consequence=finra_staleness_consequence(
                        conn, config, tickers=universe_tickers, end_date=end_date
                    ),
                    sweep_kwargs={
                        "cache_dir": cache_dir / "finra_short_interest",
                        "user_agent": user_agent,
                        "max_files": args.finra_max_files,
                    },
                )
            else:
                finra_outcome = {
                    "feed": "finra_short_interest",
                    "status": "skipped",
                    "rows": 0,
                    "message": "skipped by --skip-daily-finra-short-interest/--skip-finra-short-interest",
                }
            LOGGER.info(
                "Daily FINRA short-interest sweep outcome: model_family=%s status=%s rows=%d message=%s",
                model_family,
                finra_outcome["status"],
                finra_outcome["rows"],
                finra_outcome["message"],
            )
            if daily_plan["sec_13f"] == "check":
                sec_13f_outcome = execute_daily_sec_13f_sweep(
                    conn,
                    tickers=universe_tickers,
                    tickers_csv=tickers_csv,
                    history_start=history_start,
                    end_date=end_date,
                    require_success=daily_plan["sec_13f_failure_mode"] == "fatal",
                    staleness_consequence=sec_13f_staleness_consequence(
                        conn, config, tickers=active_tickers, end_date=end_date
                    ),
                    marker_feed_name=f"industrials_daily_13f_{model_family}",
                    cache_dir=cache_dir / "sec_13f",
                    user_agent=user_agent,
                )
            else:
                sec_13f_outcome = {
                    "feed": "sec_13f",
                    "status": "skipped",
                    "rows": 0,
                    "message": "skipped by --skip-daily-13f/--skip-13f/--offline-13f-cache-only",
                }
            LOGGER.info(
                "Daily SEC 13F sweep outcome: model_family=%s status=%s rows=%d message=%s",
                model_family,
                sec_13f_outcome["status"],
                sec_13f_outcome["rows"],
                sec_13f_outcome["message"],
            )
            if daily_plan["ibkr_borrow_fee_rate"] == "run":
                borrow_outcome = execute_daily_ibkr_borrow_sweep(
                    conn,
                    tickers_csv=tickers_csv,
                    history_start=history_start,
                    end_date=end_date,
                    require_success=daily_plan["borrow_failure_mode"] == "fatal",
                    staleness_consequence=borrow_staleness_consequence(conn, config, end_date),
                    sweep_kwargs={
                        "host": args.ibkr_host,
                        "port": args.ibkr_port,
                        "client_id": args.ibkr_client_id,
                        "market_data_type": args.ibkr_market_data_type,
                        "fee_rate_incremental_duration": args.ibkr_fee_rate_incremental_duration,
                        "snapshot_wait_sec": args.ibkr_snapshot_wait_sec,
                        "max_tickers": args.ibkr_max_tickers,
                    },
                )
            else:
                borrow_outcome = {
                    "feed": "ibkr_borrow_fee_rate",
                    "status": "skipped",
                    "rows": 0,
                    "message": "skipped by --skip-daily-ibkr-borrow/--skip-ibkr-borrow",
                }
            LOGGER.info(
                "Daily IBKR borrow sweep outcome: model_family=%s status=%s rows=%d message=%s",
                model_family,
                borrow_outcome["status"],
                borrow_outcome["rows"],
                borrow_outcome["message"],
            )
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

