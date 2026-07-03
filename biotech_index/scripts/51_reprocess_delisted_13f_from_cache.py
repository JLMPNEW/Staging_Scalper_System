#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sqlite3
import sys
import zipfile
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from biotech_index.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
DEFAULT_CUSIP_MAP = PACKAGE_ROOT / "data" / "delisted_13f_cusip_ticker_map.csv"
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "output" / "biotech_index_reports" / "delisted_13f_reprocess"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Reprocess cached SEC 13F data-set archives for delisted biotech candidates by CUSIP."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--market-positioning-db", type=Path, default=None)
    parser.add_argument("--cusip-map", type=Path, default=DEFAULT_CUSIP_MAP)
    parser.add_argument("--cache-dir", type=Path, default=None)
    parser.add_argument("--start-date", type=str, default="")
    parser.add_argument("--end-date", type=str, default="")
    parser.add_argument("--max-archives", type=int, default=0)
    parser.add_argument("--output-dir", type=Path, default=None)
    return parser.parse_args()


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_date(raw: object) -> date | None:
    text = str(raw or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%m/%d/%Y", "%d-%b-%Y", "%d-%B-%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            try:
                return datetime.strptime(text[:10], fmt).date()
            except ValueError:
                pass
    return None


def normalize_cusip(raw: object) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(raw or "").upper())[:9]


def normalize_ticker(raw: object) -> str:
    return str(raw or "").strip().upper().replace(".", "-")


def to_float(raw: object) -> float | None:
    text = str(raw if raw is not None else "").strip().replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [{str(key): str(value or "") for key, value in row.items()} for row in csv.DictReader(handle)]


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_cusip_map(path: Path) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for row in read_csv_rows(path):
        ticker = normalize_ticker(row.get("ticker"))
        cusip = normalize_cusip(row.get("cusip"))
        if ticker and cusip:
            out[cusip] = {**row, "ticker": ticker, "cusip": cusip}
    if not out:
        raise RuntimeError(f"No CUSIP mappings found in {path}")
    return out


def connect_market_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=60.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def zip_member_for(zf: zipfile.ZipFile, hint: str) -> str:
    matches = [name for name in zf.namelist() if hint.upper() in name.upper() and not name.endswith("/")]
    if not matches:
        return ""
    return matches[0]


