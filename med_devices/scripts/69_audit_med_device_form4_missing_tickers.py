#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from med_devices.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from med_devices.core.db import connect, init_db  # noqa: E402
from med_devices.core.logging_utils import configure_utc_logging  # noqa: E402
from med_devices.core.text_norm import normalize_ticker  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
FIELDNAMES = [
    "ticker",
    "company_id",
    "company_name",
    "cik",
    "exchange",
    "security_type",
    "country",
    "med_devices_form4_count",
    "med_devices_form4_min_date",
    "med_devices_form4_max_date",
    "canonical_count_by_ticker",
    "canonical_count_by_cik",
    "canonical_latest_date",
    "canonical_issuer_text_hits",
    "canonical_owner_text_hits",
    "source_ps_rows_by_ticker",
    "source_ps_rows_by_cik",
    "source_ps_min_date",
    "source_ps_max_date",
    "source_symbols_for_cik",
    "status",
    "recommended_next_step",
    "canonical_detail",
]


@dataclass(frozen=True)
class Summary:
    count: int = 0
    min_date: str = ""
    max_date: str = ""
    symbols: tuple[str, ...] = ()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit active med-device tickers missing imported SEC Form 4 rows.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--sec-form4-db", type=Path, default=None)
    parser.add_argument("--history-start", default="")
    parser.add_argument("--asof", default="")
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--include-covered", action="store_true", help="Include tickers that already have med-devices facts.")
    return parser.parse_args()


def parse_sec_date(raw: object) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d-%b-%y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return text


def cik_aliases(raw: object) -> set[str]:
    text = str(raw or "").strip()
    if not text:
        return set()
    out = {text}
    if text.isdigit():
        out.add(str(int(text)))
        out.add(text.zfill(10))
    return {value for value in out if value}


def cik_key(raw: object) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    return str(int(text)) if text.isdigit() else text


def qmarks(values: list[str]) -> str:
    return ",".join("?" for _ in values)


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name = ?", (table,)).fetchone())


def summarize_records(records: list[tuple[object, object]], *, start: str, asof: str) -> Summary:
    dates: list[str] = []
    symbols: set[str] = set()
    for raw_date, raw_symbol in records:
        parsed_date = parse_sec_date(raw_date)
        if not parsed_date or parsed_date < start or parsed_date > asof:
            continue
        dates.append(parsed_date)
        symbol = normalize_ticker(raw_symbol)
        if symbol:
            symbols.add(symbol)
    if not dates:
        return Summary()
    return Summary(count=len(dates), min_date=min(dates), max_date=max(dates), symbols=tuple(sorted(symbols)))


def med_devices_form4_summary(conn: Any, company_id: int, *, start: str, asof: str) -> Summary:
    row = conn.execute(
        """
        SELECT COUNT(*) AS row_count, MIN(transaction_date) AS min_date, MAX(transaction_date) AS max_date
        FROM fact_sec_form4_transaction
        WHERE company_id = ?
          AND source_id = 'sec_form4_edgar'
          AND COALESCE(transaction_date, '') >= ?
          AND COALESCE(transaction_date, '') <= ?
        """,
        (company_id, start, asof),
    ).fetchone()
    if not row or not int(row["row_count"] or 0):
        return Summary()
    return Summary(count=int(row["row_count"] or 0), min_date=str(row["min_date"] or ""), max_date=str(row["max_date"] or ""))


def med_devices_form4_summary_map(conn: Any, *, start: str, asof: str) -> dict[int, Summary]:
    rows = conn.execute(
        """
        SELECT
            company_id,
            COUNT(*) AS row_count,
            MIN(transaction_date) AS min_date,
            MAX(transaction_date) AS max_date
        FROM fact_sec_form4_transaction
        WHERE source_id = 'sec_form4_edgar'
          AND COALESCE(transaction_date, '') >= ?
          AND COALESCE(transaction_date, '') <= ?
        GROUP BY company_id
        """,
        (start, asof),
    ).fetchall()
    return {
        int(row["company_id"]): Summary(
            count=int(row["row_count"] or 0),
            min_date=str(row["min_date"] or ""),
            max_date=str(row["max_date"] or ""),
        )
        for row in rows
        if row["company_id"] is not None
    }


