#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
import time
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from market_positioning.api_collectors import normalize_cusip, normalize_issuer_name  # noqa: E402
from med_devices.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from med_devices.core.db import connect, finish_run, init_db, start_run, utc_now  # noqa: E402
from med_devices.core.logging_utils import configure_utc_logging  # noqa: E402
from med_devices.core.text_norm import normalize_ticker  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
SOURCE_ID = "sec_13f_edgar"
AGGREGATE_MANAGER_NAME = "aggregate_13f_common_share"
LEGACY_AGGREGATE_MANAGER_NAME = "aggregate_13f_snapshot"
NON_COMMON_TITLE_RE = re.compile(
    r"\b(NOTE|NOTES|BOND|BONDS|DEBT|DEB|DEBENTURE|PFD|PREFERRED|WARRANT|RIGHT|UNIT|CALL|PUT)\b",
    re.I,
)
FIELDNAMES = [
    "accession_nodash",
    "report_date",
    "source_id",
    "manager_cik",
    "manager_name",
    "ticker",
    "company_id",
    "cusip",
    "shares",
    "market_value_usd",
    "manager_count",
    "institutional_ownership_pct",
    "institutional_ownership_delta_pct",
    "put_call",
]
SUMMARY_FIELDNAMES = [
    "archive",
    "infotable_rows",
    "matched_common_share_rows",
    "candidate_latest_rows",
    "unique_tickers",
    "periods",
    "elapsed_sec",
]


@dataclass(frozen=True)
class Holding:
    ticker: str
    period: date
    manager_key: str
    cusip: str
    filing_key: str
    filing_date: date
    accepted_at: str
    shares: float
    market_value: float
    title_of_class: str
    share_type: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Rebuild med-device SEC 13F aggregate facts from cached common-share rows only."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--history-start", default="")
    parser.add_argument("--asof", default="")
    parser.add_argument("--tickers-csv", type=Path, default=None)
    parser.add_argument("--cache-dir", type=Path, default=None)
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--summary-csv", type=Path, default=None)
    parser.add_argument("--max-archives", type=int, default=0)
    return parser.parse_args()


