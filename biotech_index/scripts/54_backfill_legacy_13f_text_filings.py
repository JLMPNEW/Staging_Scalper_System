#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import html
import json
import re
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from biotech_index.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
DEFAULT_CUSIP_MAP = PACKAGE_ROOT / "data" / "delisted_13f_cusip_ticker_map.csv"
DEFAULT_CANDIDATES = PACKAGE_ROOT / "data" / "delisted_biotech_calibration_universe.csv"
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "output" / "biotech_index_reports" / "delisted_legacy_13f_backfill"
DEFAULT_CACHE_DIR = PROJECT_ROOT / "output" / "market_positioning_cache" / "sec_13f_legacy_text"
SEC_ARCHIVES_BASE = "https://www.sec.gov/Archives"
LEGACY_SOURCE = "sec_legacy_13f_text"

FORM_TYPES = {"13F-HR", "13F-HR/A"}
HTML_TAG_RE = re.compile(r"<[^>]+>")
NUMBER_RE = re.compile(r"[-+]?\(?\$?[0-9][0-9,]*(?:\.[0-9]+)?\)?")
ACCESSION_RE = re.compile(r"(\d{10}-\d{2}-\d{6})")
ACCEPTED_RE = re.compile(r"ACCEPTANCE-DATETIME:\s*(\d{14})", re.I)
PERIOD_RE = re.compile(r"CONFORMED PERIOD OF REPORT:\s*(\d{8})", re.I)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill pre-2013 delisted-biotech 13F holdings from legacy SEC manager-filed text filings. "
            "This complements the structured SEC DERA 13F ZIP importer, which starts around Q3 2013."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--market-positioning-db", type=Path, default=None)
    parser.add_argument("--cusip-map", type=Path, default=DEFAULT_CUSIP_MAP)
    parser.add_argument("--candidates", type=Path, default=DEFAULT_CANDIDATES)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--tickers", type=str, default="", help="Comma-separated target tickers. Default: all CUSIP-map rows.")
    parser.add_argument("--start-date", type=str, default="2003-01-01")
    parser.add_argument("--end-date", type=str, default="2013-06-30")
    parser.add_argument("--sleep-sec", type=float, default=0.10)
    parser.add_argument("--skip-filings", type=int, default=0, help="Skip this many in-range 13F-HR filings before parsing.")
    parser.add_argument("--max-filings", type=int, default=0, help="Safety cap for downloaded/parsed 13F filings.")
    parser.add_argument("--max-quarters", type=int, default=0)
    parser.add_argument("--commit-every", type=int, default=500, help="Upsert matched holdings after this many parsed rows.")
    parser.add_argument("--force", action="store_true", help="Redownload filings even when cached.")
    parser.add_argument("--index-only", action="store_true", help="Only download/scan quarterly indexes and report counts.")
    return parser.parse_args()


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_date(raw: object) -> date | None:
    text = str(raw or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%m/%d/%Y", "%d-%b-%Y", "%d-%B-%Y"):
        try:
            return datetime.strptime(text[: min(10, len(text))], fmt).date()
        except ValueError:
            pass
    return None


def normalize_cusip(raw: object) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(raw or "").upper())[:9]


def normalize_ticker(raw: object) -> str:
    return str(raw or "").strip().upper().replace(".", "-")


def normalize_cik(raw: object) -> str:
    digits = re.sub(r"\D", "", str(raw or ""))
    return digits.zfill(10) if digits else ""


def to_float(raw: object) -> float | None:
    text = str(raw if raw is not None else "").strip().replace(",", "").replace("$", "")
    if not text:
        return None
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()")
    try:
        value = float(text)
    except ValueError:
        return None
    return -value if negative else value


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [{str(k): str(v or "") for k, v in row.items()} for row in csv.DictReader(handle)]