def source_ps_summary(
    form4_conn: sqlite3.Connection,
    *,
    ticker: str,
    ciks: set[str],
    start: str,
    asof: str,
    match_on: str,
) -> Summary:
    if not table_exists(form4_conn, "sec_ownership_submission") or not table_exists(form4_conn, "sec_ownership_nonderiv_trans"):
        return Summary()
    params: list[str] = []
    if match_on == "ticker":
        where = "UPPER(COALESCE(s.issuer_trading_symbol, '')) = ?"
        params.append(ticker)
    else:
        if not ciks:
            return Summary()
        where = f"CAST(s.issuer_cik AS TEXT) IN ({qmarks(sorted(ciks))})"
        params.extend(sorted(ciks))
    rows = form4_conn.execute(
        f"""
        SELECT t.transaction_date, s.issuer_trading_symbol
        FROM sec_ownership_submission s
        JOIN sec_ownership_nonderiv_trans t ON t.accession_number = s.accession_number
        WHERE {where}
          AND UPPER(COALESCE(t.transaction_code, '')) IN ('P', 'S')
        """,
        params,
    ).fetchall()
    return summarize_records([(row["transaction_date"], row["issuer_trading_symbol"]) for row in rows], start=start, asof=asof)


def legacy_table_summary(
    form4_conn: sqlite3.Connection,
    *,
    table: str,
    date_col: str,
    ticker_col: str,
    cik_col: str,
    ticker: str,
    ciks: set[str],
    start: str,
    asof: str,
    match_on: str,
) -> Summary:
    if not table_exists(form4_conn, table):
        return Summary()
    params: list[str] = []
    if match_on == "ticker":
        where = f"UPPER(COALESCE({ticker_col}, '')) = ?"
        params.append(ticker)
    else:
        if not ciks:
            return Summary()
        where = f"CAST({cik_col} AS TEXT) IN ({qmarks(sorted(ciks))})"
        params.extend(sorted(ciks))
    rows = form4_conn.execute(
        f"""
        SELECT {date_col} AS event_date, {ticker_col} AS symbol
        FROM {table}
        WHERE {where}
        """,
        params,
    ).fetchall()
    return summarize_records([(row["event_date"], row["symbol"]) for row in rows], start=start, asof=asof)


def merge_summaries(summaries: list[Summary]) -> Summary:
    count = sum(summary.count for summary in summaries)
    dates = [value for summary in summaries for value in (summary.min_date, summary.max_date) if value]
    symbols = sorted({symbol for summary in summaries for symbol in summary.symbols})
    return Summary(count=count, min_date=min(dates) if dates else "", max_date=max(dates) if dates else "", symbols=tuple(symbols))


def add_summary_record(
    bucket: dict[str, list[tuple[str, str]]],
    key: str,
    raw_date: object,
    raw_symbol: object,
    *,
    start: str,
    asof: str,
) -> None:
    if not key:
        return
    parsed_date = parse_sec_date(raw_date)
    if not parsed_date or parsed_date < start or parsed_date > asof:
        return
    bucket.setdefault(key, []).append((parsed_date, normalize_ticker(raw_symbol)))


def summary_map_from_bucket(bucket: dict[str, list[tuple[str, str]]]) -> dict[str, Summary]:
    out: dict[str, Summary] = {}
    for key, records in bucket.items():
        dates = [record[0] for record in records]
        symbols = sorted({record[1] for record in records if record[1]})
        out[key] = Summary(count=len(records), min_date=min(dates), max_date=max(dates), symbols=tuple(symbols))
    return out


def summary_for_aliases(summary_map: dict[str, Summary], aliases: set[str]) -> Summary:
    return merge_summaries([summary_map[alias] for alias in aliases if alias in summary_map])


