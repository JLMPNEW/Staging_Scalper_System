from __future__ import annotations

import csv
import io
import json
import json.decoder
import math
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from market_positioning.core import (
    aggregate_13f_ownership,  # noqa: F401  (kept exported for backward compatibility)
    aggregate_13f_ownership_for_tickers,
    normalize_pct,
    normalize_ticker,
    parse_date,
    read_csv_rows,
    to_float,
    update_feed_state,
    utc_now,
)
from market_positioning.ibkr_capacity import (
    DEFAULT_IBKR_LOCK_TIMEOUT_SEC,
    IBKRMarketDataLock,
    bounded_streaming_batch_size,
)


DEFAULT_USER_AGENT = "JL Independent Research jm.357@hotmail.com"
DEFAULT_FINRA_SHORT_INTEREST_URL = "https://api.finra.org/data/group/otcMarket/name/EquityShortInterest"
DEFAULT_FINRA_EQUITY_SHORT_INTEREST_FILES_BASE_URL = "https://cdn.finra.org/equity/otcmarket/biweekly"
DEFAULT_SEC_13F_DATASETS_URL = "https://www.sec.gov/data-research/sec-markets-data/form-13f-data-sets"
SEC_13F_DOWNLOAD_RE = re.compile(r'href=["\'](?P<href>[^"\']+?form-13f-data-sets/[^"\']+?\.zip)["\']', re.I)
LEGAL_SUFFIX_RE = re.compile(
    r"\b(incorporated|inc|corp|corporation|co|company|ltd|limited|plc|sa|nv|ag|se|spa|lp|llc|adr|ads|ordinary|common|stock|class|cl|shs|shares)\b",
    re.I,
)
NON_ALNUM_RE = re.compile(r"[^A-Z0-9]+")


@dataclass(frozen=True)
class SyncResult:
    feed_name: str
    rows: int
    message: str


def normalize_cusip(raw: object) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(raw or "").upper())[:9]


def normalize_issuer_name(raw: object) -> str:
    text = str(raw or "").upper()
    text = LEGAL_SUFFIX_RE.sub(" ", text)
    text = NON_ALNUM_RE.sub(" ", text)
    return " ".join(part for part in text.split() if part)


def http_request(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    user_agent: str = DEFAULT_USER_AGENT,
    timeout_sec: float = 60.0,
) -> bytes:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, method=method.upper())
    request.add_header("User-Agent", user_agent)
    if payload is not None:
        request.add_header("Content-Type", "application/json")
        request.add_header("Accept", "application/json")
    with urllib.request.urlopen(request, timeout=timeout_sec) as response:  # noqa: S310
        return response.read()


def http_json(
    url: str,
    *,
    payload: dict[str, Any],
    user_agent: str,
    timeout_sec: float,
) -> Any:
    raw = http_request(url, method="POST", payload=payload, user_agent=user_agent, timeout_sec=timeout_sec)
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return []
    try:
        return json.loads(text)
    except json.decoder.JSONDecodeError:
        if "issueSymbolIdentifier" in text[:512] or "settlementDate" in text[:512]:
            return [dict(row) for row in csv.DictReader(io.StringIO(text))]
        raise RuntimeError(f"FINRA response was not JSON/CSV. First 200 chars: {text[:200]!r}") from None


def load_universe_tickers(path: Path | None) -> list[str]:
    if path is None or not path.exists():
        return []
    tickers = sorted(
        {
            ticker
            for ticker in (normalize_ticker(row.get("ticker") or row.get("symbol")) for row in read_csv_rows(path))
            if ticker
        }
    )
    return tickers


def load_universe_name_map(path: Path | None) -> dict[str, str]:
    if path is None or not path.exists():
        return {}
    out: dict[str, str] = {}
    for row in read_csv_rows(path):
        ticker = normalize_ticker(row.get("ticker") or row.get("symbol"))
        if not ticker:
            continue
        for key in (
            "company_name",
            "issuer_name",
            "name",
            "institutional_13f_issuer_alias",
            "issuer_alias",
            "company_name_alias",
        ):
            for raw_name in str(row.get(key) or "").split(";"):
                normalized = normalize_issuer_name(raw_name)
                if normalized:
                    out.setdefault(normalized, ticker)
    return out


def load_universe_exchange_map(path: Path | None) -> dict[str, str]:
    if path is None or not path.exists():
        return {}
    aliases = {
        "NASDAQ": "NASDAQ",
        "NASD": "NASDAQ",
        "NMS": "NASDAQ",
        "NASDAQGS": "NASDAQ",
        "NASDAQGM": "NASDAQ",
        "NASDAQCM": "NASDAQ",
        "NYSE": "NYSE",
        "NEW YORK STOCK EXCHANGE": "NYSE",
        "AMEX": "AMEX",
        "NYSEAMERICAN": "AMEX",
        "NYSE AMERICAN": "AMEX",
        "ARCA": "ARCA",
        "NYSEARCA": "ARCA",
    }
    out: dict[str, str] = {}
    for row in read_csv_rows(path):
        ticker = normalize_ticker(row.get("ticker") or row.get("symbol"))
        exchange = str(row.get("exchange") or row.get("primary_exchange") or "").strip().upper()
        if ticker and exchange:
            out[ticker] = aliases.get(exchange.replace(" ", ""), aliases.get(exchange, exchange))
    return out


def load_universe_ibkr_symbol_map(path: Path | None) -> dict[str, str]:
    if path is None or not path.exists():
        return {}
    out: dict[str, str] = {}
    for row in read_csv_rows(path):
        ticker = normalize_ticker(row.get("ticker") or row.get("symbol"))
        ibkr_ticker = str(
            row.get("ibkr_ticker")
            or row.get("ibkr_symbol")
            or row.get("interactive_brokers_ticker")
            or ""
        ).strip().upper()
        if ticker and ibkr_ticker:
            out[ticker] = ibkr_ticker
    return out