def parse_date(raw: object) -> date | None:
    text = str(raw or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%m/%d/%Y", "%d-%b-%Y"):
        try:
            return datetime.strptime(text[:10] if fmt == "%Y-%m-%d" else text.upper(), fmt).date()
        except ValueError:
            continue
    return None


def to_float(raw: object) -> float | None:
    try:
        value = float(str(raw).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return value if value == value else None


def first_present(row: dict[str, Any], *names: str) -> str:
    for name in names:
        value = row.get(name)
        if value is not None and str(value).strip() != "":
            return str(value).strip()
    return ""


def locate_member(zip_file: zipfile.ZipFile, name_hint: str) -> str | None:
    matches = [
        name
        for name in zip_file.namelist()
        if name_hint.upper() in name.upper() and not name.endswith("/")
    ]
    return matches[0] if matches else None


def detect_zip_table_format(zip_file: zipfile.ZipFile, member: str) -> tuple[str, str]:
    with zip_file.open(member, "r") as handle:
        raw = handle.read(65536)
    encoding = "utf-8-sig"
    for candidate in ("utf-8-sig", "latin-1"):
        try:
            sample = raw.decode(candidate)
            encoding = candidate
            break
        except UnicodeDecodeError:
            continue
    else:
        sample = raw.decode("utf-8", errors="replace")
    delimiter = "\t" if sample.count("\t") >= sample.count("|") else "|"
    return encoding, delimiter


def iter_zip_table(zip_file: zipfile.ZipFile, name_hint: str) -> Iterable[dict[str, str]]:
    member = locate_member(zip_file, name_hint)
    if member is None:
        return
    encoding, delimiter = detect_zip_table_format(zip_file, member)
    with zip_file.open(member, "r") as raw_handle:
        text_handle = io.TextIOWrapper(raw_handle, encoding=encoding, errors="replace", newline="")
        reader = csv.DictReader(text_handle, delimiter=delimiter)
        for row in reader:
            yield {str(key or "").strip(): str(value or "").strip() for key, value in row.items()}


def load_submission_map(zip_file: zipfile.ZipFile) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for row in iter_zip_table(zip_file, "SUBMISSION"):
        accession = first_present(row, "ACCESSION_NUMBER", "accession_number")
        if accession:
            out[accession] = row
    return out


def load_name_map(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    normalized_names: list[tuple[str, str]] = []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            ticker = normalize_ticker(row.get("ticker") or row.get("symbol"))
            if not ticker:
                continue
            for key in ("company_name", "issuer_name", "name"):
                normalized = normalize_issuer_name(row.get(key))
                if normalized:
                    out.setdefault(normalized, ticker)
                    normalized_names.append((normalized, ticker))
    first_token_counts: dict[str, int] = defaultdict(int)
    first_token_tickers: dict[str, str] = {}
    for normalized, ticker in normalized_names:
        first = normalized.split()[0] if normalized.split() else ""
        if len(first) >= 4:
            first_token_counts[first] += 1
            first_token_tickers[first] = ticker
    for first, count in first_token_counts.items():
        if count == 1:
            out.setdefault(first, first_token_tickers[first])
    return out


def load_company_map(conn: Any) -> dict[str, dict[str, Any]]:
    rows = conn.execute("SELECT company_id, ticker FROM dim_company WHERE is_active = 1").fetchall()
    return {normalize_ticker(row["ticker"]): dict(row) for row in rows}


def match_ticker(row: dict[str, str], *, name_map: dict[str, str]) -> str:
    issuer = normalize_issuer_name(first_present(row, "NAMEOFISSUER", "nameOfIssuer", "issuerName"))
    if issuer in name_map:
        return name_map[issuer]
    first = issuer.split()[0] if issuer.split() else ""
    return name_map.get(first, "")


def is_common_share_row(row: dict[str, str]) -> bool:
    share_type = first_present(row, "SSHPRNAMTTYPE", "sshPrnamtType", "share_type").upper()
    put_call = first_present(row, "PUTCALL", "putCall", "put_call").upper()
    title = first_present(row, "TITLEOFCLASS", "titleOfClass", "title_of_class")
    if share_type != "SH":
        return False
    if put_call:
        return False
    return not NON_COMMON_TITLE_RE.search(title)


def parse_holding(
    row: dict[str, str],
    *,
    ticker: str,
    submission: dict[str, str],
    history_start: date,
    asof: date,
) -> Holding | None:
    accession = first_present(row, "ACCESSION_NUMBER", "accession_number")
    if not accession:
        return None
    filing_date = parse_date(
        first_present(submission, "FILING_DATE", "filing_date", "FILEDASOFDATE", "filedAsOfDate")
    )
    period = parse_date(
        first_present(submission, "REPORTCALENDARORQUARTER", "PERIODOFREPORT", "periodOfReport")
    )
    if filing_date is None or period is None:
        return None
    if period < history_start or period > asof or filing_date > asof:
        return None
    manager_cik = first_present(submission, "CIK", "cik", "FILERCIK", "filerCik")
    manager_name = first_present(submission, "NAME", "name", "FILERNAME", "filerName")
    manager_key = manager_cik or manager_name
    if not manager_key:
        return None
    shares = to_float(first_present(row, "SSHPRNAMT", "sshPrnamt", "shares"))
    market_value = to_float(first_present(row, "VALUE", "value", "market_value"))
    if shares is None or shares <= 0:
        return None
    return Holding(
        ticker=ticker,
        period=period,
        manager_key=manager_key,
        cusip=normalize_cusip(first_present(row, "CUSIP", "cusip")),
        filing_key=accession,
        filing_date=filing_date,
        accepted_at=first_present(submission, "ACCEPTANCE_DATETIME", "acceptedAt") or filing_date.isoformat(),
        shares=shares,
        market_value=market_value or 0.0,
        title_of_class=first_present(row, "TITLEOFCLASS", "titleOfClass"),
        share_type=first_present(row, "SSHPRNAMTTYPE", "sshPrnamtType"),
    )


def newer_holding(candidate: Holding, incumbent: Holding | None) -> bool:
    if incumbent is None:
        return True
    return (candidate.filing_date.isoformat(), candidate.accepted_at, candidate.filing_key) > (
        incumbent.filing_date.isoformat(),
        incumbent.accepted_at,
        incumbent.filing_key,
    )


def read_archives(
    *,
    cache_dir: Path,
    name_map: dict[str, str],
    history_start: date,
    asof: date,
    max_archives: int,
) -> tuple[dict[tuple[str, date, str, str], Holding], list[dict[str, Any]]]:
    archive_paths = sorted(cache_dir.glob("*.zip"))
    if max_archives > 0:
        archive_paths = archive_paths[:max_archives]
    latest_by_key: dict[tuple[str, date, str, str], Holding] = {}
    summary_rows: list[dict[str, Any]] = []
    for index, archive_path in enumerate(archive_paths, start=1):
        started = time.monotonic()
        infotable_rows = 0
        matched_rows = 0
        archive_tickers: set[str] = set()
        archive_periods: set[str] = set()
        with zipfile.ZipFile(archive_path) as zip_file:
            submissions = load_submission_map(zip_file)
            for row in iter_zip_table(zip_file, "INFOTABLE"):
                infotable_rows += 1
                if not is_common_share_row(row):
                    continue
                ticker = match_ticker(row, name_map=name_map)
                if not ticker:
                    continue
                submission = submissions.get(first_present(row, "ACCESSION_NUMBER", "accession_number"), {})
                holding = parse_holding(
                    row,
                    ticker=ticker,
                    submission=submission,
                    history_start=history_start,
                    asof=asof,
                )
                if holding is None:
                    continue
                matched_rows += 1
                archive_tickers.add(ticker)
                archive_periods.add(holding.period.isoformat())
                key = (holding.ticker, holding.period, holding.manager_key, holding.cusip)
                if newer_holding(holding, latest_by_key.get(key)):
                    latest_by_key[key] = holding
        elapsed = time.monotonic() - started
        summary = {
            "archive": archive_path.name,
            "infotable_rows": infotable_rows,
            "matched_common_share_rows": matched_rows,
            "candidate_latest_rows": len(latest_by_key),
            "unique_tickers": len(archive_tickers),
            "periods": ",".join(sorted(archive_periods)),
            "elapsed_sec": round(elapsed, 2),
        }
        summary_rows.append(summary)
        print(
            f"[{index}/{len(archive_paths)}] {archive_path.name} "
            f"matched_common_rows={matched_rows} tickers={len(archive_tickers)} "
            f"latest_rows={len(latest_by_key)} elapsed_sec={elapsed:.1f}",
            flush=True,
        )
    return latest_by_key, summary_rows


def aggregate_holdings(
    holdings: dict[tuple[str, date, str, str], Holding],
    *,
    company_by_ticker: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, date], list[Holding]] = defaultdict(list)
    for holding in holdings.values():
        if holding.ticker in company_by_ticker:
            grouped[(holding.ticker, holding.period)].append(holding)
    rows_by_ticker: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for (ticker, period), group in sorted(grouped.items(), key=lambda item: (item[0][0], item[0][1])):
        manager_count = len({holding.manager_key for holding in group})
        rows_by_ticker[ticker].append(
            {
                "accession_nodash": f"common_share_aggregate_{ticker}_{period.strftime('%Y%m%d')}",
                "report_date": period.isoformat(),
                "source_id": SOURCE_ID,
                "manager_cik": "",
                "manager_name": AGGREGATE_MANAGER_NAME,
                "ticker": ticker,
                "company_id": int(company_by_ticker[ticker]["company_id"]),
                "cusip": "",
                "shares": round(sum(holding.shares for holding in group), 4),
                "market_value_usd": round(sum(holding.market_value for holding in group), 4),
                "manager_count": float(manager_count),
                "institutional_ownership_pct": None,
                "institutional_ownership_delta_pct": 0.0,
                "put_call": "",
                "investment_discretion": "",
                "voting_authority_json": "",
                "payload_json": json.dumps(
                    {
                        "source": "sec_form_13f_data_sets_cached_common_share_rebuild",
                        "raw_latest_rows": len(group),
                        "security_filter": "SSHPRNAMTTYPE=SH;PUTCALL empty;exclude note/bond/preferred/warrant titles",
                    },
                    sort_keys=True,
                    ensure_ascii=True,
                ),
            }
        )
    out: list[dict[str, Any]] = []
    for ticker, rows in rows_by_ticker.items():
        prior_shares: float | None = None
        for row in sorted(rows, key=lambda item: item["report_date"]):
            shares = float(row["shares"] or 0.0)
            row["institutional_ownership_delta_pct"] = (
                (shares - prior_shares) / prior_shares if prior_shares and prior_shares > 0 else 0.0
            )
            prior_shares = shares
            out.append(row)
    return sorted(out, key=lambda row: (row["ticker"], row["report_date"]))


def ensure_source(conn: Any) -> None:
    now = utc_now()
    conn.execute(
        """
        INSERT INTO source_registry(
            source_id, stage, source_name, source_type, base_url,
            authentication_required, free_key_required, priority, status, created_at, updated_at
        )
        VALUES (?, 'stage_1', 'SEC Form 13F institutional holdings', 'sec_dataset_cache',
                'https://www.sec.gov/data-research/sec-markets-data/form-13f-data-sets',
                0, 0, 63, 'active', ?, ?)
        ON CONFLICT(source_id) DO UPDATE SET
            source_type = excluded.source_type,
            status = excluded.status,
            updated_at = excluded.updated_at
        """,
        (SOURCE_ID, now, now),
    )


def replace_facts(conn: Any, rows: list[dict[str, Any]], *, history_start: date, asof: date) -> int:
    now = utc_now()
    conn.execute(
        """
        DELETE FROM fact_sec_13f_holding
        WHERE source_id = ?
          AND report_date >= ?
          AND report_date <= ?
          AND (
              manager_name IN (?, ?)
              OR accession_nodash LIKE 'aggregate_%'
              OR accession_nodash LIKE 'common_share_aggregate_%'
          )
        """,
        (
            SOURCE_ID,
            history_start.isoformat(),
            asof.isoformat(),
            AGGREGATE_MANAGER_NAME,
            LEGACY_AGGREGATE_MANAGER_NAME,
        ),
    )
    conn.executemany(
        """
        INSERT INTO fact_sec_13f_holding(
            accession_nodash, report_date, source_id, manager_cik, manager_name, ticker,
            company_id, cusip, shares, market_value_usd, manager_count, institutional_ownership_pct,
            institutional_ownership_delta_pct, put_call, investment_discretion, voting_authority_json,
            payload_json, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT DO UPDATE SET
            shares = excluded.shares,
            market_value_usd = excluded.market_value_usd,
            manager_count = excluded.manager_count,
            institutional_ownership_pct = excluded.institutional_ownership_pct,
            institutional_ownership_delta_pct = excluded.institutional_ownership_delta_pct,
            investment_discretion = excluded.investment_discretion,
            voting_authority_json = excluded.voting_authority_json,
            payload_json = excluded.payload_json,
            updated_at = excluded.updated_at
        """,
        [
            (
                row["accession_nodash"],
                row["report_date"],
                row["source_id"],
                row["manager_cik"],
                row["manager_name"],
                row["ticker"],
                row["company_id"],
                row["cusip"],
                row["shares"],
                row["market_value_usd"],
                row["manager_count"],
                row["institutional_ownership_pct"],
                row["institutional_ownership_delta_pct"],
                row["put_call"],
                row["investment_discretion"],
                row["voting_authority_json"],
                row["payload_json"],
                now,
                now,
            )
            for row in rows
        ],
    )
    return len(rows)


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
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
            cfg_get(config, "market_positioning_update.sec_13f.cache_dir", "../output/market_positioning_cache/sec_13f"),
            base_dir=base_dir,
        )
    )
    output_csv = (
        args.output_csv.expanduser().resolve()
        if args.output_csv
        else resolve_path(cfg_get(config, "sec_13f_ingestion.output_csv"), base_dir=base_dir)
    )
    summary_csv = (
        args.summary_csv.expanduser().resolve()
        if args.summary_csv
        else resolve_path(
            cfg_get(
                config,
                "sec_13f_ingestion.common_share_rebuild_summary_csv",
                "../output/med_devices_reports/med_device_sec_13f_common_share_rebuild_summary.csv",
            ),
            base_dir=base_dir,
        )
    )
    history_start = parse_date(args.history_start or str(cfg_get(config, "external_positioning_import.history_start", "2019-01-01")))
    asof = parse_date(args.asof or datetime.now().date().isoformat())
    if history_start is None or asof is None:
        raise ValueError("history-start and asof must parse as dates")
    if not cache_dir.exists():
        raise FileNotFoundError(f"SEC 13F cache directory not found: {cache_dir}")
    name_map = load_name_map(tickers_csv)
    if not name_map:
        raise ValueError(f"No ticker/name mapping found in {tickers_csv}")
    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        init_db(conn)
        ensure_source(conn)
        company_by_ticker = load_company_map(conn)
        run_id = start_run(conn, run_type="rebuild_med_device_sec_13f_common_share_facts", input_path=cache_dir)
        try:
            latest_holdings, summary_rows = read_archives(
                cache_dir=cache_dir,
                name_map=name_map,
                history_start=history_start,
                asof=asof,
                max_archives=max(0, int(args.max_archives or 0)),
            )
            rows = aggregate_holdings(latest_holdings, company_by_ticker=company_by_ticker)
            expected_tickers = set(company_by_ticker)
            observed_tickers = {str(row["ticker"]) for row in rows}
            missing = sorted(expected_tickers - observed_tickers)
            if missing:
                raise RuntimeError(f"SEC 13F common-share rebuild missing {len(missing)} tickers: {missing}")
            count = replace_facts(conn, rows, history_start=history_start, asof=asof)
            write_csv(output_csv, rows, FIELDNAMES)
            write_csv(summary_csv, summary_rows, SUMMARY_FIELDNAMES)
            message = (
                f"rows={count} tickers={len(observed_tickers)} "
                f"periods={min(row['report_date'] for row in rows)}..{max(row['report_date'] for row in rows)}"
            )
            finish_run(conn, run_id=run_id, status="success", row_count=count, message=message)
            print(f"rebuilt_sec13f_common_share_rows={count} tickers={len(observed_tickers)} output={output_csv}")
            print(f"summary={summary_csv}")
        except BaseException as exc:
            finish_run(conn, run_id=run_id, status="failed", row_count=0, message=f"{type(exc).__name__}: {exc}")
            raise


if __name__ == "__main__":
    main()