def source_ps_summary_maps(
    form4_conn: sqlite3.Connection,
    *,
    tickers: set[str],
    ciks: set[str],
    start: str,
    asof: str,
) -> tuple[dict[str, Summary], dict[str, Summary]]:
    if not table_exists(form4_conn, "sec_ownership_submission") or not table_exists(form4_conn, "sec_ownership_nonderiv_trans"):
        return {}, {}
    clauses: list[str] = []
    params: list[str] = []
    if tickers:
        clauses.append(f"UPPER(COALESCE(s.issuer_trading_symbol, '')) IN ({qmarks(sorted(tickers))})")
        params.extend(sorted(tickers))
    if ciks:
        clauses.append(f"CAST(s.issuer_cik AS TEXT) IN ({qmarks(sorted(ciks))})")
        params.extend(sorted(ciks))
    if not clauses:
        return {}, {}
    rows = form4_conn.execute(
        f"""
        SELECT s.issuer_cik, s.issuer_trading_symbol, t.transaction_date
        FROM sec_ownership_submission s
        JOIN sec_ownership_nonderiv_trans t ON t.accession_number = s.accession_number
        WHERE ({" OR ".join(clauses)})
          AND UPPER(COALESCE(t.transaction_code, '')) IN ('P', 'S')
        """,
        params,
    ).fetchall()
    by_ticker_bucket: dict[str, list[tuple[str, str]]] = {}
    by_cik_bucket: dict[str, list[tuple[str, str]]] = {}
    allowed_cik_keys = {cik_key(value) for value in ciks if cik_key(value)}
    for row in rows:
        symbol = normalize_ticker(row["issuer_trading_symbol"])
        if symbol in tickers:
            add_summary_record(by_ticker_bucket, symbol, row["transaction_date"], symbol, start=start, asof=asof)
        key = cik_key(row["issuer_cik"])
        if key in allowed_cik_keys:
            add_summary_record(by_cik_bucket, key, row["transaction_date"], symbol, start=start, asof=asof)
    return summary_map_from_bucket(by_ticker_bucket), summary_map_from_bucket(by_cik_bucket)


def legacy_summary_maps(
    form4_conn: sqlite3.Connection,
    *,
    table: str,
    date_col: str,
    ticker_col: str,
    cik_col: str,
    tickers: set[str],
    ciks: set[str],
    start: str,
    asof: str,
) -> tuple[dict[str, Summary], dict[str, Summary]]:
    if not table_exists(form4_conn, table):
        return {}, {}
    clauses: list[str] = []
    params: list[str] = []
    if tickers:
        clauses.append(f"UPPER(COALESCE({ticker_col}, '')) IN ({qmarks(sorted(tickers))})")
        params.extend(sorted(tickers))
    if ciks:
        clauses.append(f"CAST({cik_col} AS TEXT) IN ({qmarks(sorted(ciks))})")
        params.extend(sorted(ciks))
    if not clauses:
        return {}, {}
    rows = form4_conn.execute(
        f"""
        SELECT {date_col} AS event_date, {ticker_col} AS symbol, {cik_col} AS issuer_cik
        FROM {table}
        WHERE ({" OR ".join(clauses)})
        """,
        params,
    ).fetchall()
    by_ticker_bucket: dict[str, list[tuple[str, str]]] = {}
    by_cik_bucket: dict[str, list[tuple[str, str]]] = {}
    allowed_cik_keys = {cik_key(value) for value in ciks if cik_key(value)}
    for row in rows:
        symbol = normalize_ticker(row["symbol"])
        if symbol in tickers:
            add_summary_record(by_ticker_bucket, symbol, row["event_date"], symbol, start=start, asof=asof)
        key = cik_key(row["issuer_cik"])
        if key in allowed_cik_keys:
            add_summary_record(by_cik_bucket, key, row["event_date"], symbol, start=start, asof=asof)
    return summary_map_from_bucket(by_ticker_bucket), summary_map_from_bucket(by_cik_bucket)


def canonical_summaries(
    form4_conn: sqlite3.Connection,
    *,
    ticker: str,
    ciks: set[str],
    start: str,
    asof: str,
) -> tuple[Summary, Summary, str]:
    specs = [
        ("form4_events_tier1", "trans_date", "issuer_trading_symbol", "issuer_cik"),
        ("form4_buy_events_v1", "trans_date", "issuer_trading_symbol", "issuer_cik"),
        ("stock_signal_snapshot_tier1", "as_of_date", "issuer_trading_symbol", "issuer_cik"),
    ]
    ticker_summaries: list[Summary] = []
    cik_summaries: list[Summary] = []
    details: list[str] = []
    for table, date_col, ticker_col, cik_col in specs:
        ticker_summary = legacy_table_summary(
            form4_conn,
            table=table,
            date_col=date_col,
            ticker_col=ticker_col,
            cik_col=cik_col,
            ticker=ticker,
            ciks=ciks,
            start=start,
            asof=asof,
            match_on="ticker",
        )
        cik_summary = legacy_table_summary(
            form4_conn,
            table=table,
            date_col=date_col,
            ticker_col=ticker_col,
            cik_col=cik_col,
            ticker=ticker,
            ciks=ciks,
            start=start,
            asof=asof,
            match_on="cik",
        )
        ticker_summaries.append(ticker_summary)
        cik_summaries.append(cik_summary)
        details.append(
            f"{table}[ticker={ticker_summary.count}:{ticker_summary.min_date}:{ticker_summary.max_date};"
            f"cik={cik_summary.count}:{cik_summary.min_date}:{cik_summary.max_date}]"
        )
    return merge_summaries(ticker_summaries), merge_summaries(cik_summaries), " | ".join(details)


