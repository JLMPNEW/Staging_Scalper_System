#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from industrials.core.db import connect, init_db, utc_now  # noqa: E402
from industrials.core.reports import write_csv_atomic  # noqa: E402
from industrials.machinery.build_contract import (  # noqa: E402
    DISCLOSURE_PARSER_VERSION,
)
from industrials.machinery.disclosure_candidates import (  # noqa: E402
    accepted_date,
    extract_machinery_prose_candidates,
    is_known_by_asof,
    reapply_reviewed_disclosure_policies,
    reconcile_machinery_disclosure_facts,
    replace_document_candidates_and_facts,
    resolve_machinery_disclosure_candidates,
)
from industrials.machinery.disclosure_documents import (  # noqa: E402
    extract_document_text,
    filing_summary_report_documents,
)
from industrials.machinery.financial_contract import required_metric_names  # noqa: E402
from industrials.machinery.reporting_currency import resolve_reporting_currency  # noqa: E402
from industrials.machinery.scoring import parse_asof  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
OUTPUT_FIELDS = [
    "priority_rank",
    "ticker",
    "metric_name",
    "availability_status",
    "status_reason",
    "applicable_metric_count",
    "covered_metric_count",
    "missing_metric_count",
    "candidate_count",
    "best_candidate_status",
    "candidate_value",
    "unit",
    "period_start",
    "period_end",
    "scope",
    "confidence",
    "accession_number",
    "form_type",
    "filing_date",
    "accepted_at",
    "document_name",
    "extraction_method",
    "candidate_reason",
    "evidence_text",
]
REVIEW_OUTPUT_FIELDS = [
    "review_rank",
    "ticker",
    "metric_name",
    "candidate_value",
    "unit",
    "period_start",
    "period_end",
    "scope",
    "confidence",
    "accession_number",
    "form_type",
    "filing_date",
    "accepted_at",
    "document_name",
    "extraction_method",
    "status_reason",
    "evidence_text",
]
DOCUMENT_SUFFIXES = frozenset({".htm", ".html", ".xhtml", ".pdf"})
EXCLUDED_DOCUMENT_MARKERS = ("-index", "_lab.", "_pre.", "_def.", "_cal.")
SCAN_LEDGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS fact_machinery_disclosure_cache_scan (
    ticker TEXT NOT NULL,
    asof_date TEXT NOT NULL,
    scan_start_date TEXT NOT NULL DEFAULT '',
    max_filings_per_ticker INTEGER NOT NULL,
    parser_version TEXT NOT NULL,
    filing_count INTEGER NOT NULL,
    document_count INTEGER NOT NULL,
    candidate_count INTEGER NOT NULL,
    promoted_raw_count INTEGER NOT NULL,
    promoted_mapped_count INTEGER NOT NULL,
    completed_at TEXT NOT NULL,
    PRIMARY KEY(
        ticker, asof_date, scan_start_date,
        max_filings_per_ticker, parser_version
    )
);
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit cached SEC prose candidates for missing machinery metrics."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--output-csv", type=Path, default=None)
    parser.add_argument("--review-output-csv", type=Path, default=None)
    parser.add_argument("--tickers", default="")
    parser.add_argument(
        "--include-historical",
        action="store_true",
        help="Include machinery members whose point-in-time membership ended before the as-of date.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=40,
        help="Select the active tickers with the fewest missing applicable metrics; 0 means all.",
    )
    parser.add_argument(
        "--scan-cache",
        action="store_true",
        help="Rescan cached filing documents and persist disclosure candidates before reporting.",
    )
    parser.add_argument(
        "--max-filings-per-ticker",
        type=int,
        default=12,
        help="Maximum newest accepted filings scanned per ticker; 0 enables an explicit full-history bootstrap.",
    )
    parser.add_argument(
        "--scan-start-date",
        default="",
        help="Optional inclusive SEC acceptance-date lower bound for cache scans.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Skip tickers already completed for the same scan bounds and parser version.",
    )
    return parser.parse_args()


