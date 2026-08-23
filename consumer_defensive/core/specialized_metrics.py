from __future__ import annotations

import csv
import hashlib
import json
import math
import re
import sqlite3
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from statistics import median
from typing import Any, Iterable

from dedicated_parser.catalog import validate_consumer_defensive_catalog_contract
from dedicated_parser.contracts import file_sha256

from consumer_defensive.adapters.dedicated_parser_adapter import (
    ADAPTER_VERSION,
    policy_manifest,
)

from .atomic_io import atomic_text_writer
from .config import ConfigBundle, cfg_get, resolve_path
from .db import utc_now
from .financial_pipeline import build_financial_feature_bundle
from .financial_semantics import FinancialFact, select_safe_flow_value
from .market_data import write_csv, write_json
from .metric_registry import (
    ALLOWED_SOURCE_AVAILABILITY_CLASSES,
    SpecializedMetric,
    load_metric_registry,
)
from .scoring_features import (
    _specialized_applicable,
    component_observation_id,
    input_observation_id,
    bootstrap_stage6a,
)
from .source_registry import load_source_registry, upsert_source_registry
from .stage4 import build_financial_features
from .stage6b_schema import (
    STAGE6B_SCHEMA_VERSION,
    STAGE6B_V6_MIGRATION_SHA256,
    ensure_stage6b_schema,
)


MODEL_FAMILY = 'consumer_defensive'
SOURCE_ID = 'shared_dedicated_sec_parser'
DERIVED_SOURCE_ID = 'consumer_defensive_stage6b_specialized_measurement'
DEFINITION_VERSION = 'consumer_defensive_specialized_measurements_v1'
_HASH_RE = re.compile(r'^[0-9a-f]{64}$')
_ADVERTISING_FLOW_CONCEPTS = (
    'MarketingAndAdvertisingExpense',
    'AdvertisingExpense',
)
_EXCISE_TAX_CONCEPT = 'ExciseAndSalesTaxes'
_GROSS_REVENUE_INCLUDING_TAX_CONCEPT = (
    'RevenueFromContractWithCustomerIncludingAssessedTax'
)
_INTEREST_EXPENSE_CONCEPTS = (
    'InterestExpenseNonoperating',
    'InterestExpense',
    'InterestExpenseDebt',
)
_LEASE_COST_CONCEPTS = (
    'OperatingLeaseCost',
    'LeaseCost',
    'OperatingLeasesRentExpenseNet',
)
_LEASE_LIABILITY_TOTAL_CONCEPTS = ('OperatingLeaseLiability',)
_LEASE_LIABILITY_COMPONENT_CONCEPTS = (
    'OperatingLeaseLiabilityCurrent',
    'OperatingLeaseLiabilityNoncurrent',
)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode('utf-8')).hexdigest()


def _metric_registry(
    bundle: ConfigBundle,
) -> tuple[str, list[SpecializedMetric]]:
    path = resolve_path(
        cfg_get(bundle.payload, 'specialized_metrics.registry_path'),
        base_dir=bundle.base_dir,
    )
    return load_metric_registry(path)


def stage6b_policy_sha256() -> str:
    return _sha256(policy_manifest())