def active_companies(conn: Any) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            c.company_id,
            c.ticker,
            c.company_name,
            c.cik,
            c.exchange,
            c.country,
            COALESCE(s.security_type, '') AS security_type
        FROM dim_company c
        LEFT JOIN dim_security s
          ON s.company_id = c.company_id
         AND s.is_primary_listing = 1
        WHERE c.is_active = 1
        ORDER BY UPPER(c.ticker)
        """
    ).fetchall()
    return [dict(row) for row in rows]


def status_for(
    *,
    med_summary: Summary,
    source_by_ticker: Summary,
    source_by_cik: Summary,
    canonical_by_ticker: Summary,
    canonical_by_cik: Summary,
) -> tuple[str, str]:
    if med_summary.count:
        return "covered", "No action; med-devices fact_sec_form4_transaction has imported P/S rows."
    if source_by_ticker.count:
        return "source_has_ticker_rows_import_gap", "Review med-devices Form 4 importer; source has matching P/S rows by ticker."
    if source_by_cik.count:
        return "source_has_cik_rows_ticker_mismatch", "CIK fallback should import these rows; review source symbol and importer matching."
    if canonical_by_ticker.count or canonical_by_cik.count:
        return (
            "snapshot_only_or_source_filter_gap",
            "Canonical snapshots have evidence, but source event tables queried by importer do not have matching P/S rows in window.",
        )
    return "no_canonical_rows_for_company", "Check SEC Form 4 applicability and CIK coverage; likely no source rows in canonical DB."


def output_path_from_config(config: dict[str, Any], base_dir: Path, *, asof: str) -> Path:
    template = str(
        cfg_get(
            config,
            "form4_missing_ticker_audit.output_csv_template",
            "../output/med_devices_reports/med_device_form4_missing_ticker_audit_{asof}.csv",
        )
    )
    return resolve_path(template.format(asof=asof), base_dir=base_dir)


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    asof = args.asof.strip() or date.today().isoformat()
    start = args.history_start.strip() or str(cfg_get(config, "external_positioning_import.history_start", "2019-01-01"))
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    form4_db = (
        args.sec_form4_db.expanduser().resolve()
        if args.sec_form4_db
        else resolve_path(cfg_get(config, "external_positioning_import.sec_form4_db_path"), base_dir=base_dir)
    )
    output_csv = args.output_csv.expanduser().resolve() if args.output_csv else output_path_from_config(config, base_dir, asof=asof)
    if not form4_db.exists():
        raise FileNotFoundError(f"SEC Form 4 DB not found: {form4_db}")

    rows: list[dict[str, Any]] = []
    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        init_db(conn)
        companies = active_companies(conn)
        form4_conn = sqlite3.connect(str(form4_db))
        form4_conn.row_factory = sqlite3.Row
        try:
            med_summary_by_company = med_devices_form4_summary_map(conn, start=start, asof=asof)
            active_tickers = {normalize_ticker(company["ticker"]) for company in companies}
            active_ciks: set[str] = set()
            company_ciks: dict[int, set[str]] = {}
            company_cik_keys: dict[int, set[str]] = {}
            for company in companies:
                aliases = cik_aliases(company.get("cik"))
                company_ciks[int(company["company_id"])] = aliases
                company_cik_keys[int(company["company_id"])] = {cik_key(value) for value in aliases if cik_key(value)}
                active_ciks.update(aliases)
            source_ticker_map, source_cik_map = source_ps_summary_maps(
                form4_conn,
                tickers=active_tickers,
                ciks=active_ciks,
                start=start,
                asof=asof,
            )
            legacy_specs = [
                ("form4_events_tier1", "trans_date", "issuer_trading_symbol", "issuer_cik"),
                ("form4_buy_events_v1", "trans_date", "issuer_trading_symbol", "issuer_cik"),
                ("stock_signal_snapshot_tier1", "as_of_date", "issuer_trading_symbol", "issuer_cik"),
            ]
            legacy_maps: list[tuple[str, dict[str, Summary], dict[str, Summary]]] = []
            for table, date_col, ticker_col, cik_col in legacy_specs:
                ticker_map, cik_map = legacy_summary_maps(
                    form4_conn,
                    table=table,
                    date_col=date_col,
                    ticker_col=ticker_col,
                    cik_col=cik_col,
                    tickers=active_tickers,
                    ciks=active_ciks,
                    start=start,
                    asof=asof,
                )
                legacy_maps.append((table, ticker_map, cik_map))
            for company in companies:
                ticker = normalize_ticker(company["ticker"])
                company_id = int(company["company_id"])
                cik_keys = company_cik_keys.get(company_id, set())
                med_summary = med_summary_by_company.get(company_id, Summary())
                source_by_ticker = source_ticker_map.get(ticker, Summary())
                source_by_cik = summary_for_aliases(source_cik_map, cik_keys)
                canonical_ticker_summaries: list[Summary] = []
                canonical_cik_summaries: list[Summary] = []
                detail_parts: list[str] = []
                for table, ticker_map, cik_map in legacy_maps:
                    ticker_summary = ticker_map.get(ticker, Summary())
                    cik_summary = summary_for_aliases(cik_map, cik_keys)
                    canonical_ticker_summaries.append(ticker_summary)
                    canonical_cik_summaries.append(cik_summary)
                    detail_parts.append(
                        f"{table}[ticker={ticker_summary.count}:{ticker_summary.min_date}:{ticker_summary.max_date};"
                        f"cik={cik_summary.count}:{cik_summary.min_date}:{cik_summary.max_date}]"
                    )
                canonical_by_ticker = merge_summaries(canonical_ticker_summaries)
                canonical_by_cik = merge_summaries(canonical_cik_summaries)
                canonical_detail = " | ".join(detail_parts)
                status, next_step = status_for(
                    med_summary=med_summary,
                    source_by_ticker=source_by_ticker,
                    source_by_cik=source_by_cik,
                    canonical_by_ticker=canonical_by_ticker,
                    canonical_by_cik=canonical_by_cik,
                )
                if status == "covered" and not args.include_covered:
                    continue
                canonical_latest_dates = [
                    value for value in (canonical_by_ticker.max_date, canonical_by_cik.max_date) if value
                ]
                source_dates = [value for value in (source_by_ticker.min_date, source_by_ticker.max_date, source_by_cik.min_date, source_by_cik.max_date) if value]
                rows.append(
                    {
                        "ticker": ticker,
                        "company_id": company_id,
                        "company_name": str(company.get("company_name") or ""),
                        "cik": str(company.get("cik") or ""),
                        "exchange": str(company.get("exchange") or ""),
                        "security_type": str(company.get("security_type") or ""),
                        "country": str(company.get("country") or ""),
                        "med_devices_form4_count": med_summary.count,
                        "med_devices_form4_min_date": med_summary.min_date,
                        "med_devices_form4_max_date": med_summary.max_date,
                        "canonical_count_by_ticker": canonical_by_ticker.count,
                        "canonical_count_by_cik": canonical_by_cik.count,
                        "canonical_latest_date": max(canonical_latest_dates) if canonical_latest_dates else "",
                        "canonical_issuer_text_hits": 0,
                        "canonical_owner_text_hits": 0,
                        "source_ps_rows_by_ticker": source_by_ticker.count,
                        "source_ps_rows_by_cik": source_by_cik.count,
                        "source_ps_min_date": min(source_dates) if source_dates else "",
                        "source_ps_max_date": max(source_dates) if source_dates else "",
                        "source_symbols_for_cik": " ".join(source_by_cik.symbols),
                        "status": status,
                        "recommended_next_step": next_step,
                        "canonical_detail": canonical_detail,
                    }
                )
        finally:
            form4_conn.close()
    write_csv(output_csv, rows)
    print(f"form4_missing_ticker_audit={output_csv} rows={len(rows)} asof={asof}")


if __name__ == "__main__":
    main()
