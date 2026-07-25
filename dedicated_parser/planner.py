from __future__ import annotations

import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Iterable

from dedicated_parser.adapters import load_ticker_selector
from dedicated_parser.catalog import (
    accession_directory,
    build_document_refs,
    filing_rows,
    relevant_document_names,
)
from dedicated_parser.contracts import (
    AdapterRegistry,
    FilingRef,
    PlanSummary,
    WorkItem,
    file_sha256,
)
from dedicated_parser.storage import catalog_documents, completed_work_keys


MISSING_STATUSES = frozenset(
    {
        "NOT_DISCLOSED",
        "DISCLOSED_UNPARSED",
        "PARSER_FAILURE",
        "PROXY",
    }
)


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (name,),
        ).fetchone()
        is not None
    )


def active_tickers(
    conn: sqlite3.Connection,
    *,
    model_family: str,
    asof_date: str,
) -> list[str]:
    return [
        str(row["ticker"])
        for row in conn.execute(
            """
            SELECT DISTINCT ticker
            FROM dim_universe_membership
            WHERE model_family = ?
              AND start_date <= ?
              AND COALESCE(end_date, '9999-12-31') >= ?
            ORDER BY ticker
            """,
            (model_family, asof_date, asof_date),
        )
    ]


def unresolved_requests(
    conn: sqlite3.Connection,
    *,
    registry: AdapterRegistry,
    asof_date: str,
    tickers: Iterable[str],
) -> dict[str, set[str]]:
    ticker_list = sorted(set(tickers))
    if not ticker_list:
        return {}
    requests: dict[str, set[str]] = defaultdict(set)
    if not _table_exists(conn, "feature_financial_metric_availability"):
        return {
            ticker: {metric.metric_name for metric in registry.source_metrics}
            for ticker in ticker_list
        }
    placeholders = ",".join("?" for _ in ticker_list)
    rows = conn.execute(
        f"""
        SELECT ticker, metric_name, availability_status
        FROM feature_financial_metric_availability
        WHERE model_family = ?
          AND ticker IN ({placeholders})
          AND asof_date = (
              SELECT MAX(a2.asof_date)
              FROM feature_financial_metric_availability AS a2
              WHERE a2.model_family = feature_financial_metric_availability.model_family
                AND a2.ticker = feature_financial_metric_availability.ticker
                AND a2.asof_date <= ?
          )
        """,
        (registry.model_family, *ticker_list, asof_date),
    ).fetchall()
    for row in rows:
        if str(row["availability_status"]) not in MISSING_STATUSES:
            continue
        request = registry.request(str(row["metric_name"]))
        if request is not None:
            requests[str(row["ticker"])].add(request.metric_name)
    return dict(requests)


def _database_has_source_fact(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    metric_name: str,
    asof_date: str,
) -> bool:
    return (
        conn.execute(
            """
            SELECT 1
            FROM fact_sec_xbrl_fact
            WHERE ticker = ? AND canonical_metric = ?
              AND SUBSTR(
                    COALESCE(NULLIF(accepted_at, ''), filing_date),
                    1,
                    10
                  ) <= ?
              AND value IS NOT NULL
            LIMIT 1
            """,
            (ticker, metric_name, asof_date),
        ).fetchone()
        is not None
    )


def _planning_scope(
    conn: sqlite3.Connection,
    *,
    registry: AdapterRegistry,
    adapter_path: str,
    asof_date: str,
    tickers: Iterable[str] | None,
    accessions: Iterable[str] | None,
    max_filings_per_ticker: int,
    force: bool,
) -> tuple[list[str], dict[str, set[str]], int, dict[str, list[FilingRef]]]:
    selector = load_ticker_selector(adapter_path)
    selected_tickers = sorted(
        set(
            tickers
            if tickers is not None
            else selector(conn, asof_date)
            if selector is not None
            else active_tickers(
                conn,
                model_family=registry.model_family,
                asof_date=asof_date,
            )
        )
    )
    unresolved = unresolved_requests(
        conn,
        registry=registry,
        asof_date=asof_date,
        tickers=selected_tickers,
    )
    if force:
        unresolved = {
            ticker: {
                request.metric_name for request in registry.source_metrics
            }
            for ticker in selected_tickers
        }
    database_satisfied = 0
    for ticker, metrics in list(unresolved.items()):
        remaining = (
            set(metrics)
            if force
            else {
                metric
                for metric in metrics
                if not _database_has_source_fact(
                    conn,
                    ticker=ticker,
                    metric_name=metric,
                    asof_date=asof_date,
                )
            }
        )
        database_satisfied += len(metrics) - len(remaining)
        if remaining:
            unresolved[ticker] = remaining
        else:
            unresolved.pop(ticker, None)
    filings = filing_rows(
        conn,
        asof_date=asof_date,
        tickers=unresolved,
        accessions=accessions,
        supported_forms=registry.supported_forms,
        max_filings_per_ticker=max_filings_per_ticker,
    )
    return selected_tickers, unresolved, database_satisfied, filings


