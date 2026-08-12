from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable, Mapping

from dedicated_parser.adapters import load_ticker_selector
from dedicated_parser.catalog import (
    build_document_refs,
    filing_rows,
    validate_consumer_defensive_catalog_contract,
)
from dedicated_parser.contracts import (
    AdapterRegistry,
    DocumentRef,
    FilingRef,
    PlanSummary,
    WorkItem,
    file_sha256,
)
from dedicated_parser.path_io import (
    is_file_path,
    resolve_path as resolve_filesystem_path,
    runtime_path,
    stat_path,
)
from dedicated_parser.storage import catalog_documents, completed_work_keys
from dedicated_parser.sec_paths import (
    SEC_DOCUMENT_SUFFIXES,
    resolve_sec_seal_root,
    validate_sec_relative_document_path,
)


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


def _validate_consumer_defensive_direct_filings(
    conn: sqlite3.Connection,
    *,
    direct_filings: DirectFilings,
    asof_date: str,
) -> None:
    cutoff = asof_date + 'T23:59:59Z'
    for raw_key, filing in sorted(direct_filings.items()):
        key = (str(raw_key[0]).upper(), str(raw_key[1]))
        filing_key = (filing.ticker.upper(), filing.accession_number)
        if key != filing_key:
            raise ValueError(
                f'Consumer Defensive direct filing key mismatch: {key!r}'
            )
        row = conn.execute('''SELECT f.ticker,f.cik,f.archive_cik,
            f.accession_number,f.form_type,f.filing_date,f.accepted_at,
            f.report_date,f.primary_document,f.source_id,f.company_currency
            FROM consumer_defensive_sec_parser_filing_input f
            WHERE f.ticker=? AND f.accession_number=?
              AND SUBSTR(COALESCE(NULLIF(f.accepted_at,''),f.filing_date),1,10)<=?
              AND (SELECT e.event_type
                   FROM sec_filing_company_association_event e
                   WHERE e.accession_number=f.accession_number
                     AND e.issuer_company_id=f.issuer_company_id
                     AND e.effective_asof<=?
                   ORDER BY e.effective_asof DESC,e.event_id DESC LIMIT 1)
                  IN ('observed','reactivated')''',
            (key[0],key[1],asof_date,cutoff),
        ).fetchone()
        if row is None:
            raise ValueError(
                'Consumer Defensive direct filing is not an active PIT '
                f'association: {key[0]}/{key[1]}'
            )
        actual_cik = str(row[2] or row[1] or '').zfill(10)
        supplied_cik = str(filing.archive_cik or filing.cik or '').zfill(10)
        expected = (
            str(row[4] or ''), str(row[5] or ''),
            str(row[6] or row[5] or ''), str(row[7] or ''),
            str(row[8] or ''), str(row[9] or ''), str(row[10] or '').upper(),
        )
        supplied = (
            filing.form_type, filing.filing_date, filing.accepted_at,
            filing.report_date, filing.primary_document, filing.source_id,
            filing.company_currency.upper(),
        )
        if supplied_cik != actual_cik or supplied != expected:
            raise ValueError(
                'Consumer Defensive direct filing metadata does not match '
                f'the active PIT association: {key[0]}/{key[1]}'
            )