def load_universe_membership_end_map(path: Path | None) -> dict[str, date]:
    """Return terminal membership dates for tickers without an open interval."""
    if path is None or not path.exists():
        return {}
    terminal_dates: dict[str, date] = {}
    open_tickers: set[str] = set()
    for row in read_csv_rows(path):
        ticker = normalize_ticker(row.get("ticker") or row.get("symbol"))
        if not ticker:
            continue
        raw_end = row.get("membership_end_date") or row.get("end_date") or row.get("terminal_date")
        terminal_date = parse_date(raw_end)
        if terminal_date is None:
            open_tickers.add(ticker)
            continue
        prior = terminal_dates.get(ticker)
        if prior is None or terminal_date > prior:
            terminal_dates[ticker] = terminal_date
    return {ticker: terminal_date for ticker, terminal_date in terminal_dates.items() if ticker not in open_tickers}


def filter_ibkr_tickers_for_asof(
    tickers: list[str],
    membership_end_by_ticker: dict[str, date],
    end_date: date,
) -> tuple[list[str], set[str]]:
    """Exclude historical lineages that had already ended by the IB as-of date."""
    ended_before_asof = {
        ticker for ticker, membership_end in membership_end_by_ticker.items() if membership_end < end_date
    }
    return [ticker for ticker in tickers if ticker not in ended_before_asof], ended_before_asof


def load_cusip_ticker_map(path: Path | None) -> dict[str, str]:
    if path is None or not path.exists():
        return {}
    out: dict[str, str] = {}
    for row in read_csv_rows(path):
        ticker = normalize_ticker(row.get("ticker") or row.get("symbol"))
        cusip = normalize_cusip(row.get("cusip"))
        if ticker and cusip:
            out[cusip] = ticker
    return out


def finra_payload_for_ticker(
    ticker: str,
    *,
    start_date: date,
    end_date: date,
    limit: int,
    offset: int,
) -> dict[str, Any]:
    return {
        "limit": limit,
        "offset": offset,
        "compareFilters": [
            {"compareType": "EQUAL", "fieldName": "issueSymbolIdentifier", "fieldValue": ticker},
            {"compareType": "greaterOrEqual", "fieldName": "settlementDate", "fieldValue": start_date.isoformat()},
            {"compareType": "lesserOrEqual", "fieldName": "settlementDate", "fieldValue": end_date.isoformat()},
        ],
        "sortFields": ["settlementDate"],
    }


def finra_payload_for_ticker_without_range(ticker: str, *, limit: int, offset: int) -> dict[str, Any]:
    return {
        "limit": limit,
        "offset": offset,
        "compareFilters": [
            {"compareType": "EQUAL", "fieldName": "issueSymbolIdentifier", "fieldValue": ticker},
        ],
        "sortFields": ["settlementDate"],
    }


def previous_weekday(day: date) -> date:
    out = day
    while out.weekday() >= 5:
        out -= timedelta(days=1)
    return out


def add_business_days(day: date, business_days: int) -> date:
    """Advance a date by N business days (weekends skipped; holidays not modeled)."""
    out = day
    remaining = max(0, int(business_days))
    while remaining > 0:
        out += timedelta(days=1)
        if out.weekday() < 5:
            remaining -= 1
    return out


def last_weekday_of_month(year: int, month: int) -> date:
    if month == 12:
        day = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        day = date(year, month + 1, 1) - timedelta(days=1)
    return previous_weekday(day)


def finra_settlement_dates(start_date: date, end_date: date) -> list[date]:
    dates: set[date] = set()
    year = start_date.year
    month = start_date.month
    while (year, month) <= (end_date.year, end_date.month):
        mid = previous_weekday(date(year, month, 15))
        end = last_weekday_of_month(year, month)
        for candidate in (mid, end):
            if start_date <= candidate <= end_date:
                dates.add(candidate)
        if month == 12:
            year += 1
            month = 1
        else:
            month += 1
    return sorted(dates)


def finra_payload_for_settlement_date(settlement_date: date, *, limit: int, offset: int) -> dict[str, Any]:
    return {
        "limit": limit,
        "offset": offset,
        "compareFilters": [
            {"compareType": "EQUAL", "fieldName": "settlementDate", "fieldValue": settlement_date.isoformat()},
        ],
        "sortFields": ["issueSymbolIdentifier"],
    }


def first_float(row: Mapping[str, object], *keys: str) -> float | None:
    for key in keys:
        value = to_float(row.get(key))
        if value is not None:
            return value
    return None


def finra_days_to_cover(row: Mapping[str, object], short_shares: float | None) -> float | None:
    """Return FINRA days-to-cover, deriving only from average daily volume.

    FINRA payloads may expose either daysToCoverQuantity or daysToCoverNumber
    depending on the endpoint/file vintage. When absent, the valid formula is
    current short shares divided by average daily volume. averageShortShareNumber
    is not a volume denominator and would materially understate the value.
    """
    explicit_days = first_float(row, "daysToCoverQuantity", "daysToCoverNumber")
    if explicit_days is not None:
        return explicit_days
    avg_daily_volume = first_float(
        row,
        "averageDailyVolumeQuantity",
        "averageDailyVolumeNumber",
        "averageDailyShareVolumeQuantity",
        "averageDailyShareVolumeNumber",
    )
    if short_shares is not None and avg_daily_volume is not None and avg_daily_volume > 0:
        return round(short_shares / avg_daily_volume, 4)
    return None