def read_zip_csv(zf: zipfile.ZipFile, member: str) -> tuple[list[str], list[dict[str, str]]]:
    raw = zf.read(member)
    for encoding in ("utf-8-sig", "latin-1"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("utf-8", errors="replace")
    sample = text[:4096]
    delimiter = "\t" if sample.count("\t") >= sample.count("|") else "|"
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    return list(reader.fieldnames or []), [dict(row) for row in reader]


def iter_zip_csv_rows(zf: zipfile.ZipFile, member: str):
    raw = zf.read(member)
    for encoding in ("utf-8-sig", "latin-1"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("utf-8", errors="replace")
    sample = text[:4096]
    delimiter = "\t" if sample.count("\t") >= sample.count("|") else "|"
    yield from csv.DictReader(io.StringIO(text), delimiter=delimiter)


def first_present(row: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return ""


def aggregate_holding_rows(holding_rows: list[tuple[Any, ...]]) -> list[tuple[Any, ...]]:
    """Pre-aggregate duplicate (filing_key, ticker, cusip) holding rows.

    One 13F filing can report the same issuer CUSIP on several INFOTABLE lines
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


def aggregate_for_tickers(conn: sqlite3.Connection, tickers: set[str], *, source: str = "sec_13f_data_sets") -> int:
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
    cache_dir = (
        args.cache_dir.expanduser().resolve()
        if args.cache_dir
        else resolve_path(cfg_get(config, "market_positioning.institutional_13f.cache_dir"), base_dir=base_dir)
    )
    start_date = parse_date(args.start_date) or parse_date(cfg_get(config, "market_positioning.history_start_date")) or date(2019, 1, 1)
    end_date = parse_date(args.end_date) or datetime.utcnow().date()
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else DEFAULT_OUTPUT_ROOT / end_date.strftime("%Y%m%d")
    )
    cusip_map = load_cusip_map(args.cusip_map.expanduser().resolve())
    tickers = {row["ticker"] for row in cusip_map.values()}
    archives = sorted(cache_dir.glob("*.zip"))
    if args.max_archives > 0:
        archives = archives[-args.max_archives :]
    now = utc_now()
    processed = 0
    matched_holdings = 0
    matched_archives = 0
    archives_missing_filing_date = 0
    with connect_market_db(market_db) as conn:
        for archive in archives:
            filing_rows_by_key: dict[str, tuple[Any, ...]] = {}
            holding_rows: list[tuple[Any, ...]] = []
            with zipfile.ZipFile(archive) as zf:
                submission_member = zip_member_for(zf, "SUBMISSION")
                infotable_member = zip_member_for(zf, "INFOTABLE")
                if not submission_member or not infotable_member:
                    continue
                submission_fields, submission_rows = read_zip_csv(zf, submission_member)
                # Without a filing-date column every matched holding would fail
                # the date-window check and be dropped silently; make that loud.
                if not {"FILING_DATE", "FILEDASOFDATE"} & {str(field).strip().upper() for field in submission_fields}:
                    print(
                        f"ERROR: {archive.name}: SUBMISSION member {submission_member} has no "
                        f"FILING_DATE/FILEDASOFDATE column (fields: {submission_fields}); skipping archive",
                        file=sys.stderr,
                    )
                    archives_missing_filing_date += 1
                    continue
                submissions = {
                    first_present(row, "ACCESSION_NUMBER", "accession_number").strip(): row
                    for row in submission_rows
                }
                for row in iter_zip_csv_rows(zf, infotable_member):
                    cusip = normalize_cusip(first_present(row, "CUSIP", "cusip"))
                    mapped = cusip_map.get(cusip)
                    if not mapped:
                        continue
                    accession = first_present(row, "ACCESSION_NUMBER", "accession_number").strip()
                    if not accession:
                        continue
                    submission = submissions.get(accession, {})
                    filing_date = parse_date(
                        first_present(submission, "FILING_DATE", "filing_date", "FILEDASOFDATE", "filedAsOfDate")
                    )
                    if filing_date is None or filing_date < start_date or filing_date > end_date:
                        continue
                    period = parse_date(
                        first_present(submission, "REPORTCALENDARORQUARTER", "PERIODOFREPORT", "periodOfReport")
                    )
                    manager_cik = first_present(submission, "CIK", "cik", "FILERCIK", "filerCik")
                    manager_name = first_present(submission, "NAME", "name", "FILERNAME", "filerName")
                    accepted_at = first_present(submission, "ACCEPTANCE_DATETIME", "acceptedAt") or filing_date.isoformat()
                    filing_rows_by_key[accession] = (
                        accession,
                        accession,
                        manager_cik,
                        manager_name,
                        period.isoformat() if period else "",
                        filing_date.isoformat(),
                        accepted_at,
                        "sec_13f_data_sets",
                        str(archive),
                        now,
                        now,
                    )
                    holding_rows.append(
                        (
                            accession,
                            manager_cik,
                            manager_name,
                            mapped["ticker"],
                            cusip,
                            period.isoformat() if period else "",
                            filing_date.isoformat(),
                            accepted_at,
                            to_float(first_present(row, "SSHPRNAMT", "sshPrnamt", "shares")),
                            to_float(first_present(row, "VALUE", "value")),
                            first_present(row, "TITLEOFCLASS", "titleOfClass"),
                            first_present(row, "SSHPRNAMTTYPE", "sshPrnamtType"),
                            first_present(row, "PUTCALL", "putCall"),
                            "sec_13f_data_sets",
                            str(archive),
                            now,
                            now,
                        )
                    )
            if holding_rows:
                upsert_records(conn, filing_rows=list(filing_rows_by_key.values()), holding_rows=holding_rows)
                matched_archives += 1
                matched_holdings += len(holding_rows)
            processed += 1
        snapshot_rows = aggregate_for_tickers(conn, tickers, source="sec_13f_data_sets")
    summary = {
        "created_at": now,
        "market_positioning_db": str(market_db),
        "cache_dir": str(cache_dir),
        "cusip_map": str(args.cusip_map.expanduser().resolve()),
        "mapped_cusips": len(cusip_map),
        "archives_seen": len(archives),
        "archives_processed": processed,
        "archives_with_matches": matched_archives,
        "archives_missing_filing_date_column": archives_missing_filing_date,
        "matched_holdings": matched_holdings,
        "snapshot_rows_upserted": snapshot_rows,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    summary_path = output_dir / "delisted_13f_reprocess_summary.json"
    write_json(summary_path, summary)
    summary["summary_json"] = str(summary_path)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