def ensure_scan_ledger_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCAN_LEDGER_SCHEMA)


def completed_scan_tickers(
    conn: sqlite3.Connection,
    *,
    asof: str,
    scan_start_date: str,
    max_filings_per_ticker: int,
) -> set[str]:
    ensure_scan_ledger_schema(conn)
    return {
        str(row[0])
        for row in conn.execute(
            """
            SELECT ticker
            FROM fact_machinery_disclosure_cache_scan
            WHERE asof_date = ? AND scan_start_date = ?
              AND max_filings_per_ticker = ? AND parser_version = ?
            """,
            (
                asof,
                scan_start_date,
                max_filings_per_ticker,
                DISCLOSURE_PARSER_VERSION,
            ),
        )
    }


def active_members(
    conn: sqlite3.Connection,
    *,
    asof: str,
    include_historical: bool = False,
) -> dict[str, dict[str, str]]:
    end_date_clause = "" if include_historical else "AND COALESCE(m.end_date, '9999-12-31') >= ?"
    params = (asof,) if include_historical else (asof, asof)
    rows = conn.execute(
        f"""
        SELECT DISTINCT m.ticker, c.cik, c.currency
        FROM dim_universe_membership m
        JOIN dim_company c ON c.ticker = m.ticker
        WHERE m.model_family = 'machinery'
          AND m.start_date <= ?
          {end_date_clause}
        """,
        params,
    ).fetchall()
    output: dict[str, dict[str, str]] = {}
    for row in rows:
        ticker = str(row["ticker"])
        output[ticker] = {
            "cik": str(row["cik"] or ""),
            "currency": resolve_reporting_currency(
                conn,
                ticker=ticker,
                model_family="machinery",
                asof=asof,
                fallback=str(row["currency"] or "USD"),
            ),
        }
    return output


def ticker_priorities(
    conn: sqlite3.Connection,
    *,
    asof: str,
    members: dict[str, dict[str, str]],
) -> list[dict[str, Any]]:
    metrics = set(required_metric_names())
    rows = conn.execute(
        """
        SELECT ticker, metric_name, availability_status
        FROM feature_financial_metric_availability
        WHERE model_family = 'machinery'
          AND asof_date = (
              SELECT MAX(asof_date)
              FROM feature_financial_metric_availability
              WHERE model_family = 'machinery'
                AND asof_date <= ?
          )
        """,
        (asof,),
    ).fetchall()
    by_ticker: dict[str, dict[str, str]] = {ticker: {} for ticker in members}
    for row in rows:
        ticker = str(row["ticker"])
        metric = str(row["metric_name"])
        if ticker in by_ticker and metric in metrics:
            by_ticker[ticker][metric] = str(row["availability_status"])
    output: list[dict[str, Any]] = []
    for ticker, statuses in by_ticker.items():
        applicable = {
            metric: status
            for metric, status in statuses.items()
            if status not in {"EXEMPT", "NOT_APPLICABLE"}
        }
        covered = sum(status in {"REPORTED", "PROXY"} for status in applicable.values())
        missing = sum(
            status in {"NOT_DISCLOSED", "PARSER_FAILURE", "DISCLOSED_UNPARSED"}
            for status in applicable.values()
        )
        output.append(
            {
                "ticker": ticker,
                "applicable_metric_count": len(applicable),
                "covered_metric_count": covered,
                "missing_metric_count": missing,
            }
        )
    return sorted(
        output,
        key=lambda row: (
            int(row["missing_metric_count"]),
            -int(row["covered_metric_count"]),
            str(row["ticker"]),
        ),
    )