def write_csv_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = sorted({key for row in rows for key in row})
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_cusip_map(path: Path, tickers: set[str]) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for row in read_csv_rows(path):
        ticker = normalize_ticker(row.get("ticker"))
        cusip = normalize_cusip(row.get("cusip"))
        if tickers and ticker not in tickers:
            continue
        if ticker and cusip:
            out[cusip] = {**row, "ticker": ticker, "cusip": cusip}
    if not out:
        raise RuntimeError(f"No CUSIP mappings found in {path}")
    return out


def load_candidate_windows(path: Path) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for row in read_csv_rows(path):
        ticker = normalize_ticker(row.get("ticker"))
        if not ticker:
            continue
        out[ticker] = {
            "ticker": ticker,
            "company_name": row.get("company_name", ""),
            "issuer_cik": normalize_cik(row.get("cik")),
            "price_start_date": str(row.get("price_start_date") or ""),
            "price_end_date": str(row.get("price_end_date") or ""),
        }
    return out


def connect_market_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=60.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def quarters_between(start: date, end: date) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    year = start.year
    while year <= end.year:
        for q in range(1, 5):
            q_start = date(year, 3 * (q - 1) + 1, 1)
            q_end = date(year, 3 * q, 1) + timedelta(days=31)
            q_end = date(q_end.year, q_end.month, 1) - timedelta(days=1)
            if q_end >= start and q_start <= end:
                out.append((year, q))
        year += 1
    return out


def request_bytes(url: str, user_agent: str, *, timeout: float = 120.0) -> bytes:
    request = Request(url, headers={"User-Agent": user_agent, "Accept-Encoding": "identity"})
    with urlopen(request, timeout=timeout) as response:
        return response.read()


def cached_download(url: str, path: Path, user_agent: str, *, force: bool, sleep_sec: float) -> bytes:
    if path.exists() and not force:
        return path.read_bytes()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = request_bytes(url, user_agent)
    path.write_bytes(data)
    if sleep_sec > 0:
        time.sleep(sleep_sec)
    return data


def parse_master_index(raw: bytes) -> list[dict[str, str]]:
    text = raw.decode("latin-1", errors="replace")
    rows: list[dict[str, str]] = []
    started = False
    for line in text.splitlines():
        if not started:
            if line.startswith("CIK|Company Name|Form Type|Date Filed|Filename"):
                started = True
            continue
        parts = line.split("|")
        if len(parts) != 5:
            continue
        cik, company, form, filing_date, filename = [part.strip() for part in parts]
        if form.upper() in FORM_TYPES:
            rows.append(
                {
                    "manager_cik": normalize_cik(cik),
                    "manager_name": company,
                    "form": form.upper(),
                    "filing_date": filing_date,
                    "filename": filename,
                }
            )
    return rows


def filing_cache_path(cache_dir: Path, filename: str) -> Path:
    return cache_dir / "filings" / filename


def accession_from_filename(filename: str) -> str:
    match = ACCESSION_RE.search(filename)
    if match:
        return match.group(1)
    return Path(filename).stem


def clean_line(raw: str) -> str:
    line = html.unescape(raw)
    line = HTML_TAG_RE.sub(" ", line)
    line = line.replace("\xa0", " ")
    return re.sub(r"\s+", " ", line).strip()