def finra_short_interest_records(
    *,
    tickers: list[str],
    start_date: date,
    end_date: date,
    api_url: str = DEFAULT_FINRA_SHORT_INTEREST_URL,
    page_size: int = 5000,
    sleep_sec: float = 0.15,
    user_agent: str = DEFAULT_USER_AGENT,
    timeout_sec: float = 60.0,
    max_tickers: int = 0,
    publication_lag_business_days: int = 8,
) -> list[tuple[Any, ...]]:
    """Collect FINRA API short-interest rows.

    FINRA disseminates biweekly short interest several business days after the
    settlement date, so publication_date (and the PIT asof_date) must be
    settlement + `publication_lag_business_days` — never the settlement date
    itself, or point-in-time consumers would see the data before FINRA
    published it. Callers should pass the configured
    `market_positioning.finra_api_publication_lag_days` value (default 8).
    """
    records: list[tuple[Any, ...]] = []
    now = utc_now()
    scoped_tickers = set(tickers[:max_tickers] if max_tickers and max_tickers > 0 else tickers)
    for settlement_candidate in finra_settlement_dates(start_date, end_date):
        offset = 0
        while True:
            payload = finra_payload_for_settlement_date(
                settlement_candidate,
                limit=max(1, int(page_size)),
                offset=offset,
            )
            try:
                rows = http_json(api_url, payload=payload, user_agent=user_agent, timeout_sec=timeout_sec)
            except urllib.error.HTTPError as exc:
                if exc.code == 400:
                    break
                raise
            if not isinstance(rows, list):
                raise RuntimeError(f"FINRA response for {settlement_candidate} was not a JSON list")
            for row in rows:
                if not isinstance(row, dict):
                    continue
                settlement = parse_date(row.get("settlementDate"))
                if settlement is None or settlement < start_date or settlement > end_date:
                    continue
                api_ticker = normalize_ticker(row.get("issueSymbolIdentifier"))
                if api_ticker not in scoped_tickers:
                    continue
                short_shares = to_float(row.get("currentShortShareNumber"))
                days_to_cover = finra_days_to_cover(row, short_shares)
                publication = add_business_days(settlement, publication_lag_business_days)
                records.append(
                    (
                        api_ticker,
                        publication.isoformat(),
                        settlement.isoformat(),
                        publication.isoformat(),
                        short_shares,
                        None,
                        None,
                        days_to_cover,
                        "finra_equity_short_interest",
                        api_url,
                        now,
                        now,
                    )
                )
            if len(rows) < max(1, int(page_size)):
                break
            offset += max(1, int(page_size))
            if sleep_sec > 0:
                time.sleep(sleep_sec)
        if sleep_sec > 0:
            time.sleep(sleep_sec)
    return records


def sync_finra_short_interest(
    conn: sqlite3.Connection,
    *,
    tickers_csv: Path | None,
    history_start_date: date,
    end_date: date,
    api_url: str = DEFAULT_FINRA_SHORT_INTEREST_URL,
    page_size: int = 5000,
    sleep_sec: float = 0.15,
    user_agent: str = DEFAULT_USER_AGENT,
    timeout_sec: float = 60.0,
    max_tickers: int = 0,
    publication_lag_business_days: int = 8,
) -> SyncResult:
    tickers = load_universe_tickers(tickers_csv)
    if not tickers:
        raise RuntimeError("FINRA short-interest sync requires a non-empty ticker universe CSV")
    records = finra_short_interest_records(
        tickers=tickers,
        start_date=history_start_date,
        end_date=end_date,
        api_url=api_url,
        page_size=page_size,
        sleep_sec=sleep_sec,
        user_agent=user_agent,
        timeout_sec=timeout_sec,
        max_tickers=max_tickers,
        publication_lag_business_days=publication_lag_business_days,
    )
    with conn:
        # Remove superseded rows for the same (ticker, settlement) whose asof/publication
        # stamp differs (legacy rows stamped publication_date = settlement_date leaked
        # pre-publication data into PIT consumers; the corrected row replaces them).
        conn.executemany(
            """
            DELETE FROM short_interest_snapshots
            WHERE source = 'finra_equity_short_interest'
              AND ticker = ?
              AND settlement_date = ?
              AND asof_date <> ?
            """,
            [(record[0], record[2], record[1]) for record in records],
        )
        conn.executemany(
            """
            INSERT INTO short_interest_snapshots(
                ticker, asof_date, settlement_date, publication_date,
                short_interest_shares, float_shares, short_interest_pct_float, days_to_cover,
                source, source_file, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker, asof_date, settlement_date, source) DO UPDATE SET
                publication_date = excluded.publication_date,
                short_interest_shares = excluded.short_interest_shares,
                float_shares = excluded.float_shares,
                short_interest_pct_float = excluded.short_interest_pct_float,
                days_to_cover = excluded.days_to_cover,
                source_file = excluded.source_file,
                updated_at = excluded.updated_at
            """,
            records,
        )
    total_rows = int(conn.execute("SELECT COUNT(*) FROM short_interest_snapshots").fetchone()[0])
    message = (
        "FINRA public EquityShortInterest API is the OTC equity endpoint; "
        "exchange-listed short-interest coverage may require exchange or licensed sources."
    )
    update_feed_state(
        conn,
        feed_name="short_interest",
        history_start_date=history_start_date,
        source="finra_equity_short_interest",
        source_file=None,
        row_count=total_rows,
        message=message,
    )
    return SyncResult("short_interest", total_rows, message)


def finra_equity_short_interest_file_url(base_url: str, settlement_date: date) -> str:
    return f"{base_url.rstrip('/')}/shrt{settlement_date.strftime('%Y%m%d')}.csv"


def write_bytes_atomic(path: Path, data: bytes) -> None:
    """Write bytes via tmp + os.replace so a crash never leaves a truncated cache file."""
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_bytes(data)
    os.replace(tmp_path, path)


def delimited_file_is_intact(path: Path, *, delimiter: str = "|") -> bool:
    """Cheap integrity check for cached delimited CSVs.

    A truncated download usually ends mid-line, so require the header to
    contain the delimiter and the last non-empty line to carry the same field
    count as the header. Cannot detect truncation exactly at a line boundary,
    but catches the common corrupt-cache failure mode that otherwise fails
    open (DictReader silently yields only the surviving lines).
    """
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return False
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return False
    header = lines[0]
    if delimiter not in header:
        return False
    expected_fields = header.count(delimiter)
    return lines[-1].count(delimiter) == expected_fields


def download_finra_equity_short_interest_file(
    *,
    url: str,
    cache_dir: Path,
    user_agent: str,
    timeout_sec: float,
) -> Path | None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / Path(urllib.parse.urlparse(url).path).name
    if path.exists() and path.stat().st_size > 0:
        if delimited_file_is_intact(path):
            return path
        # Corrupt cache (e.g. truncated earlier download): delete and refetch once.
        path.unlink()
    try:
        raw = http_request(url, user_agent=user_agent, timeout_sec=timeout_sec)
    except urllib.error.HTTPError as exc:
        if exc.code in {403, 404}:
            return None
        raise
    if not raw:
        return None
    write_bytes_atomic(path, raw)
    if not delimited_file_is_intact(path):
        path.unlink(missing_ok=True)
        raise RuntimeError(
            f"FINRA short-interest file failed pipe-delimited integrity verification after download: {url}"
        )
    return path


