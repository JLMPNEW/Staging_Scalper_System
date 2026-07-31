from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable, Mapping

from dedicated_parser.adapters import load_ticker_selector
from dedicated_parser.catalog import (
    build_document_refs,
    filing_rows,
)
from dedicated_parser.contracts import (
    AdapterRegistry,
    DocumentRef,
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

DocumentScope = Mapping[tuple[str, str], Mapping[str, str]]
DirectFilings = Mapping[tuple[str, str], FilingRef]
DirectDocuments = Mapping[tuple[str, str], tuple[DocumentRef, ...]]
MetricScope = Mapping[tuple[str, str], frozenset[str]]


def _source_documents(
    conn: sqlite3.Connection,
    *,
    cache_dir: Path,
    filing: FilingRef,
    keywords: tuple[str, ...],
    max_documents: int,
    required_documents: Mapping[str, str] | None,
    direct_documents: DirectDocuments | None,
) -> tuple[DocumentRef, ...]:
    if direct_documents is not None:
        return tuple(
            direct_documents.get(
                (filing.ticker.upper(), filing.accession_number),
                (),
            )
        )
    return build_document_refs(
        conn,
        cache_dir=cache_dir,
        filing=filing,
        keywords=keywords,
        max_documents=max_documents,
        required_documents=required_documents,
    )


def _apply_document_scope(
    *,
    filing: FilingRef,
    documents: tuple[DocumentRef, ...],
    document_scope: DocumentScope | None,
) -> tuple[tuple[DocumentRef, ...], str]:
    if document_scope is None:
        return documents, ""
    expected = document_scope.get((filing.ticker.upper(), filing.accession_number))
    if expected is None:
        return (), "filing_not_present_in_source_manifest"
    actual = {document.name: document for document in documents}
    missing = sorted(set(expected) - set(actual))
    unexpected = sorted(set(actual) - set(expected))
    mismatched = sorted(
        name
        for name in set(expected) & set(actual)
        if str(expected[name]).lower() != str(actual[name].content_sha256).lower()
    )
    if missing:
        return (), "manifest_documents_missing:" + "|".join(missing)
    if unexpected:
        return (), "unsealed_documents_present:" + "|".join(unexpected)
    if mismatched:
        return (), "manifest_document_hash_mismatch:" + "|".join(mismatched)
    return (
        tuple(actual[name] for name in sorted(expected)),
        "",
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
    dependencies = unresolved_dependency_requirements(
        conn,
        registry=registry,
        asof_date=asof_date,
        tickers=tickers,
    )
    return {ticker: set(source_requirements) for ticker, source_requirements in dependencies.items()}


def unresolved_dependency_requirements(
    conn: sqlite3.Connection,
    *,
    registry: AdapterRegistry,
    asof_date: str,
    tickers: Iterable[str],
) -> dict[str, dict[str, set[str]]]:
    ticker_list = sorted(set(tickers))
    if not ticker_list:
        return {}
    requests: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    if not _table_exists(conn, "feature_financial_metric_availability"):
        return {
            ticker: {metric.metric_name: {metric.metric_name} for metric in registry.parser_metrics}
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
        dependent_metric = str(row["metric_name"])
        request = registry.request(dependent_metric)
        if request is not None:
            requests[str(row["ticker"])][request.metric_name].add(dependent_metric)
    return {
        ticker: {
            source_metric: set(dependent_metrics) for source_metric, dependent_metrics in source_requirements.items()
        }
        for ticker, source_requirements in requests.items()
    }


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


def _feature_field_is_populated(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    model_family: str,
    asof_date: str,
    field: str,
) -> bool:
    if not field.replace("_", "").isalnum():
        raise ValueError(f"Unsafe feature field in parser requirement: {field!r}")
    if not _table_exists(conn, "feature_financial_statement"):
        return False
    columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(feature_financial_statement)")}
    if field not in columns:
        return False
    row = conn.execute(
        f"""
        SELECT {field}
        FROM feature_financial_statement
        WHERE ticker = ? AND model_family = ? AND asof_date <= ?
        ORDER BY asof_date DESC
        LIMIT 1
        """,
        (ticker, model_family, asof_date),
    ).fetchone()
    return row is not None and row[0] is not None


def _database_satisfies_requirements(
    conn: sqlite3.Connection,
    *,
    registry: AdapterRegistry,
    ticker: str,
    source_metric: str,
    dependent_metrics: set[str],
    asof_date: str,
) -> bool:
    for dependent_metric in dependent_metrics:
        requirement = registry.metric_requirements.get(dependent_metric)
        if requirement is None:
            if not _database_has_source_fact(
                conn,
                ticker=ticker,
                metric_name=source_metric,
                asof_date=asof_date,
            ):
                return False
            continue
        feature_populated = _feature_field_is_populated(
            conn,
            ticker=ticker,
            model_family=registry.model_family,
            asof_date=asof_date,
            field=requirement.satisfaction_field,
        )
        if feature_populated:
            continue
        if requirement.mode == "point" and _database_has_source_fact(
            conn,
            ticker=ticker,
            metric_name=source_metric,
            asof_date=asof_date,
        ):
            continue
        if not feature_populated:
            return False
    return True


def _latest_feature_row(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    model_family: str,
    asof_date: str,
) -> dict[str, object]:
    if not _table_exists(conn, "feature_financial_statement"):
        return {}
    row = conn.execute(
        """
        SELECT *
        FROM feature_financial_statement
        WHERE ticker = ? AND model_family = ? AND asof_date <= ?
        ORDER BY asof_date DESC
        LIMIT 1
        """,
        (ticker, model_family, asof_date),
    ).fetchone()
    return dict(row) if row is not None else {}


def _series_gap_detail(
    conn: sqlite3.Connection,
    *,
    registry: AdapterRegistry,
    ticker: str,
    source_metric: str,
    dependent_metric: str,
    asof_date: str,
) -> dict[str, object]:
    requirement = registry.metric_requirements[dependent_metric]
    feature = _latest_feature_row(
        conn,
        ticker=ticker,
        model_family=registry.model_family,
        asof_date=asof_date,
    )
    anchor = str(feature.get("fiscal_period_end") or asof_date)[:10]
    try:
        lower_bound = (
            date.fromisoformat(anchor) - timedelta(days=max(1, requirement.lookback_days or 460))
        ).isoformat()
    except ValueError:
        lower_bound = ""
    source_rows = conn.execute(
        """
        SELECT DISTINCT period_start, period_end
        FROM fact_sec_xbrl_fact
        WHERE ticker = ? AND canonical_metric = ?
          AND value IS NOT NULL
          AND period_end <= ?
          AND (? = '' OR period_end >= ?)
          AND SUBSTR(
                COALESCE(NULLIF(accepted_at, ''), filing_date),
                1,
                10
              ) <= ?
        ORDER BY period_end
        """,
        (
            ticker,
            source_metric,
            anchor,
            lower_bound,
            lower_bound,
            asof_date,
        ),
    ).fetchall()
    available_periods = [
        {
            "period_start": str(row["period_start"] or ""),
            "period_end": str(row["period_end"] or ""),
        }
        for row in source_rows
    ]
    revenue_rows = conn.execute(
        """
        SELECT DISTINCT period_start, period_end
        FROM fact_sec_xbrl_fact
        WHERE ticker = ? AND canonical_metric = 'revenue'
          AND value IS NOT NULL
          AND period_start IS NOT NULL AND period_start <> ''
          AND period_end <= ?
          AND julianday(period_end) - julianday(period_start)
              BETWEEN 45 AND 130
          AND SUBSTR(
                COALESCE(NULLIF(accepted_at, ''), filing_date),
                1,
                10
              ) <= ?
        ORDER BY period_end DESC
        LIMIT ?
        """,
        (
            ticker,
            anchor,
            asof_date,
            max(1, requirement.minimum_discrete_periods or 4),
        ),
    ).fetchall()
    required_periods = [
        {
            "period_start": str(row["period_start"] or ""),
            "period_end": str(row["period_end"] or ""),
        }
        for row in reversed(revenue_rows)
    ]
    available_keys = {(item["period_start"], item["period_end"]) for item in available_periods}
    missing_periods = [
        item for item in required_periods if (item["period_start"], item["period_end"]) not in available_keys
    ]
    return {
        "ticker": ticker,
        "source_metric": source_metric,
        "dependent_metric": dependent_metric,
        "facet": "series_incomplete_for_ttm",
        "anchor_period_end": anchor,
        "minimum_discrete_periods": (requirement.minimum_discrete_periods or 4),
        "lookback_days": requirement.lookback_days or 460,
        "available_periods": available_periods,
        "required_periods": required_periods,
        "missing_periods": missing_periods,
    }


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
    all_metrics: bool,
    document_scope: DocumentScope | None = None,
    direct_filings: DirectFilings | None = None,
) -> tuple[
    list[str],
    dict[str, set[str]],
    int,
    dict[str, list[FilingRef]],
    tuple[dict[str, object], ...],
]:
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
    dependency_requirements = unresolved_dependency_requirements(
        conn,
        registry=registry,
        asof_date=asof_date,
        tickers=selected_tickers,
    )
    if force or all_metrics:
        dependency_requirements = {
            ticker: {request.metric_name: {request.metric_name} for request in registry.parser_metrics}
            for ticker in selected_tickers
        }
    database_satisfied = 0
    unresolved: dict[str, set[str]] = {}
    series_gaps: list[dict[str, object]] = []
    for ticker, source_requirements in dependency_requirements.items():
        remaining: set[str] = set()
        for source_metric, dependent_metrics in source_requirements.items():
            satisfied = (
                False
                if force or all_metrics
                else _database_satisfies_requirements(
                    conn,
                    registry=registry,
                    ticker=ticker,
                    source_metric=source_metric,
                    dependent_metrics=dependent_metrics,
                    asof_date=asof_date,
                )
            )
            if satisfied:
                database_satisfied += 1
                continue
            remaining.add(source_metric)
            for dependent_metric in sorted(dependent_metrics):
                requirement = registry.metric_requirements.get(dependent_metric)
                if requirement is not None and requirement.mode == "series_ttm":
                    series_gaps.append(
                        _series_gap_detail(
                            conn,
                            registry=registry,
                            ticker=ticker,
                            source_metric=source_metric,
                            dependent_metric=dependent_metric,
                            asof_date=asof_date,
                        )
                    )
        if remaining:
            unresolved[ticker] = remaining
    target_periods_by_ticker: dict[str, tuple[str, ...]] = {}
    for detail in series_gaps:
        ticker = str(detail["ticker"])
        missing_periods = detail.get("missing_periods")
        if not isinstance(missing_periods, list):
            continue
        target_periods_by_ticker[ticker] = tuple(
            sorted(
                {
                    str(item.get("period_end") or "")
                    for item in missing_periods
                    if isinstance(item, dict) and str(item.get("period_end") or "")
                }
            )
        )
    if direct_filings is not None:
        filings = {ticker: [] for ticker in unresolved}
        for (ticker, _), filing in sorted(direct_filings.items()):
            if ticker in filings:
                filings[ticker].append(filing)
    else:
        filings = filing_rows(
            conn,
            model_family=registry.model_family,
            asof_date=asof_date,
            tickers=unresolved,
            accessions=accessions,
            supported_forms=registry.supported_forms,
            max_filings_per_ticker=max_filings_per_ticker,
            target_periods_by_ticker=target_periods_by_ticker,
        )
    if document_scope is not None:
        # An accession number identifies an SEC submission, not a ticker
        # lifecycle. Predecessor/current tickers can therefore share a CIK and
        # accession. The source manifest is sealed at ticker + accession +
        # document granularity, so do not let the global accession filter pull
        # a current issuer's filing into an inactive predecessor's scope.
        filings = {
            ticker: [
                filing
                for filing in ticker_filings
                if (
                    filing.ticker.upper(),
                    filing.accession_number,
                )
                in document_scope
            ]
            for ticker, ticker_filings in filings.items()
        }
    return (
        selected_tickers,
        unresolved,
        database_satisfied,
        filings,
        tuple(series_gaps),
    )


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
    all_metrics: bool = False,
    document_scope: DocumentScope | None = None,
    direct_filings: DirectFilings | None = None,
    direct_documents: DirectDocuments | None = None,
) -> PlanSummary:
    (
        selected_tickers,
        unresolved,
        database_satisfied,
        filings,
        series_gaps,
    ) = _planning_scope(
        conn,
        registry=registry,
        adapter_path=adapter_path,
        asof_date=asof_date,
        tickers=tickers,
        accessions=accessions,
        max_filings_per_ticker=max_filings_per_ticker,
        force=force,
        all_metrics=all_metrics,
        document_scope=document_scope,
        direct_filings=direct_filings,
    )
    available_accessions = 0
    available_documents = 0
    missing_cache_details: list[dict[str, str]] = []
    for ticker in sorted(unresolved):
        for filing in filings.get(ticker, []):
            expected_documents = (
                document_scope.get((filing.ticker.upper(), filing.accession_number))
                if document_scope is not None
                else None
            )
            documents = _source_documents(
                conn,
                cache_dir=cache_dir,
                filing=filing,
                keywords=registry.document_keywords,
                max_documents=max_documents_per_filing,
                required_documents=expected_documents,
                direct_documents=direct_documents,
            )
            documents, scope_error = _apply_document_scope(
                filing=filing,
                documents=documents,
                document_scope=document_scope,
            )
            if not documents:
                missing_cache_details.append(
                    {
                        "ticker": filing.ticker,
                        "accession_number": filing.accession_number,
                        "form_type": filing.form_type,
                        "filing_date": filing.filing_date,
                        "reason": scope_error or "no_cached_source_document",
                    }
                )
                continue
            available_accessions += 1
            available_documents += len(documents)
    return PlanSummary(
        asof_date=asof_date,
        model_family=registry.model_family,
        requested_tickers=len(selected_tickers),
        unresolved_metric_pairs=sum(len(metrics) for metrics in unresolved.values()),
        database_satisfied_pairs=database_satisfied,
        scheduled_accessions=available_accessions,
        scheduled_documents=available_documents,
        skipped_completed_accessions=0,
        missing_cache_accessions=len(missing_cache_details),
        missing_cache_details=tuple(missing_cache_details),
        series_gap_details=series_gaps,
        selected_tickers=tuple(selected_tickers),
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
    all_metrics: bool = False,
    enable_arelle: bool = True,
    enable_edgartools: bool = True,
    enable_pdf_ocr: bool = False,
    max_pdf_pages: int = 250,
    max_pdf_bytes: int = 25_000_000,
    pdf_extraction_timeout_seconds: float = 30.0,
    document_scope: DocumentScope | None = None,
    direct_filings: DirectFilings | None = None,
    direct_documents: DirectDocuments | None = None,
    metric_scope: MetricScope | None = None,
    catalog_documents_enabled: bool = True,
) -> tuple[list[WorkItem], PlanSummary]:
    review_policy_path = (
        Path(registry.review_policy_path).expanduser().resolve() if registry.review_policy_path else None
    )
    review_policy_sha256 = file_sha256(review_policy_path) if review_policy_path is not None else ""
    (
        selected_tickers,
        unresolved,
        database_satisfied,
        filings,
        series_gaps,
    ) = _planning_scope(
        conn,
        registry=registry,
        adapter_path=adapter_path,
        asof_date=asof_date,
        tickers=tickers,
        accessions=accessions,
        max_filings_per_ticker=max_filings_per_ticker,
        force=force,
        all_metrics=all_metrics,
        document_scope=document_scope,
        direct_filings=direct_filings,
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
    skipped_completed_work: list[dict[str, str]] = []
    missing_cache = 0
    missing_cache_details: list[dict[str, str]] = []
    scheduled_documents = 0
    for ticker in sorted(unresolved):
        for filing in filings.get(ticker, []):
            expected_documents = (
                document_scope.get((filing.ticker.upper(), filing.accession_number))
                if document_scope is not None
                else None
            )
            documents = _source_documents(
                conn,
                cache_dir=cache_dir,
                filing=filing,
                keywords=registry.document_keywords,
                max_documents=max_documents_per_filing,
                required_documents=expected_documents,
                direct_documents=direct_documents,
            )
            documents, scope_error = _apply_document_scope(
                filing=filing,
                documents=documents,
                document_scope=document_scope,
            )
            if not documents:
                missing_cache += 1
                missing_cache_details.append(
                    {
                        "ticker": filing.ticker,
                        "accession_number": filing.accession_number,
                        "form_type": filing.form_type,
                        "filing_date": filing.filing_date,
                        "reason": scope_error or "no_cached_source_document",
                    }
                )
                continue
            if catalog_documents_enabled:
                catalog_documents(
                    conn,
                    filing=filing,
                    documents=documents,
                )
            key = (filing.ticker.upper(), filing.accession_number)
            scoped_metric_names = unresolved[ticker]
            if metric_scope is not None:
                requested_scope = metric_scope.get(key)
                if requested_scope is None:
                    raise ValueError(
                        "Direct source manifest has no requested metric "
                        f"scope for {filing.ticker}/{filing.accession_number}"
                    )
                registry_metric_names = {request.metric_name for request in registry.parser_metrics}
                unknown_metrics = sorted(set(requested_scope) - registry_metric_names)
                if unknown_metrics:
                    raise ValueError(
                        f"Source manifest requested metrics absent from adapter registry: {unknown_metrics}"
                    )
                scoped_metric_names = set(scoped_metric_names) & set(requested_scope)
                if not scoped_metric_names:
                    raise ValueError(
                        "Source manifest has no applicable requested "
                        f"metrics for {filing.ticker}/"
                        f"{filing.accession_number}"
                    )
            item = WorkItem(
                model_family=registry.model_family,
                adapter_path=adapter_path,
                adapter_version=registry.adapter_version,
                filing=filing,
                documents=documents,
                requested_metrics=tuple(
                    request for request in registry.parser_metrics if request.metric_name in scoped_metric_names
                ),
                review_policy_path=(str(review_policy_path) if review_policy_path is not None else ""),
                review_policy_sha256=review_policy_sha256,
                enable_arelle=enable_arelle,
                enable_edgartools=enable_edgartools,
                enable_pdf_ocr=enable_pdf_ocr,
                max_pdf_pages=max_pdf_pages,
                max_pdf_bytes=max_pdf_bytes,
                pdf_extraction_timeout_seconds=(pdf_extraction_timeout_seconds),
            )
            if item.work_key in completed:
                skipped_completed += 1
                skipped_completed_work.append(
                    {
                        "work_key": item.work_key,
                        "ticker": filing.ticker,
                        "accession_number": filing.accession_number,
                    }
                )
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
        series_gap_details=series_gaps,
        skipped_completed_work=tuple(skipped_completed_work),
        selected_tickers=tuple(selected_tickers),
    )