def normalized_line(raw: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", raw.upper())


def parse_accepted_at(text: str, fallback: date | None) -> str:
    match = ACCEPTED_RE.search(text[:10000])
    if not match:
        return fallback.isoformat() if fallback else ""
    try:
        return datetime.strptime(match.group(1), "%Y%m%d%H%M%S").isoformat()
    except ValueError:
        return fallback.isoformat() if fallback else ""


def parse_period(text: str) -> str:
    match = PERIOD_RE.search(text[:20000])
    if not match:
        return ""
    parsed = parse_date(match.group(1))
    return parsed.isoformat() if parsed else ""


def value_is_thousands(text: str) -> bool:
    # Legacy 13F-HR text tables report VALUE in thousands by SEC convention.
    # Header markers (e.g. "VALUE (X$1000)") can only *confirm* thousands;
    # there is no reliable "whole dollars" signal to distinguish, so honestly
    # default to thousands regardless of header mangling.
    return True


_CUSIP_PATTERNS: dict[str, re.Pattern[str]] = {}


def cusip_pattern(cusip: str) -> re.Pattern[str]:
    """Compile a token-boundary pattern for a 9-char CUSIP.

    Matching the CUSIP as a bare substring of the fully-concatenated
    normalized line lets any 9-char window of an unrelated identifier/number
    run produce a phantom holding.  Require non-alphanumeric (or line-edge)
    boundaries around the CUSIP in the original cleaned line, while still
    allowing short whitespace/dash separators inside it because fixed-width
    tables often split the issuer/issue/check-digit groups.
    """
    pattern = _CUSIP_PATTERNS.get(cusip)
    if pattern is None:
        body = r"[\s\-]{0,2}".join(re.escape(char) for char in cusip)
        pattern = re.compile(rf"(?<![A-Z0-9]){body}(?![A-Z0-9])")
        _CUSIP_PATTERNS[cusip] = pattern
    return pattern


def extract_numbers_after_cusip(line: str, tail_start: int) -> tuple[float | None, float | None]:
    tail = line[tail_start:]
    numbers = [to_float(match.group(0)) for match in NUMBER_RE.finditer(tail)]
    numbers = [value for value in numbers if value is not None]
    if len(numbers) >= 2:
        return numbers[0], numbers[1]
    # Do NOT guess from elsewhere on the line: the trailing numbers are often
    # voting-authority columns, so callers must skip the line instead.
    return None, None


def parse_matching_holdings(
    *,
    text: str,
    cusip_map: dict[str, dict[str, str]],
    source_file: str,
) -> list[dict[str, Any]]:
    thousands = value_is_thousands(text)
    matches: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    lines = text.splitlines()
    for raw in lines:
        line = clean_line(raw)
        if len(line) < 9:
            continue
        upper = line.upper()
        for cusip, mapped in cusip_map.items():
            cusip_match = cusip_pattern(cusip).search(upper)
            if cusip_match is None:
                continue
            put_call = "PUT" if re.search(r"\bPUT\b", upper) else "CALL" if re.search(r"\bCALL\b", upper) else ""
            if put_call:
                continue
            value, shares = extract_numbers_after_cusip(line, cusip_match.end())
            if value is None and shares is None:
                print(
                    f"WARNING: could not locate value/shares after CUSIP {cusip} in {source_file}; "
                    f"skipped line: {line[:200]}",
                    file=sys.stderr,
                )
                continue
            if shares is None or shares <= 0:
                continue
            market_value = value * 1000.0 if value is not None and thousands else value
            key = (mapped["ticker"], cusip, f"{shares:.6f}:{market_value if market_value is not None else ''}")
            if key in seen:
                continue
            seen.add(key)
            matches.append(
                {
                    "ticker": mapped["ticker"],
                    "cusip": cusip,
                    "issuer_name_match": mapped.get("company_name", ""),
                    "shares": shares,
                    "market_value": market_value,
                    "title_of_class": "",
                    "share_type": "SH",
                    "put_call": "",
                    "raw_line": line[:1000],
                    "source_file": source_file,
                }
            )
    return matches


def aggregate_holding_rows(holding_rows: list[tuple[Any, ...]]) -> list[tuple[Any, ...]]:
    """Pre-aggregate duplicate (filing_key, ticker, cusip) holding rows.

    One 13F filing can report the same issuer CUSIP on several table lines
    (SOLE/SHARED/NONE investment-discretion splits).  The holdings table keys
    on (filing_key, ticker, cusip), so upserting the raw lines would let the
    last line win and undercount the position.  Sum shares and market_value
    across the duplicate lines -- preferring plain share rows over put/call
    rows when both appear -- and keep the other fields from the row with the
    largest share count.  Row tuple layout: [0]=filing_key, [3]=ticker,
    [4]=cusip, [8]=shares, [9]=market_value, [12]=put_call.
    """
    grouped: dict[tuple[Any, Any, Any], list[tuple[Any, ...]]] = {}
    for row in holding_rows:
        grouped.setdefault((row[0], row[3], row[4]), []).append(row)
    out: list[tuple[Any, ...]] = []
    for rows in grouped.values():
        share_rows = [row for row in rows if not str(row[12] or "").strip()]
        rows = share_rows or rows
        if len(rows) == 1:
            out.append(rows[0])
            continue
        shares = [float(row[8]) for row in rows if row[8] is not None]
        values = [float(row[9]) for row in rows if row[9] is not None]
        merged = list(max(rows, key=lambda row: float(row[8] or 0.0)))
        merged[8] = sum(shares) if shares else None
        merged[9] = sum(values) if values else None
        out.append(tuple(merged))
    return out


def upsert_records(
    conn: sqlite3.Connection,
    *,
    filing_rows: list[tuple[Any, ...]],
    holding_rows: list[tuple[Any, ...]],
) -> None:
    holding_rows = aggregate_holding_rows(holding_rows)
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
                source_file = excluded.source_file,
                updated_at = excluded.updated_at
            """,
            holding_rows,
        )


def aggregate_for_tickers(conn: sqlite3.Connection, tickers: set[str], *, source: str) -> int:
    if not tickers:
        return 0
    placeholders = ",".join("?" for _ in tickers)
    rows = conn.execute(
        f"""
        SELECT ticker, filing_date, period_of_report,
               COALESCE(NULLIF(manager_cik, ''), NULLIF(manager_name, ''), filing_key) AS manager_key,
               COALESCE(shares, 0.0) AS shares,
               COALESCE(market_value, 0.0) AS market_value
        FROM institutional_13f_holdings
        WHERE ticker IN ({placeholders})
          AND source = ?
          AND UPPER(COALESCE(share_type, '')) IN ('', 'SH')
          AND COALESCE(put_call, '') = ''
        ORDER BY ticker, period_of_report, filing_date
        """,
        tuple(sorted(tickers)) + (source,),
    ).fetchall()
    # Group holdings by period_of_report (quarter) so each snapshot aggregates
    # ALL managers that reported for that quarter.  13F filing dates for one
    # quarter are spread over ~45 days, so grouping by filing_date fragments
    # the quarter and makes the new/exiting-buyer diff compare disjoint
    # per-day filer subsets.  The snapshot's asof_date column carries
    # MAX(filing_date) of the included filings, which keeps the snapshot
    # point-in-time safe (only visible once every included filing existed).
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        period = str(row["period_of_report"] or "") or str(row["filing_date"] or "")
        key = (str(row["ticker"]), period)
        bucket = grouped.setdefault(key, {"shares": 0.0, "value": 0.0, "managers": set(), "asof_date": ""})
        bucket["shares"] += float(row["shares"] or 0.0)
        bucket["value"] += float(row["market_value"] or 0.0)
        filing_date = str(row["filing_date"] or "")
        if filing_date > str(bucket["asof_date"]):
            bucket["asof_date"] = filing_date
        manager_key = str(row["manager_key"] or "")
        if manager_key:
            bucket["managers"].add(manager_key)

    by_ticker: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    for (ticker, period), payload in grouped.items():
        by_ticker[ticker].append((period, payload))

    now = utc_now()
    records: list[tuple[Any, ...]] = []
    for ticker, ticker_rows in by_ticker.items():
        prior_shares: float | None = None
        prior_managers: set[str] | None = None
        # Quarter-over-quarter diff: consecutive period_of_report snapshots.
        for period, payload in sorted(ticker_rows, key=lambda item: item[0]):
            shares = float(payload["shares"] or 0.0)
            delta = (shares - prior_shares) / prior_shares if prior_shares and prior_shares > 0.0 else None
            prior_shares = shares
            managers = set(payload.get("managers") or set())
            if prior_managers is None:
                new_buyer_count = 0
                exiting_holder_count = 0
            else:
                new_buyer_count = len(managers - prior_managers)
                exiting_holder_count = len(prior_managers - managers)
            prior_managers = managers
            records.append(
                (
                    ticker,
                    str(payload["asof_date"] or "") or period,
                    period,
                    shares,
                    float(payload["value"] or 0.0),
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
    with conn:
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


def main() -> int:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    market_db = (
        args.market_positioning_db.expanduser().resolve()
        if args.market_positioning_db
        else resolve_path(cfg_get(config, "market_positioning.database_path"), base_dir=base_dir)
    )
    user_agent = str(cfg_get(config, "market_positioning.user_agent", "") or "").strip()
    if not user_agent:
        raise RuntimeError("SEC legacy 13F backfill requires market_positioning.user_agent in config")

    start_date = parse_date(args.start_date)
    end_date = parse_date(args.end_date)
    if start_date is None or end_date is None:
        raise RuntimeError("--start-date and --end-date are required")
    requested_tickers = {normalize_ticker(item) for item in args.tickers.split(",") if normalize_ticker(item)}
    candidates = load_candidate_windows(args.candidates.expanduser().resolve())
    cusip_map = load_cusip_map(args.cusip_map.expanduser().resolve(), requested_tickers)
    tickers = {row["ticker"] for row in cusip_map.values()}
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else DEFAULT_OUTPUT_ROOT / end_date.strftime("%Y%m%d")
    )
    cache_dir = args.cache_dir.expanduser().resolve()
    quarters = quarters_between(start_date, end_date)
    if args.max_quarters > 0:
        quarters = quarters[: args.max_quarters]

    now = utc_now()
    index_rows_total = 0
    filings_seen = 0
    filings_skipped = 0
    filings_downloaded_or_cached = 0
    filings_with_matches = 0
    holdings_matched = 0
    per_ticker_matches: defaultdict[str, int] = defaultdict(int)
    match_audit_rows: list[dict[str, Any]] = []
    filing_rows_by_key: dict[str, tuple[Any, ...]] = {}
    holding_rows: list[tuple[Any, ...]] = []
    conn: sqlite3.Connection | None = None

    def flush_records() -> None:
        nonlocal filing_rows_by_key, holding_rows
        if args.index_only or conn is None or not filing_rows_by_key or not holding_rows:
            return
        upsert_records(conn, filing_rows=list(filing_rows_by_key.values()), holding_rows=holding_rows)
        filing_rows_by_key = {}
        holding_rows = []

    try:
        conn = None if args.index_only else connect_market_db(market_db)
        for year, quarter in quarters:
            index_url = f"{SEC_ARCHIVES_BASE}/edgar/full-index/{year}/QTR{quarter}/master.idx"
            index_path = cache_dir / "indexes" / str(year) / f"QTR{quarter}" / "master.idx"
            try:
                index_raw = cached_download(index_url, index_path, user_agent, force=args.force, sleep_sec=args.sleep_sec)
            except (HTTPError, URLError, TimeoutError) as exc:
                print(f"WARNING: failed to download {index_url}: {type(exc).__name__}: {exc}", file=sys.stderr)
                continue
            index_rows = parse_master_index(index_raw)
            index_rows_total += len(index_rows)
            if args.index_only:
                continue
            for item in index_rows:
                filing_date = parse_date(item["filing_date"])
                if filing_date is None or filing_date < start_date or filing_date > end_date:
                    continue
                if filings_skipped < max(0, args.skip_filings):
                    filings_skipped += 1
                    continue
                if args.max_filings > 0 and filings_seen >= args.max_filings:
                    break
                filings_seen += 1
                filename = item["filename"]
                accession = accession_from_filename(filename)
                source_file = f"{SEC_ARCHIVES_BASE}/{filename}"
                local_path = filing_cache_path(cache_dir, filename)
                try:
                    raw = cached_download(source_file, local_path, user_agent, force=args.force, sleep_sec=args.sleep_sec)
                except (HTTPError, URLError, TimeoutError) as exc:
                    print(f"WARNING: failed to download {source_file}: {type(exc).__name__}: {exc}", file=sys.stderr)
                    continue
                filings_downloaded_or_cached += 1
                text = raw.decode("latin-1", errors="replace")
                if not any(cusip in normalized_line(text) for cusip in cusip_map):
                    continue
                matches = parse_matching_holdings(text=text, cusip_map=cusip_map, source_file=source_file)
                if not matches:
                    continue
                period = parse_period(text)
                accepted_at = parse_accepted_at(text, filing_date)
                filing_key = f"{LEGACY_SOURCE}:{accession}"
                filing_rows_by_key[filing_key] = (
                    filing_key,
                    accession,
                    item["manager_cik"],
                    item["manager_name"],
                    period,
                    filing_date.isoformat(),
                    accepted_at,
                    LEGACY_SOURCE,
                    source_file,
                    now,
                    now,
                )
                filings_with_matches += 1
                for match in matches:
                    ticker = str(match["ticker"])
                    candidate = candidates.get(ticker, {})
                    per_ticker_matches[ticker] += 1
                    holdings_matched += 1
                    holding_rows.append(
                        (
                            filing_key,
                            item["manager_cik"],
                            item["manager_name"],
                            ticker,
                            match["cusip"],
                            period,
                            filing_date.isoformat(),
                            accepted_at,
                            match["shares"],
                            match["market_value"],
                            match["title_of_class"],
                            match["share_type"],
                            match["put_call"],
                            LEGACY_SOURCE,
                            source_file,
                            now,
                            now,
                        )
                    )
                    match_audit_rows.append(
                        {
                            "ticker": ticker,
                            "company_name": candidate.get("company_name", ""),
                            "issuer_cik": candidate.get("issuer_cik", ""),
                            "cusip": match["cusip"],
                            "manager_cik": item["manager_cik"],
                            "manager_name": item["manager_name"],
                            "filing_date": filing_date.isoformat(),
                            "period_of_report": period,
                            "accession_number": accession,
                            "shares": match["shares"],
                            "market_value": match["market_value"],
                            "source_file": source_file,
                            "raw_line": match["raw_line"],
                        }
                    )
                if args.commit_every > 0 and len(holding_rows) >= args.commit_every:
                    flush_records()
            if args.max_filings > 0 and filings_seen >= args.max_filings:
                break
        flush_records()
    finally:
        if conn is not None:
            conn.close()

    snapshot_rows = 0
    if not args.index_only:
        with connect_market_db(market_db) as conn:
            snapshot_rows = aggregate_for_tickers(conn, tickers, source=LEGACY_SOURCE)

    output_dir.mkdir(parents=True, exist_ok=True)
    match_csv = output_dir / "legacy_13f_text_matches.csv"
    write_csv_rows(match_csv, match_audit_rows)
    summary = {
        "created_at": now,
        "source": LEGACY_SOURCE,
        "market_positioning_db": str(market_db),
        "cache_dir": str(cache_dir),
        "cusip_map": str(args.cusip_map.expanduser().resolve()),
        "candidate_csv": str(args.candidates.expanduser().resolve()),
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "tickers": sorted(tickers),
        "mapped_cusips": len(cusip_map),
        "quarters_scanned": len(quarters),
        "index_13f_rows_seen": index_rows_total,
        "filings_seen": filings_seen,
        "filings_skipped": filings_skipped,
        "filings_downloaded_or_cached": filings_downloaded_or_cached,
        "filings_with_matches": filings_with_matches,
        "matched_holdings": holdings_matched,
        "snapshot_rows_upserted": snapshot_rows,
        "per_ticker_matches": dict(sorted(per_ticker_matches.items())),
        "match_csv": str(match_csv),
        "index_only": bool(args.index_only),
    }
    summary_path = output_dir / "legacy_13f_text_backfill_summary.json"
    write_json(summary_path, summary)
    summary["summary_json"] = str(summary_path)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