def read_finra_equity_short_interest_file(path: Path) -> list[dict[str, str]]:
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    if not text.strip():
        return []
    csv.field_size_limit(min(sys.maxsize, 2_147_483_647))
    return [dict(row) for row in csv.DictReader(io.StringIO(text), delimiter="|")]


def sync_finra_equity_short_interest_files(
    conn: sqlite3.Connection,
    *,
    tickers_csv: Path | None,
    history_start_date: date,
    end_date: date,
    base_url: str = DEFAULT_FINRA_EQUITY_SHORT_INTEREST_FILES_BASE_URL,
    cache_dir: Path,
    publication_lag_days: int = 12,
    sleep_sec: float = 0.05,
    user_agent: str = DEFAULT_USER_AGENT,
    timeout_sec: float = 60.0,
    max_files: int = 0,
) -> SyncResult:
    tickers = set(load_universe_tickers(tickers_csv))
    if not tickers:
        raise RuntimeError("FINRA Equity Short Interest file sync requires a non-empty ticker universe CSV")
    now = utc_now()
    records: list[tuple[Any, ...]] = []
    files_downloaded = 0
    files_found = 0
    files_missing = 0
    settlement_dates = finra_settlement_dates(history_start_date, end_date)
    if max_files and max_files > 0:
        settlement_dates = settlement_dates[-max_files:]
    for settlement in settlement_dates:
        url = finra_equity_short_interest_file_url(base_url, settlement)
        path = download_finra_equity_short_interest_file(
            url=url,
            cache_dir=cache_dir,
            user_agent=user_agent,
            timeout_sec=timeout_sec,
        )
        if path is None:
            files_missing += 1
            continue
        files_found += 1
        files_downloaded += 1
        for row in read_finra_equity_short_interest_file(path):
            ticker = normalize_ticker(row.get("symbolCode"))
            if ticker not in tickers:
                continue
            row_settlement = parse_date(row.get("settlementDate")) or settlement
            if row_settlement < history_start_date or row_settlement > end_date:
                continue
            row_publication = row_settlement + timedelta(days=max(0, publication_lag_days))
            short_shares = to_float(row.get("currentShortPositionQuantity"))
            records.append(
                (
                    ticker,
                    row_publication.isoformat(),
                    row_settlement.isoformat(),
                    row_publication.isoformat(),
                    short_shares,
                    None,
                    None,
                    finra_days_to_cover(row, short_shares),
                    "finra_equity_short_interest_files",
                    str(path),
                    now,
                    now,
                )
            )
        if sleep_sec > 0:
            time.sleep(sleep_sec)
    with conn:
        # Keep file-ingest PIT stamps consistent with the API path: if the
        # publication lag changes, corrected rows replace same-ticker/same-settlement
        # stale rows instead of coexisting under a different asof_date primary key.
        conn.executemany(
            """
            DELETE FROM short_interest_snapshots
            WHERE source = 'finra_equity_short_interest_files'
              AND ticker = ?
              AND settlement_date = ?
              AND asof_date <> ?
            """,
            [(record[0], record[2], record[1]) for record in records],
        )
        conn.executemany(
            """
            INSERT INTO short_interest_snapshots(
                ticker, asof_date, settlement_date, publication_date,
                short_interest_shares, float_shares, short_interest_pct_float, days_to_cover,
                source, source_file, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker, asof_date, settlement_date, source) DO UPDATE SET
                publication_date = excluded.publication_date,
                short_interest_shares = excluded.short_interest_shares,
                float_shares = excluded.float_shares,
                short_interest_pct_float = excluded.short_interest_pct_float,
                days_to_cover = excluded.days_to_cover,
                source_file = excluded.source_file,
                updated_at = excluded.updated_at
            """,
            records,
        )
    total_rows = int(
        conn.execute(
            "SELECT COUNT(*) FROM short_interest_snapshots WHERE source = ?",
            ("finra_equity_short_interest_files",),
        ).fetchone()[0]
    )
    message = (
        "FINRA Equity Short Interest files loaded. Pre-June-2021 files are OTC-only per FINRA; "
        f"files_found={files_found} files_missing={files_missing} new_matched_rows={len(records)} total_file_rows={total_rows}"
    )
    update_feed_state(
        conn,
        feed_name="short_interest",
        history_start_date=history_start_date,
        source="finra_equity_short_interest_files",
        source_file=None,
        row_count=total_rows,
        message=message,
    )
    return SyncResult("short_interest", total_rows, message)


def discover_sec_13f_archives(
    *,
    index_url: str = DEFAULT_SEC_13F_DATASETS_URL,
    start_year: int,
    end_year: int,
    user_agent: str = DEFAULT_USER_AGENT,
    timeout_sec: float = 60.0,
) -> list[str]:
    html = http_request(index_url, user_agent=user_agent, timeout_sec=timeout_sec).decode("utf-8", errors="replace")
    urls: list[str] = []
    seen: set[str] = set()
    valid_years = {str(year) for year in range(start_year, end_year + 1)}
    for match in SEC_13F_DOWNLOAD_RE.finditer(html):
        href = match.group("href")
        absolute = urllib.parse.urljoin(index_url, href)
        if absolute in seen:
            continue
        if not any(year in absolute for year in valid_years):
            continue
        urls.append(absolute)
        seen.add(absolute)
    return urls


def cached_download_is_intact(path: Path) -> bool:
    """Verify a cached download; zip archives must have a readable central directory.

    `zipfile.is_zipfile` fails on truncated archives because the end-of-central-
    directory record lives at the end of the file, which is exactly what a
    partial download loses.
    """
    if path.suffix.lower() == ".zip":
        try:
            return zipfile.is_zipfile(path)
        except OSError:
            return False
    return path.stat().st_size > 0