def accepted_filing_rows(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    asof: str,
    source_id: str,
    max_filings: int,
    scan_start_date: str = "",
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT accession_number, form_type, filing_date, accepted_at,
               report_date, fiscal_year, fiscal_period, primary_document
        FROM fact_sec_filing
        WHERE ticker = ? AND source_id = ?
        ORDER BY filing_date DESC, accession_number DESC
        """,
        (ticker, source_id),
    ).fetchall()
    accepted = [
        dict(row)
        for row in rows
        if is_known_by_asof(dict(row), asof)
        and (
            not scan_start_date
            or accepted_date(str(row["accepted_at"] or ""), str(row["filing_date"] or ""))
            >= scan_start_date
        )
    ]
    return accepted[:max_filings] if max_filings > 0 else accepted


def cached_document_names(accession_dir: Path, filing: dict[str, Any]) -> list[str]:
    names: set[str] = set()
    index_path = accession_dir / "index.json"
    if index_path.exists():
        try:
            payload = json.loads(index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        for item in ((payload.get("directory") or {}).get("item") or []):
            if isinstance(item, dict):
                names.add(str(item.get("name") or ""))
    names.update(path.name for path in accession_dir.iterdir() if path.is_file())
    primary = str(filing.get("primary_document") or "")
    form_type = str(filing.get("form_type") or "").strip().upper()
    event_filing = form_type in {"8-K", "8-K/A", "6-K", "6-K/A"}
    targeted_reports: set[str] = set()
    summary_path = accession_dir / "FilingSummary.xml"
    if not event_filing and summary_path.exists():
        try:
            targeted_reports = filing_summary_report_documents(
                summary_path.read_text(encoding="utf-8", errors="replace")
            )
        except OSError:
            targeted_reports = set()

    def is_relevant(name: str) -> bool:
        if name == primary or name in targeted_reports:
            return True
        lower = name.lower()
        if not event_filing:
            return False
        return lower.endswith(".pdf") or bool(
            re.search(r"(?:ex(?:hibit)?[-_ ]?99|earnings|presentation|release)", lower)
        )

    candidates = [
        name
        for name in names
        if Path(name).suffix.lower() in DOCUMENT_SUFFIXES
        and not any(marker in name.lower() for marker in EXCLUDED_DOCUMENT_MARKERS)
        and is_relevant(name)
    ]
    return sorted(
        candidates,
        key=lambda name: (
            0 if name == primary else 1 if "ex99" in name.lower() or "exhibit99" in name.lower() else 2,
            name.lower(),
        ),
    )


def scan_cached_candidates(
    conn: sqlite3.Connection,
    *,
    tickers: list[str],
    members: dict[str, dict[str, str]],
    asof: str,
    cache_dir: Path,
    source_id: str,
    filings_source_id: str,
    max_filings_per_ticker: int,
    scan_start_date: str = "",
    resume: bool = False,
    pdf_ocr_enabled: bool = False,
    max_pdf_pages: int = 250,
    max_pdf_bytes: int = 25_000_000,
    pdf_extraction_timeout_sec: float = 30.0,
) -> tuple[int, int, int, int]:
    ensure_scan_ledger_schema(conn)
    completed = (
        completed_scan_tickers(
            conn,
            asof=asof,
            scan_start_date=scan_start_date,
            max_filings_per_ticker=max_filings_per_ticker,
        )
        if resume
        else set()
    )
    inserted = 0
    promoted_raw = 0
    promoted_mapped = 0
    skipped = 0
    now = utc_now()
    processed = 0
    for ticker in tickers:
        if ticker in completed:
            skipped += 1
            continue
        cik = members[ticker]["cik"]
        currency = members[ticker]["currency"]
        cik_dir = cache_dir / "sec_archive_xbrl" / f"CIK{cik}"
        if not cik or not cik_dir.exists():
            continue
        staged: list[tuple[dict[str, Any], str, list[Any]]] = []
        filings = accepted_filing_rows(
            conn,
            ticker=ticker,
            asof=asof,
            source_id=filings_source_id,
            max_filings=max_filings_per_ticker,
            scan_start_date=scan_start_date,
        )
        for filing in filings:
            accession = str(filing.get("accession_number") or "").replace("-", "")
            accession_dir = cik_dir / accession
            if not accession_dir.exists():
                continue
            for document_name in cached_document_names(accession_dir, filing):
                path = accession_dir / document_name
                try:
                    extracted = extract_document_text(
                        path.read_bytes(),
                        document_name=document_name,
                        enable_pdf_ocr=pdf_ocr_enabled,
                        max_pdf_pages=max_pdf_pages,
                        max_pdf_bytes=max_pdf_bytes,
                        pdf_extraction_timeout_sec=pdf_extraction_timeout_sec,
                    )
                except OSError:
                    continue
                document_text = extracted.text
                if not document_text.strip():
                    continue
                candidates = extract_machinery_prose_candidates(
                    document_text,
                    filing=filing,
                    company_currency=currency,
                )
                candidates = resolve_machinery_disclosure_candidates(
                    candidates,
                    ticker=ticker,
                    filing=filing,
                )
                staged.append((filing, document_name, candidates))
        ticker_inserted = 0
        ticker_promoted_raw = 0
        ticker_promoted_mapped = 0
        with conn:
            for filing, document_name, candidates in staged:
                candidate_count, raw_count, mapped_count = replace_document_candidates_and_facts(
                    conn,
                    ticker=ticker,
                    cik=cik,
                    source_id=source_id,
                    model_family="machinery",
                    filing=filing,
                    document_name=document_name,
                    candidates=candidates,
                    now=now,
                )
                ticker_inserted += candidate_count
                ticker_promoted_raw += raw_count
                ticker_promoted_mapped += mapped_count
            reconciliation = reconcile_machinery_disclosure_facts(
                conn,
                ticker=ticker,
                source_id=source_id,
                model_family="machinery",
                now=now,
            )
            ticker_promoted_raw -= reconciliation["raw_facts_deleted"]
            ticker_promoted_mapped -= reconciliation["mapped_facts_deleted"]
            conn.execute(
                """
                INSERT INTO fact_machinery_disclosure_cache_scan(
                    ticker, asof_date, scan_start_date, max_filings_per_ticker,
                    parser_version, filing_count, document_count, candidate_count,
                    promoted_raw_count, promoted_mapped_count, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(
                    ticker, asof_date, scan_start_date,
                    max_filings_per_ticker, parser_version
                ) DO UPDATE SET
                    filing_count = excluded.filing_count,
                    document_count = excluded.document_count,
                    candidate_count = excluded.candidate_count,
                    promoted_raw_count = excluded.promoted_raw_count,
                    promoted_mapped_count = excluded.promoted_mapped_count,
                    completed_at = excluded.completed_at
                """,
                (
                    ticker,
                    asof,
                    scan_start_date,
                    max_filings_per_ticker,
                    DISCLOSURE_PARSER_VERSION,
                    len(filings),
                    len(staged),
                    ticker_inserted,
                    ticker_promoted_raw,
                    ticker_promoted_mapped,
                    utc_now(),
                ),
            )
        inserted += ticker_inserted
        promoted_raw += ticker_promoted_raw
        promoted_mapped += ticker_promoted_mapped
        processed += 1
        if processed % 10 == 0 or processed + skipped == len(tickers):
            print(
                f"Cache scan progress: processed={processed} skipped={skipped} "
                f"requested={len(tickers)}",
                flush=True,
            )
    return inserted, promoted_raw, promoted_mapped, skipped


def audit_rows(
    conn: sqlite3.Connection,
    *,
    asof: str,
    priorities: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for priority_rank, priority in enumerate(priorities, start=1):
        ticker = str(priority["ticker"])
        availability_rows = conn.execute(
            """
            SELECT metric_name, availability_status, status_reason
            FROM feature_financial_metric_availability
            WHERE ticker = ? AND model_family = 'machinery' AND asof_date = ?
              AND availability_status IN (
                    'NOT_DISCLOSED', 'PARSER_FAILURE', 'DISCLOSED_UNPARSED'
              )
            ORDER BY metric_name
            """,
            (ticker, asof),
        ).fetchall()
        for availability in availability_rows:
            metric_name = str(availability["metric_name"])
            source_metric = {
                "orders_yoy_growth": "orders",
                "book_to_bill": "orders",
                "backlog_yoy_growth": "funded_backlog",
                "backlog_to_revenue": "funded_backlog",
                "reported_backlog_yoy_growth": "reported_backlog",
                "reported_backlog_to_revenue": "reported_backlog",
                "rpo_current": "remaining_performance_obligation",
                "rpo_yoy_growth": "remaining_performance_obligation",
                "rpo_to_revenue": "remaining_performance_obligation",
                "rpo_implied_orders": "remaining_performance_obligation",
                "rpo_implied_book_to_bill": "remaining_performance_obligation",
            }.get(metric_name, metric_name)
            candidates = conn.execute(
                """
                SELECT *
                FROM fact_sec_metric_disclosure_candidate
                WHERE ticker = ? AND model_family = 'machinery' AND metric_name = ?
                  AND CASE
                        WHEN COALESCE(accepted_at, '') GLOB '????-??-??*'
                            THEN SUBSTR(accepted_at, 1, 10)
                        ELSE COALESCE(NULLIF(filing_date, ''), '9999-12-31')
                      END <= ?
                ORDER BY candidate_status = 'ACCEPTED' DESC,
                         confidence DESC, period_end DESC, accession_number DESC
                """,
                (ticker, source_metric, asof),
            ).fetchall()
            best = dict(candidates[0]) if candidates else {}
            output.append(
                {
                    "priority_rank": priority_rank,
                    "ticker": ticker,
                    "metric_name": metric_name,
                    "availability_status": str(availability["availability_status"]),
                    "status_reason": str(availability["status_reason"] or ""),
                    "applicable_metric_count": priority["applicable_metric_count"],
                    "covered_metric_count": priority["covered_metric_count"],
                    "missing_metric_count": priority["missing_metric_count"],
                    "candidate_count": len(candidates),
                    "best_candidate_status": best.get("candidate_status", ""),
                    "candidate_value": best.get("candidate_value", ""),
                    "unit": best.get("unit", ""),
                    "period_start": best.get("period_start", ""),
                    "period_end": best.get("period_end", ""),
                    "scope": best.get("scope", ""),
                    "confidence": best.get("confidence", ""),
                    "accession_number": best.get("accession_number", ""),
                    "form_type": best.get("form_type", ""),
                    "filing_date": best.get("filing_date", ""),
                    "accepted_at": accepted_date(
                        str(best.get("accepted_at", "")),
                        str(best.get("filing_date", "")),
                    ),
                    "document_name": best.get("document_name", ""),
                    "extraction_method": best.get("extraction_method", ""),
                    "candidate_reason": best.get("status_reason", ""),
                    "evidence_text": best.get("evidence_text", ""),
                }
            )
    return output


def review_required_rows(conn: sqlite3.Connection, *, asof: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT ticker, metric_name, candidate_value, unit, period_start, period_end,
               scope, confidence, accession_number, form_type, filing_date,
               accepted_at, document_name, extraction_method, status_reason,
               evidence_text
        FROM fact_sec_metric_disclosure_candidate
        WHERE model_family = 'machinery'
          AND candidate_status = 'REVIEW_REQUIRED'
          AND CASE
                WHEN COALESCE(accepted_at, '') GLOB '????-??-??*'
                    THEN SUBSTR(accepted_at, 1, 10)
                ELSE COALESCE(NULLIF(filing_date, ''), '9999-12-31')
              END <= ?
        ORDER BY confidence DESC, ticker, metric_name, period_end DESC,
                 accession_number DESC, document_name
        """,
        (asof,),
    ).fetchall()
    return [
        {
            "review_rank": rank,
            **dict(row),
            "accepted_at": accepted_date(
                str(row["accepted_at"] or ""),
                str(row["filing_date"] or ""),
            ),
        }
        for rank, row in enumerate(rows, start=1)
    ]


def main() -> int:
    args = parse_args()
    if args.limit < 0 or args.max_filings_per_ticker < 0:
        raise ValueError("--limit and --max-filings-per-ticker must be non-negative")
    asof = parse_asof(args.asof)
    scan_start_date = parse_asof(args.scan_start_date) if args.scan_start_date else ""
    if scan_start_date and scan_start_date > asof:
        raise ValueError("--scan-start-date must be on or before --asof")
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    db_path = (
        args.db.expanduser().resolve()
        if args.db
        else resolve_path(cfg_get(config, "paths.database_path"), base_dir=config_path.parent)
    )
    output_path = (
        args.output_csv.expanduser().resolve()
        if args.output_csv
        else resolve_path(
            cfg_get(
                config,
                "sec_fundamentals.disclosure_candidate_output_csv",
                "../../output/industrials/machinery/stage4/disclosure_candidate_audit.csv",
            ),
            base_dir=config_path.parent,
        )
    )
    review_output_path = (
        args.review_output_csv.expanduser().resolve()
        if args.review_output_csv
        else resolve_path(
            cfg_get(
                config,
                "sec_fundamentals.disclosure_review_output_csv",
                "../../output/industrials/machinery/stage4/disclosure_candidate_review_required.csv",
            ),
            base_dir=config_path.parent,
        )
    )
    requested = {item.strip().upper() for item in args.tickers.split(",") if item.strip()}
    with connect(db_path) as conn:
        init_db(conn)
        members = active_members(conn, asof=asof, include_historical=args.include_historical)
        priorities = ticker_priorities(conn, asof=asof, members=members)
        if requested:
            priorities = [row for row in priorities if str(row["ticker"]) in requested]
        elif args.limit > 0:
            priorities = priorities[: args.limit]
        tickers = [str(row["ticker"]) for row in priorities]
        if args.scan_cache:
            cache_dir = resolve_path(
                cfg_get(config, "sec_fundamentals.cache_dir"),
                base_dir=config_path.parent,
            )
            source_id = str(
                cfg_get(config, "sec_fundamentals.companyfacts_source_id", "sec_companyfacts")
            )
            filings_source_id = str(
                cfg_get(config, "sec_fundamentals.submissions_source_id", "sec_submissions")
            )
            inserted, promoted_raw, promoted_mapped, skipped = scan_cached_candidates(
                conn,
                tickers=tickers,
                members=members,
                asof=asof,
                cache_dir=cache_dir,
                source_id=source_id,
                filings_source_id=filings_source_id,
                max_filings_per_ticker=args.max_filings_per_ticker,
                scan_start_date=scan_start_date,
                resume=bool(args.resume),
                pdf_ocr_enabled=bool(cfg_get(config, "sec_archive.pdf_ocr_enabled", False)),
                max_pdf_pages=int(cfg_get(config, "sec_archive.max_pdf_pages", 250) or 250),
                max_pdf_bytes=int(
                    cfg_get(config, "sec_archive.max_pdf_bytes", 25_000_000) or 25_000_000
                ),
                pdf_extraction_timeout_sec=float(
                    cfg_get(config, "sec_archive.pdf_extraction_timeout_sec", 30.0) or 30.0
                ),
            )
            print(
                "Cache scan: "
                f"candidates={inserted} promoted_raw={promoted_raw} "
                f"promoted_mapped={promoted_mapped} skipped={skipped}"
            )
            replayed = reapply_reviewed_disclosure_policies(
                conn,
                tickers=tickers,
                model_family="machinery",
                now=utc_now(),
            )
            print(
                "Policy replay: "
                f"documents={replayed['documents_replayed']} "
                f"candidates={replayed['candidate_rows']} "
                f"promoted_raw={replayed['promoted_raw']} "
                f"promoted_mapped={replayed['promoted_mapped']}"
            )
        rows = audit_rows(conn, asof=asof, priorities=priorities)
        review_rows = review_required_rows(conn, asof=asof)
    write_csv_atomic(output_path, OUTPUT_FIELDS, rows)
    write_csv_atomic(review_output_path, REVIEW_OUTPUT_FIELDS, review_rows)
    print(f"PASS: wrote {len(rows)} missing-metric audit rows for {len(priorities)} tickers to {output_path}")
    print(
        f"PASS: wrote {len(review_rows)} review-required candidates to {review_output_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