def _validate_consumer_defensive_direct_documents(
    conn: sqlite3.Connection,
    *,
    direct_filings: DirectFilings,
    direct_documents: DirectDocuments,
    asof_date: str,
    cache_dir: Path,
) -> dict[tuple[str, str], tuple[DocumentRef, ...]]:
    '''Bind caller-supplied document bytes to the exact immutable Stage4 seal.

    A source manifest is only a transport envelope: its hash and local path
    are caller-controlled.  Validate those bytes against both the active PIT
    document projection and the reconciled as-of cache snapshot, then return
    refs whose paths point at the verified immutable seal objects.  Returning
    rebound refs closes the validate-then-mutate window between planning and
    provider execution.
    '''
    seal = conn.execute('''SELECT s.seal_relative_path,s.cache_manifest_json,
        s.cache_manifest_sha256
        FROM consumer_defensive_sec_cache_snapshot s
        JOIN consumer_defensive_sec_reconciliation_state r USING(asof_date)
        WHERE s.asof_date=? AND r.status='complete'
          AND s.scope_contract_version=3
          AND r.scope_contract_version=3
          AND s.trust_state='trusted_current'
          AND r.trust_state='trusted_current'
          AND s.cache_manifest_json=r.cache_manifest_json
          AND s.cache_manifest_sha256=r.cache_manifest_sha256''',
        (asof_date,),
    ).fetchone()
    if seal is None:
        raise RuntimeError(
            'Consumer Defensive direct documents require an exact reconciled seal'
        )
    try:
        entries = json.loads(str(seal[1]))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError('Consumer Defensive SEC seal manifest is invalid') from exc
    if not isinstance(entries, list) or not entries:
        raise RuntimeError('Consumer Defensive SEC seal manifest is empty')
    manifest: dict[str, dict[str, object]] = {}
    normalized_entries: list[dict[str, object]] = []
    casefold_paths: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise RuntimeError('Consumer Defensive SEC seal entry is invalid')
        logical = str(entry.get('logical_path') or '')
        digest = str(entry.get('sha256') or '').lower()
        object_path = str(entry.get('object_path') or '')
        try:
            byte_count = int(entry.get('bytes'))
        except (TypeError, ValueError) as exc:
            raise RuntimeError('Consumer Defensive SEC seal byte count is invalid') from exc
        if (
            not logical
            or logical in manifest
            or logical.casefold() in casefold_paths
        ):
            raise RuntimeError('Consumer Defensive SEC seal has duplicate paths')
        if (
            not re.fullmatch(r'[0-9a-f]{64}', digest)
            or byte_count < 0
            or object_path != f'objects/sha256/{digest}'
        ):
            raise RuntimeError('Consumer Defensive SEC seal entry identity is invalid')
        normalized = {
            'logical_path': logical,
            'object_path': object_path,
            'bytes': byte_count,
            'sha256': digest,
        }
        manifest[logical] = normalized
        normalized_entries.append(normalized)
        casefold_paths.add(logical.casefold())
    normalized_entries.sort(key=lambda item: str(item['logical_path']))
    encoded = json.dumps(
        normalized_entries, sort_keys=True, separators=(',', ':'),
    ).encode()
    if hashlib.sha256(encoded).hexdigest() != str(seal[2]):
        raise RuntimeError('Consumer Defensive SEC seal manifest hash mismatch')
    sealed_root = resolve_sec_seal_root(
        cache_dir, str(seal[0]), expected_asof=asof_date
    )
    cutoff = asof_date + 'T23:59:59Z'
    normalized_filings: dict[tuple[str, str], FilingRef] = {}
    for raw_key, filing in direct_filings.items():
        key = (str(raw_key[0]).upper(), str(raw_key[1]))
        if key in normalized_filings:
            raise ValueError(f'Duplicate Consumer Defensive direct filing key: {key!r}')
        normalized_filings[key] = filing
    normalized_documents: dict[tuple[str, str], tuple[DocumentRef, ...]] = {}
    for raw_key, documents in direct_documents.items():
        key = (str(raw_key[0]).upper(), str(raw_key[1]))
        if key in normalized_documents:
            raise ValueError(f'Duplicate Consumer Defensive direct document key: {key!r}')
        normalized_documents[key] = tuple(documents)
    rebound: dict[tuple[str, str], tuple[DocumentRef, ...]] = {}
    for key, filing in sorted(normalized_filings.items()):
        normalized_key = (str(key[0]).upper(), str(key[1]))
        documents = normalized_documents.get(normalized_key, ())
        if not documents:
            raise ValueError(
                'Consumer Defensive direct filing has no sealed documents: '
                f'{normalized_key[0]}/{normalized_key[1]}'
            )
        seen_names: set[str] = set()
        primary_count = 0
        rebound_documents: list[DocumentRef] = []
        for document in documents:
            document_name = validate_sec_relative_document_path(
                document.name, allowed_suffixes=SEC_DOCUMENT_SUFFIXES,
                context='Consumer Defensive direct document name',
            )
            if document_name.casefold() in seen_names:
                raise ValueError(
                    'Duplicate case-insensitive Consumer Defensive direct '
                    f'document name: {normalized_key[0]}/{normalized_key[1]}/'
                    f'{document_name}'
                )
            seen_names.add(document_name.casefold())
            primary_count += int(document.is_primary)
            bridge_rows = conn.execute('''SELECT d.issuer_cik,d.primary_document,
                d.content_sha256,d.hydration_status,d.accepted_at,d.source_id
                FROM bridge_sec_filing_document_company d
                JOIN bridge_sec_filing_company b
                  ON b.accession_number=d.accession_number
                 AND b.issuer_company_id=d.issuer_company_id
                WHERE d.issuer_ticker=? AND d.accession_number=?
                  AND d.primary_document=? AND d.accepted_at<=?
                  AND (SELECT e.event_type
                       FROM sec_filing_company_association_event e
                       WHERE e.accession_number=b.accession_number
                         AND e.issuer_company_id=b.issuer_company_id
                         AND e.effective_asof<=?
                       ORDER BY e.effective_asof DESC,e.event_id DESC LIMIT 1)
                      IN ('observed','reactivated')''',
                (
                    normalized_key[0], normalized_key[1], document_name,
                    cutoff, cutoff,
                ),
            ).fetchall()
            if len(bridge_rows) != 1 or str(bridge_rows[0][3]) != 'hydrated':
                raise ValueError(
                    'Consumer Defensive direct document is not an active '
                    'hydrated PIT document: '
                    f'{normalized_key[0]}/{normalized_key[1]}/{document_name}'
                )
            bridge = bridge_rows[0]
            filing_cik = str(filing.archive_cik or filing.cik or '').zfill(10)
            if (
                str(bridge[0] or '').zfill(10) != filing_cik
                or str(bridge[1]) != document_name
                or str(bridge[4] or '') != filing.accepted_at
            ):
                raise ValueError(
                    'Consumer Defensive direct document metadata differs from '
                    f'the active PIT filing: {normalized_key[0]}/'
                    f'{normalized_key[1]}/{document_name}'
                )
            logical = (
                f'filings/{str(bridge[0]).zfill(10)}/'
                f'{normalized_key[1]}/{document_name}'
            )
            entry = manifest.get(logical)
            expected_hash = str(bridge[2] or '').lower()
            if entry is None or any((
                str(entry.get('sha256') or '') != expected_hash,
                str(document.content_sha256).lower() != expected_hash,
                int(entry.get('bytes') or -1) != document.file_size,
            )):
                raise ValueError(
                    'Consumer Defensive direct document does not match the '
                    f'exact Stage4 seal: {logical}'
                )
            try:
                local_path = resolve_filesystem_path(
                    Path(document.path), strict=True
                )
            except OSError as exc:
                raise ValueError(
                    f'Consumer Defensive direct document is unavailable: {logical}'
                ) from exc
            if (
                not is_file_path(local_path)
                or stat_path(local_path).st_size != int(entry['bytes'])
                or file_sha256(local_path) != expected_hash
            ):
                raise ValueError(
                    'Consumer Defensive direct document local bytes differ '
                    f'from the Stage4 seal: {logical}'
                )
            sealed_path = resolve_filesystem_path(
                sealed_root / str(entry.get('object_path') or ''), strict=True
            )
            try:
                sealed_path.relative_to(sealed_root)
            except ValueError as exc:
                raise RuntimeError('Consumer Defensive sealed object escapes') from exc
            if (
                not is_file_path(sealed_path)
                or stat_path(sealed_path).st_size != int(entry['bytes'])
                or file_sha256(sealed_path) != expected_hash
            ):
                raise RuntimeError(
                    f'Consumer Defensive sealed object is corrupt: {logical}'
                )
            if document.is_primary and document_name != filing.primary_document:
                raise ValueError(
                    'Consumer Defensive direct primary document metadata differs '
                    f'from the PIT filing: {logical}'
                )
            rebound_stat = stat_path(sealed_path)
            rebound_documents.append(DocumentRef(
                name=document_name,
                # Store the Windows extended-length spelling when needed so
                # provider subprocesses can open an otherwise valid >260-char
                # immutable CAS object without reintroducing mutable discovery.
                path=str(runtime_path(sealed_path)),
                content_sha256=expected_hash,
                file_size=int(rebound_stat.st_size),
                modified_ns=int(rebound_stat.st_mtime_ns),
                is_primary=document.is_primary,
                # Stage4 hydrates governed primary documents, not raw SEC
                # full-submission SGML. Never trust a transport-manifest flag
                # to switch provider routing for otherwise valid sealed bytes.
                is_full_submission=False,
                source_kind='stage4_sealed_cas',
            ))
        if (
            primary_count != 1
            or sum(doc.name == filing.primary_document for doc in documents) != 1
            or not any(
                doc.is_primary and doc.name == filing.primary_document
                for doc in documents
            )
        ):
            raise ValueError(
                'Consumer Defensive direct documents require exactly one '
                f'PIT primary document: {normalized_key[0]}/{normalized_key[1]}'
            )
        rebound[normalized_key] = tuple(rebound_documents)
    return rebound


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
    cache_dir: Path | None = None,
    expected_ingestion_config_sha256: str | None = None,
) -> tuple[
    list[str],
    dict[str, set[str]],
    int,
    dict[str, list[FilingRef]],
    tuple[dict[str, object], ...],
]:
    if registry.model_family == 'consumer_defensive' and direct_filings is not None:
        validate_consumer_defensive_catalog_contract(
            conn,asof_date=asof_date,
            expected_ingestion_config_sha256=expected_ingestion_config_sha256,
            cache_dir=cache_dir,
        )
        _validate_consumer_defensive_direct_filings(
            conn, direct_filings=direct_filings, asof_date=asof_date
        )
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
            cache_dir=cache_dir,
            expected_ingestion_config_sha256=expected_ingestion_config_sha256,
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
    expected_ingestion_config_sha256: str | None = None,
) -> PlanSummary:
    if registry.model_family == 'consumer_defensive':
        validate_consumer_defensive_catalog_contract(
            conn,asof_date=asof_date,
            expected_ingestion_config_sha256=expected_ingestion_config_sha256,
            cache_dir=cache_dir,
        )
        if direct_documents is None or direct_filings is None:
            raise RuntimeError(
                'Consumer Defensive cache audit requires paired immutable '
                'as-of direct_filings and direct_documents bindings'
            )
        direct_filing_keys = {
            (str(key[0]).upper(), str(key[1])) for key in direct_filings
        }
        direct_document_keys = {
            (str(key[0]).upper(), str(key[1])) for key in direct_documents
        }
        if not direct_filing_keys or direct_document_keys != direct_filing_keys:
            raise ValueError(
                'Consumer Defensive direct filing/document keysets must be '
                'nonempty and exact'
            )
        _validate_consumer_defensive_direct_filings(
            conn, direct_filings=direct_filings, asof_date=asof_date,
        )
        direct_documents = _validate_consumer_defensive_direct_documents(
            conn, direct_filings=direct_filings,
            direct_documents=direct_documents, asof_date=asof_date,
            cache_dir=cache_dir,
        )
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
        cache_dir=cache_dir,
        expected_ingestion_config_sha256=expected_ingestion_config_sha256,
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
    expected_ingestion_config_sha256: str | None = None,
) -> tuple[list[WorkItem], PlanSummary]:
    if registry.model_family == 'consumer_defensive':
        validate_consumer_defensive_catalog_contract(
            conn,asof_date=asof_date,
            expected_ingestion_config_sha256=expected_ingestion_config_sha256,
            cache_dir=cache_dir,
        )
    if registry.model_family == 'consumer_defensive' and (
        direct_documents is None or direct_filings is None
    ):
        raise RuntimeError(
            'Consumer Defensive dedicated-parser planning requires an immutable '
            'as-of paired direct_filings/direct_documents binding; mutable '
            'sec_archive_xbrl discovery is disabled; exact Stage 4 sealed '
            'bindings are required'
        )
    if registry.model_family == 'consumer_defensive' and direct_filings is not None:
        direct_filing_keys = {
            (str(key[0]).upper(), str(key[1])) for key in direct_filings
        }
        direct_document_keys = {
            (str(key[0]).upper(), str(key[1])) for key in direct_documents or {}
        }
        if not direct_filing_keys or direct_document_keys != direct_filing_keys:
            raise ValueError(
                'Consumer Defensive direct filing/document keysets differ: '
                f'filings_only={sorted(direct_filing_keys-direct_document_keys)} '
                f'documents_only={sorted(direct_document_keys-direct_filing_keys)}'
            )
        _validate_consumer_defensive_direct_filings(
            conn, direct_filings=direct_filings, asof_date=asof_date,
        )
        direct_documents = _validate_consumer_defensive_direct_documents(
            conn, direct_filings=direct_filings,
            direct_documents=direct_documents or {}, asof_date=asof_date,
            cache_dir=cache_dir,
        )
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
        cache_dir=cache_dir,
        expected_ingestion_config_sha256=expected_ingestion_config_sha256,
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