def bootstrap_stage6b(conn: sqlite3.Connection, bundle: ConfigBundle) -> str:
    ensure_stage6b_schema(conn)
    bootstrap_stage6a(conn, bundle)
    registry_path = resolve_path(
        cfg_get(bundle.payload, 'source_registry.path'),
        base_dir=bundle.base_dir,
    )
    required_ids = {SOURCE_ID, DERIVED_SOURCE_ID}
    sources = [
        row for row in load_source_registry(registry_path)
        if row.source_id in required_ids
    ]
    observed_ids = {row.source_id for row in sources}
    if observed_ids != required_ids:
        raise RuntimeError(
            'Stage 6B source registry is incomplete: '
            f'missing={sorted(required_ids - observed_ids)}'
        )
    if any(row.status != 'active' for row in sources):
        raise RuntimeError('Stage 6B parser and measurement sources must be active.')
    upsert_source_registry(conn, sources)

    manifest = policy_manifest()
    policy_sha = _sha256(manifest)
    now = utc_now()
    rows = []
    for metric_id, policy in sorted(manifest['metrics'].items()):
        metric_payload = {
            'adapter_version': manifest['adapter_version'],
            'metric_registry_version': manifest['metric_registry_version'],
            'term_registry_version': manifest['term_registry_version'],
            'metric_id': metric_id,
            **policy,
        }
        rows.append((
            metric_id,
            str(manifest['adapter_version']),
            str(manifest['metric_registry_version']),
            str(manifest['term_registry_version']),
            str(policy['unit_family']),
            str(policy['source_availability_class']),
            _canonical_json(policy['cohorts']),
            _canonical_json(policy['applicability_subtypes']),
            _canonical_json(policy['terms']),
            'measurement_only',
            0.0,
            _sha256(metric_payload),
            now,
        ))
    with conn:
        conn.execute('DELETE FROM stage6b_metric_policy')
        conn.executemany(
            '''INSERT INTO stage6b_metric_policy(
                   metric_id,adapter_version,registry_version,
                   term_registry_version,unit_family,source_availability_class,
                   cohorts_json,
                   applicability_subtypes_json,terms_json,production_status,
                   production_weight,policy_sha256,updated_at
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            rows,
        )
    return policy_sha


def _trusted_seal(
    conn: sqlite3.Connection,
    *,
    as_of: str,
    cache_dir: Path,
) -> dict[str, Any]:
    row = conn.execute(
        '''SELECT r.asof_date,r.cache_manifest_sha256,r.cache_manifest_json,
                  r.ingestion_config_sha256,r.issuer_scope_sha256,
                  s.seal_relative_path,r.scope_issuer_count,r.association_count
           FROM consumer_defensive_sec_reconciliation_state r
           JOIN consumer_defensive_sec_cache_snapshot s USING(asof_date)
           WHERE r.asof_date=? AND r.status='complete'
             AND r.trust_state='trusted_current'
             AND s.trust_state='trusted_current'
             AND r.scope_contract_version=3 AND s.scope_contract_version=3
             AND r.cache_manifest_sha256=s.cache_manifest_sha256
             AND r.cache_manifest_json=s.cache_manifest_json
             AND r.ingestion_config_sha256=s.ingestion_config_sha256
             AND r.issuer_scope_sha256=s.issuer_scope_sha256''',
        (as_of,),
    ).fetchone()
    if row is None:
        raise RuntimeError(
            f'Stage 6B requires an exact trusted Stage 4 SEC seal at {as_of}.'
        )
    result = dict(row)
    seal_root = (cache_dir / str(result['seal_relative_path'])).resolve()
    if not seal_root.is_dir():
        raise RuntimeError(
            'Stage 6B immutable SEC cache root is unavailable: '
            f'cache_dir={cache_dir.resolve()} expected_seal={seal_root}'
        )
    validate_consumer_defensive_catalog_contract(
        conn,
        asof_date=as_of,
        expected_ingestion_config_sha256=str(result['ingestion_config_sha256']),
        cache_dir=cache_dir,
    )
    return result


def _seal_entries(seal: dict[str, Any]) -> dict[str, dict[str, Any]]:
    try:
        raw_entries = json.loads(str(seal['cache_manifest_json']))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError('Stage 6B SEC seal manifest is invalid.') from exc
    if not isinstance(raw_entries, list) or not raw_entries:
        raise RuntimeError('Stage 6B SEC seal manifest is empty.')
    entries: dict[str, dict[str, Any]] = {}
    for raw in raw_entries:
        if not isinstance(raw, dict):
            raise RuntimeError('Stage 6B SEC seal entry is not an object.')
        logical = str(raw.get('logical_path') or '')
        digest = str(raw.get('sha256') or '').lower()
        object_path = str(raw.get('object_path') or '')
        byte_count = int(raw.get('bytes') or 0)
        if (
            not logical
            or logical in entries
            or not _HASH_RE.fullmatch(digest)
            or object_path != f'objects/sha256/{digest}'
            or byte_count < 0
        ):
            raise RuntimeError(f'Invalid Stage 6B SEC seal entry: {logical!r}')
        entries[logical] = {
            'logical_path': logical,
            'sha256': digest,
            'object_path': object_path,
            'bytes': byte_count,
        }
    return entries


def _historical_document_seal(
    conn: sqlite3.Connection,
    *,
    as_of: str,
    cache_dir: Path,
    ingestion_config_sha256: str,
    issuer_scope_sha256: str,
) -> dict[str, Any] | None:
    row = conn.execute(
        '''SELECT snapshot_run_id,manifest_sha256,manifest_json,
                  seal_relative_path,target_document_count,
                  hydrated_document_count,ingestion_config_sha256,
                  issuer_scope_sha256
           FROM stage6b_historical_document_snapshot_run
           WHERE asof_date=? AND status='PASS'
             AND ingestion_config_sha256=? AND issuer_scope_sha256=?
           ORDER BY snapshot_run_id DESC LIMIT 1''',
        (as_of, ingestion_config_sha256, issuer_scope_sha256),
    ).fetchone()
    if row is None:
        return None
    result = dict(row)
    if int(result['target_document_count']) != int(result['hydrated_document_count']):
        raise RuntimeError('Stage 6B historical document snapshot is incomplete.')
    entries = _seal_entries({'cache_manifest_json': result['manifest_json']})
    normalized = [entries[key] for key in sorted(entries)]
    encoded = json.dumps(
        normalized, sort_keys=True, separators=(',', ':'),
    ).encode('utf-8')
    if hashlib.sha256(encoded).hexdigest() != str(result['manifest_sha256']):
        raise RuntimeError('Stage 6B historical document manifest hash is invalid.')
    seal_root = (cache_dir / str(result['seal_relative_path'])).resolve()
    try:
        seal_root.relative_to(cache_dir.resolve())
    except ValueError as exc:
        raise RuntimeError('Stage 6B historical seal escapes the SEC cache root.') from exc
    if not seal_root.is_dir():
        raise RuntimeError('Stage 6B historical document seal is unavailable.')
    persisted = int(conn.execute(
        '''SELECT COUNT(*) FROM stage6b_historical_document_snapshot
           WHERE snapshot_run_id=?''',
        (int(result['snapshot_run_id']),),
    ).fetchone()[0])
    if persisted != int(result['hydrated_document_count']):
        raise RuntimeError('Stage 6B historical snapshot row count is invalid.')
    result['entries'] = entries
    result['seal_root'] = seal_root
    return result


def _event_document_seal(
    conn: sqlite3.Connection,
    *,
    as_of: str,
    cache_dir: Path,
    ingestion_config_sha256: str,
    issuer_scope_sha256: str,
) -> dict[str, Any] | None:
    table = conn.execute(
        '''SELECT 1 FROM sqlite_master WHERE type='table'
           AND name='stage6b_event_document_snapshot_run' '''
    ).fetchone()
    if table is None:
        return None
    row = conn.execute(
        '''SELECT event_snapshot_run_id,manifest_sha256,manifest_json,
                  seal_relative_path,target_filing_count,indexed_filing_count,
                  selected_document_count,ingestion_config_sha256,
                  issuer_scope_sha256
           FROM stage6b_event_document_snapshot_run
           WHERE asof_date=? AND status='PASS'
             AND ingestion_config_sha256=? AND issuer_scope_sha256=?
           ORDER BY event_snapshot_run_id DESC LIMIT 1''',
        (as_of, ingestion_config_sha256, issuer_scope_sha256),
    ).fetchone()
    if row is None:
        return None
    result = dict(row)
    if int(result['target_filing_count']) != int(result['indexed_filing_count']):
        raise RuntimeError('Stage 6B event document snapshot is incomplete.')
    entries = _seal_entries({'cache_manifest_json': result['manifest_json']})
    normalized = [entries[key] for key in sorted(entries)]
    encoded = json.dumps(
        normalized, sort_keys=True, separators=(',', ':'),
    ).encode('utf-8')
    if hashlib.sha256(encoded).hexdigest() != str(result['manifest_sha256']):
        raise RuntimeError('Stage 6B event document manifest hash is invalid.')
    seal_root = (cache_dir / str(result['seal_relative_path'])).resolve()
    try:
        seal_root.relative_to(cache_dir.resolve())
    except ValueError as exc:
        raise RuntimeError('Stage 6B event seal escapes the SEC cache root.') from exc
    if not seal_root.is_dir():
        raise RuntimeError('Stage 6B event document seal is unavailable.')
    persisted = int(conn.execute(
        '''SELECT COUNT(*) FROM stage6b_event_document_snapshot
           WHERE event_snapshot_run_id=?''',
        (int(result['event_snapshot_run_id']),),
    ).fetchone()[0])
    if persisted != int(result['selected_document_count']):
        raise RuntimeError('Stage 6B event snapshot row count is invalid.')
    result['entries'] = entries
    result['seal_root'] = seal_root
    return result


def _taxonomy(
    conn: sqlite3.Connection,
) -> dict[str, dict[str, str]]:
    return {
        str(row['ticker']): {
            'cohort_id': str(row['calibration_cohort_id']),
            'subtype': str(row['applicability_subtype'] or ''),
        }
        for row in conn.execute(
            '''SELECT ticker,calibration_cohort_id,applicability_subtype
               FROM dim_consumer_defensive_taxonomy
               WHERE model_family='consumer_defensive' ORDER BY ticker'''
        )
    }


def _applicable_metric_ids(
    metrics: Iterable[SpecializedMetric],
    *,
    cohort_id: str,
    subtype: str,
) -> list[str]:
    return sorted(
        metric.metric_id for metric in metrics
        if _specialized_applicable(
            metric, cohort_id=cohort_id, subtype=subtype
        )
    )


def build_stage6b_source_manifest(
    conn: sqlite3.Connection,
    bundle: ConfigBundle,
    *,
    as_of: str,
    cache_dir: Path,
    output_path: Path,
) -> dict[str, Any]:
    policy_sha = bootstrap_stage6b(conn, bundle)
    seal = _trusted_seal(conn, as_of=as_of, cache_dir=cache_dir)
    entries = _seal_entries(seal)
    registry_version, metrics = _metric_registry(bundle)
    taxonomy = _taxonomy(conn)
    if len(taxonomy) != int(
        cfg_get(bundle.payload, 'specialized_disclosure_census.expected_applicability_rows')
    ):
        raise RuntimeError('Stage 6B taxonomy does not match the reviewed 119-name scope.')
    cutoff = as_of + 'T23:59:59Z'
    seal_root = cache_dir / str(seal['seal_relative_path'])
    historical_seal = _historical_document_seal(
        conn,
        as_of=as_of,
        cache_dir=cache_dir,
        ingestion_config_sha256=str(seal['ingestion_config_sha256']),
        issuer_scope_sha256=str(seal['issuer_scope_sha256']),
    )
    event_seal = _event_document_seal(
        conn,
        as_of=as_of,
        cache_dir=cache_dir,
        ingestion_config_sha256=str(seal['ingestion_config_sha256']),
        issuer_scope_sha256=str(seal['issuer_scope_sha256']),
    )
    rows: list[dict[str, Any]] = []
    inventory_rows: list[tuple[Any, ...]] = []
    unsealed_documents = 0
    seen_keys: set[tuple[str, str, str]] = set()
    now = utc_now()
    current_rows = list(conn.execute(
        '''SELECT f.ticker,f.cik,f.archive_cik,f.accession_number,f.form_type,
                  f.filing_date,f.accepted_at,f.report_date,f.primary_document,
                  f.source_id,f.company_currency,f.issuer_company_id,
                  d.content_sha256,d.hydration_status,d.accepted_at AS doc_accepted_at
           FROM consumer_defensive_sec_parser_filing_input f
           JOIN bridge_sec_filing_document_company d
             ON d.accession_number=f.accession_number
            AND d.issuer_company_id=f.issuer_company_id
            AND d.primary_document=f.primary_document
           WHERE f.accepted_at<=? AND d.accepted_at<=?
             AND d.hydration_status='hydrated'
             AND (SELECT e.event_type
                  FROM sec_filing_company_association_event e
                  WHERE e.accession_number=f.accession_number
                    AND e.issuer_company_id=f.issuer_company_id
                    AND e.effective_asof<=?
                  ORDER BY e.effective_asof DESC,e.event_id DESC LIMIT 1)
                 IN ('observed','reactivated')
           ORDER BY f.ticker,f.accepted_at,f.accession_number''',
        (cutoff, cutoff, cutoff),
    ))
    candidates: dict[tuple[str, str, str], dict[str, Any]] = {}
    for current in current_rows:
        row = dict(current)
        cik = str(row['archive_cik'] or row['cik'] or '').zfill(10)
        logical = (
            f"filings/{cik}/{row['accession_number']}/{row['primary_document']}"
        )
        candidates[(
            str(row['ticker']), str(row['accession_number']),
            str(row['primary_document']).casefold(),
        )] = {
            'row': row,
            'entry': entries.get(logical),
            'seal_root': seal_root,
            'seal_manifest_sha256': str(seal['cache_manifest_sha256']),
            'source_kind': 'stage4_sealed_cas',
            'inventory_status': 'sealed_current_snapshot',
            'document_role': 'primary_filing',
            'sec_document_type': '',
            'document_sequence': '',
            'document_description': '',
            'content_type': Path(str(row['primary_document'])).suffix.casefold().lstrip('.'),
        }
    if historical_seal is not None:
        historical_entries = historical_seal['entries']
        for historical in conn.execute(
            '''SELECT h.ticker,h.archive_cik AS cik,h.archive_cik,
                      h.accession_number,h.form_type,h.filing_date,h.accepted_at,
                      h.report_date,h.document_name AS primary_document,
                      h.source_id,h.company_currency,f.issuer_company_id,
                      h.content_sha256,'hydrated' AS hydration_status,
                      h.accepted_at AS doc_accepted_at,h.logical_path
               FROM stage6b_historical_document_snapshot h
               JOIN consumer_defensive_sec_parser_filing_input f
                 ON f.ticker=h.ticker AND f.accession_number=h.accession_number
               WHERE h.snapshot_run_id=? AND h.accepted_at<=?
               ORDER BY h.ticker,h.accepted_at,h.accession_number''',
            (int(historical_seal['snapshot_run_id']), cutoff),
        ):
            row = dict(historical)
            logical = str(row.pop('logical_path'))
            entry = historical_entries.get(logical)
            if entry is None:
                raise RuntimeError(
                    f'Historical Stage 6B document is absent from its seal: {logical}'
                )
            key = (
                str(row['ticker']), str(row['accession_number']),
                str(row['primary_document']).casefold(),
            )
            current = candidates.get(key)
            if current is None or current['entry'] is None:
                candidates[key] = {
                    'row': row,
                    'entry': entry,
                    'seal_root': historical_seal['seal_root'],
                    'seal_manifest_sha256': str(historical_seal['manifest_sha256']),
                    'source_kind': 'stage6b_historical_sealed_cas',
                    'inventory_status': 'sealed_historical_snapshot',
                    'document_role': 'primary_filing',
                    'sec_document_type': '',
                    'document_sequence': '',
                    'document_description': '',
                    'content_type': Path(str(row['primary_document'])).suffix.casefold().lstrip('.'),
                }
    if event_seal is not None:
        event_entries = event_seal['entries']
        for event in conn.execute(
            '''SELECT e.ticker,e.archive_cik AS cik,e.archive_cik,
                      e.accession_number,e.form_type,e.filing_date,e.accepted_at,
                      e.report_date,f.primary_document,e.document_name,
                      e.source_id,e.company_currency,f.issuer_company_id,
                      e.content_sha256,'hydrated' AS hydration_status,
                      e.accepted_at AS doc_accepted_at,e.logical_path,
                      e.document_role,e.sec_document_type,e.document_sequence,
                      e.document_description,e.content_type
               FROM stage6b_event_document_snapshot e
               JOIN consumer_defensive_sec_parser_filing_input f
                 ON f.ticker=e.ticker AND f.accession_number=e.accession_number
               WHERE e.event_snapshot_run_id=? AND e.accepted_at<=?
               ORDER BY e.ticker,e.accepted_at,e.accession_number,
                        e.document_role,e.document_name''',
            (int(event_seal['event_snapshot_run_id']), cutoff),
        ):
            row = dict(event)
            logical = str(row.pop('logical_path'))
            entry = event_entries.get(logical)
            if entry is None:
                raise RuntimeError(
                    f'Event Stage 6B document is absent from its seal: {logical}'
                )
            document_name = str(row['document_name'])
            key = (
                str(row['ticker']), str(row['accession_number']),
                document_name.casefold(),
            )
            candidates[key] = {
                'row': row,
                'entry': entry,
                'seal_root': event_seal['seal_root'],
                'seal_manifest_sha256': str(event_seal['manifest_sha256']),
                'source_kind': 'stage6b_event_sealed_cas',
                'inventory_status': 'sealed_event_snapshot',
                'logical_path': logical,
                'document_role': str(row['document_role']),
                'sec_document_type': str(row['sec_document_type']),
                'document_sequence': str(row['document_sequence']),
                'document_description': str(row['document_description']),
                'content_type': str(row['content_type']),
            }
    for candidate in [candidates[key] for key in sorted(candidates)]:
        row = candidate['row']
        ticker = str(row['ticker'])
        member = taxonomy.get(ticker)
        if member is None:
            continue
        metric_ids = _applicable_metric_ids(
            metrics,
            cohort_id=member['cohort_id'],
            subtype=member['subtype'],
        )
        if not metric_ids:
            continue
        cik = str(row['archive_cik'] or row['cik'] or '').zfill(10)
        accession = str(row['accession_number'])
        document = str(row.get('document_name') or row['primary_document'])
        logical = str(candidate.get('logical_path') or (
            f'filings/{cik}/{accession}/{document}'
        ))
        entry = candidate['entry']
        expected_hash = str(row['content_sha256'] or '').lower()
        if entry is None:
            unsealed_documents += 1
            inventory_rows.append((
                as_of, ticker, accession, document, str(row['form_type']),
                str(candidate.get('document_role') or 'primary_filing'),
                str(candidate.get('sec_document_type') or ''),
                str(candidate.get('document_sequence') or ''),
                str(candidate.get('document_description') or ''),
                str(candidate.get('content_type') or Path(document).suffix.casefold().lstrip('.')),
                str(candidate['source_kind']),
                str(row['filing_date'] or ''), str(row['accepted_at']),
                str(row['report_date'] or ''), expected_hash, 0,
                str(candidate['seal_manifest_sha256']),
                str(seal['ingestion_config_sha256']),
                str(seal['issuer_scope_sha256']), _canonical_json(metric_ids),
                'active_hydrated_not_in_current_seal', now,
            ))
            continue
        if expected_hash != str(entry['sha256']):
            raise RuntimeError(
                f'Active Stage 6B document hash conflicts with seal: {logical}'
            )
        key = (ticker, accession, document.casefold())
        if key in seen_keys:
            raise RuntimeError(f'Duplicate Stage 6B document identity: {key!r}')
        seen_keys.add(key)
        local_path = (
            Path(candidate['seal_root']) / str(entry['object_path'])
        ).resolve()
        if not local_path.is_file() or file_sha256(local_path) != expected_hash:
            raise RuntimeError(f'Stage 6B sealed object is unavailable: {logical}')
        row_payload = {
            'ticker': ticker,
            'accession_number': accession,
            'document_name': document,
            'content_sha256': expected_hash,
            'cache_status': 'CACHED_HASHED',
            'local_path': str(local_path),
            'primary_document': str(row['primary_document']),
            'company_currency': str(row['company_currency'] or 'USD').upper(),
            'cik': cik,
            'form_type': str(row['form_type']),
            'filing_date': str(row['filing_date'] or ''),
            'accepted_at': str(row['accepted_at']),
            'report_date': str(row['report_date'] or row['filing_date'] or ''),
            'source_id': str(row['source_id']),
            'is_primary': (
                '1' if document == str(row['primary_document']) else '0'
            ),
            'is_full_submission': '0',
            'source_kind': str(candidate['source_kind']),
            'requested_metric_ids': '|'.join(metric_ids),
        }
        rows.append(row_payload)
        inventory_rows.append((
            as_of, ticker, accession, document, str(row['form_type']),
            str(candidate.get('document_role') or 'primary_filing'),
            str(candidate.get('sec_document_type') or ''),
            str(candidate.get('document_sequence') or ''),
            str(candidate.get('document_description') or ''),
            str(candidate.get('content_type') or Path(document).suffix.casefold().lstrip('.')),
            str(candidate['source_kind']),
            str(row['filing_date'] or ''), str(row['accepted_at']),
            str(row['report_date'] or ''), expected_hash, int(entry['bytes']),
            str(candidate['seal_manifest_sha256']),
            str(seal['ingestion_config_sha256']),
            str(seal['issuer_scope_sha256']), _canonical_json(metric_ids),
            str(candidate['inventory_status']), now,
        ))
    if not rows:
        raise RuntimeError('Stage 6B source manifest has no sealed documents.')
    fieldnames = list(rows[0])
    with atomic_text_writer(output_path, encoding='utf-8', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    manifest_sha = file_sha256(output_path)
    with conn:
        conn.execute('DELETE FROM stage6b_document_inventory WHERE asof_date=?', (as_of,))
        conn.executemany(
            '''INSERT INTO stage6b_document_inventory(
                   asof_date,ticker,accession_number,document_name,form_type,
                   document_role,sec_document_type,document_sequence,
                   document_description,content_type,source_kind,
                   filing_date,accepted_at,report_date,content_sha256,bytes,
                   seal_manifest_sha256,ingestion_config_sha256,
                   issuer_scope_sha256,requested_metrics_json,inventory_status,
                   created_at
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            inventory_rows,
        )
    return {
        'status': 'PASS',
        'asof_date': as_of,
        'adapter_version': ADAPTER_VERSION,
        'policy_sha256': policy_sha,
        'registry_version': registry_version,
        'source_manifest_path': str(output_path),
        'source_manifest_sha256': manifest_sha,
        'document_count': len(rows),
        'unsealed_document_count': unsealed_documents,
        'filing_count': len({(row['ticker'], row['accession_number']) for row in rows}),
        'ticker_count': len({row['ticker'] for row in rows}),
        'seal_manifest_sha256': str(seal['cache_manifest_sha256']),
        'ingestion_config_sha256': str(seal['ingestion_config_sha256']),
        'issuer_scope_sha256': str(seal['issuer_scope_sha256']),
    }


def _parser_run_id(
    conn: sqlite3.Connection,
    *,
    as_of: str,
    parser_run_id: int | None = None,
) -> int:
    row = conn.execute(
        '''SELECT run_id,status,failed_work_count
           FROM sec_parser_run
           WHERE run_id=COALESCE(?,run_id)
             AND model_family='consumer_defensive'
             AND asof_date=? AND adapter_version=?
           ORDER BY run_id DESC LIMIT 1''',
        (parser_run_id, as_of, ADAPTER_VERSION),
    ).fetchone()
    if (
        row is None
        or str(row['status']) != 'COMPLETED'
        or int(row['failed_work_count']) != 0
    ):
        raise RuntimeError(
            f'Stage 6B requires a completed zero-failure parser run at {as_of}.'
        )
    return int(row['run_id'])


def _observation_payload(row: dict[str, Any]) -> dict[str, Any]:
    fields = (
        'ticker', 'metric_id', 'period_start', 'period_end', 'accepted_at',
        'numeric_value', 'unit', 'definition_version', 'applicability_status',
        'evidence_status', 'evidence_key', 'source_id', 'source_document',
        'confidence', 'extraction_method', 'scope', 'lineage_json',
        'production_status', 'parser_run_id',
    )
    return {field: row.get(field) for field in fields}


def specialized_observation_sha256(row: dict[str, Any]) -> str:
    return _sha256(_observation_payload(row))


def _lineage_hashes(value: Any) -> set[str]:
    output: set[str] = set()
    if isinstance(value, str) and _HASH_RE.fullmatch(value):
        output.add(value)
    elif isinstance(value, dict):
        for item in value.values():
            output.update(_lineage_hashes(item))
    elif isinstance(value, list):
        for item in value:
            output.update(_lineage_hashes(item))
    return output


def _financial_accepted_at(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    lineage_json: str,
    period_end: str,
) -> str | None:
    try:
        lineage = json.loads(lineage_json or '{}')
    except json.JSONDecodeError:
        lineage = {}
    hashes = sorted(_lineage_hashes(lineage))
    if hashes:
        placeholders = ','.join('?' for _ in hashes)
        row = conn.execute(
            f'''SELECT MAX(accepted_at)
                FROM fact_financial_statement_canonical
                WHERE ticker=? AND source_observation_id IN ({placeholders})''',
            (ticker, *hashes),
        ).fetchone()
        if row is not None and row[0]:
            return str(row[0])
    row = conn.execute(
        '''SELECT MAX(accepted_at)
           FROM fact_financial_statement_canonical
           WHERE ticker=? AND period_end=?''',
        (ticker, period_end),
    ).fetchone()
    return str(row[0]) if row is not None and row[0] else None


def _parser_observations(
    conn: sqlite3.Connection,
    *,
    parser_run_id: int,
    as_of: str,
    metric_by_id: dict[str, SpecializedMetric],
    taxonomy: dict[str, dict[str, str]],
    minimum_confidence: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    cutoff = as_of + 'T23:59:59Z'
    grouped: dict[
        tuple[str, str, str, str, str, str], list[sqlite3.Row]
    ] = defaultdict(list)
    for row in conn.execute(
        '''SELECT e.*
           FROM sec_parser_run_metric_evidence r
           JOIN sec_parser_metric_evidence_shadow e
             ON e.evidence_key=r.evidence_key
           WHERE r.run_id=? AND e.model_family='consumer_defensive'
             AND e.candidate_status='ACCEPTED'
             AND e.candidate_value IS NOT NULL
             AND e.confidence>=?
             AND e.accepted_at<=?
             AND SUBSTR(e.period_end,1,10)<=?
           ORDER BY e.ticker,e.metric_name,e.period_end,e.accepted_at,
                    e.confidence DESC,e.evidence_key''',
        (parser_run_id, minimum_confidence, cutoff, as_of),
    ):
        ticker = str(row['ticker'])
        metric_id = str(row['metric_name'])
        member = taxonomy.get(ticker)
        metric = metric_by_id.get(metric_id)
        if member is None or metric is None or not _specialized_applicable(
            metric,
            cohort_id=member['cohort_id'],
            subtype=member['subtype'],
        ):
            continue
        grouped[(
            ticker,
            metric_id,
            str(row['period_start'] or ''),
            str(row['period_end']),
            str(row['scope'] or 'unknown'),
            str(row['unit'] or '').casefold(),
        )].append(row)

    candidates: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    for (
        ticker, metric_id, period_start, period_end, scope, unit_family
    ), rows in sorted(grouped.items()):
        amended = [
            row for row in rows
            if str(row['form_type']).upper().endswith('/A')
        ]
        base_forms = {
            str(row['form_type']).upper().removesuffix('/A') for row in rows
        }
        if amended and len(base_forms) == 1:
            rows = amended
        values = {
            round(float(row['candidate_value']), 8) for row in rows
        }
        if (
            len(values) > 1
            and max(values) - min(values) <= 0.51
            and (min(values) >= 0.0 or max(values) <= 0.0)
        ):
            def reported_precision(row: sqlite3.Row) -> int:
                try:
                    provenance = json.loads(str(row['provenance_json'] or '{}'))
                except (json.JSONDecodeError, TypeError):
                    provenance = {}
                raw = str(provenance.get('matched_numeric_text') or '')
                match = re.search(r'\d+\.(\d+)', raw)
                return len(match.group(1)) if match else 0

            precise = max(
                rows,
                key=lambda row: (
                    reported_precision(row),
                    str(row['accepted_at']),
                    float(row['confidence']),
                    str(row['evidence_key']),
                ),
            )
            rows = [precise]
            values = {round(float(precise['candidate_value']), 8)}
        if len(values) > 1:
            conflicts.append({
                'ticker': ticker,
                'metric_id': metric_id,
                'period_start': period_start,
                'period_end': period_end,
                'scope': scope,
                'unit': unit_family,
                'values': sorted(values),
                'evidence_keys': sorted(str(row['evidence_key']) for row in rows),
            })
            continue
        winner = max(
            rows,
            key=lambda row: (
                str(row['accepted_at']),
                float(row['confidence']),
                str(row['evidence_key']),
            ),
        )
        lineage = {
            'channel': 'dedicated_parser_shadow',
            'parser_run_id': parser_run_id,
            'work_key': str(winner['work_key']),
            'accession_number': str(winner['accession_number']),
            'form_type': str(winner['form_type']),
            'concept_name': str(winner['concept_name']),
            'status_reason': str(winner['status_reason'] or ''),
            'parser_release': str(winner['parser_release']),
            'adapter_version': str(winner['adapter_version']),
            'provenance': json.loads(str(winner['provenance_json'] or '{}')),
        }
        observation = {
            'ticker': ticker,
            'metric_id': metric_id,
            'period_start': period_start,
            'period_end': period_end,
            'accepted_at': str(winner['accepted_at']),
            'numeric_value': float(winner['candidate_value']),
            'unit': str(winner['unit'] or ''),
            'definition_version': DEFINITION_VERSION,
            'applicability_status': 'applicable',
            'evidence_status': 'accepted_measurement_only',
            'evidence_key': str(winner['evidence_key']),
            'source_id': SOURCE_ID,
            'source_document': str(winner['source_document'] or ''),
            'confidence': float(winner['confidence']),
            'extraction_method': str(winner['extraction_method']),
            'scope': str(winner['scope']),
            'lineage_json': _canonical_json(lineage),
            'production_status': 'measurement_only',
            'parser_run_id': parser_run_id,
        }
        observation['observation_sha256'] = specialized_observation_sha256(
            observation
        )
        candidates.append(observation)
    return candidates, conflicts


def _derived_observation(
    *,
    ticker: str,
    metric_id: str,
    value: float,
    unit: str,
    period_start: str,
    period_end: str,
    accepted_at: str,
    field: str,
    lineage: dict[str, Any],
    confidence: float,
    scope: str = 'consolidated',
) -> dict[str, Any]:
    evidence_key = _sha256({
        'ticker': ticker,
        'metric_id': metric_id,
        'period_start': period_start,
        'period_end': period_end,
        'accepted_at': accepted_at,
        'value': value,
        'unit': unit,
        'scope': scope,
        'lineage': lineage,
    })
    observation = {
        'ticker': ticker,
        'metric_id': metric_id,
        'period_start': period_start,
        'period_end': period_end,
        'accepted_at': accepted_at,
        'numeric_value': value,
        'unit': unit,
        'definition_version': DEFINITION_VERSION,
        'applicability_status': 'applicable',
        'evidence_status': 'accepted_measurement_only',
        'evidence_key': evidence_key,
        'source_id': DERIVED_SOURCE_ID,
        'source_document': field,
        'confidence': confidence,
        'extraction_method': f'stage6b:{field}',
        'scope': scope,
        'lineage_json': _canonical_json(lineage),
        'production_status': 'measurement_only',
        'parser_run_id': None,
    }
    observation['observation_sha256'] = specialized_observation_sha256(
        observation
    )
    return observation


def _derived_parser_growth_observations(
    parser_rows: Iterable[dict[str, Any]],
    *,
    metric_by_id: dict[str, SpecializedMetric],
    taxonomy: dict[str, dict[str, str]],
) -> list[dict[str, Any]]:
    '''Derive matched-scope price/unit growth only when direct KPI is absent.'''
    by_key = {
        (
            str(row['ticker']), str(row['metric_id']), str(row['period_end']),
            str(row['scope']), str(row['unit']).casefold(),
        ): row
        for row in parser_rows
    }
    output: list[dict[str, Any]] = []
    direct_targets = {
        (str(row['ticker']), str(row['metric_id']), str(row['period_end']), str(row['scope']))
        for row in parser_rows
    }
    base_keys = sorted({
        (ticker, period_end, scope)
        for ticker, metric_id, period_end, scope, unit in by_key
        if metric_id == 'organic_revenue_growth_pct' and unit == 'percent'
    })
    for ticker, period_end, scope in base_keys:
        revenue = by_key.get((
            ticker, 'organic_revenue_growth_pct', period_end, scope, 'percent'
        ))
        volume = by_key.get((
            ticker, 'volume_growth_pct', period_end, scope, 'percent'
        ))
        member = taxonomy.get(ticker)
        if revenue is None or volume is None or member is None:
            continue
        denominator = 1.0 + float(volume['numeric_value']) / 100.0
        if denominator <= 0.0:
            continue
        residual = (
            (1.0 + float(revenue['numeric_value']) / 100.0) / denominator
            - 1.0
        ) * 100.0
        accepted_at = max(str(revenue['accepted_at']), str(volume['accepted_at']))
        lineage = {
            'channel': 'stage6b_matched_scope_growth_derivation',
            'formula': '(1+organic_revenue_growth)/(1+volume_growth)-1',
            'organic_revenue_observation_sha256': revenue['observation_sha256'],
            'volume_observation_sha256': volume['observation_sha256'],
            'scope': scope,
        }
        for metric_id, field, confidence in (
            ('price_mix_growth_pct', 'organic_revenue_less_volume_residual', 0.88),
            ('revenue_per_unit_growth_pct', 'revenue_per_unit_growth', 0.90),
        ):
            metric = metric_by_id[metric_id]
            if (
                (ticker, metric_id, period_end, scope) in direct_targets
                or not _specialized_applicable(
                    metric,
                    cohort_id=member['cohort_id'],
                    subtype=member['subtype'],
                )
            ):
                continue
            output.append(_derived_observation(
                ticker=ticker,
                metric_id=metric_id,
                value=residual,
                unit='percent',
                period_start='',
                period_end=period_end,
                accepted_at=accepted_at,
                field=field,
                lineage=lineage,
                confidence=confidence,
                scope=scope,
            ))
    return output


def _average_fx_rate(
    conn: sqlite3.Connection,
    *,
    currency: str,
    period_start: str,
    period_end: str,
) -> float | None:
    if currency == 'USD':
        return 1.0
    row = conn.execute(
        '''SELECT AVG(rate),COUNT(*) FROM fact_fx_rate
           WHERE base_currency=? AND quote_currency='USD'
             AND quality_status='usable' AND rate_date BETWEEN ? AND ?''',
        (currency, period_start, period_end),
    ).fetchone()
    if row is None or row[0] is None or int(row[1] or 0) < 20:
        return None
    value = float(row[0])
    return value if math.isfinite(value) and value > 0.0 else None


def _advertising_ratio_observation(
    conn: sqlite3.Connection,
    *,
    as_of: str,
    feature_row: sqlite3.Row,
    member: dict[str, str],
    metric: SpecializedMetric,
) -> dict[str, Any] | None:
    if not _specialized_applicable(
        metric, cohort_id=member['cohort_id'], subtype=member['subtype']
    ):
        return None
    basis_end = str(feature_row['basis_period_end'] or '')
    if not basis_end:
        return None
    try:
        feature_lineage = json.loads(str(feature_row['lineage_json'] or '{}'))
    except json.JSONDecodeError:
        return None
    basis = feature_lineage.get('basis') if isinstance(feature_lineage, dict) else None
    if not isinstance(basis, dict):
        return None
    taxonomy_name = str(basis.get('taxonomy') or '')
    currency = str(basis.get('reported_currency') or '').upper()
    if not taxonomy_name or not currency:
        return None
    cutoff = as_of + 'T23:59:59Z'
    selections: list[tuple[FinancialFact | Any, str]] = []
    for concept in _ADVERTISING_FLOW_CONCEPTS:
        facts = [
            FinancialFact(
                metric='advertising_expense',
                value=float(row['numeric_value']),
                period_start=str(row['period_start'] or '') or None,
                period_end=str(row['period_end']),
                accepted_at=str(row['accepted_at']),
                accession_number=str(row['accession_number'] or ''),
                taxonomy=str(row['taxonomy'] or ''),
                currency=str(row['unit'] or '').upper(),
                concept=str(row['concept'] or ''),
                raw_fact_id=str(
                    row['source_observation_id'] or row['raw_fact_id'] or ''
                ),
            )
            for row in conn.execute(
                '''SELECT * FROM fact_sec_xbrl_fact_raw
                   WHERE ticker=? AND concept=? AND taxonomy=? AND unit=?
                     AND numeric_value IS NOT NULL AND period_start IS NOT NULL
                     AND dimensions_json='{}'
                     AND period_end<=? AND accepted_at<=?
                   ORDER BY period_end,period_start,accepted_at,raw_fact_id''',
                (
                    str(feature_row['ticker']), concept, taxonomy_name,
                    currency, basis_end, cutoff,
                ),
            )
        ]
        selection = select_safe_flow_value(facts, as_of=cutoff)
        if selection.selected is not None:
            selections.append((selection.selected, concept))
    if not selections:
        return None
    for selected, selected_concept in sorted(
        selections,
        key=lambda item: (
            str(item[0].period_end),
            str(item[0].period_start or ''),
            str(item[0].accepted_at or ''),
            item[1],
        ),
        reverse=True,
    ):
        period_start = str(selected.period_start or '')
        period_end = str(selected.period_end)
        if not period_start or not period_end:
            continue
        revenue = conn.execute(
            '''SELECT reported_value,accepted_at,source_observation_id,
                      accession_number,source_concept
               FROM fact_financial_statement_canonical
               WHERE ticker=? AND canonical_metric='revenue'
                 AND canonical_component='total'
                 AND period_start=? AND period_end=?
                 AND reported_currency=? AND accepted_at<=?
               ORDER BY accepted_at DESC,canonical_fact_id DESC
               LIMIT 1''',
            (
                str(feature_row['ticker']), period_start, period_end,
                currency, cutoff,
            ),
        ).fetchone()
        if revenue is None or revenue['reported_value'] is None:
            continue
        revenue_value = abs(float(revenue['reported_value']))
        if not math.isfinite(revenue_value) or revenue_value <= 0.0:
            continue
        ratio = abs(float(selected.value)) / revenue_value * 100.0
        if not math.isfinite(ratio) or not 0.0 <= ratio <= 100.0:
            continue
        selected_accepted = (
            selected.accepted_at.isoformat().replace('+00:00', 'Z')
            if hasattr(selected.accepted_at, 'isoformat')
            else str(selected.accepted_at or '')
        )
        accepted_at = max(selected_accepted, str(revenue['accepted_at'] or ''))
        if not accepted_at or accepted_at[:10] > as_of:
            continue
        lineage = {
            'channel': 'stage4_same_period_advertising_to_revenue_derivation',
            'formula': 'abs(advertising_expense)/abs(reported_revenue)*100',
            'advertising_concept': selected_concept,
            'advertising_basis': selected.basis,
            'advertising_lineage': list(selected.lineage),
            'revenue_concept': str(revenue['source_concept'] or ''),
            'revenue_source_observation_id': str(
                revenue['source_observation_id'] or ''
            ),
            'revenue_accession_number': str(
                revenue['accession_number'] or ''
            ),
            'reported_currency': currency,
            'reported_revenue': revenue_value,
            'financial_feature_lineage': feature_lineage,
        }
        return _derived_observation(
            ticker=str(feature_row['ticker']),
            metric_id='advertising_promotion_pct_sales',
            value=ratio,
            unit='percent',
            period_start=period_start,
            period_end=period_end,
            accepted_at=accepted_at,
            field=(
                f'fact_sec_xbrl_fact_raw:{selected_concept}/'
                'fact_financial_statement_canonical:revenue'
            ),
            lineage=lineage,
            confidence=0.99,
        )
    return None


def _gross_margin_change_observation(
    conn: sqlite3.Connection,
    *,
    as_of: str,
    feature_row: sqlite3.Row,
    member: dict[str, str],
    metric: SpecializedMetric,
) -> dict[str, Any] | None:
    if not _specialized_applicable(
        metric, cohort_id=member['cohort_id'], subtype=member['subtype']
    ):
        return None
    current_margin = feature_row['gross_margin']
    basis_end = str(feature_row['basis_period_end'] or '')
    if current_margin is None or not basis_end:
        return None
    current_end = date.fromisoformat(basis_end)
    prior_limit = (current_end - timedelta(days=300)).isoformat()
    canonical_rows = list(conn.execute(
        '''SELECT * FROM fact_financial_statement_canonical
           WHERE ticker=? AND accepted_at<=? AND period_end<=?
           ORDER BY period_end,accepted_at,canonical_fact_id''',
        (str(feature_row['ticker']), as_of + 'T23:59:59Z', prior_limit),
    ))
    if not canonical_rows:
        return None
    prior = build_financial_feature_bundle(
        canonical_rows,
        as_of=as_of,
        listing_start_date=None,
        listing_end_date=None,
        maximum_period_age_days=10_000,
    )
    prior_margin = prior.values.get('gross_margin')
    if prior_margin is None or not prior.basis_period_end:
        return None
    gap = (current_end - date.fromisoformat(prior.basis_period_end)).days
    if not 300 <= gap <= 430:
        return None
    value = (float(current_margin) - float(prior_margin)) * 10_000.0
    if not math.isfinite(value) or not -10_000.0 <= value <= 10_000.0:
        return None
    current_accepted = _financial_accepted_at(
        conn,
        ticker=str(feature_row['ticker']),
        lineage_json=str(feature_row['lineage_json'] or '{}'),
        period_end=basis_end,
    )
    prior_lineage_json = _canonical_json(prior.lineage)
    prior_accepted = _financial_accepted_at(
        conn,
        ticker=str(feature_row['ticker']),
        lineage_json=prior_lineage_json,
        period_end=prior.basis_period_end,
    )
    accepted_at = max(current_accepted, prior_accepted)
    if not accepted_at or accepted_at[:10] > as_of:
        return None
    lineage = {
        'channel': 'stage4_comparable_gross_margin_derivation',
        'formula': '(current_gross_margin-prior_gross_margin)*10000',
        'current_basis_period_end': basis_end,
        'prior_basis_period_end': prior.basis_period_end,
        'current_gross_margin': float(current_margin),
        'prior_gross_margin': float(prior_margin),
        'current_financial_feature_lineage': json.loads(
            str(feature_row['lineage_json'] or '{}')
        ),
        'prior_financial_feature_lineage': prior.lineage,
    }
    return _derived_observation(
        ticker=str(feature_row['ticker']),
        metric_id='gross_margin_change_bps',
        value=value,
        unit='basis_points',
        period_start=prior.basis_period_end,
        period_end=basis_end,
        accepted_at=accepted_at,
        field='feature_financial_statement:gross_margin_comparable_change',
        lineage=lineage,
        confidence=0.99,
    )


def _canonical_reported_value(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    metric: str,
    taxonomy_name: str,
    currency: str,
    period_start: str | None,
    period_end: str,
    cutoff: str,
) -> tuple[float, str, list[dict[str, Any]]] | None:
    period_clause = (
        'period_start IS NULL' if period_start is None else 'period_start=?'
    )
    params: list[Any] = [
        ticker, metric, taxonomy_name, currency, period_end, cutoff,
    ]
    if period_start is not None:
        params.insert(4, period_start)
    rows = list(conn.execute(
        f'''SELECT canonical_fact_id,canonical_component,reported_value,
                   accepted_at,source_observation_id,accession_number,
                   source_concept
            FROM fact_financial_statement_canonical
            WHERE ticker=? AND canonical_metric=? AND taxonomy=?
              AND reported_currency=? AND {period_clause} AND period_end=?
              AND accepted_at<=? AND reported_value IS NOT NULL
            ORDER BY accepted_at,canonical_fact_id''',
        params,
    ))
    if not rows:
        return None
    latest: dict[str, sqlite3.Row] = {}
    for row in rows:
        component = str(row['canonical_component'] or 'total')
        previous = latest.get(component)
        if previous is None or (
            str(row['accepted_at']), int(row['canonical_fact_id'])
        ) > (
            str(previous['accepted_at']), int(previous['canonical_fact_id'])
        ):
            latest[component] = row
    selected = [latest['total']] if 'total' in latest else list(latest.values())
    values = [float(row['reported_value']) for row in selected]
    if not values or any(not math.isfinite(value) for value in values):
        return None
    lineage = [
        {
            'canonical_component': str(row['canonical_component'] or 'total'),
            'source_concept': str(row['source_concept'] or ''),
            'source_observation_id': str(row['source_observation_id'] or ''),
            'accession_number': str(row['accession_number'] or ''),
            'accepted_at': str(row['accepted_at']),
        }
        for row in sorted(selected, key=lambda item: int(item['canonical_fact_id']))
    ]
    return sum(values), max(str(row['accepted_at']) for row in selected), lineage


def _raw_reported_value(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    concepts: tuple[str, ...],
    taxonomy_name: str,
    currency: str,
    period_start: str | None,
    period_end: str,
    cutoff: str,
) -> tuple[float, str, dict[str, Any]] | None:
    period_clause = (
        'period_start IS NULL' if period_start is None else 'period_start=?'
    )
    for concept in concepts:
        params: list[Any] = [
            ticker, concept, taxonomy_name, currency, period_end, cutoff,
        ]
        if period_start is not None:
            params.insert(4, period_start)
        rows = list(conn.execute(
            f'''SELECT raw_fact_id,numeric_value,accepted_at,
                       source_observation_id,accession_number,concept
                FROM fact_sec_xbrl_fact_raw
                WHERE ticker=? AND concept=? AND taxonomy=? AND unit=?
                  AND {period_clause} AND period_end=? AND accepted_at<=?
                  AND numeric_value IS NOT NULL AND dimensions_json='{{}}'
                ORDER BY accepted_at DESC,raw_fact_id DESC''',
            params,
        ))
        if not rows:
            continue
        latest_acceptance = str(rows[0]['accepted_at'])
        finalists = [
            row for row in rows if str(row['accepted_at']) == latest_acceptance
        ]
        values = {
            round(float(row['numeric_value']), 8) for row in finalists
            if math.isfinite(float(row['numeric_value']))
        }
        if len(values) != 1:
            continue
        winner = finalists[0]
        value = float(winner['numeric_value'])
        return value, latest_acceptance, {
            'concept': concept,
            'source_observation_id': str(
                winner['source_observation_id'] or winner['raw_fact_id']
            ),
            'accession_number': str(winner['accession_number'] or ''),
            'accepted_at': latest_acceptance,
        }
    return None


def _safe_raw_flow_value(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    concepts: tuple[str, ...],
    taxonomy_name: str,
    currency: str,
    period_start: str,
    period_end: str,
    cutoff: str,
) -> tuple[float, str, dict[str, Any]] | None:
    for concept in concepts:
        facts = [
            FinancialFact(
                metric=concept,
                value=float(row['numeric_value']),
                period_start=str(row['period_start'] or '') or None,
                period_end=str(row['period_end']),
                accepted_at=str(row['accepted_at']),
                accession_number=str(row['accession_number'] or ''),
                taxonomy=str(row['taxonomy'] or ''),
                currency=str(row['unit'] or '').upper(),
                concept=str(row['concept'] or ''),
                raw_fact_id=str(
                    row['source_observation_id'] or row['raw_fact_id'] or ''
                ),
            )
            for row in conn.execute(
                '''SELECT * FROM fact_sec_xbrl_fact_raw
                   WHERE ticker=? AND concept=? AND taxonomy=? AND unit=?
                     AND numeric_value IS NOT NULL AND period_start IS NOT NULL
                     AND dimensions_json='{}'
                     AND period_end<=? AND accepted_at<=?
                   ORDER BY period_end,period_start,accepted_at,raw_fact_id''',
                (
                    ticker, concept, taxonomy_name, currency, period_end, cutoff,
                ),
            )
        ]
        selection = select_safe_flow_value(facts, as_of=cutoff)
        selected = selection.selected
        if selected is None or (
            str(selected.period_start or '') != period_start
            or str(selected.period_end) != period_end
        ):
            continue
        accepted_at = (
            selected.accepted_at.isoformat().replace('+00:00', 'Z')
            if hasattr(selected.accepted_at, 'isoformat')
            else str(selected.accepted_at or '')
        )
        return float(selected.value), accepted_at, {
            'concept': concept,
            'basis': selected.basis,
            'source_observation_ids': list(selected.lineage),
            'accepted_at': accepted_at,
        }
    return None


def _operating_lease_liability(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    taxonomy_name: str,
    currency: str,
    period_end: str,
    cutoff: str,
) -> tuple[float, str, list[dict[str, Any]]] | None:
    total = _raw_reported_value(
        conn,
        ticker=ticker,
        concepts=_LEASE_LIABILITY_TOTAL_CONCEPTS,
        taxonomy_name=taxonomy_name,
        currency=currency,
        period_start=None,
        period_end=period_end,
        cutoff=cutoff,
    )
    if total is not None and float(total[0]) >= 0.0:
        return float(total[0]), str(total[1]), [dict(total[2])]
    components = []
    for concept in _LEASE_LIABILITY_COMPONENT_CONCEPTS:
        component = _raw_reported_value(
            conn,
            ticker=ticker,
            concepts=(concept,),
            taxonomy_name=taxonomy_name,
            currency=currency,
            period_start=None,
            period_end=period_end,
            cutoff=cutoff,
        )
        if component is None or float(component[0]) < 0.0:
            return None
        components.append(component)
    return (
        sum(float(component[0]) for component in components),
        max(str(component[1]) for component in components),
        [dict(component[2]) for component in components],
    )


def _retail_fixed_observations(
    conn: sqlite3.Connection,
    *,
    as_of: str,
    feature_row: sqlite3.Row,
    member: dict[str, str],
    metric_by_id: dict[str, SpecializedMetric],
    _basis_period: tuple[str, str] | None = None,
) -> list[dict[str, Any]]:
    requested = {
        metric_id: metric_by_id[metric_id]
        for metric_id in ('fixed_charge_coverage', 'lease_adjusted_net_leverage')
        if _specialized_applicable(
            metric_by_id[metric_id],
            cohort_id=member['cohort_id'],
            subtype=member['subtype'],
        )
    }
    if not requested:
        return []
    try:
        feature_lineage = json.loads(str(feature_row['lineage_json'] or '{}'))
    except json.JSONDecodeError:
        return []
    basis = feature_lineage.get('basis') if isinstance(feature_lineage, dict) else None
    if not isinstance(basis, dict):
        return []
    ticker = str(feature_row['ticker'])
    taxonomy_name = str(basis.get('taxonomy') or '')
    currency = str(basis.get('reported_currency') or '').upper()
    if not all((taxonomy_name, currency)):
        return []
    cutoff = as_of + 'T23:59:59Z'
    if _basis_period is None:
        annual_periods: set[tuple[str, str]] = set()
        placeholders = ','.join('?' for _ in _LEASE_COST_CONCEPTS)
        for row in conn.execute(
            f'''SELECT DISTINCT period_start,period_end
                FROM fact_sec_xbrl_fact_raw
                WHERE ticker=? AND concept IN ({placeholders})
                  AND taxonomy=? AND unit=? AND period_start IS NOT NULL
                  AND period_end<=? AND accepted_at<=?
                  AND numeric_value IS NOT NULL AND dimensions_json='{{}}'
                ORDER BY period_end DESC,period_start DESC''',
            (
                ticker, *_LEASE_COST_CONCEPTS, taxonomy_name, currency,
                as_of, cutoff,
            ),
        ):
            start = str(row['period_start'] or '')
            end = str(row['period_end'] or '')
            try:
                duration = (date.fromisoformat(end) - date.fromisoformat(start)).days
            except ValueError:
                continue
            if 330 <= duration <= 400:
                annual_periods.add((start, end))
        selected: dict[str, dict[str, Any]] = {}
        for period in sorted(annual_periods, key=lambda item: item[1], reverse=True):
            for observation in _retail_fixed_observations(
                conn,
                as_of=as_of,
                feature_row=feature_row,
                member=member,
                metric_by_id=metric_by_id,
                _basis_period=period,
            ):
                selected.setdefault(str(observation['metric_id']), observation)
            if set(selected) == set(requested):
                break
        return [selected[key] for key in sorted(selected)]
    period_start, period_end = _basis_period
    operating_income = _canonical_reported_value(
        conn, ticker=ticker, metric='operating_income',
        taxonomy_name=taxonomy_name, currency=currency,
        period_start=period_start, period_end=period_end, cutoff=cutoff,
    )
    depreciation = _canonical_reported_value(
        conn, ticker=ticker, metric='depreciation_amortization',
        taxonomy_name=taxonomy_name, currency=currency,
        period_start=period_start, period_end=period_end, cutoff=cutoff,
    )
    lease_cost = _safe_raw_flow_value(
        conn, ticker=ticker, concepts=_LEASE_COST_CONCEPTS,
        taxonomy_name=taxonomy_name, currency=currency,
        period_start=period_start, period_end=period_end, cutoff=cutoff,
    )
    interest = _safe_raw_flow_value(
        conn, ticker=ticker, concepts=_INTEREST_EXPENSE_CONCEPTS,
        taxonomy_name=taxonomy_name, currency=currency,
        period_start=period_start, period_end=period_end, cutoff=cutoff,
    )
    lease_liability = _operating_lease_liability(
        conn, ticker=ticker, taxonomy_name=taxonomy_name, currency=currency,
        period_end=period_end, cutoff=cutoff,
    )
    output: list[dict[str, Any]] = []
    if (
        'fixed_charge_coverage' in requested
        and operating_income is not None
        and lease_cost is not None
        and interest is not None
    ):
        fixed_cost = abs(float(lease_cost[0]))
        interest_cost = abs(float(interest[0]))
        denominator = fixed_cost + interest_cost
        numerator = float(operating_income[0]) + fixed_cost
        value = numerator / denominator if denominator > 0.0 else math.nan
        if math.isfinite(value) and -50.0 <= value <= 100.0:
            accepted_at = max(
                str(operating_income[1]), str(lease_cost[1]), str(interest[1])
            )
            output.append(_derived_observation(
                ticker=ticker,
                metric_id='fixed_charge_coverage',
                value=value,
                unit='ratio',
                period_start=period_start,
                period_end=period_end,
                accepted_at=accepted_at,
                field='stage4_financial_and_lease_facts:fixed_charge_coverage',
                lineage={
                    'channel': 'stage4_exact_period_fixed_charge_derivation',
                    'formula': '(operating_income+operating_lease_cost)/(interest_expense+operating_lease_cost)',
                    'reported_currency': currency,
                    'operating_income': operating_income[2],
                    'interest_expense': interest[2],
                    'operating_lease_cost': lease_cost[2],
                },
                confidence=0.99,
            ))
    if (
        'lease_adjusted_net_leverage' in requested
        and operating_income is not None
        and depreciation is not None
        and lease_liability is not None
    ):
        debt_current = _canonical_reported_value(
            conn, ticker=ticker, metric='debt_current',
            taxonomy_name=taxonomy_name, currency=currency,
            period_start=None, period_end=period_end, cutoff=cutoff,
        )
        debt_noncurrent = _canonical_reported_value(
            conn, ticker=ticker, metric='debt_noncurrent',
            taxonomy_name=taxonomy_name, currency=currency,
            period_start=None, period_end=period_end, cutoff=cutoff,
        )
        cash = _canonical_reported_value(
            conn, ticker=ticker, metric='cash',
            taxonomy_name=taxonomy_name, currency=currency,
            period_start=None, period_end=period_end, cutoff=cutoff,
        )
        if debt_current is not None and debt_noncurrent is not None and cash is not None:
            ebitda = float(operating_income[0]) + abs(float(depreciation[0]))
            numerator = (
                abs(float(debt_current[0]))
                + abs(float(debt_noncurrent[0]))
                + float(lease_liability[0])
                - abs(float(cash[0]))
            )
            value = numerator / ebitda if ebitda > 0.0 else math.nan
            if math.isfinite(value) and -10.0 <= value <= 50.0:
                accepted_at = max(
                    str(operating_income[1]), str(depreciation[1]),
                    str(debt_current[1]), str(debt_noncurrent[1]),
                    str(cash[1]), str(lease_liability[1]),
                )
                output.append(_derived_observation(
                    ticker=ticker,
                    metric_id='lease_adjusted_net_leverage',
                    value=value,
                    unit='ratio',
                    period_start=period_start,
                    period_end=period_end,
                    accepted_at=accepted_at,
                    field='stage4_financial_and_lease_facts:lease_adjusted_net_leverage',
                    lineage={
                        'channel': 'stage4_exact_period_lease_leverage_derivation',
                        'formula': '(debt_current+debt_noncurrent+operating_lease_liability-cash)/(operating_income+depreciation_amortization)',
                        'reported_currency': currency,
                        'operating_income': operating_income[2],
                        'depreciation_amortization': depreciation[2],
                        'debt_current': debt_current[2],
                        'debt_noncurrent': debt_noncurrent[2],
                        'cash': cash[2],
                        'operating_lease_liability': lease_liability[2],
                    },
                    confidence=0.99,
                ))
    return output


def _net_store_growth_observation(
    conn: sqlite3.Connection,
    *,
    as_of: str,
    ticker: str,
    member: dict[str, str],
    metric: SpecializedMetric,
) -> dict[str, Any] | None:
    if not _specialized_applicable(
        metric, cohort_id=member['cohort_id'], subtype=member['subtype']
    ):
        return None
    rows = list(conn.execute(
        '''SELECT raw_fact_id,source_observation_id,accession_number,taxonomy,
                  concept,unit,period_end,accepted_at,numeric_value
           FROM fact_sec_xbrl_fact_raw
           WHERE ticker=? AND concept='NumberOfStores'
             AND numeric_value IS NOT NULL AND period_start IS NULL
             AND dimensions_json='{}' AND period_end<=? AND accepted_at<=?
           ORDER BY period_end,accepted_at,raw_fact_id''',
        (ticker, as_of, as_of + 'T23:59:59Z'),
    ))
    main_units = {
        'store', 'stores', 'pure', 'warehouse_club', 'warehouse club',
        'retail_store', 'retail store',
    }
    by_period: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        value = float(row['numeric_value'])
        unit = str(row['unit'] or '').strip().lower()
        if (
            unit not in main_units
            or not math.isfinite(value)
            or value <= 0.0
        ):
            continue
        by_period[str(row['period_end'])].append(row)
    if len(by_period) < 2:
        return None

    def period_row(period_end: str) -> sqlite3.Row:
        return max(
            by_period[period_end],
            key=lambda row: (
                float(row['numeric_value']),
                str(row['accepted_at']),
                int(row['raw_fact_id']),
            ),
        )

    current_end = max(by_period)
    current_date = date.fromisoformat(current_end)
    prior_periods = [
        period_end
        for period_end in by_period
        if 300 <= (current_date - date.fromisoformat(period_end)).days <= 430
    ]
    if not prior_periods:
        return None
    prior_end = min(
        prior_periods,
        key=lambda period_end: (
            abs((current_date - date.fromisoformat(period_end)).days - 365),
            period_end,
        ),
    )
    current = period_row(current_end)
    prior = period_row(prior_end)
    current_value = float(current['numeric_value'])
    prior_value = float(prior['numeric_value'])
    if current_value <= 0.0 or prior_value <= 0.0:
        return None
    value = (current_value / prior_value - 1.0) * 100.0
    if not math.isfinite(value) or not -100.0 <= value <= 300.0:
        return None
    accepted_at = max(
        str(current['accepted_at'] or ''),
        str(prior['accepted_at'] or ''),
    )
    if not accepted_at or accepted_at[:10] > as_of:
        return None
    lineage = {
        'channel': 'stage4_number_of_stores_yoy_derivation',
        'formula': '(current_store_count/prior_year_store_count-1)*100',
        'current': {
            'period_end': current_end,
            'value': current_value,
            'unit': str(current['unit']),
            'source_observation_id': str(
                current['source_observation_id'] or current['raw_fact_id']
            ),
            'accession_number': str(current['accession_number'] or ''),
        },
        'prior': {
            'period_end': prior_end,
            'value': prior_value,
            'unit': str(prior['unit']),
            'source_observation_id': str(
                prior['source_observation_id'] or prior['raw_fact_id']
            ),
            'accession_number': str(prior['accession_number'] or ''),
        },
        'comparison_gap_days': (current_date - date.fromisoformat(prior_end)).days,
    }
    return _derived_observation(
        ticker=ticker,
        metric_id='net_store_growth_pct',
        value=value,
        unit='percent',
        period_start=prior_end,
        period_end=current_end,
        accepted_at=accepted_at,
        field='fact_sec_xbrl_fact_raw:NumberOfStores:yoy',
        lineage=lineage,
        confidence=0.99,
    )


def _excise_tax_burden_change_observation(
    conn: sqlite3.Connection,
    *,
    as_of: str,
    ticker: str,
    member: dict[str, str],
    metric: SpecializedMetric,
) -> dict[str, Any] | None:
    if not _specialized_applicable(
        metric, cohort_id=member['cohort_id'], subtype=member['subtype']
    ):
        return None
    rows = list(conn.execute(
        '''SELECT raw_fact_id,source_observation_id,accession_number,taxonomy,
                  concept,unit,period_start,period_end,accepted_at,numeric_value
           FROM fact_sec_xbrl_fact_raw
           WHERE ticker=? AND taxonomy='us-gaap'
             AND concept IN (?,?)
             AND numeric_value IS NOT NULL AND period_start IS NOT NULL
             AND dimensions_json='{}' AND period_end<=? AND accepted_at<=?
           ORDER BY accepted_at,accession_number,period_end,concept,raw_fact_id''',
        (
            ticker,
            _EXCISE_TAX_CONCEPT,
            _GROSS_REVENUE_INCLUDING_TAX_CONCEPT,
            as_of,
            as_of + 'T23:59:59Z',
        ),
    ))
    grouped: dict[
        tuple[str, str, str, str],
        dict[tuple[str, str], dict[str, sqlite3.Row]],
    ] = defaultdict(lambda: defaultdict(dict))
    for row in rows:
        period_start = str(row['period_start'] or '')
        period_end = str(row['period_end'] or '')
        if not period_start or not period_end:
            continue
        try:
            duration = (
                date.fromisoformat(period_end) - date.fromisoformat(period_start)
            ).days
        except ValueError:
            continue
        if not 330 <= duration <= 400:
            continue
        key = (
            str(row['accession_number'] or ''),
            str(row['accepted_at'] or ''),
            str(row['taxonomy'] or ''),
            str(row['unit'] or '').upper(),
        )
        period = (period_start, period_end)
        concept = str(row['concept'])
        previous = grouped[key][period].get(concept)
        if previous is None or int(row['raw_fact_id']) > int(previous['raw_fact_id']):
            grouped[key][period][concept] = row
    candidates: list[tuple[tuple[str, str, str], dict[str, Any]]] = []
    for key, periods in grouped.items():
        paired = [
            (period, facts)
            for period, facts in periods.items()
            if _EXCISE_TAX_CONCEPT in facts
            and _GROSS_REVENUE_INCLUDING_TAX_CONCEPT in facts
        ]
        for current_period, current_facts in sorted(
            paired, key=lambda item: item[0][1], reverse=True
        ):
            current_end = date.fromisoformat(current_period[1])
            prior_choices = [
                (period, facts)
                for period, facts in paired
                if 300 <= (
                    current_end - date.fromisoformat(period[1])
                ).days <= 430
            ]
            if not prior_choices:
                continue
            prior_period, prior_facts = min(
                prior_choices,
                key=lambda item: (
                    abs(
                        (
                            current_end - date.fromisoformat(item[0][1])
                        ).days - 365
                    ),
                    item[0][1],
                ),
            )
            current_excise = abs(float(
                current_facts[_EXCISE_TAX_CONCEPT]['numeric_value']
            ))
            current_gross = abs(float(
                current_facts[
                    _GROSS_REVENUE_INCLUDING_TAX_CONCEPT
                ]['numeric_value']
            ))
            prior_excise = abs(float(
                prior_facts[_EXCISE_TAX_CONCEPT]['numeric_value']
            ))
            prior_gross = abs(float(
                prior_facts[
                    _GROSS_REVENUE_INCLUDING_TAX_CONCEPT
                ]['numeric_value']
            ))
            if (
                min(current_gross, prior_gross) <= 0.0
                or current_excise > current_gross
                or prior_excise > prior_gross
            ):
                continue
            value = (
                current_excise / current_gross
                - prior_excise / prior_gross
            ) * 10_000.0
            if not math.isfinite(value) or not -10_000.0 <= value <= 10_000.0:
                continue
            accepted_at = key[1]
            lineage = {
                'channel': 'stage4_same_period_excise_burden_derivation',
                'formula': (
                    '(current_excise/current_gross_revenue-'
                    'prior_excise/prior_gross_revenue)*10000'
                ),
                'reported_currency': key[3],
                'current': {
                    'period_start': current_period[0],
                    'period_end': current_period[1],
                    'excise_tax': current_excise,
                    'gross_revenue_including_tax': current_gross,
                    'excise_source_observation_id': str(
                        current_facts[_EXCISE_TAX_CONCEPT][
                            'source_observation_id'
                        ]
                        or current_facts[_EXCISE_TAX_CONCEPT]['raw_fact_id']
                    ),
                    'revenue_source_observation_id': str(
                        current_facts[
                            _GROSS_REVENUE_INCLUDING_TAX_CONCEPT
                        ]['source_observation_id']
                        or current_facts[
                            _GROSS_REVENUE_INCLUDING_TAX_CONCEPT
                        ]['raw_fact_id']
                    ),
                },
                'prior': {
                    'period_start': prior_period[0],
                    'period_end': prior_period[1],
                    'excise_tax': prior_excise,
                    'gross_revenue_including_tax': prior_gross,
                    'excise_source_observation_id': str(
                        prior_facts[_EXCISE_TAX_CONCEPT][
                            'source_observation_id'
                        ]
                        or prior_facts[_EXCISE_TAX_CONCEPT]['raw_fact_id']
                    ),
                    'revenue_source_observation_id': str(
                        prior_facts[
                            _GROSS_REVENUE_INCLUDING_TAX_CONCEPT
                        ]['source_observation_id']
                        or prior_facts[
                            _GROSS_REVENUE_INCLUDING_TAX_CONCEPT
                        ]['raw_fact_id']
                    ),
                },
                'accession_number': key[0],
            }
            candidates.append((
                (current_period[1], accepted_at, key[0]),
                _derived_observation(
                    ticker=ticker,
                    metric_id='excise_tax_impact_bps',
                    value=value,
                    unit='basis_points',
                    period_start=prior_period[1],
                    period_end=current_period[1],
                    accepted_at=accepted_at,
                    field=(
                        'fact_sec_xbrl_fact_raw:'
                        'ExciseAndSalesTaxes/'
                        'RevenueFromContractWithCustomerIncludingAssessedTax'
                    ),
                    lineage=lineage,
                    confidence=0.99,
                ),
            ))
            break
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def _derived_financial_observations(
    conn: sqlite3.Connection,
    *,
    as_of: str,
    metric_by_id: dict[str, SpecializedMetric],
    taxonomy: dict[str, dict[str, str]],
    history_start: str = '2019-01-02',
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for row in conn.execute(
        '''SELECT ticker,source_id,basis_period_end,lineage_json,
                  financial_quality_status,revenue_ttm_usd,gross_margin,
                  inventory_turnover,net_debt_to_ebitda
           FROM feature_financial_statement
           WHERE model_family='consumer_defensive' AND asof_date=?
             AND financial_quality_status IN ('complete','partial')
           ORDER BY ticker''',
        (as_of,),
    ):
        ticker = str(row['ticker'])
        member = taxonomy.get(ticker)
        if member is None:
            continue
        for derived in (
            _advertising_ratio_observation(
                conn,
                as_of=as_of,
                feature_row=row,
                member=member,
                metric=metric_by_id['advertising_promotion_pct_sales'],
            ),
            _gross_margin_change_observation(
                conn,
                as_of=as_of,
                feature_row=row,
                member=member,
                metric=metric_by_id['gross_margin_change_bps'],
            ),
        ):
            if derived is not None:
                output.append(derived)
        output.extend(_retail_fixed_observations(
            conn,
            as_of=as_of,
            feature_row=row,
            member=member,
            metric_by_id=metric_by_id,
        ))
        for metric_id, field in (
            ('inventory_turnover', 'inventory_turnover'),
            ('net_debt_to_ebitda', 'net_debt_to_ebitda'),
        ):
            value = row[field]
            metric = metric_by_id[metric_id]
            if (
                value is None
                or not math.isfinite(float(value))
                or not _specialized_applicable(
                    metric,
                    cohort_id=member['cohort_id'],
                    subtype=member['subtype'],
                )
            ):
                continue
            period_end = str(row['basis_period_end'] or '')
            accepted_at = _financial_accepted_at(
                conn,
                ticker=ticker,
                lineage_json=str(row['lineage_json'] or '{}'),
                period_end=period_end,
            )
            if not period_end or not accepted_at or accepted_at[:10] > as_of:
                continue
            lineage = {
                'channel': 'stage4_financial_feature_derivation',
                'upstream_table': 'feature_financial_statement',
                'upstream_source_id': str(row['source_id']),
                'upstream_asof_date': as_of,
                'upstream_quality_status': str(row['financial_quality_status']),
                'upstream_lineage': json.loads(str(row['lineage_json'] or '{}')),
            }
            output.append(_derived_observation(
                ticker=ticker,
                metric_id=metric_id,
                value=float(value),
                unit='ratio',
                period_start='',
                period_end=period_end,
                accepted_at=accepted_at,
                field=f'stage4_financial_feature:{field}',
                lineage=lineage,
                confidence=0.99,
            ))
    for ticker, member in sorted(taxonomy.items()):
        for derived in (
            _net_store_growth_observation(
                conn,
                as_of=as_of,
                ticker=ticker,
                member=member,
                metric=metric_by_id['net_store_growth_pct'],
            ),
            _excise_tax_burden_change_observation(
                conn,
                as_of=as_of,
                ticker=ticker,
                member=member,
                metric=metric_by_id['excise_tax_impact_bps'],
            ),
        ):
            if derived is not None:
                output.append(derived)
    output.extend(_historical_financial_observations(
        conn,
        as_of=as_of,
        history_start=history_start,
        metric_by_id=metric_by_id,
        taxonomy=taxonomy,
    ))
    return list({
        str(row['observation_sha256']): row for row in output
    }.values())


def _historical_financial_observations(
    conn: sqlite3.Connection,
    *,
    as_of: str,
    history_start: str,
    metric_by_id: dict[str, SpecializedMetric],
    taxonomy: dict[str, dict[str, str]],
) -> list[dict[str, Any]]:
    """Derive filing-by-filing financial metrics without carrying current data back.

    Each snapshot is rebuilt only from canonical facts accepted by that filing
    date.  Period, taxonomy, currency, numerator/denominator lineage, and the
    latest contributing acceptance timestamp remain bound in the observation.
    """

    date.fromisoformat(history_start)
    date.fromisoformat(as_of)
    output: list[dict[str, Any]] = []
    for ticker, member in sorted(taxonomy.items()):
        canonical = [dict(row) for row in conn.execute(
            '''SELECT * FROM fact_financial_statement_canonical
               WHERE ticker=? AND accepted_at<=?
               ORDER BY accepted_at,canonical_fact_id''',
            (ticker, as_of + 'T23:59:59Z'),
        )]
        if not canonical:
            continue
        snapshot_dates = sorted({
            str(row['accepted_at'])[:10]
            for row in canonical
            if str(row['canonical_metric']) == 'revenue'
            and history_start <= str(row['accepted_at'])[:10] <= as_of
        })
        seen_snapshot: set[tuple[str, str, float]] = set()
        for snapshot_date in snapshot_dates:
            snapshot_rows = [
                row for row in canonical
                if str(row['accepted_at']) <= snapshot_date + 'T23:59:59Z'
            ]
            feature = build_financial_feature_bundle(
                snapshot_rows,
                as_of=snapshot_date,
                listing_start_date=None,
                listing_end_date=None,
                maximum_period_age_days=550,
            )
            period_end = str(feature.basis_period_end or '')
            if (
                not period_end
                or feature.quality_status not in {'complete', 'partial'}
            ):
                continue
            feature_row: dict[str, Any] = {
                'ticker': ticker,
                'source_id': 'sec_companyfacts',
                'basis_period_end': period_end,
                'lineage_json': _canonical_json(feature.lineage),
                'financial_quality_status': feature.quality_status,
                **feature.values,
            }
            for derived in (
                _advertising_ratio_observation(
                    conn,
                    as_of=snapshot_date,
                    feature_row=feature_row,
                    member=member,
                    metric=metric_by_id['advertising_promotion_pct_sales'],
                ),
                _gross_margin_change_observation(
                    conn,
                    as_of=snapshot_date,
                    feature_row=feature_row,
                    member=member,
                    metric=metric_by_id['gross_margin_change_bps'],
                ),
            ):
                if derived is not None:
                    output.append(derived)
            output.extend(_retail_fixed_observations(
                conn,
                as_of=snapshot_date,
                feature_row=feature_row,
                member=member,
                metric_by_id=metric_by_id,
            ))
            for metric_id, field in (
                ('inventory_turnover', 'inventory_turnover'),
                ('net_debt_to_ebitda', 'net_debt_to_ebitda'),
            ):
                metric = metric_by_id[metric_id]
                value = feature.values.get(field)
                if (
                    value is None
                    or not math.isfinite(float(value))
                    or not _specialized_applicable(
                        metric,
                        cohort_id=member['cohort_id'],
                        subtype=member['subtype'],
                    )
                ):
                    continue
                identity = (metric_id, period_end, round(float(value), 12))
                if identity in seen_snapshot:
                    continue
                accepted_at = _financial_accepted_at(
                    conn,
                    ticker=ticker,
                    lineage_json=feature_row['lineage_json'],
                    period_end=period_end,
                )
                if not accepted_at or accepted_at[:10] > snapshot_date:
                    continue
                seen_snapshot.add(identity)
                output.append(_derived_observation(
                    ticker=ticker,
                    metric_id=metric_id,
                    value=float(value),
                    unit='ratio',
                    period_start='',
                    period_end=period_end,
                    accepted_at=accepted_at,
                    field=f'historical_stage4_financial_feature:{field}',
                    lineage={
                        'channel': (
                            'stage4_historical_pit_financial_feature_derivation'
                        ),
                        'snapshot_date': snapshot_date,
                        'upstream_quality_status': feature.quality_status,
                        'upstream_quality_reasons': list(feature.quality_reasons),
                        'upstream_lineage': feature.lineage,
                    },
                    confidence=0.99,
                ))
    return output


def _refresh_stage6b_financial_snapshot(
    conn: sqlite3.Connection,
    bundle: ConfigBundle,
    *,
    as_of: str,
) -> dict[str, Any]:
    """Build and verify the exact Stage 4 financial prerequisite.

    Stage 6B derivations must not silently run against an absent or stale
    financial snapshot. Rebuilding is deterministic from the already loaded
    SEC and FX facts and makes the dependency explicit for every measurement
    run.
    """
    result = build_financial_features(conn, bundle, as_of=as_of)
    expected = int(conn.execute(
        '''SELECT COUNT(*) FROM dim_consumer_defensive_taxonomy
           WHERE model_family=?''',
        (MODEL_FAMILY,),
    ).fetchone()[0])
    observed = int(conn.execute(
        '''SELECT COUNT(*) FROM feature_financial_statement
           WHERE model_family=? AND asof_date=?''',
        (MODEL_FAMILY, as_of),
    ).fetchone()[0])
    fx_missing = int(conn.execute(
        '''SELECT COUNT(*) FROM fact_financial_statement_canonical
           WHERE definition_version=? AND quality_status='fx_missing'
             AND accepted_at<=?''',
        (str(result['definition_version']), as_of + 'T23:59:59Z'),
    ).fetchone()[0])
    if int(result['canonical_facts']) <= 0:
        raise RuntimeError(
            'Stage 6B requires canonical financial facts; run the Stage 4 '
            'SEC fundamentals sync first.'
        )
    if observed != expected or int(result['feature_rows']) != expected:
        raise RuntimeError(
            'Stage 6B financial prerequisite is incomplete: '
            f'expected={expected}, observed={observed}.'
        )
    if fx_missing:
        raise RuntimeError(
            'Stage 6B financial prerequisite has missing FX conversions: '
            f'{fx_missing}. Run Stage 4 FX sync through the requested cutoff.'
        )
    return {
        'canonical_fact_count': int(result['canonical_facts']),
        'feature_row_count': observed,
        'definition_version': str(result['definition_version']),
        'feature_definition_version': str(result['feature_definition_version']),
        'canonical_fx_missing': fx_missing,
        'feature_quality_counts': dict(result['feature_quality_counts']),
    }


def _insert_observations(
    conn: sqlite3.Connection,
    observations: list[dict[str, Any]],
) -> int:
    now = utc_now()
    with conn:
        conn.executemany(
            '''INSERT INTO fact_specialized_metric_observation(
                   ticker,metric_id,period_start,period_end,accepted_at,
                   numeric_value,unit,definition_version,applicability_status,
                   evidence_status,evidence_key,source_id,source_document,
                   created_at,confidence,extraction_method,scope,lineage_json,
                   observation_sha256,production_status,parser_run_id
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT DO NOTHING''',
            [(
                row['ticker'], row['metric_id'], row['period_start'],
                row['period_end'], row['accepted_at'], row['numeric_value'],
                row['unit'], row['definition_version'],
                row['applicability_status'], row['evidence_status'],
                row['evidence_key'], row['source_id'], row['source_document'],
                now, row['confidence'], row['extraction_method'], row['scope'],
                row['lineage_json'], row['observation_sha256'],
                row['production_status'], row['parser_run_id'],
            ) for row in observations],
        )
    return len(observations)


def promote_stage6b_measurements(
    conn: sqlite3.Connection,
    bundle: ConfigBundle,
    *,
    as_of: str,
    source_manifest_path: Path,
    parser_run_id: int | None = None,
    minimum_confidence: float = 0.85,
) -> dict[str, Any]:
    policy_sha = bootstrap_stage6b(conn, bundle)
    parser_run_id = _parser_run_id(
        conn, as_of=as_of, parser_run_id=parser_run_id
    )
    manifest_sha = file_sha256(source_manifest_path)
    seal = conn.execute(
        '''SELECT cache_manifest_sha256,ingestion_config_sha256,
                  issuer_scope_sha256
           FROM consumer_defensive_sec_reconciliation_state
           WHERE asof_date=? AND status='complete'
             AND trust_state='trusted_current' ''',
        (as_of,),
    ).fetchone()
    if seal is None:
        raise RuntimeError('Stage 6B promotion requires the current trusted seal.')
    _, metrics = _metric_registry(bundle)
    metric_by_id = {metric.metric_id: metric for metric in metrics}
    taxonomy = _taxonomy(conn)
    financial_dependency = _refresh_stage6b_financial_snapshot(
        conn, bundle, as_of=as_of
    )
    parser_rows, conflicts = _parser_observations(
        conn,
        parser_run_id=parser_run_id,
        as_of=as_of,
        metric_by_id=metric_by_id,
        taxonomy=taxonomy,
        minimum_confidence=minimum_confidence,
    )
    derived_rows = _derived_financial_observations(
        conn,
        as_of=as_of,
        metric_by_id=metric_by_id,
        taxonomy=taxonomy,
        history_start=str(
            cfg_get(bundle.payload, 'stage6b.historical_inventory_start')
        ),
    )
    derived_rows.extend(_derived_parser_growth_observations(
        parser_rows,
        metric_by_id=metric_by_id,
        taxonomy=taxonomy,
    ))
    observations = parser_rows + derived_rows
    observation_hashes = sorted({
        str(row['observation_sha256']) for row in observations
    })
    if (
        len(observation_hashes) != len(observations)
        or any(not _HASH_RE.fullmatch(value) for value in observation_hashes)
    ):
        raise RuntimeError(
            'Stage 6B run produced duplicate or invalid observation identities.'
        )
    _insert_observations(conn, observations)
    now = utc_now()
    with conn:
        conn.execute(
            '''INSERT INTO stage6b_specialized_run(
                   asof_date,parser_run_id,adapter_version,policy_sha256,
                   source_manifest_sha256,seal_manifest_sha256,
                   ingestion_config_sha256,issuer_scope_sha256,started_at,
                   completed_at,status,inventory_document_count,
                   accepted_observation_count,metadata_json
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(asof_date,adapter_version,source_manifest_sha256)
               DO UPDATE SET
                   parser_run_id=excluded.parser_run_id,
                   policy_sha256=excluded.policy_sha256,
                   seal_manifest_sha256=excluded.seal_manifest_sha256,
                   ingestion_config_sha256=excluded.ingestion_config_sha256,
                   issuer_scope_sha256=excluded.issuer_scope_sha256,
                   completed_at=excluded.completed_at,
                   status=excluded.status,
                   inventory_document_count=excluded.inventory_document_count,
                   accepted_observation_count=excluded.accepted_observation_count,
                   metadata_json=excluded.metadata_json''',
            (
                as_of, parser_run_id, ADAPTER_VERSION, policy_sha,
                manifest_sha, str(seal[0]), str(seal[1]), str(seal[2]),
                now, now, 'measurement_only_complete',
                int(conn.execute(
                    'SELECT COUNT(*) FROM stage6b_document_inventory WHERE asof_date=?',
                    (as_of,),
                ).fetchone()[0]),
                len(observations),
                _canonical_json({
                    'minimum_confidence': minimum_confidence,
                    'parser_observations': len(parser_rows),
                    'derived_financial_observations': len(derived_rows),
                    'financial_dependency': financial_dependency,
                    'observation_sha256s': observation_hashes,
                    'conflicts': conflicts,
                    'production_weight': 0.0,
                }),
            ),
        )
        run_id = int(conn.execute(
            '''SELECT stage6b_run_id FROM stage6b_specialized_run
               WHERE asof_date=? AND adapter_version=?
                 AND source_manifest_sha256=?''',
            (as_of, ADAPTER_VERSION, manifest_sha),
        ).fetchone()[0])
    return {
        'status': 'PASS',
        'stage6b_run_id': run_id,
        'parser_run_id': parser_run_id,
        'asof_date': as_of,
        'parser_observation_count': len(parser_rows),
        'derived_financial_observation_count': len(derived_rows),
        'financial_dependency': financial_dependency,
        'accepted_observation_count': len(observations),
        'conflict_count': len(conflicts),
        'conflicts': conflicts,
        'production_weight': 0.0,
    }


def _coverage_tier(coverage: float) -> tuple[str, str]:
    if coverage >= 0.70:
        return (
            'broad',
            'retain_measurement_only_and_extend_point_in_time_history',
        )
    if coverage >= 0.40:
        return (
            'moderate',
            'hydrate_more_earnings_documents_and_expand_reviewed_examples',
        )
    if coverage >= 0.15:
        return (
            'limited',
            'target_metric_specific_exhibits_tables_and_definition_review',
        )
    if coverage > 0.0:
        return (
            'sparse',
            'add_targeted_terms_and_alternate_primary_disclosure_channels',
        )
    return (
        'none',
        'evaluate_alternate_primary_source_or_keep_metric_unavailable',
    )


def _current_tickers(conn: sqlite3.Connection, *, as_of: str) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute(
            '''SELECT DISTINCT t.ticker
               FROM dim_consumer_defensive_taxonomy t
               JOIN dim_universe_membership m
                 ON m.ticker=t.ticker AND m.model_family=t.model_family
               WHERE t.model_family='consumer_defensive'
                 AND m.live_investable_flag=1
                 AND m.start_date<=?
                 AND COALESCE(m.end_date,'9999-12-31')>=?''',
            (as_of, as_of),
        )
    }


def _status_tickers(
    conn: sqlite3.Connection,
    *,
    parser_run_id: int,
) -> dict[tuple[str, str], set[str]]:
    result: dict[tuple[str, str], set[str]] = defaultdict(set)
    for row in conn.execute(
        '''SELECT e.metric_name,e.candidate_status,e.ticker
           FROM sec_parser_run_metric_evidence r
           JOIN sec_parser_metric_evidence_shadow e
             ON e.evidence_key=r.evidence_key
           WHERE r.run_id=? AND e.model_family='consumer_defensive' ''',
        (parser_run_id,),
    ):
        result[(str(row['metric_name']), str(row['candidate_status']))].add(
            str(row['ticker'])
        )
    return result


def _run_observation_hashes(run: sqlite3.Row) -> tuple[str, ...]:
    try:
        metadata = json.loads(str(run['metadata_json'] or '{}'))
    except json.JSONDecodeError as exc:
        raise RuntimeError('Stage 6B run metadata is invalid JSON.') from exc
    raw_hashes = (
        metadata.get('observation_sha256s')
        if isinstance(metadata, dict)
        else None
    )
    if not isinstance(raw_hashes, list):
        raise RuntimeError(
            'Stage 6B run is missing its exact observation manifest.'
        )
    hashes = tuple(str(value) for value in raw_hashes)
    if (
        len(hashes) != int(run['accepted_observation_count'])
        or len(set(hashes)) != len(hashes)
        or any(not _HASH_RE.fullmatch(value) for value in hashes)
    ):
        raise RuntimeError('Stage 6B run observation manifest is invalid.')
    return hashes


def _run_observations(
    conn: sqlite3.Connection,
    *,
    as_of: str,
    run: sqlite3.Row,
) -> list[sqlite3.Row]:
    hashes = _run_observation_hashes(run)
    rows: list[sqlite3.Row] = []
    for offset in range(0, len(hashes), 400):
        chunk = hashes[offset:offset + 400]
        placeholders = ','.join('?' for _ in chunk)
        rows.extend(conn.execute(
            f'''SELECT * FROM fact_specialized_metric_observation
                WHERE observation_sha256 IN ({placeholders})
                  AND accepted_at<=?
                  AND production_status='measurement_only' ''',
            (*chunk, as_of + 'T23:59:59Z'),
        ))
    if (
        len(rows) != len(hashes)
        or {str(row['observation_sha256']) for row in rows} != set(hashes)
    ):
        raise RuntimeError(
            'Stage 6B run observation manifest does not match stored observations.'
        )
    return sorted(
        rows,
        key=lambda row: (
            str(row['metric_id']), str(row['ticker']),
            str(row['period_end']), str(row['accepted_at']),
            str(row['observation_sha256']),
        ),
    )


def _measurement_tickers(
    observations: Iterable[sqlite3.Row],
) -> dict[str, set[str]]:
    result: dict[str, set[str]] = defaultdict(set)
    for row in observations:
        if (
            str(row['evidence_status']) == 'accepted_measurement_only'
            and row['numeric_value'] is not None
        ):
            result[str(row['metric_id'])].add(str(row['ticker']))
    return result


def _requested_metric_names(payload: str, *, source: str) -> set[str]:
    try:
        decoded = json.loads(payload or '[]')
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f'Invalid requested-metric JSON in {source}.'
        ) from exc
    if not isinstance(decoded, list):
        raise RuntimeError(
            f'Requested-metric contract in {source} must be a list.'
        )
    names: set[str] = set()
    for item in decoded:
        name = (
            str(item.get('metric_name') or '').strip()
            if isinstance(item, dict)
            else str(item).strip()
            if isinstance(item, str)
            else ''
        )
        if not name:
            raise RuntimeError(
                f'Requested-metric contract in {source} has a blank entry.'
            )
        names.add(name)
    return names


def _metric_targeting_tickers(
    conn: sqlite3.Connection,
    *,
    parser_run_id: int,
    as_of: str,
) -> tuple[
    dict[str, set[str]],
    dict[str, set[str]],
    dict[str, set[str]],
]:
    requested: dict[str, set[str]] = defaultdict(set)
    completed: dict[str, set[str]] = defaultdict(set)
    for row in conn.execute(
        '''SELECT w.ticker,w.status,w.requested_metrics_json
           FROM sec_parser_run_work r
           JOIN sec_parser_work_ledger w ON w.work_key=r.work_key
           WHERE r.run_id=?''',
        (parser_run_id,),
    ):
        ticker = str(row['ticker'])
        metric_names = _requested_metric_names(
            str(row['requested_metrics_json'] or '[]'),
            source=f'parser work {ticker}',
        )
        for metric_id in metric_names:
            requested[metric_id].add(ticker)
            if str(row['status']) == 'COMPLETED':
                completed[metric_id].add(ticker)

    inventoried: dict[str, set[str]] = defaultdict(set)
    for row in conn.execute(
        '''SELECT ticker,requested_metrics_json
           FROM stage6b_document_inventory
           WHERE asof_date=? AND inventory_status LIKE 'sealed_%' ''',
        (as_of,),
    ):
        ticker = str(row['ticker'])
        metric_names = _requested_metric_names(
            str(row['requested_metrics_json'] or '[]'),
            source=f'sealed inventory {ticker}',
        )
        for metric_id in metric_names:
            inventoried[metric_id].add(ticker)
    return requested, completed, inventoried


def _history_depth_row(
    observations: Iterable[sqlite3.Row],
    *,
    stage6b_run_id: int,
    scope_name: str,
    cohort_id: str,
    applicability_subtype: str,
    metric_id: str,
    applicable: set[str],
    created_at: str,
) -> tuple[Any, ...]:
    selected = [
        row for row in observations
        if str(row['metric_id']) == metric_id
        and str(row['ticker']) in applicable
        and str(row['evidence_status']) == 'accepted_measurement_only'
        and row['numeric_value'] is not None
    ]
    periods: dict[str, set[str]] = defaultdict(set)
    for row in selected:
        period_end = str(row['period_end'] or '')
        if period_end:
            periods[str(row['ticker'])].add(period_end)
    counts = sorted(len(values) for values in periods.values())
    period_values = sorted({
        period for values in periods.values() for period in values
    })
    return (
        stage6b_run_id,
        scope_name,
        cohort_id,
        applicability_subtype,
        metric_id,
        len(periods),
        len(selected),
        sum(counts),
        sum(value >= 2 for value in counts),
        float(median(counts)) if counts else 0.0,
        period_values[0] if period_values else '',
        period_values[-1] if period_values else '',
        _canonical_json({
            ticker: len(values) for ticker, values in sorted(periods.items())
        }),
        created_at,
    )


def _uncovered_evidence_states(
    *,
    metric: SpecializedMetric,
    remaining: set[str],
    completed_work: set[str],
    requested_work: set[str],
    inventoried_metrics: set[str],
    inventory_tickers: set[str],
) -> dict[str, set[str]]:
    unresolved = set(remaining)
    states: dict[str, set[str]] = {}
    completed_targets = unresolved & completed_work
    completed_state = (
        'selective_disclosure_not_confirmed'
        if metric.source_availability_class == 'sec_selective'
        else 'metric_targeted_corpus_no_candidate'
    )
    states[completed_state] = completed_targets
    unresolved -= completed_targets
    incomplete_targets = unresolved & requested_work
    states['metric_requested_work_incomplete'] = incomplete_targets
    unresolved -= incomplete_targets
    not_planned = unresolved & inventoried_metrics
    states['metric_targeted_document_not_planned'] = not_planned
    unresolved -= not_planned
    issuer_only = unresolved & inventory_tickers
    states['issuer_corpus_present_metric_not_targeted'] = issuer_only
    unresolved -= issuer_only
    states['not_in_sealed_corpus'] = unresolved
    return states


def build_stage6b_coverage(
    conn: sqlite3.Connection,
    bundle: ConfigBundle,
    *,
    as_of: str,
    stage6b_run_id: int | None = None,
) -> dict[str, Any]:
    bootstrap_stage6b(conn, bundle)
    run = conn.execute(
        '''SELECT * FROM stage6b_specialized_run
           WHERE stage6b_run_id=COALESCE(?,stage6b_run_id)
             AND asof_date=? AND status='measurement_only_complete'
           ORDER BY stage6b_run_id DESC LIMIT 1''',
        (stage6b_run_id, as_of),
    ).fetchone()
    if run is None or run['parser_run_id'] is None:
        raise RuntimeError('Stage 6B coverage requires a completed measurement run.')
    stage6b_run_id = int(run['stage6b_run_id'])
    parser_run_id = int(run['parser_run_id'])
    _, metrics = _metric_registry(bundle)
    taxonomy = _taxonomy(conn)
    all_tickers = set(taxonomy)
    current_tickers = _current_tickers(conn, as_of=as_of)
    inventory_tickers = {
        str(row[0])
        for row in conn.execute(
            '''SELECT DISTINCT ticker FROM stage6b_document_inventory
               WHERE asof_date=? AND inventory_status LIKE 'sealed_%' ''',
            (as_of,),
        )
    }
    census_hits: dict[str, set[str]] = defaultdict(set)
    for row in conn.execute(
        '''SELECT metric_id,ticker
           FROM fact_specialized_metric_disclosure_summary
           WHERE asof_date=? AND disclosure_status='applicable_term_hit' ''',
        (as_of,),
    ):
        census_hits[str(row['metric_id'])].add(str(row['ticker']))
    statuses = _status_tickers(conn, parser_run_id=parser_run_id)
    run_observations = list(_run_observations(conn, as_of=as_of, run=run))
    requested_work, completed_work, inventoried_metrics = (
        _metric_targeting_tickers(
            conn, parser_run_id=parser_run_id, as_of=as_of
        )
    )
    measurement = _measurement_tickers(run_observations)
    direct_measurement: dict[str, set[str]] = defaultdict(set)
    derived_measurement: dict[str, set[str]] = defaultdict(set)
    for observation in run_observations:
        target = (
            derived_measurement
            if str(observation['source_id'] or '') == DERIVED_SOURCE_ID
            else direct_measurement
        )
        target[str(observation['metric_id'])].add(str(observation['ticker']))
    try:
        run_metadata = json.loads(str(run['metadata_json'] or '{}'))
    except (json.JSONDecodeError, TypeError):
        run_metadata = {}
    conflict_tickers: dict[str, set[str]] = defaultdict(set)
    for conflict in (
        run_metadata.get('conflicts', [])
        if isinstance(run_metadata, dict) else []
    ):
        if isinstance(conflict, dict):
            conflict_tickers[str(conflict.get('metric_id') or '')].add(
                str(conflict.get('ticker') or '')
            )
    now = utc_now()
    coverage_rows: list[dict[str, Any]] = []
    coverage_status_rows: list[tuple[Any, ...]] = []
    history_depth_rows: list[tuple[Any, ...]] = []
    for scope_name, scope_tickers, denominator_kind, sec_only in (
        ('all_taxonomy', all_tickers, 'registered_applicable', False),
        ('current_live', current_tickers, 'registered_applicable', False),
        (
            'all_taxonomy_sec_addressable',
            all_tickers,
            'sec_addressable',
            True,
        ),
        (
            'current_live_sec_addressable',
            current_tickers,
            'sec_addressable',
            True,
        ),
    ):
        for metric in metrics:
            if sec_only and not metric.sec_addressable:
                continue
            applicable_all = {
                ticker for ticker in scope_tickers
                if _specialized_applicable(
                    metric,
                    cohort_id=taxonomy[ticker]['cohort_id'],
                    subtype=taxonomy[ticker]['subtype'],
                )
            }
            group_specs: set[tuple[str, str]] = {('*', '*')}
            group_specs.update(
                (taxonomy[ticker]['cohort_id'], '*')
                for ticker in applicable_all
            )
            group_specs.update(
                (
                    taxonomy[ticker]['cohort_id'],
                    taxonomy[ticker]['subtype'],
                )
                for ticker in applicable_all
            )
            for cohort_id, subtype in sorted(group_specs):
                applicable = {
                    ticker for ticker in applicable_all
                    if (cohort_id == '*' or taxonomy[ticker]['cohort_id'] == cohort_id)
                    and (subtype == '*' or taxonomy[ticker]['subtype'] == subtype)
                }
                if not applicable:
                    continue
                accepted = statuses.get((metric.metric_id, 'ACCEPTED'), set())
                review = statuses.get(
                    (metric.metric_id, 'REVIEW_REQUIRED'), set()
                )
                rejected = set().union(*(
                    tickers
                    for (metric_id, status), tickers in statuses.items()
                    if metric_id == metric.metric_id
                    and status.startswith(('REJECTED', 'SUPPRESSED'))
                ), set())
                failures = statuses.get(
                    (metric.metric_id, 'PARSER_FAILURE'), set()
                )
                candidates = accepted | review | rejected
                measured = measurement.get(metric.metric_id, set())
                covered = applicable & measured
                fraction = len(covered) / len(applicable)
                tier, action = _coverage_tier(fraction)
                coverage_rows.append({
                    'stage6b_run_id': stage6b_run_id,
                    'scope_name': scope_name,
                    'cohort_id': cohort_id,
                    'applicability_subtype': subtype,
                    'metric_id': metric.metric_id,
                    'source_availability_class': (
                        metric.source_availability_class
                    ),
                    'denominator_kind': denominator_kind,
                    'applicable_issuer_count': len(applicable),
                    'hydrated_document_issuer_count': len(
                        applicable & inventory_tickers
                    ),
                    'census_term_hit_issuer_count': len(
                        applicable & census_hits.get(metric.metric_id, set())
                    ),
                    'parser_candidate_issuer_count': len(applicable & candidates),
                    'parser_accepted_issuer_count': len(applicable & accepted),
                    'measurement_issuer_count': len(covered),
                    'review_required_issuer_count': len(applicable & review),
                    'rejected_issuer_count': len(applicable & rejected),
                    'parser_failure_issuer_count': len(applicable & failures),
                    'measurement_coverage': fraction,
                    'coverage_tier': tier,
                    'recommended_action': action,
                    'uncovered_tickers_json': _canonical_json(
                        sorted(applicable - measured)
                    ),
                    'created_at': now,
                })
                history_depth_rows.append(_history_depth_row(
                    run_observations,
                    stage6b_run_id=stage6b_run_id,
                    scope_name=scope_name,
                    cohort_id=cohort_id,
                    applicability_subtype=subtype,
                    metric_id=metric.metric_id,
                    applicable=applicable,
                    created_at=now,
                ))
                proposals = {
                    'conflict': conflict_tickers.get(metric.metric_id, set()),
                    'direct_numeric': direct_measurement.get(metric.metric_id, set()),
                    'derived': derived_measurement.get(metric.metric_id, set()),
                    'review_required': review,
                    'numeric_candidate_rejected': rejected,
                    'parser_failure': failures,
                }
                state_tickers: dict[str, set[str]] = {}
                remaining = set(applicable)
                for evidence_state in (
                    'conflict', 'direct_numeric', 'derived', 'review_required',
                    'numeric_candidate_rejected', 'parser_failure',
                ):
                    selected = remaining & proposals[evidence_state]
                    state_tickers[evidence_state] = selected
                    remaining -= selected
                non_sec = (
                    set(remaining) if not metric.sec_addressable else set()
                )
                state_tickers['non_sec_required'] = non_sec
                remaining -= non_sec
                state_tickers.update(_uncovered_evidence_states(
                    metric=metric,
                    remaining=remaining,
                    completed_work=completed_work.get(metric.metric_id, set()),
                    requested_work=requested_work.get(metric.metric_id, set()),
                    inventoried_metrics=inventoried_metrics.get(
                        metric.metric_id, set()
                    ),
                    inventory_tickers=inventory_tickers,
                ))
                for evidence_state, tickers in sorted(state_tickers.items()):
                    coverage_status_rows.append((
                        stage6b_run_id, scope_name, cohort_id, subtype,
                        metric.metric_id, evidence_state, len(tickers),
                        _canonical_json(sorted(tickers)), now,
                    ))
    with conn:
        conn.execute(
            'DELETE FROM stage6b_metric_coverage WHERE stage6b_run_id=?',
            (stage6b_run_id,),
        )
        conn.executemany(
            '''INSERT INTO stage6b_metric_coverage(
                   stage6b_run_id,scope_name,cohort_id,
                   applicability_subtype,metric_id,applicable_issuer_count,
                   hydrated_document_issuer_count,census_term_hit_issuer_count,
                   parser_candidate_issuer_count,parser_accepted_issuer_count,
                   measurement_issuer_count,review_required_issuer_count,
                   rejected_issuer_count,parser_failure_issuer_count,
                   measurement_coverage,coverage_tier,recommended_action,
                   uncovered_tickers_json,created_at,
                   source_availability_class,denominator_kind
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            [tuple(row[key] for key in (
                'stage6b_run_id', 'scope_name', 'cohort_id',
                'applicability_subtype', 'metric_id',
                'applicable_issuer_count', 'hydrated_document_issuer_count',
                'census_term_hit_issuer_count',
                'parser_candidate_issuer_count',
                'parser_accepted_issuer_count', 'measurement_issuer_count',
                'review_required_issuer_count', 'rejected_issuer_count',
                'parser_failure_issuer_count', 'measurement_coverage',
                'coverage_tier', 'recommended_action',
                'uncovered_tickers_json', 'created_at',
                'source_availability_class', 'denominator_kind',
            )) for row in coverage_rows],
        )
        conn.execute(
            'DELETE FROM stage6b_metric_coverage_status WHERE stage6b_run_id=?',
            (stage6b_run_id,),
        )
        conn.executemany(
            '''INSERT INTO stage6b_metric_coverage_status(
                   stage6b_run_id,scope_name,cohort_id,
                   applicability_subtype,metric_id,evidence_state,
                   issuer_count,tickers_json,created_at)
               VALUES(?,?,?,?,?,?,?,?,?)''',
            coverage_status_rows,
        )
        conn.execute(
            'DELETE FROM stage6b_metric_history_depth WHERE stage6b_run_id=?',
            (stage6b_run_id,),
        )
        conn.executemany(
            '''INSERT INTO stage6b_metric_history_depth(
                   stage6b_run_id,scope_name,cohort_id,
                   applicability_subtype,metric_id,measured_issuer_count,
                   observation_count,issuer_period_count,
                   multi_period_issuer_count,median_periods_per_measured_issuer,
                   earliest_period_end,latest_period_end,periods_per_issuer_json,
                   created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            history_depth_rows,
        )
    overall_rows = [
        row for row in coverage_rows
        if row['cohort_id'] == '*' and row['applicability_subtype'] == '*'
    ]
    return {
        'status': 'PASS',
        'stage6b_run_id': stage6b_run_id,
        'asof_date': as_of,
        'coverage_rows': coverage_rows,
        'overall_rows': overall_rows,
        'metric_count': len(metrics),
        'current_ticker_count': len(current_tickers),
        'taxonomy_ticker_count': len(all_tickers),
        'inventory_ticker_count': len(inventory_tickers),
        'coverage_status_row_count': len(coverage_status_rows),
        'history_depth_row_count': len(history_depth_rows),
        'evidence_state_summary': {
            scope_name: {
                evidence_state: sum(
                    int(row[6]) for row in coverage_status_rows
                    if str(row[1]) == scope_name
                    and str(row[2]) == '*'
                    and str(row[3]) == '*'
                    and str(row[5]) == evidence_state
                )
                for evidence_state in sorted({
                    str(row[5]) for row in coverage_status_rows
                    if str(row[1]) == scope_name
                    and str(row[2]) == '*'
                    and str(row[3]) == '*'
                })
            }
            for scope_name in sorted({
                str(row[1]) for row in coverage_status_rows
            })
        },
        'history_depth_summary': {
            scope_name: {
                'observation_count': sum(
                    int(row[6]) for row in history_depth_rows
                    if str(row[1]) == scope_name
                    and str(row[2]) == '*'
                    and str(row[3]) == '*'
                ),
                'issuer_period_count': sum(
                    int(row[7]) for row in history_depth_rows
                    if str(row[1]) == scope_name
                    and str(row[2]) == '*'
                    and str(row[3]) == '*'
                ),
                'multi_period_issuer_metric_pairs': sum(
                    int(row[8]) for row in history_depth_rows
                    if str(row[1]) == scope_name
                    and str(row[2]) == '*'
                    and str(row[3]) == '*'
                ),
            }
            for scope_name in sorted({
                str(row[1]) for row in history_depth_rows
            })
        },
        'denominator_summary': {
            scope_name: {
                'applicable_issuer_metric_pairs': sum(
                    int(row['applicable_issuer_count'])
                    for row in overall_rows
                    if row['scope_name'] == scope_name
                ),
                'measurement_issuer_metric_pairs': sum(
                    int(row['measurement_issuer_count'])
                    for row in overall_rows
                    if row['scope_name'] == scope_name
                ),
            }
            for scope_name in sorted({
                str(row['scope_name']) for row in overall_rows
            })
        },
    }


def apply_stage6b_measurement_overlays(
    conn: sqlite3.Connection,
    *,
    as_of: str,
) -> dict[str, Any]:
    components = list(conn.execute(
        '''SELECT * FROM feature_scoring_component
           WHERE model_family='consumer_defensive' AND asof_date=?
             AND component_group='specialized'
           ORDER BY ticker,component_name''',
        (as_of,),
    ))
    if not components:
        return {
            'status': 'NOT_APPLICABLE_NO_MATCHING_STAGE6A_MATRIX',
            'asof_date': as_of,
            'updated_component_count': 0,
        }
    cutoff = as_of + 'T23:59:59Z'
    updated = 0
    now = utc_now()
    with conn:
        for component in components:
            metric_id = str(component['source_field'])
            observation = conn.execute(
                '''SELECT * FROM fact_specialized_metric_observation
                   WHERE ticker=? AND metric_id=? AND accepted_at<=?
                     AND production_status='measurement_only'
                     AND evidence_status='accepted_measurement_only'
                     AND numeric_value IS NOT NULL
                   ORDER BY period_end DESC,accepted_at DESC,confidence DESC,
                            observation_sha256 DESC LIMIT 1''',
                (str(component['ticker']), metric_id, cutoff),
            ).fetchone()
            if observation is None:
                continue
            row = dict(component)
            lineage = json.loads(str(row['lineage_json'] or '{}'))
            lineage['stage6b_overlay'] = {
                'observation_sha256': str(observation['observation_sha256']),
                'evidence_key': str(observation['evidence_key'] or ''),
                'accepted_at': str(observation['accepted_at']),
                'period_end': str(observation['period_end']),
                'observation_unit': str(observation['unit'] or ''),
                'confidence': float(observation['confidence']),
                'production_status': 'measurement_only',
            }
            row.update({
                'raw_value': float(observation['numeric_value']),
                'normalized_value': None,
                'component_score': None,
                'component_weight': 0.0,
                'availability_status': 'measurement_only',
                'source_asof_date': str(observation['accepted_at'])[:10],
                'quality_status': 'accepted_measurement_only',
                'source_id': str(observation['source_id']),
                'exclusion_reason': 'zero_weight_pending_signal_validation',
                'lineage_json': _canonical_json(lineage),
                'production_status': 'measurement_only',
            })
            row['component_observation_id'] = component_observation_id(row)
            conn.execute(
                '''UPDATE feature_scoring_component SET
                       raw_value=?,normalized_value=NULL,component_score=NULL,
                       component_weight=0.0,availability_status=?,
                       source_asof_date=?,quality_status=?,source_id=?,
                       exclusion_reason=?,lineage_json=?,
                       component_observation_id=?,production_status=?,created_at=?
                   WHERE model_family='consumer_defensive'
                     AND ticker=? AND asof_date=? AND component_name=?''',
                (
                    row['raw_value'], row['availability_status'],
                    row['source_asof_date'], row['quality_status'],
                    row['source_id'], row['exclusion_reason'],
                    row['lineage_json'], row['component_observation_id'],
                    row['production_status'], now, row['ticker'], as_of,
                    row['component_name'],
                ),
            )
            updated += 1
        for input_row in conn.execute(
            '''SELECT * FROM feature_scoring_input
               WHERE model_family='consumer_defensive' AND asof_date=?
               ORDER BY ticker''',
            (as_of,),
        ).fetchall():
            row = dict(input_row)
            ticker_components = conn.execute(
                '''SELECT component_observation_id,component_group,
                          availability_status,raw_value
                   FROM feature_scoring_component
                   WHERE model_family='consumer_defensive'
                     AND ticker=? AND asof_date=?
                   ORDER BY component_name''',
                (str(row['ticker']), as_of),
            ).fetchall()
            component_ids = [str(value[0]) for value in ticker_components]
            applicable_specialized = sum(
                str(value['component_group']) == 'specialized'
                and str(value['availability_status']) != 'not_applicable'
                for value in ticker_components
            )
            available_specialized = sum(
                str(value['component_group']) == 'specialized'
                and str(value['availability_status']) in {
                    'available', 'measurement_only'
                }
                and value['raw_value'] is not None
                for value in ticker_components
            )
            total_core = (
                int(row['core_available_component_count'])
                + int(row['core_missing_component_count'])
            )
            full_denominator = total_core + applicable_specialized
            full_available = (
                int(row['core_available_component_count'])
                + available_specialized
            )
            row['full_data_quality_confidence'] = (
                full_available / full_denominator
                if full_denominator else 0.0
            )
            lineage = json.loads(str(row['lineage_json'] or '{}'))
            lineage['component_observation_ids'] = component_ids
            lineage['stage6b_measurement_overlay'] = True
            lineage['specialized_applicable_count'] = applicable_specialized
            lineage['specialized_available_count'] = available_specialized
            lineage['specialized_missing_count'] = (
                applicable_specialized - available_specialized
            )
            lineage['specialized_missing_value_policy'] = (
                'neutral_zero_contribution_no_weight_redistribution'
            )
            lineage['specialized_nonapplicable_policy'] = (
                'excluded_from_denominator'
            )
            lineage['specialized_weight_activation_policy'] = (
                'shared_factor_validation_acceptance_required'
            )
            row['lineage_json'] = _canonical_json(lineage)
            row['input_observation_id'] = input_observation_id(row)
            conn.execute(
                '''UPDATE feature_scoring_input
                   SET full_data_quality_confidence=?,lineage_json=?,
                       input_observation_id=?,created_at=?
                   WHERE model_family='consumer_defensive'
                     AND ticker=? AND asof_date=?''',
                (
                    row['full_data_quality_confidence'], row['lineage_json'],
                    row['input_observation_id'], now, row['ticker'], as_of,
                ),
            )
    return {
        'status': 'PASS',
        'asof_date': as_of,
        'updated_component_count': updated,
    }


def _inventory_history(
    conn: sqlite3.Connection,
    *,
    as_of: str,
    historical_start: str,
) -> dict[str, Any]:
    seal_dates = [
        str(row[0])
        for row in conn.execute(
            '''SELECT asof_date FROM consumer_defensive_sec_cache_snapshot
               WHERE asof_date<=? AND trust_state='trusted_current'
               ORDER BY asof_date''',
            (as_of,),
        )
    ]
    inventory = conn.execute(
        '''SELECT MIN(SUBSTR(accepted_at,1,10)),MAX(SUBSTR(accepted_at,1,10)),
                  COUNT(*),COUNT(DISTINCT ticker)
           FROM stage6b_document_inventory WHERE asof_date=?''',
        (as_of,),
    ).fetchone()
    early_tickers = {
        str(row[0])
        for row in conn.execute(
            '''SELECT DISTINCT ticker FROM stage6b_document_inventory
               WHERE asof_date=? AND SUBSTR(accepted_at,1,10)<'2020-01-01' ''',
            (as_of,),
        )
    }
    taxonomy_count = int(conn.execute(
        '''SELECT COUNT(*) FROM dim_consumer_defensive_taxonomy
           WHERE model_family='consumer_defensive' '''
    ).fetchone()[0])
    historical = conn.execute(
        '''SELECT r.snapshot_run_id,r.status,r.target_document_count,
                  r.hydrated_document_count,i.history_start,i.history_end,
                  i.uncovered_target_count,
                  (SELECT COUNT(*)
                   FROM stage6b_historical_document_snapshot h
                   WHERE h.snapshot_run_id=r.snapshot_run_id) AS persisted_count,
                  (SELECT COUNT(DISTINCT h.ticker)
                   FROM stage6b_historical_document_snapshot h
                   WHERE h.snapshot_run_id=r.snapshot_run_id) AS ticker_count,
                  (SELECT MIN(SUBSTR(h.accepted_at,1,10))
                   FROM stage6b_historical_document_snapshot h
                   WHERE h.snapshot_run_id=r.snapshot_run_id) AS earliest_acceptance
           FROM stage6b_historical_document_snapshot_run r
           JOIN stage6b_historical_inventory_run i USING(inventory_run_id)
           WHERE r.asof_date=? ORDER BY r.snapshot_run_id DESC LIMIT 1''',
        (as_of,),
    ).fetchone()
    historical_complete = bool(
        historical is not None
        and str(historical['status']) == 'PASS'
        and str(historical['history_start']) <= historical_start
        and str(historical['history_end']) <= as_of
        and int(historical['uncovered_target_count']) == 0
        and int(historical['target_document_count']) > 0
        and int(historical['target_document_count'])
        == int(historical['hydrated_document_count'])
        == int(historical['persisted_count'])
    )
    complete = historical_complete
    return {
        'requested_history_start': historical_start,
        'exact_seal_count': len(seal_dates),
        'exact_seal_dates': seal_dates,
        'earliest_exact_seal': min(seal_dates) if seal_dates else None,
        'earliest_inventory_acceptance': str(inventory[0] or ''),
        'latest_inventory_acceptance': str(inventory[1] or ''),
        'inventory_document_count': int(inventory[2] or 0),
        'inventory_ticker_count': int(inventory[3] or 0),
        'tickers_with_pre_2020_document': len(early_tickers),
        'taxonomy_ticker_count': taxonomy_count,
        'historical_snapshot_run_id': (
            int(historical['snapshot_run_id']) if historical else None
        ),
        'historical_snapshot_document_count': (
            int(historical['persisted_count']) if historical else 0
        ),
        'historical_snapshot_ticker_count': (
            int(historical['ticker_count']) if historical else 0
        ),
        'historical_snapshot_earliest_acceptance': (
            str(historical['earliest_acceptance'] or '') if historical else ''
        ),
        'historical_inventory_complete': complete,
        'gap_reason': (
            None
            if complete
            else 'exact_stage6b_historical_document_snapshot_is_incomplete'
        ),
    }


def validate_stage6b(
    conn: sqlite3.Connection,
    bundle: ConfigBundle,
    *,
    as_of: str,
    cache_dir: Path,
    stage6b_run_id: int | None = None,
) -> dict[str, Any]:
    policy_sha = bootstrap_stage6b(conn, bundle)
    checks: list[dict[str, Any]] = []

    def check(name: str, passed: bool, **details: Any) -> None:
        checks.append({'check': name, 'passed': bool(passed), **details})

    ledger = conn.execute(
        '''SELECT migration_sha256 FROM stage6b_schema_migrations
           WHERE migration_version=?''',
        (STAGE6B_SCHEMA_VERSION,),
    ).fetchone()
    check(
        'stage6b_migration_current',
        ledger is not None and str(ledger[0]) == STAGE6B_V6_MIGRATION_SHA256,
        version=STAGE6B_SCHEMA_VERSION,
    )
    policy_rows = list(conn.execute(
        'SELECT * FROM stage6b_metric_policy ORDER BY metric_id'
    ))
    check(
        'metric_policy_exact_and_zero_weight',
        len(policy_rows) == 38
        and all(
            str(row['adapter_version']) == ADAPTER_VERSION
            and str(row['production_status']) == 'measurement_only'
            and float(row['production_weight']) == 0.0
            and str(row['source_availability_class'])
            in ALLOWED_SOURCE_AVAILABILITY_CLASSES
            for row in policy_rows
        ),
        metric_count=len(policy_rows),
        policy_sha256=policy_sha,
    )
    seal_error = ''
    try:
        seal = _trusted_seal(conn, as_of=as_of, cache_dir=cache_dir)
    except (RuntimeError, ValueError) as exc:
        seal = None
        seal_error = str(exc)
    check(
        'exact_stage4_seal_bound',
        seal is not None,
        error=seal_error,
    )
    run = conn.execute(
        '''SELECT * FROM stage6b_specialized_run
           WHERE stage6b_run_id=COALESCE(?,stage6b_run_id)
             AND asof_date=? ORDER BY stage6b_run_id DESC LIMIT 1''',
        (stage6b_run_id, as_of),
    ).fetchone()
    check(
        'measurement_run_complete',
        run is not None
        and str(run['status']) == 'measurement_only_complete'
        and run['parser_run_id'] is not None,
        stage6b_run_id=int(run['stage6b_run_id']) if run else None,
    )
    parser = (
        conn.execute(
            'SELECT status,failed_work_count FROM sec_parser_run WHERE run_id=?',
            (int(run['parser_run_id']),),
        ).fetchone()
        if run is not None and run['parser_run_id'] is not None
        else None
    )
    check(
        'shadow_parser_zero_failure',
        parser is not None
        and str(parser['status']) == 'COMPLETED'
        and int(parser['failed_work_count']) == 0,
        parser_status=str(parser['status']) if parser else None,
        failed_work_count=int(parser['failed_work_count']) if parser else None,
    )
    inventory = list(conn.execute(
        '''SELECT * FROM stage6b_document_inventory
           WHERE asof_date=? ORDER BY ticker,accession_number,document_name''',
        (as_of,),
    ))
    taxonomy_tickers = set(_taxonomy(conn))
    sealed_inventory = [
        row for row in inventory
        if str(row['inventory_status']) in {
            'sealed_current_snapshot', 'sealed_historical_snapshot'
        }
    ]
    retained_unsealed = [
        row for row in inventory
        if str(row['inventory_status']) not in {
            'sealed_current_snapshot', 'sealed_historical_snapshot'
        }
    ]
    inventory_tickers = {str(row['ticker']) for row in sealed_inventory}
    check(
        'current_document_inventory_complete_for_taxonomy',
        inventory_tickers == taxonomy_tickers and bool(sealed_inventory),
        sealed_document_count=len(sealed_inventory),
        retained_unsealed_document_count=len(retained_unsealed),
        ticker_count=len(inventory_tickers),
        missing_tickers=sorted(taxonomy_tickers - inventory_tickers),
        unexpected_tickers=sorted(inventory_tickers - taxonomy_tickers),
    )
    taxonomy = _taxonomy(conn)
    _, metrics = _metric_registry(bundle)
    metric_by_id = {metric.metric_id: metric for metric in metrics}
    observation_errors: list[dict[str, Any]] = []
    observation_manifest_error = ''
    try:
        observations = (
            _run_observations(conn, as_of=as_of, run=run)
            if run is not None
            else []
        )
    except RuntimeError as exc:
        observations = []
        observation_manifest_error = str(exc)
    for row in observations:
        payload = dict(row)
        ticker = str(row['ticker'])
        metric = metric_by_id.get(str(row['metric_id']))
        member = taxonomy.get(ticker)
        valid = (
            metric is not None
            and member is not None
            and _specialized_applicable(
                metric,
                cohort_id=member['cohort_id'],
                subtype=member['subtype'],
            )
            and row['numeric_value'] is not None
            and math.isfinite(float(row['numeric_value']))
            and row['confidence'] is not None
            and 0.0 <= float(row['confidence']) <= 1.0
            and str(row['evidence_status']) == 'accepted_measurement_only'
            and str(row['production_status']) == 'measurement_only'
            and str(row['observation_sha256'])
            == specialized_observation_sha256(payload)
        )
        if not valid:
            observation_errors.append({
                'ticker': ticker,
                'metric_id': str(row['metric_id']),
                'observation_id': int(row['observation_id']),
            })
    check(
        'measurement_observations_semantically_valid',
        not observation_manifest_error
        and not observation_errors
        and bool(observations),
        observation_count=len(observations),
        manifest_error=observation_manifest_error,
        errors=observation_errors[:20],
    )
    nonzero = int(conn.execute(
        '''SELECT
             (SELECT COUNT(*) FROM dim_specialized_metric
              WHERE production_weight<>0.0)
             +
             (SELECT COUNT(*) FROM feature_scoring_component
              WHERE model_family='consumer_defensive'
                AND component_group='specialized'
                AND component_weight<>0.0)'''
    ).fetchone()[0])
    check(
        'specialized_production_weights_zero',
        nonzero == 0,
        nonzero_rows=nonzero,
    )
    coverage_count = (
        int(conn.execute(
            '''SELECT COUNT(*) FROM stage6b_metric_coverage
               WHERE stage6b_run_id=? AND cohort_id='*'
                 AND applicability_subtype='*' ''',
            (int(run['stage6b_run_id']),),
        ).fetchone()[0])
        if run is not None
        else 0
    )
    expected_coverage_rows = 2 * (
        len(metrics) + sum(metric.sec_addressable for metric in metrics)
    )
    check(
        'coverage_has_all_metrics_and_scopes',
        coverage_count == expected_coverage_rows,
        observed=coverage_count,
        expected=expected_coverage_rows,
    )
    fk = conn.execute('PRAGMA foreign_key_check').fetchone()
    check(
        'foreign_keys_valid',
        fk is None,
        first_violation=list(fk) if fk else None,
    )
    history = _inventory_history(
        conn,
        as_of=as_of,
        historical_start=str(
            cfg_get(bundle.payload, 'historical_contract.requested_snapshot_start')
        ),
    )
    code_status = 'PASS' if all(bool(row['passed']) for row in checks) else 'FAIL'
    status = (
        'PASS'
        if code_status == 'PASS' and history['historical_inventory_complete']
        else 'PASS_CURRENT_HISTORY_GAP'
        if code_status == 'PASS'
        else 'FAIL'
    )
    return {
        'status': status,
        'code_status': code_status,
        'asof_date': as_of,
        'checks': checks,
        'history': history,
        'summary': {
            'check_count': len(checks),
            'passed_check_count': sum(bool(row['passed']) for row in checks),
            'observation_count': len(observations),
            'metric_count': len(metrics),
            'inventory_document_count': len(inventory),
            'inventory_ticker_count': len(inventory_tickers),
            'historical_inventory_complete': history[
                'historical_inventory_complete'
            ],
        },
    }


def stage6b_report_tables(
    conn: sqlite3.Connection,
    *,
    as_of: str,
    stage6b_run_id: int,
) -> dict[str, list[dict[str, Any]]]:
    overall = [
        dict(row)
        for row in conn.execute(
            '''SELECT * FROM stage6b_metric_coverage
               WHERE stage6b_run_id=? AND cohort_id='*'
                 AND applicability_subtype='*'
               ORDER BY scope_name,measurement_coverage DESC,metric_id''',
            (stage6b_run_id,),
        )
    ]
    by_cohort = [
        dict(row)
        for row in conn.execute(
            '''SELECT * FROM stage6b_metric_coverage
               WHERE stage6b_run_id=? AND cohort_id<>'*'
                 AND applicability_subtype='*'
               ORDER BY scope_name,cohort_id,measurement_coverage DESC,metric_id''',
            (stage6b_run_id,),
        )
    ]
    by_subtype = [
        dict(row)
        for row in conn.execute(
            '''SELECT * FROM stage6b_metric_coverage
               WHERE stage6b_run_id=? AND applicability_subtype<>'*'
               ORDER BY scope_name,cohort_id,applicability_subtype,
                        measurement_coverage DESC,metric_id''',
            (stage6b_run_id,),
        )
    ]
    coverage_status = [
        dict(row)
        for row in conn.execute(
            '''SELECT * FROM stage6b_metric_coverage_status
               WHERE stage6b_run_id=?
               ORDER BY scope_name,cohort_id,applicability_subtype,
                        metric_id,evidence_state''',
            (stage6b_run_id,),
        )
    ]
    history_depth = [
        dict(row)
        for row in conn.execute(
            '''SELECT * FROM stage6b_metric_history_depth
               WHERE stage6b_run_id=?
               ORDER BY scope_name,cohort_id,applicability_subtype,metric_id''',
            (stage6b_run_id,),
        )
    ]
    run = conn.execute(
        'SELECT * FROM stage6b_specialized_run WHERE stage6b_run_id=?',
        (stage6b_run_id,),
    ).fetchone()
    if run is None or run['parser_run_id'] is None:
        raise RuntimeError('Stage 6B report run is missing.')
    parser_run_id = int(run['parser_run_id'])
    by_form = [
        dict(row)
        for row in conn.execute(
            '''SELECT e.metric_name AS metric_id,e.form_type,
                      e.candidate_status,COUNT(*) AS evidence_rows,
                      COUNT(DISTINCT e.ticker) AS issuer_count
               FROM sec_parser_run_metric_evidence r
               JOIN sec_parser_metric_evidence_shadow e
                 ON e.evidence_key=r.evidence_key
               WHERE r.run_id=?
               GROUP BY e.metric_name,e.form_type,e.candidate_status
               ORDER BY e.metric_name,e.form_type,e.candidate_status''',
            (parser_run_id,),
        )
    ]
    by_channel = [
        dict(row)
        for row in conn.execute(
            '''SELECT e.metric_name AS metric_id,e.extraction_method,
                      e.candidate_status,COUNT(*) AS evidence_rows,
                      COUNT(DISTINCT e.ticker) AS issuer_count
               FROM sec_parser_run_metric_evidence r
               JOIN sec_parser_metric_evidence_shadow e
                 ON e.evidence_key=r.evidence_key
               WHERE r.run_id=?
               GROUP BY e.metric_name,e.extraction_method,e.candidate_status
               ORDER BY e.metric_name,e.extraction_method,e.candidate_status''',
            (parser_run_id,),
        )
    ]
    observations = [
        dict(row)
        for row in _run_observations(conn, as_of=as_of, run=run)
    ]
    return {
        'overall': overall,
        'by_cohort': by_cohort,
        'by_subtype': by_subtype,
        'coverage_status': coverage_status,
        'history_depth': history_depth,
        'by_form': by_form,
        'by_channel': by_channel,
        'observations': observations,
    }


def write_stage6b_reports(
    conn: sqlite3.Connection,
    *,
    as_of: str,
    stage6b_run_id: int,
    output_dir: Path,
    validation: dict[str, Any],
) -> None:
    tables = stage6b_report_tables(
        conn, as_of=as_of, stage6b_run_id=stage6b_run_id
    )
    for name, rows in tables.items():
        write_csv(output_dir / f'specialized_coverage_{name}.csv', rows)
    write_csv(output_dir / 'stage6b_validation_checks.csv', validation['checks'])
    write_json(
        output_dir / 'stage6b_validation.json',
        {
            **validation,
            'stage6b_run_id': stage6b_run_id,
        },
    )