def audit_cache_completeness(
    conn: sqlite3.Connection,
    *,
    registry: AdapterRegistry,
    adapter_path: str,
    asof_date: str,
    cache_dir: Path,
    tickers: Iterable[str] | None = None,
    accessions: Iterable[str] | None = None,
    max_filings_per_ticker: int = 8,
    max_documents_per_filing: int = 16,
    force: bool = False,
) -> PlanSummary:
    (
        selected_tickers,
        unresolved,
        database_satisfied,
        filings,
    ) = _planning_scope(
        conn,
        registry=registry,
        adapter_path=adapter_path,
        asof_date=asof_date,
        tickers=tickers,
        accessions=accessions,
        max_filings_per_ticker=max_filings_per_ticker,
        force=force,
    )
    available_accessions = 0
    available_documents = 0
    missing_cache_details: list[dict[str, str]] = []
    for ticker in sorted(unresolved):
        for filing in filings.get(ticker, []):
            directory = accession_directory(cache_dir, filing)
            names = (
                relevant_document_names(
                    directory,
                    filing=filing,
                    keywords=registry.document_keywords,
                )
                if directory.is_dir()
                else ()
            )
            if max_documents_per_filing > 0:
                names = names[:max_documents_per_filing]
            if not names:
                missing_cache_details.append(
                    {
                        "ticker": filing.ticker,
                        "accession_number": filing.accession_number,
                        "form_type": filing.form_type,
                        "filing_date": filing.filing_date,
                    }
                )
                continue
            available_accessions += 1
            available_documents += len(names)
    return PlanSummary(
        asof_date=asof_date,
        model_family=registry.model_family,
        requested_tickers=len(selected_tickers),
        unresolved_metric_pairs=sum(
            len(metrics) for metrics in unresolved.values()
        ),
        database_satisfied_pairs=database_satisfied,
        scheduled_accessions=available_accessions,
        scheduled_documents=available_documents,
        skipped_completed_accessions=0,
        missing_cache_accessions=len(missing_cache_details),
        missing_cache_details=tuple(missing_cache_details),
    )


def build_plan(
    conn: sqlite3.Connection,
    *,
    registry: AdapterRegistry,
    adapter_path: str,
    asof_date: str,
    cache_dir: Path,
    tickers: Iterable[str] | None = None,
    accessions: Iterable[str] | None = None,
    max_filings_per_ticker: int = 8,
    max_documents_per_filing: int = 16,
    resume: bool = True,
    force: bool = False,
    enable_arelle: bool = True,
    enable_edgartools: bool = True,
    enable_pdf_ocr: bool = False,
    max_pdf_pages: int = 250,
    max_pdf_bytes: int = 25_000_000,
    pdf_extraction_timeout_seconds: float = 30.0,
) -> tuple[list[WorkItem], PlanSummary]:
    review_policy_path = (
        Path(registry.review_policy_path).expanduser().resolve()
        if registry.review_policy_path
        else None
    )
    review_policy_sha256 = (
        file_sha256(review_policy_path)
        if review_policy_path is not None
        else ""
    )
    (
        selected_tickers,
        unresolved,
        database_satisfied,
        filings,
    ) = _planning_scope(
        conn,
        registry=registry,
        adapter_path=adapter_path,
        asof_date=asof_date,
        tickers=tickers,
        accessions=accessions,
        max_filings_per_ticker=max_filings_per_ticker,
        force=force,
    )
    completed = (
        completed_work_keys(
            conn,
            model_family=registry.model_family,
            adapter_version=registry.adapter_version,
        )
        if resume and not force
        else set()
    )
    work: list[WorkItem] = []
    skipped_completed = 0
    missing_cache = 0
    missing_cache_details: list[dict[str, str]] = []
    scheduled_documents = 0
    for ticker in sorted(unresolved):
        for filing in filings.get(ticker, []):
            documents = build_document_refs(
                conn,
                cache_dir=cache_dir,
                filing=filing,
                keywords=registry.document_keywords,
                max_documents=max_documents_per_filing,
            )
            if not documents:
                missing_cache += 1
                missing_cache_details.append(
                    {
                        "ticker": filing.ticker,
                        "accession_number": filing.accession_number,
                        "form_type": filing.form_type,
                        "filing_date": filing.filing_date,
                    }
                )
                continue
            catalog_documents(conn, filing=filing, documents=documents)
            item = WorkItem(
                model_family=registry.model_family,
                adapter_path=adapter_path,
                adapter_version=registry.adapter_version,
                filing=filing,
                documents=documents,
                requested_metrics=registry.source_metrics,
                review_policy_path=(
                    str(review_policy_path)
                    if review_policy_path is not None
                    else ""
                ),
                review_policy_sha256=review_policy_sha256,
                enable_arelle=enable_arelle,
                enable_edgartools=enable_edgartools,
                enable_pdf_ocr=enable_pdf_ocr,
                max_pdf_pages=max_pdf_pages,
                max_pdf_bytes=max_pdf_bytes,
                pdf_extraction_timeout_seconds=(
                    pdf_extraction_timeout_seconds
                ),
            )
            if item.work_key in completed:
                skipped_completed += 1
                continue
            work.append(item)
            scheduled_documents += len(documents)
    conn.commit()
    return work, PlanSummary(
        asof_date=asof_date,
        model_family=registry.model_family,
        requested_tickers=len(selected_tickers),
        unresolved_metric_pairs=sum(len(metrics) for metrics in unresolved.values()),
        database_satisfied_pairs=database_satisfied,
        scheduled_accessions=len(work),
        scheduled_documents=scheduled_documents,
        skipped_completed_accessions=skipped_completed,
        missing_cache_accessions=missing_cache,
        missing_cache_details=tuple(missing_cache_details),
    )