def download_cached(url: str, *, cache_dir: Path, user_agent: str, timeout_sec: float) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    filename = Path(urllib.parse.urlparse(url).path).name
    path = cache_dir / filename
    if path.exists() and path.stat().st_size > 0:
        if cached_download_is_intact(path):
            return path
        # Corrupt cache (e.g. truncated earlier download): delete and refetch once.
        path.unlink()
    raw = http_request(url, user_agent=user_agent, timeout_sec=timeout_sec)
    write_bytes_atomic(path, raw)
    if not cached_download_is_intact(path):
        path.unlink(missing_ok=True)
        raise RuntimeError(f"Downloaded archive failed integrity verification: {url}")
    return path


def read_zip_table(zip_file: zipfile.ZipFile, name_hint: str) -> list[dict[str, str]]:
    candidates = [name for name in zip_file.namelist() if name_hint.upper() in name.upper() and not name.endswith("/")]
    if not candidates:
        return []
    with zip_file.open(candidates[0], "r") as handle:
        raw = handle.read()
    for encoding in ("utf-8-sig", "latin-1"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("utf-8", errors="replace")
    sample = text[:2048]
    delimiter = "\t" if sample.count("\t") >= sample.count("|") else "|"
    return [dict(row) for row in csv.DictReader(io.StringIO(text), delimiter=delimiter)]


def match_13f_ticker(
    row: dict[str, str],
    *,
    cusip_map: dict[str, str],
    name_map: dict[str, str],
) -> str:
    cusip = normalize_cusip(row.get("CUSIP") or row.get("cusip"))
    if cusip and cusip in cusip_map:
        return cusip_map[cusip]
    issuer = normalize_issuer_name(row.get("NAMEOFISSUER") or row.get("nameOfIssuer") or row.get("issuerName"))
    if issuer in name_map:
        return name_map[issuer]
    return ""


def archive_already_processed(conn: sqlite3.Connection, archive_path: Path) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM institutional_13f_holdings WHERE source_file = ? LIMIT 1",
            (str(archive_path),),
        ).fetchone()
        is not None
    )


def upsert_13f_records(
    conn: sqlite3.Connection,
    *,
    filing_rows: list[tuple[Any, ...]],
    holding_rows: list[tuple[Any, ...]],
) -> None:
    with conn:
        conn.executemany(
            """
            INSERT INTO institutional_13f_filings(
                filing_key, accession_number, manager_cik, manager_name, period_of_report,
                filing_date, accepted_at, source, source_file, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(filing_key) DO UPDATE SET
                accession_number = excluded.accession_number,
                manager_cik = excluded.manager_cik,
                manager_name = excluded.manager_name,
                period_of_report = excluded.period_of_report,
                filing_date = excluded.filing_date,
                accepted_at = excluded.accepted_at,
                source_file = excluded.source_file,
                updated_at = excluded.updated_at
            """,
            filing_rows,
        )
        conn.executemany(
            """
            INSERT INTO institutional_13f_holdings(
                filing_key, manager_cik, manager_name, ticker, cusip, period_of_report,
                filing_date, accepted_at, shares, market_value, title_of_class, share_type, put_call,
                source, source_file, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(filing_key, ticker, cusip) DO UPDATE SET
                shares = excluded.shares,
                market_value = excluded.market_value,
                title_of_class = excluded.title_of_class,
                share_type = excluded.share_type,
                put_call = excluded.put_call,
                updated_at = excluded.updated_at
            """,
            holding_rows,
        )


