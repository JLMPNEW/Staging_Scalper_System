from __future__ import annotations

import csv
import io
import json
import json.decoder
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from market_positioning.core import (
    aggregate_13f_ownership,
    normalize_ticker,
    parse_date,
    read_csv_rows,
    to_float,
    update_feed_state,
    utc_now,
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
        for key in ("company_name", "issuer_name", "name"):
            normalized = normalize_issuer_name(row.get(key))
            if normalized:
                out.setdefault(normalized, ticker)
    return out


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
) -> list[tuple[Any, ...]]:
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
                avg_daily_volume = to_float(row.get("averageShortShareNumber"))
                days_to_cover = to_float(row.get("daysToCoverNumber"))
                if days_to_cover is None and short_shares is not None and avg_daily_volume and avg_daily_volume > 0.0:
                    days_to_cover = short_shares / avg_daily_volume
                records.append(
                    (
                        api_ticker,
                        settlement.isoformat(),
                        settlement.isoformat(),
                        settlement.isoformat(),
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
    )
    with conn:
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
        return path
    try:
        raw = http_request(url, user_agent=user_agent, timeout_sec=timeout_sec)
    except urllib.error.HTTPError as exc:
        if exc.code in {403, 404}:
            return None
        raise
    if not raw:
        return None
    path.write_bytes(raw)
    return path


def read_finra_equity_short_interest_file(path: Path) -> list[dict[str, str]]:
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    if not text.strip():
        return []
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
            records.append(
                (
                    ticker,
                    row_publication.isoformat(),
                    row_settlement.isoformat(),
                    row_publication.isoformat(),
                    to_float(row.get("currentShortPositionQuantity")),
                    None,
                    None,
                    to_float(row.get("daysToCoverQuantity")),
                    "finra_equity_short_interest_files",
                    str(path),
                    now,
                    now,
                )
            )
        if sleep_sec > 0:
            time.sleep(sleep_sec)
    with conn:
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


def download_cached(url: str, *, cache_dir: Path, user_agent: str, timeout_sec: float) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    filename = Path(urllib.parse.urlparse(url).path).name
    path = cache_dir / filename
    if path.exists() and path.stat().st_size > 0:
        return path
    raw = http_request(url, user_agent=user_agent, timeout_sec=timeout_sec)
    path.write_bytes(raw)
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
                filing_date, accepted_at, shares, market_value, source, source_file, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(filing_key, ticker, cusip) DO UPDATE SET
                shares = excluded.shares,
                market_value = excluded.market_value,
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
    for url in archives:
        archive_path = download_cached(url, cache_dir=cache_dir, user_agent=user_agent, timeout_sec=timeout_sec)
        if archive_already_processed(conn, archive_path):
            skipped_archives += 1
            continue
        now = utc_now()
        filing_rows: dict[str, tuple[Any, ...]] = {}
        holding_rows: list[tuple[Any, ...]] = []
        with zipfile.ZipFile(archive_path) as zf:
            submissions = read_zip_table(zf, "SUBMISSION")
            infotable = read_zip_table(zf, "INFOTABLE")
        submission_by_accession: dict[str, dict[str, str]] = {
            str(row.get("ACCESSION_NUMBER") or row.get("accession_number") or "").strip(): row
            for row in submissions
        }
        for row in infotable:
            accession = str(row.get("ACCESSION_NUMBER") or row.get("accession_number") or "").strip()
            if not accession:
                continue
            submission = submission_by_accession.get(accession, {})
            filing_date = parse_date(
                submission.get("FILING_DATE")
                or submission.get("filing_date")
                or submission.get("FILEDASOFDATE")
                or submission.get("filedAsOfDate")
            )
            period = parse_date(
                submission.get("REPORTCALENDARORQUARTER")
                or submission.get("PERIODOFREPORT")
                or submission.get("periodOfReport")
            )
            if filing_date is None or filing_date < history_start_date or filing_date > end_date:
                continue
            ticker = match_13f_ticker(row, cusip_map=cusip_map, name_map=name_map)
            if not ticker:
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
    aggregate_13f_ownership(conn, source="sec_13f_data_sets")
    total_table_holdings = int(conn.execute("SELECT COUNT(*) FROM institutional_13f_holdings").fetchone()[0])
    message = (
        f"SEC Form 13F data-set archives processed={processed_archives} "
        f"skipped_already_loaded={skipped_archives} new_matched_holdings={total_holdings} "
        f"total_holdings={total_table_holdings}"
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