def sync_sec_13f_data_sets(
    conn: sqlite3.Connection,
    *,
    tickers_csv: Path | None,
    cusip_ticker_map_csv: Path | None,
    history_start_date: date,
    end_date: date,
    cache_dir: Path,
    index_url: str = DEFAULT_SEC_13F_DATASETS_URL,
    user_agent: str = DEFAULT_USER_AGENT,
    timeout_sec: float = 120.0,
    sleep_sec: float = 0.2,
    max_archives: int = 0,
    force_reprocess_archives: bool = False,
) -> SyncResult:
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
    total_holdings = 0
    processed_archives = 0
    skipped_archives = 0
    dropped_rows_missing_accession = 0
    dropped_rows_missing_submission = 0
    dropped_rows_unparsable_filing_date = 0
    matched_rows_unparsable_period = 0
    for url in archives:
        archive_path = download_cached(url, cache_dir=cache_dir, user_agent=user_agent, timeout_sec=timeout_sec)
        if not force_reprocess_archives and archive_already_processed(conn, archive_path):
            skipped_archives += 1
            continue
        now = utc_now()
        filing_rows: dict[str, tuple[Any, ...]] = {}
        holding_rows: list[tuple[Any, ...]] = []
        with zipfile.ZipFile(archive_path) as zf:
            submissions = read_zip_table(zf, "SUBMISSION")
            infotable = read_zip_table(zf, "INFOTABLE")
        if infotable and not submissions:
            # Without SUBMISSION rows every holding lacks a filing date and would be
            # dropped silently — the archive is corrupt or the SEC layout changed.
            raise RuntimeError(
                f"13F archive {archive_path} has {len(infotable)} INFOTABLE rows but no readable "
                "SUBMISSION table; refusing to silently drop them. Delete the cached archive to force a refetch."
            )
        submission_by_accession: dict[str, dict[str, str]] = {
            str(row.get("ACCESSION_NUMBER") or row.get("accession_number") or "").strip(): row
            for row in submissions
        }
        for row in infotable:
            accession = str(row.get("ACCESSION_NUMBER") or row.get("accession_number") or "").strip()
            if not accession:
                dropped_rows_missing_accession += 1
                continue
            submission = submission_by_accession.get(accession)
            if submission is None:
                dropped_rows_missing_submission += 1
                continue
            raw_filing_date = (
                submission.get("FILING_DATE")
                or submission.get("filing_date")
                or submission.get("FILEDASOFDATE")
                or submission.get("filedAsOfDate")
            )
            filing_date = parse_date(raw_filing_date)
            period = parse_date(
                submission.get("REPORTCALENDARORQUARTER")
                or submission.get("PERIODOFREPORT")
                or submission.get("periodOfReport")
            )
            if filing_date is None:
                dropped_rows_unparsable_filing_date += 1
                continue
            if filing_date < history_start_date or filing_date > end_date:
                continue
            ticker = match_13f_ticker(row, cusip_map=cusip_map, name_map=name_map)
            if not ticker:
                continue
            if period is None:
                # Stored with period_of_report = '' and permanently invisible to the
                # period-bucketed aggregator; must be visible in the run summary.
                matched_rows_unparsable_period += 1
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
            filing_key = accession
            filing_rows[filing_key] = (
                filing_key,
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
                    filing_key,
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
        upsert_13f_records(conn, filing_rows=list(filing_rows.values()), holding_rows=holding_rows)
        total_holdings += len(holding_rows)
        processed_archives += 1
        if sleep_sec > 0:
            time.sleep(sleep_sec)
    # Aggregate ONLY this package's universe (name/CUSIP-mapped tickers). The DB is
    # shared by several sector packages under the same source string, so an unscoped
    # aggregation would rebuild — and reshape — other packages' snapshot rows. The
    # period-bucketed aggregator keeps snapshot semantics identical to the
    # industrials/technology upstream syncs for any overlapping tickers.
    universe_tickers = sorted(set(name_map.values()) | set(cusip_map.values()))
    snapshot_rows = aggregate_13f_ownership_for_tickers(conn, universe_tickers, source="sec_13f_data_sets")
    total_table_holdings = int(conn.execute("SELECT COUNT(*) FROM institutional_13f_holdings").fetchone()[0])
    message = (
        f"SEC Form 13F data-set archives processed={processed_archives} "
        f"skipped_already_loaded={skipped_archives} new_matched_holdings={total_holdings} "
        f"total_holdings={total_table_holdings} snapshot_rows={snapshot_rows} "
        f"universe_tickers={len(universe_tickers)} "
        f"dropped_rows_missing_accession={dropped_rows_missing_accession} "
        f"dropped_rows_missing_submission={dropped_rows_missing_submission} "
        f"dropped_rows_unparsable_filing_date={dropped_rows_unparsable_filing_date} "
        f"matched_rows_unparsable_period={matched_rows_unparsable_period}"
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


def normalize_ibkr_fee_rate(raw: object, *, unit: str = "decimal") -> float | None:
    """Normalize IBKR FEE_RATE bars to decimal rate form.

    This account/feed returns decimal rates in testing: 0.003 means 0.3%.
    `unit=percent` is available if a different account returns percentage
    points; `unit=auto` follows the project-wide percentage heuristic.
    """
    value = to_float(raw)
    if value is None or not math.isfinite(value) or value < 0.0:
        return None
    clean_unit = str(unit or "decimal").strip().lower()
    if clean_unit == "decimal":
        return value
    if clean_unit == "auto":
        return normalize_pct(value)
    if clean_unit in {"percent", "percentage", "pct"}:
        return value / 100.0
    raise ValueError(f"Unsupported IBKR fee-rate unit: {unit!r}")


def ibkr_bar_date(raw: object) -> date | None:
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    return parse_date(raw)


def bounded_ibkr_end_date(
    requested_end_date: date,
    *,
    capture_date: date | None = None,
) -> date:
    """Never ask IBKR to materialize observations beyond the capture date."""
    observed_on = capture_date or datetime.now().astimezone().date()
    return min(requested_end_date, observed_on)


def prune_backdated_ibkr_shortable_rows(conn: sqlite3.Connection) -> int:
    """Remove current availability snapshots stamped into historical replays.

    IBKR does not provide historical shortable-share availability. UTC capture
    timestamps can be one calendar day ahead of the local market date, so only
    differences greater than one day are unambiguously backdated.
    """
    with conn:
        result = conn.execute(
            """
            DELETE FROM ibkr_shortable_shares_snapshots
            WHERE COALESCE(asof_datetime, '') <> ''
              AND julianday(date(asof_datetime)) - julianday(date(asof_date)) > 1.0
            """
        )
    return max(0, int(result.rowcount))


def latest_ibkr_fee_asof(conn: sqlite3.Connection, ticker: str) -> date | None:
    row = conn.execute(
        """
        SELECT MAX(asof_date) AS max_asof
        FROM ibkr_borrow_fee_rate_daily
        WHERE ticker = ? AND source = 'interactive_brokers'
        """,
        (ticker,),
    ).fetchone()
    return parse_date(row["max_asof"] if row else None)


def upsert_ibkr_fee_rows(conn: sqlite3.Connection, rows: list[tuple[Any, ...]]) -> None:
    if not rows:
        return
    with conn:
        conn.executemany(
            """
            INSERT INTO ibkr_borrow_fee_rate_daily(
                ticker, asof_date, con_id, borrow_fee_rate, source, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker, asof_date, source) DO UPDATE SET
                con_id = excluded.con_id,
                borrow_fee_rate = excluded.borrow_fee_rate,
                updated_at = excluded.updated_at
            """,
            rows,
        )


def upsert_ibkr_shortable_rows(conn: sqlite3.Connection, rows: list[tuple[Any, ...]]) -> None:
    if not rows:
        return
    with conn:
        conn.executemany(
            """
            INSERT INTO ibkr_shortable_shares_snapshots(
                ticker, asof_date, asof_datetime, con_id, shortable_shares,
                market_data_type, source, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker, asof_date, source) DO UPDATE SET
                asof_datetime = excluded.asof_datetime,
                con_id = excluded.con_id,
                shortable_shares = excluded.shortable_shares,
                market_data_type = excluded.market_data_type,
                updated_at = excluded.updated_at
            """,
            rows,
        )


def prune_ibkr_rows_after_membership_end(
    conn: sqlite3.Connection,
    membership_end_by_ticker: dict[str, date],
) -> tuple[int, int]:
    """Remove current-security IB rows that fall after a historical lineage ended."""
    fee_rows_deleted = 0
    shortable_rows_deleted = 0
    with conn:
        for ticker, terminal_date in membership_end_by_ticker.items():
            terminal_iso = terminal_date.isoformat()
            fee_result = conn.execute(
                """
                DELETE FROM ibkr_borrow_fee_rate_daily
                WHERE ticker = ? AND source = 'interactive_brokers' AND asof_date > ?
                """,
                (ticker, terminal_iso),
            )
            shortable_result = conn.execute(
                """
                DELETE FROM ibkr_shortable_shares_snapshots
                WHERE ticker = ? AND source = 'interactive_brokers' AND asof_date > ?
                """,
                (ticker, terminal_iso),
            )
            fee_rows_deleted += max(0, int(fee_result.rowcount))
            shortable_rows_deleted += max(0, int(shortable_result.rowcount))
    return fee_rows_deleted, shortable_rows_deleted


def _sync_ibkr_borrow_availability(
    conn: sqlite3.Connection,
    *,
    tickers_csv: Path | None,
    history_start_date: date,
    end_date: date,
    host: str = "127.0.0.1",
    port: int = 7497,
    client_id: int = 7822,
    market_data_type: int = 1,
    fee_rate_unit: str = "decimal",
    fee_rate_initial_duration: str = "7 Y",
    fee_rate_incremental_duration: str = "45 D",
    snapshot_wait_sec: float = 4.0,
    shortable_snapshot: bool = True,
    shortable_coverage_warn_min: float = 50.0,
    batch_size: int = 50,
    sleep_sec: float = 0.2,
    max_tickers: int = 0,
) -> SyncResult:
    """Load IBKR borrow fee history and current shortable-share availability.

    Historical availability is only supported for FEE_RATE in the tested TWS
    feed.  shortableShares is sampled from a streaming generic-tick
    subscription because IBKR rejects generic tick 236 in snapshot mode.  The
    subscription is cancelled immediately after the configured wait interval.
    It must be captured daily if historical supply availability is needed.
    """
    try:
        from ib_insync import IB, Stock  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError("IBKR borrow sync requires the ib_insync package and a running TWS/IB Gateway session") from exc

    tickers = load_universe_tickers(tickers_csv)
    primary_exchange_by_ticker = load_universe_exchange_map(tickers_csv)
    ibkr_symbol_by_ticker = load_universe_ibkr_symbol_map(tickers_csv)
    membership_end_by_ticker = load_universe_membership_end_map(tickers_csv)
    capture_date = datetime.now().astimezone().date()
    effective_end_date = bounded_ibkr_end_date(end_date, capture_date=capture_date)
    shortable_snapshot_requested = bool(shortable_snapshot)
    # shortableShares is a live observation. A historical replay must not stamp
    # today's value onto its requested historical as-of date.
    shortable_snapshot = bool(shortable_snapshot and end_date >= capture_date)
    backdated_shortable_rows_pruned = prune_backdated_ibkr_shortable_rows(conn)
    pruned_fee_rows, pruned_shortable_rows = prune_ibkr_rows_after_membership_end(
        conn,
        membership_end_by_ticker,
    )
    tickers, ended_before_asof = filter_ibkr_tickers_for_asof(
        tickers,
        membership_end_by_ticker,
        effective_end_date,
    )
    if max_tickers and max_tickers > 0:
        tickers = tickers[:max_tickers]
    if not tickers:
        raise RuntimeError("IBKR borrow availability sync requires a non-empty ticker universe CSV")

    ib = IB()
    qualified: dict[str, Any] = {}
    fee_rows_written = 0
    shortable_rows_written = 0
    failed_tickers: list[str] = []
    skipped_fee_history = 0
    try:
        ib.connect(host, int(port), clientId=int(client_id), readonly=True, timeout=30)
        ib.reqMarketDataType(int(market_data_type))
        for ticker in tickers:
            try:
                if not ib.isConnected():
                    raise RuntimeError("IBKR connection lost during borrow fee history sync")
                ibkr_symbol = ibkr_symbol_by_ticker.get(ticker, ticker)
                contracts = ib.qualifyContracts(Stock(ibkr_symbol, "SMART", "USD"))
                if not contracts:
                    primary_exchange = primary_exchange_by_ticker.get(ticker, "")
                    if primary_exchange:
                        contracts = ib.qualifyContracts(
                            Stock(ibkr_symbol, "SMART", "USD", primaryExchange=primary_exchange)
                        )
                if not contracts:
                    failed_tickers.append(ticker)
                    continue
                contract = contracts[0]
                latest_fee = latest_ibkr_fee_asof(conn, ticker)
                if latest_fee is not None and latest_fee >= effective_end_date:
                    skipped_fee_history += 1
                    qualified[ticker] = contract
                    continue
                duration = fee_rate_initial_duration if latest_fee is None else fee_rate_incremental_duration
                bars = ib.reqHistoricalData(
                    contract,
                    endDateTime=f"{effective_end_date.strftime('%Y%m%d')} 23:59:59",
                    durationStr=str(duration),
                    barSizeSetting="1 day",
                    whatToShow="FEE_RATE",
                    useRTH=False,
                    formatDate=1,
                    keepUpToDate=False,
                )
                now = utc_now()
                records: list[tuple[Any, ...]] = []
                for bar in bars:
                    bar_date = ibkr_bar_date(getattr(bar, "date", ""))
                    if (
                        bar_date is None
                        or bar_date < history_start_date
                        or bar_date > effective_end_date
                    ):
                        continue
                    rate = normalize_ibkr_fee_rate(getattr(bar, "close", None), unit=fee_rate_unit)
                    if rate is None:
                        continue
                    records.append(
                        (
                            ticker,
                            bar_date.isoformat(),
                            int(getattr(contract, "conId", 0) or 0),
                            rate,
                            "interactive_brokers",
                            now,
                            now,
                        )
                )
                upsert_ibkr_fee_rows(conn, records)
                fee_rows_written += len(records)
                if records or latest_fee is not None:
                    qualified[ticker] = contract
                else:
                    failed_tickers.append(ticker)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                if not ib.isConnected():
                    raise RuntimeError("IBKR connection lost during borrow fee history sync") from exc
                failed_tickers.append(ticker)
            if sleep_sec > 0:
                ib.sleep(float(sleep_sec))

        shortable_batch_size = bounded_streaming_batch_size(batch_size)
        if shortable_snapshot:
            qualified_items = list(qualified.items())
            for start in range(0, len(qualified_items), shortable_batch_size):
                batch = qualified_items[start : start + shortable_batch_size]
                subscriptions: list[tuple[str, Any, Any]] = []
                try:
                    for ticker, contract in batch:
                        try:
                            if not ib.isConnected():
                                raise RuntimeError("IBKR connection lost during shortable-share snapshot sync")
                            ticker_obj = ib.reqMktData(
                                contract,
                                genericTickList="236",
                                # Generic ticks are invalid in IBKR snapshot requests.
                                # Stream briefly, sample shortableShares, then cancel.
                                snapshot=False,
                                regulatorySnapshot=False,
                            )
                            subscriptions.append((ticker, contract, ticker_obj))
                        except KeyboardInterrupt:
                            raise
                        except Exception as exc:
                            if not ib.isConnected():
                                raise RuntimeError("IBKR connection lost during shortable-share snapshot sync") from exc
                            failed_tickers.append(ticker)
                    ib.sleep(max(0.1, float(snapshot_wait_sec)))
                    now = utc_now()
                    snapshot_rows: list[tuple[Any, ...]] = []
                    for ticker, contract, ticker_obj in subscriptions:
                        raw_shortable = getattr(ticker_obj, "shortableShares", None)
                        shortable = to_float(raw_shortable)
                        if shortable is None or not math.isfinite(shortable) or shortable < 0.0:
                            continue
                        snapshot_rows.append(
                            (
                                ticker,
                                capture_date.isoformat(),
                                now,
                                int(getattr(contract, "conId", 0) or 0),
                                shortable,
                                int(market_data_type),
                                "interactive_brokers",
                                now,
                                now,
                            )
                        )
                    upsert_ibkr_shortable_rows(conn, snapshot_rows)
                    shortable_rows_written += len(snapshot_rows)
                finally:
                    for _ticker, contract, _ticker_obj in subscriptions:
                        try:
                            ib.cancelMktData(contract)
                        except Exception:
                            pass
                if sleep_sec > 0:
                    ib.sleep(float(sleep_sec))
    finally:
        if ib.isConnected():
            ib.disconnect()

    total_fee_rows = int(conn.execute("SELECT COUNT(*) FROM ibkr_borrow_fee_rate_daily").fetchone()[0])
    total_shortable_rows = int(conn.execute("SELECT COUNT(*) FROM ibkr_shortable_shares_snapshots").fetchone()[0])
    qualified_count = len(qualified)
    shortable_coverage_pct = (
        100.0 * shortable_rows_written / qualified_count if qualified_count > 0 else 0.0
    )
    coverage_warning = ""
    if qualified_count > 0 and shortable_coverage_pct < float(shortable_coverage_warn_min):
        coverage_warning = (
            f" shortable_coverage_warning=below_min({shortable_coverage_pct:.1f}%"
            f"<{float(shortable_coverage_warn_min):.1f}%)"
        )
    message = (
        "IBKR borrow availability loaded: "
        f"requested_end_date={end_date.isoformat()} "
        f"effective_end_date={effective_end_date.isoformat()} "
        f"qualified={qualified_count} fee_rows_new_or_refreshed={fee_rows_written} "
        f"fee_history_skipped_current={skipped_fee_history} shortable_rows_new_or_refreshed={shortable_rows_written} "
        f"shortable_coverage_pct={shortable_coverage_pct:.1f} "
        f"shortable_snapshot={bool(shortable_snapshot)} failed_tickers={len(set(failed_tickers))} "
        f"ended_memberships_skipped={len(ended_before_asof)} "
        f"post_membership_fee_rows_pruned={pruned_fee_rows} "
        f"post_membership_shortable_rows_pruned={pruned_shortable_rows} "
        f"backdated_shortable_rows_pruned={backdated_shortable_rows_pruned} "
        f"shortable_snapshot_requested={shortable_snapshot_requested} "
        f"shortable_snapshot_historical_skip={shortable_snapshot_requested and not shortable_snapshot} "
        f"max_concurrent_shortable_requests={shortable_batch_size} "
        f"total_fee_rows={total_fee_rows} total_shortable_rows={total_shortable_rows}{coverage_warning}"
    )
    update_feed_state(
        conn,
        feed_name="ibkr_borrow_availability",
        history_start_date=history_start_date,
        source="interactive_brokers",
        source_file=None,
        row_count=total_fee_rows + total_shortable_rows,
        message=message,
    )
    return SyncResult("ibkr_borrow_availability", total_fee_rows + total_shortable_rows, message)


def sync_ibkr_borrow_availability(
    conn: sqlite3.Connection,
    *,
    tickers_csv: Path | None,
    history_start_date: date,
    end_date: date,
    host: str = "127.0.0.1",
    port: int = 7497,
    client_id: int = 7822,
    market_data_type: int = 1,
    fee_rate_unit: str = "decimal",
    fee_rate_initial_duration: str = "7 Y",
    fee_rate_incremental_duration: str = "45 D",
    snapshot_wait_sec: float = 4.0,
    shortable_snapshot: bool = True,
    shortable_coverage_warn_min: float = 50.0,
    batch_size: int = 50,
    sleep_sec: float = 0.2,
    max_tickers: int = 0,
    market_data_lock_timeout_sec: float = DEFAULT_IBKR_LOCK_TIMEOUT_SEC,
) -> SyncResult:
    """Serialize shared IB access and keep streaming requests below account capacity."""
    with IBKRMarketDataLock(timeout_sec=market_data_lock_timeout_sec):
        return _sync_ibkr_borrow_availability(
            conn,
            tickers_csv=tickers_csv,
            history_start_date=history_start_date,
            end_date=end_date,
            host=host,
            port=port,
            client_id=client_id,
            market_data_type=market_data_type,
            fee_rate_unit=fee_rate_unit,
            fee_rate_initial_duration=fee_rate_initial_duration,
            fee_rate_incremental_duration=fee_rate_incremental_duration,
            snapshot_wait_sec=snapshot_wait_sec,
            shortable_snapshot=shortable_snapshot,
            shortable_coverage_warn_min=shortable_coverage_warn_min,
            batch_size=batch_size,
            sleep_sec=sleep_sec,
            max_tickers=max_tickers,
        )
