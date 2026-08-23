"""Independent Consumer Defensive Stage 4 SEC, FX, feature, and disclosure census.

The module deliberately stores raw SEC facts before normalization.  Every fact and
feature is gated by the filing acceptance timestamp so historical snapshots do not
see information that was unavailable at the time.
"""

from __future__ import annotations

import csv
import hashlib
import html
import json
import math
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from consumer_defensive.core.config import ConfigBundle, cfg_get, resolve_path
from consumer_defensive.core.db import init_db, utc_now
from consumer_defensive.core.financial_pipeline import (
    FEATURE_DEFINITION_VERSION,
    build_financial_feature_bundle,
    legacy_feature_values,
    select_canonical_financial_facts,
)
from consumer_defensive.core.financial_semantics import (
    FxRateObservation,
    RedenominationExemption,
    classify_fx_daily_rates,
)
from consumer_defensive.core.inline_xbrl import (
    PARSER_VERSION as INLINE_PARSER_VERSION,
    parse_inline_xbrl,
)
from consumer_defensive.core.metric_registry import load_metric_registry, upsert_metric_registry
from consumer_defensive.core.universe import active_universe_tickers, upsert_stage2_sources
from consumer_defensive.core.source_registry import load_source_registry, upsert_source_registry
from dedicated_parser.sec_paths import (
    SEC_DOCUMENT_SUFFIXES,
    SEC_PRIMARY_DOCUMENT_SUFFIXES,
    SEC_SUBMISSIONS_ARCHIVE_SUFFIXES,
    quote_sec_document_basename,
    quote_sec_relative_document_path,
    resolve_sec_relative_document_path,
    resolve_sec_seal_root,
    validate_sec_document_basename,
    validate_sec_relative_document_path,
)
from dedicated_parser.path_io import (
    filesystem_path,
    is_dir_path,
    is_file_path,
    lexists_path,
    link_path,
    mkdir_path,
    open_path,
    path_exists,
    read_bytes,
    replace_path,
    resolve_path as resolve_filesystem_path,
    unlink_path,
)


MODEL_FAMILY = "consumer_defensive"
SEC_SUBMISSIONS = "sec_submissions"
SEC_COMPANYFACTS = "sec_companyfacts"
SEC_INLINE = "sec_inline_xbrl_fallback"
FX_SOURCE = "yahoo_fx_rates"
CANONICAL_SOURCE = "sec_companyfacts"
DISCLOSURE_SOURCE = "consumer_defensive_disclosure_census"
FX_PROVIDER_BOUNDARY_TOLERANCE_DAYS = 7
FX_PROVIDER_BOUNDARY_MAX_OBSERVATIONS = 2
FINANCIAL_FORM_FAMILIES = {
    "10-K": "10-K",
    "10-K/A": "10-K",
    "10-KT": "10-K",
    "10-KT/A": "10-K",
    "10-Q": "10-Q",
    "10-Q/A": "10-Q",
    "10-QT": "10-Q",
    "10-QT/A": "10-Q",
    "20-F": "20-F",
    "20-F/A": "20-F",
    "40-F": "40-F",
    "40-F/A": "40-F",
    "6-K": "6-K",
    "6-K/A": "6-K",
}
ALLOWED_FACT_FORMS = set(FINANCIAL_FORM_FAMILIES)
DOCUMENT_FORMS = {*FINANCIAL_FORM_FAMILIES, "8-K"}
PROFILE_FINANCIAL_FORMS = {
    form for form, family in FINANCIAL_FORM_FAMILIES.items()
    if family in {"10-K", "10-Q", "20-F", "40-F"}
}
PROFILE_CONDITIONAL_XBRL_FORMS = {"6-K", "6-K/A"}
PROFILE_ANNUAL_FORMS = {"10-K", "20-F", "40-F"}
MONETARY_UNITS = re.compile(r"^[A-Z]{3}$")
INLINE_XBRL_NAMESPACE = re.compile(
    br"https?://www\.xbrl\.org/[0-9]{4}/inlineXBRL",
    re.IGNORECASE,
)
INLINE_XBRL_ELEMENT = re.compile(
    br"<[A-Za-z_][A-Za-z0-9_.-]*:(?:header|hidden|nonfraction|nonnumeric|fraction|continuation)\b",
    re.IGNORECASE,
)
INLINE_XBRL_NUMERIC_FACT = re.compile(
    br"<[A-Za-z_][A-Za-z0-9_.-]*:nonfraction\b[^>]*\bname\s*=\s*"
    br"(?:\"(?!dei:)[^\"]+\"|'(?!dei:)[^']+')",
    re.IGNORECASE,
)


def _canonical_financial_form(form: str) -> str:
    """Return the base SEC financial-form family for a recognized variant."""

    return FINANCIAL_FORM_FAMILIES.get(form, form)


def _companyfacts_form_matches_submission(
    companyfacts_form: str, submissions_form: str,
) -> bool:
    """Match SEC's base-form normalization without masking other conflicts.

    Companyfacts can report the canonical base form for an accession whose
    submissions metadata preserves an amendment (``/A``) or a transitional
    ``10-KT``/``10-QT`` variant.  Exact matches remain valid for every form;
    the only non-exact match admitted here is the documented base form of a
    recognized financial-form variant.
    """

    return (
        companyfacts_form == submissions_form
        or (
            submissions_form in FINANCIAL_FORM_FAMILIES
            and companyfacts_form == _canonical_financial_form(submissions_form)
        )
    )


STAGE4_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS dim_issuer_reporting_profile (
    ticker TEXT PRIMARY KEY,
    cik TEXT,
    primary_annual_form TEXT,
    foreign_issuer_flag INTEGER NOT NULL DEFAULT 0,
    us_gaap_flag INTEGER NOT NULL DEFAULT 0,
    ifrs_flag INTEGER NOT NULL DEFAULT 0,
    latest_filing_accepted_at TEXT,
    latest_companyfacts_accepted_at TEXT,
    companyfacts_lag_days INTEGER,
    inline_xbrl_fallback_required INTEGER NOT NULL DEFAULT 0,
    coverage_status TEXT NOT NULL,
    review_reason TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fact_sec_filing_document (
    accession_number TEXT NOT NULL,
    ticker TEXT NOT NULL,
    form_type TEXT NOT NULL,
    accepted_at TEXT,
    primary_document TEXT,
    source_url TEXT NOT NULL,
    content_sha256 TEXT,
    cache_path TEXT,
    hydration_status TEXT NOT NULL,
    source_id TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(accession_number, source_id),
    FOREIGN KEY(accession_number) REFERENCES fact_sec_filing(accession_number) ON DELETE CASCADE,
    FOREIGN KEY(source_id) REFERENCES source_registry(source_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS bridge_sec_filing_document_company (
    accession_number TEXT NOT NULL,
    issuer_company_id INTEGER NOT NULL,
    source_id TEXT NOT NULL,
    issuer_ticker TEXT NOT NULL,
    issuer_cik TEXT NOT NULL,
    accepted_at TEXT,
    primary_document TEXT,
    source_url TEXT NOT NULL,
    content_sha256 TEXT,
    cache_path TEXT,
    hydration_status TEXT NOT NULL,
    inline_xbrl_verified INTEGER NOT NULL DEFAULT 0
        CHECK(inline_xbrl_verified IN (0,1)),
    updated_at TEXT NOT NULL,
    PRIMARY KEY(accession_number,issuer_company_id,source_id),
    FOREIGN KEY(accession_number,issuer_company_id)
        REFERENCES bridge_sec_filing_company(accession_number,issuer_company_id)
        ON DELETE CASCADE,
    FOREIGN KEY(source_id) REFERENCES source_registry(source_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS consumer_defensive_sec_reconciliation_state (
    asof_date TEXT PRIMARY KEY,
    cutoff TEXT NOT NULL,
    scope_issuer_count INTEGER NOT NULL,
    association_count INTEGER NOT NULL,
    accession_count INTEGER NOT NULL,
    shared_accession_count INTEGER NOT NULL,
    association_sha256 TEXT NOT NULL,
    ingestion_config_sha256 TEXT NOT NULL,
    issuer_scope_sha256 TEXT NOT NULL,
    cache_manifest_sha256 TEXT NOT NULL,
    cache_manifest_json TEXT NOT NULL DEFAULT '[]',
    cache_root TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK(status='complete'),
    completed_at TEXT NOT NULL,
    scope_contract_version INTEGER NOT NULL DEFAULT 3,
    trust_state TEXT NOT NULL DEFAULT 'trusted_current',
    quarantine_reason TEXT
);

CREATE TABLE IF NOT EXISTS consumer_defensive_sec_cache_snapshot (
    asof_date TEXT PRIMARY KEY,
    seal_relative_path TEXT NOT NULL,
    cache_manifest_sha256 TEXT NOT NULL,
    cache_manifest_json TEXT NOT NULL,
    ingestion_config_sha256 TEXT NOT NULL,
    issuer_scope_sha256 TEXT NOT NULL,
    scope_contract_version INTEGER NOT NULL DEFAULT 3,
    trust_state TEXT NOT NULL DEFAULT 'trusted_current',
    quarantine_reason TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sec_filing_company_association_event (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    accession_number TEXT NOT NULL,
    issuer_company_id INTEGER NOT NULL,
    issuer_ticker TEXT NOT NULL,
    issuer_cik TEXT NOT NULL,
    effective_asof TEXT NOT NULL,
    event_type TEXT NOT NULL
        CHECK(event_type IN ('observed','retired','reactivated')),
    reason TEXT NOT NULL,
    event_sha256 TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    FOREIGN KEY(accession_number) REFERENCES fact_sec_filing(accession_number)
        ON DELETE CASCADE,
    FOREIGN KEY(issuer_company_id) REFERENCES dim_company(company_id)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS consumer_defensive_stage4_schema_migration (
    migration_version INTEGER PRIMARY KEY,
    migration_name TEXT NOT NULL DEFAULT '',
    migration_sha256 TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK(status='complete'),
    applied_at TEXT NOT NULL
);

DROP TRIGGER IF EXISTS trg_sec_bridge_invalidate_reconciliation_insert;
CREATE TRIGGER IF NOT EXISTS trg_sec_bridge_invalidate_reconciliation_insert
AFTER INSERT ON bridge_sec_filing_company BEGIN
    DELETE FROM consumer_defensive_sec_reconciliation_state;
END;
DROP TRIGGER IF EXISTS trg_sec_bridge_invalidate_reconciliation_delete;
CREATE TRIGGER IF NOT EXISTS trg_sec_bridge_invalidate_reconciliation_delete
AFTER DELETE ON bridge_sec_filing_company BEGIN
    DELETE FROM consumer_defensive_sec_reconciliation_state;
END;
DROP TRIGGER IF EXISTS trg_sec_bridge_invalidate_reconciliation_update;
CREATE TRIGGER IF NOT EXISTS trg_sec_bridge_invalidate_reconciliation_update
AFTER UPDATE OF issuer_company_id,issuer_ticker,issuer_cik,relationship,
    relationship_evidence,form_type,filing_date,accepted_at,report_date,
    primary_document,source_id,source_url,association_status,
    retirement_effective_asof,retirement_reason ON bridge_sec_filing_company
WHEN OLD.issuer_company_id IS NOT NEW.issuer_company_id
  OR OLD.issuer_ticker IS NOT NEW.issuer_ticker
  OR OLD.issuer_cik IS NOT NEW.issuer_cik
  OR OLD.relationship IS NOT NEW.relationship
  OR OLD.relationship_evidence IS NOT NEW.relationship_evidence
  OR OLD.form_type IS NOT NEW.form_type
  OR OLD.filing_date IS NOT NEW.filing_date
  OR OLD.accepted_at IS NOT NEW.accepted_at
  OR OLD.report_date IS NOT NEW.report_date
  OR OLD.primary_document IS NOT NEW.primary_document
  OR OLD.source_id IS NOT NEW.source_id
  OR OLD.source_url IS NOT NEW.source_url
  OR OLD.association_status IS NOT NEW.association_status
  OR OLD.retirement_effective_asof IS NOT NEW.retirement_effective_asof
  OR OLD.retirement_reason IS NOT NEW.retirement_reason
BEGIN
    DELETE FROM consumer_defensive_sec_reconciliation_state;
END;

CREATE TABLE IF NOT EXISTS fact_specialized_metric_disclosure_census (
    ticker TEXT NOT NULL,
    accession_number TEXT NOT NULL,
    metric_id TEXT NOT NULL,
    calibration_cohort_id TEXT NOT NULL,
    applicability_subtype TEXT NOT NULL,
    accepted_at TEXT,
    form_type TEXT NOT NULL,
    hit_count INTEGER NOT NULL,
    matched_terms_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    source_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(ticker, accession_number, metric_id, parser_version),
    FOREIGN KEY(metric_id) REFERENCES dim_specialized_metric(metric_id) ON DELETE RESTRICT,
    FOREIGN KEY(source_id) REFERENCES source_registry(source_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS fact_specialized_metric_disclosure_summary (
    ticker TEXT NOT NULL,
    metric_id TEXT NOT NULL,
    calibration_cohort_id TEXT NOT NULL,
    applicability_subtype TEXT NOT NULL,
    asof_date TEXT NOT NULL,
    applicability_status TEXT NOT NULL,
    filings_searched INTEGER NOT NULL,
    filings_with_hits INTEGER NOT NULL,
    disclosure_status TEXT NOT NULL,
    first_disclosure_accepted_at TEXT,
    last_disclosure_accepted_at TEXT,
    parser_version TEXT NOT NULL,
    source_id TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(ticker, metric_id, parser_version, asof_date),
    FOREIGN KEY(metric_id) REFERENCES dim_specialized_metric(metric_id) ON DELETE RESTRICT,
    FOREIGN KEY(source_id) REFERENCES source_registry(source_id) ON DELETE RESTRICT
);

DROP INDEX IF EXISTS idx_stage4_raw_ticker_accepted;
CREATE INDEX IF NOT EXISTS idx_stage4_raw_ticker_source_accepted
    ON fact_sec_xbrl_fact_raw(ticker, source_id, accepted_at);
CREATE INDEX IF NOT EXISTS idx_stage4_canonical_ticker_accepted
    ON fact_financial_statement_canonical(ticker, accepted_at, canonical_metric);
CREATE INDEX IF NOT EXISTS idx_stage4_canonical_raw_fact_id
    ON fact_financial_statement_canonical(source_raw_fact_id);
CREATE INDEX IF NOT EXISTS idx_stage4_association_event_pit
    ON sec_filing_company_association_event(
        accession_number,issuer_company_id,effective_asof,event_id
    );

DROP VIEW IF EXISTS consumer_defensive_sec_parser_filing_input;
CREATE VIEW consumer_defensive_sec_parser_filing_input AS
SELECT b.issuer_ticker AS ticker,
       b.issuer_cik AS cik,
       b.issuer_cik AS archive_cik,
       f.accession_number,
       b.form_type,
       b.filing_date,
       f.accepted_at AS accepted_at,
       b.accepted_at AS observed_accepted_at,
       b.report_date,
       b.primary_document,
       b.source_id,
       b.relationship,
       b.issuer_company_id,
       b.association_status,
       b.retirement_effective_asof,
       NULL AS fiscal_year,
       NULL AS fiscal_period,
       COALESCE(NULLIF(c.reporting_currency, ''), 'USD') AS company_currency
FROM bridge_sec_filing_company AS b
JOIN fact_sec_filing AS f ON f.accession_number=b.accession_number
JOIN dim_company AS c ON c.company_id=b.issuer_company_id;
"""


@dataclass(frozen=True)
class HttpPolicy:
    user_agent: str
    timeout_sec: float
    retries: int
    sleep_sec: float


Fetcher = Callable[[str], bytes]

STAGE4_SCHEMA_VERSION = 10
SEC_INGESTION_CONFIG_VERSION = 8
STAGE4_MIGRATION_MANIFESTS = {
    2: b'''v2|create:reporting_profile,filing_document,document_company,reconciliation,census,summary,indexes|alter:filing.metadata_quality_flags_json|backfill:v2_legacy_filing_association_v1,v2_legacy_document_association_v1''',
    3: b'''v3|alter:reconciliation.cache_manifest_json,reconciliation.cache_root|create:immutable_cache_snapshot''',
    4: b'''v4|alter:association.status,association.retirement_effective_asof,association.retirement_reason,document.inline_xbrl_verified|create:reconciliation_invalidation_triggers,parser_input_view''',
    5: b'''v5|alter:reconciliation.config_hash,reconciliation.scope_hash,snapshot.config_hash,snapshot.scope_hash,raw.source_observation_id,canonical.source_observation_id|create:association_event,event_immutability_triggers,observation_indexes|identity:ingestion_config_canonical_json_v1,issuer_scope_ticker_cik_v1,source_observation_semantic_sha256_v1|backfill:source_observation_sha256_v1,association_event_projection_v1|reconcile:late_legacy_rows_v1,late_legacy_summary_backup_v1''',
    6: b'''v6|create:sec_ingestion_watermark|identity:issuer_scope_ticker_company_cik_v2,source_observation_semantic_sha256_v1,association_event_sha256_v1|backfill:monotonic_stage4_watermark_v1,source_observation_exact_keyset_v1,association_event_exact_keyset_v1|contract:reject_reverse_sec_replay_before_mutation_v1,exact_cache_parser_preflight_v1''',
    7: b'''v7|create:document_bridge_reconciliation_invalidation_insert_delete_material_update,association_event_reconciliation_invalidation_insert|contract:updated_at_nonmaterial,pre_v7_reconciliation_fail_closed_v1|invalidate:all_existing_reconciliation_preserve_cache_snapshots''',
    8: b'''v8|alter:reconciliation.scope_contract_version,trust_state,quarantine_reason,snapshot.scope_contract_version,trust_state,quarantine_reason|create:fact_filing_and_company_currency_invalidation,non_destructive_reconciliation_invalidation|identity:issuer_scope_ticker_company_cik_reporting_currency_v3,ingestion_config_v7|quarantine:pre_v8_scope_v2_pointers_in_place|contract:current_watermark_live_rebuild_only_v1''',
    9: b'''v9|create:index.raw_accession_fact,index.canonical_accession,index.census_accession|contract:shared_accession_reconciliation_indexed_v1''',
    10: b'''v10|alter:reporting_profile.latest_fallback_accepted_at,reporting_profile.fallback_document_sha256,reporting_profile.fallback_parser_version|create:inline_xbrl_fallback_run|contract:sealed_numeric_context_parser_v1,model_mapped_coverage_v1''',
}
STAGE4_MIGRATION_HISTORY = (
    (
        2,
        'shared_sec_accession_v2',
        hashlib.sha256(STAGE4_MIGRATION_MANIFESTS[2]).hexdigest(),
    ),
    (
        3,
        'immutable_sec_cache_snapshot_v3',
        hashlib.sha256(STAGE4_MIGRATION_MANIFESTS[3]).hexdigest(),
    ),
    (
        4,
        'association_retirement_v4',
        hashlib.sha256(STAGE4_MIGRATION_MANIFESTS[4]).hexdigest(),
    ),
    (
        5,
        'deterministic_ingestion_lifecycle_v5',
        hashlib.sha256(STAGE4_MIGRATION_MANIFESTS[5]).hexdigest(),
    ),
    (
        6,
        'monotonic_sec_ingestion_contract_v6',
        hashlib.sha256(STAGE4_MIGRATION_MANIFESTS[6]).hexdigest(),
    ),
    (
        7,
        'sealed_input_invalidation_v7',
        hashlib.sha256(STAGE4_MIGRATION_MANIFESTS[7]).hexdigest(),
    ),
    (
        8,
        'parser_semantic_seal_binding_v8',
        hashlib.sha256(STAGE4_MIGRATION_MANIFESTS[8]).hexdigest(),
    ),
    (
        9,
        'shared_accession_reconciliation_indexes_v9',
        hashlib.sha256(STAGE4_MIGRATION_MANIFESTS[9]).hexdigest(),
    ),
    (
        10,
        'sealed_inline_xbrl_fallback_v10',
        hashlib.sha256(STAGE4_MIGRATION_MANIFESTS[10]).hexdigest(),
    ),
)
# One short-lived pre-release build stamped the whole mutable schema as v2.
# Recognize only its exact known digest; all other checksum drift fails closed.
LEGACY_V2_SCHEMA_SHA256 = (
    '175e09c2348116fc8ec5c7e9359941471ddbd5145b89b33852e08043d32489f2'
)
LEGACY_MIGRATION_CHECKSUMS = {
    2: {
        LEGACY_V2_SCHEMA_SHA256,
        '639929b37edaacdc18f9d0cc83d057f61161e2f68202e037fbe683c050a38020',
    },
    3: {'133c8528ede95ddfe09cbaac9eadd0ed88149fd2148096d5bd659796ffc5fe66'},
    4: {'02e6461c59498b745be4a3bb910038912cfeaa3dd2ce8fc90bc2f6cd8dbc2081'},
}


def _schema_statements(script: str) -> tuple[str, ...]:
    '''Split DDL while preserving complete trigger bodies.'''
    statements: list[str] = []
    pending: list[str] = []
    for line in script.splitlines():
        pending.append(line)
        statement = chr(10).join(pending).strip()
        if statement and sqlite3.complete_statement(statement):
            statements.append(statement)
            pending.clear()
    if chr(10).join(pending).strip():
        raise sqlite3.OperationalError('Incomplete Stage4 schema statement')
    return tuple(statements)


STAGE4_SCHEMA_STATEMENTS = _schema_statements(STAGE4_SCHEMA_SQL)


def _quarantined_prerelease_schema_repair(conn: sqlite3.Connection) -> None:
    """Historical pre-v5 repair code retained only to decode legacy behavior.

    It is deliberately private and has no call sites.  The public migrator below
    applies immutable ordered units and never invokes this mutable-schema repair.
    """
    nested = conn.in_transaction
    conn.execute(
        'SAVEPOINT stage4_schema_migration' if nested else 'BEGIN IMMEDIATE'
    )
    try:
        bridge_columns = {str(row[1]) for row in conn.execute(
            'PRAGMA table_info(bridge_sec_filing_company)'
        )}
        if bridge_columns and 'association_status' not in bridge_columns:
            status_ddl = 'ALTER TABLE bridge_sec_filing_company ADD COLUMN '
            status_ddl += 'association_status TEXT NOT NULL DEFAULT '
            status_ddl += chr(39) + 'active' + chr(39)
            conn.execute(status_ddl)
        if bridge_columns and 'retirement_effective_asof' not in bridge_columns:
            conn.execute(
                'ALTER TABLE bridge_sec_filing_company ADD COLUMN '
                'retirement_effective_asof TEXT'
            )
        if bridge_columns and 'retirement_reason' not in bridge_columns:
            conn.execute(
                'ALTER TABLE bridge_sec_filing_company ADD COLUMN retirement_reason TEXT'
            )
        for statement in STAGE4_SCHEMA_STATEMENTS:
            conn.execute(statement)
        filing_columns = {
            str(row[1]) for row in conn.execute('PRAGMA table_info(fact_sec_filing)')
        }
        if 'metadata_quality_flags_json' not in filing_columns:
            conn.execute(
                'ALTER TABLE fact_sec_filing ADD COLUMN '
                'metadata_quality_flags_json TEXT NOT NULL DEFAULT \'[]\''
            )
        document_columns = {
            str(row[1]) for row in conn.execute(
                'PRAGMA table_info(bridge_sec_filing_document_company)'
            )
        }
        if 'inline_xbrl_verified' not in document_columns:
            conn.execute(
                'ALTER TABLE bridge_sec_filing_document_company ADD COLUMN '
                'inline_xbrl_verified INTEGER NOT NULL DEFAULT 0 '
                'CHECK(inline_xbrl_verified IN (0,1))'
            )
        reconciliation_columns = {
            str(row[1]) for row in conn.execute(
                'PRAGMA table_info(consumer_defensive_sec_reconciliation_state)'
            )
        }
        additions = (
            ('cache_manifest_json', '''TEXT NOT NULL DEFAULT '[]' '''),
            ('cache_root', '''TEXT NOT NULL DEFAULT '' '''),
        )
        for name, declaration in additions:
            if name not in reconciliation_columns:
                conn.execute(
                    'ALTER TABLE consumer_defensive_sec_reconciliation_state '
                    f'ADD COLUMN {name} {declaration}'
                )
        # Existing databases predate issuer associations. Preserve the legacy
        # issuer occurrence without rewriting its accession-level row.
        conn.execute(
            """
            INSERT OR IGNORE INTO bridge_sec_filing_company(
                accession_number,issuer_company_id,issuer_ticker,issuer_cik,
                relationship,relationship_evidence,form_type,filing_date,accepted_at,
                report_date,primary_document,source_id,source_url,created_at,updated_at
            )
            SELECT f.accession_number,f.company_id,f.ticker,
                   printf('%010d',CAST(f.cik AS INTEGER)),
                   'associated_via_submissions',
                   'legacy_fact_sec_filing_backfill',f.form_type,f.filing_date,f.accepted_at,
                   f.report_date,f.primary_document,f.source_id,
                   'https://www.sec.gov/Archives/edgar/data/' ||
                   CAST(CAST(f.cik AS INTEGER) AS TEXT) || '/' ||
                   REPLACE(f.accession_number,'-','') || '/' ||
                   COALESCE(f.primary_document,''),
                   f.created_at,f.updated_at
            FROM fact_sec_filing AS f
            WHERE f.company_id IS NOT NULL AND COALESCE(f.cik,'')<>''
            """
        )
        conn.execute(
            '''
            INSERT OR IGNORE INTO bridge_sec_filing_document_company(
                accession_number,issuer_company_id,source_id,issuer_ticker,issuer_cik,
                accepted_at,primary_document,source_url,content_sha256,cache_path,
                hydration_status,updated_at
            )
            SELECT d.accession_number,b.issuer_company_id,d.source_id,b.issuer_ticker,
                   b.issuer_cik,f.accepted_at,d.primary_document,
                   COALESCE(NULLIF(b.source_url,''),d.source_url),d.content_sha256,
                   d.cache_path,d.hydration_status,d.updated_at
            FROM fact_sec_filing_document AS d
            JOIN bridge_sec_filing_company AS b
              ON b.accession_number=d.accession_number
             AND b.issuer_ticker=d.ticker
            JOIN fact_sec_filing AS f ON f.accession_number=d.accession_number
            '''
        )
        summary_columns = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(fact_specialized_metric_disclosure_summary)")
        }
        if summary_columns and "asof_date" not in summary_columns:
            conn.execute(
                "ALTER TABLE fact_specialized_metric_disclosure_summary RENAME TO fact_specialized_metric_disclosure_summary_legacy"
            )
            conn.execute(
                """CREATE TABLE fact_specialized_metric_disclosure_summary (
                       ticker TEXT NOT NULL,
                       metric_id TEXT NOT NULL,
                       calibration_cohort_id TEXT NOT NULL,
                       applicability_subtype TEXT NOT NULL,
                       asof_date TEXT NOT NULL,
                       applicability_status TEXT NOT NULL,
                       filings_searched INTEGER NOT NULL,
                       filings_with_hits INTEGER NOT NULL,
                       disclosure_status TEXT NOT NULL,
                       first_disclosure_accepted_at TEXT,
                       last_disclosure_accepted_at TEXT,
                       parser_version TEXT NOT NULL,
                       source_id TEXT NOT NULL,
                       updated_at TEXT NOT NULL,
                       PRIMARY KEY(ticker, metric_id, parser_version, asof_date),
                       FOREIGN KEY(metric_id) REFERENCES dim_specialized_metric(metric_id) ON DELETE RESTRICT,
                       FOREIGN KEY(source_id) REFERENCES source_registry(source_id) ON DELETE RESTRICT
                   )"""
            )
            conn.execute(
                """INSERT INTO fact_specialized_metric_disclosure_summary(
                       ticker,metric_id,calibration_cohort_id,applicability_subtype,asof_date,
                       applicability_status,filings_searched,filings_with_hits,disclosure_status,
                       first_disclosure_accepted_at,last_disclosure_accepted_at,parser_version,source_id,updated_at
                   )
                   SELECT ticker,metric_id,calibration_cohort_id,applicability_subtype,
                          COALESCE(NULLIF(substr(updated_at,1,10),''),date('now')),
                          applicability_status,filings_searched,filings_with_hits,disclosure_status,
                          first_disclosure_accepted_at,last_disclosure_accepted_at,parser_version,source_id,updated_at
                   FROM fact_specialized_metric_disclosure_summary_legacy"""
            )
            conn.execute("DROP TABLE fact_specialized_metric_disclosure_summary_legacy")
        census_source_registered = conn.execute(
            "SELECT 1 FROM source_registry WHERE source_id=?", (DISCLOSURE_SOURCE,)
        ).fetchone()
        if census_source_registered:
            conn.execute(
                """UPDATE fact_specialized_metric_disclosure_census
                   SET source_id=? WHERE source_id='shared_dedicated_sec_parser'""",
                (DISCLOSURE_SOURCE,),
            )
            conn.execute(
                """UPDATE fact_specialized_metric_disclosure_summary
                   SET source_id=? WHERE source_id='shared_dedicated_sec_parser'""",
                (DISCLOSURE_SOURCE,),
            )
        conn.execute(
            """UPDATE fact_specialized_metric_disclosure_summary
               SET disclosure_status=CASE disclosure_status
                   WHEN 'applicable_and_disclosed' THEN 'applicable_term_hit'
                   WHEN 'applicable_not_disclosed' THEN 'applicable_no_term_hit'
                   ELSE disclosure_status END"""
        )
        migration_columns = {
            str(row[1]) for row in conn.execute(
                'PRAGMA table_info(consumer_defensive_stage4_schema_migration)'
            )
        }
        for name in ('migration_name', 'migration_sha256'):
            if name not in migration_columns:
                ddl = (
                    'ALTER TABLE consumer_defensive_stage4_schema_migration '
                    + 'ADD COLUMN migration_name TEXT NOT NULL DEFAULT \'\''
                    if name == 'migration_name'
                    else 'ALTER TABLE consumer_defensive_stage4_schema_migration '
                    + 'ADD COLUMN migration_sha256 TEXT NOT NULL DEFAULT \'\''
                )
                conn.execute(ddl)
        ledger = conn.execute(
            'SELECT migration_version,migration_name,migration_sha256,status '
            'FROM consumer_defensive_stage4_schema_migration '
            'ORDER BY migration_version'
        ).fetchall()
        if ledger:
            versions = [int(row[0]) for row in ledger]
            expected_prefix = [row[0] for row in STAGE4_MIGRATION_HISTORY[:len(ledger)]]
            if versions != expected_prefix:
                raise RuntimeError('Stage4 migration ledger has a gap or future version')
            for actual, expected in zip(
                ledger, STAGE4_MIGRATION_HISTORY[:len(ledger)], strict=True
            ):
                version, name, checksum = expected
                actual_checksum = str(actual[2])
                checksum_valid = actual_checksum == checksum or (
                    version == 2
                    and actual_checksum == LEGACY_V2_SCHEMA_SHA256
                )
                if (
                    str(actual[1]) != name
                    or not checksum_valid
                    or str(actual[3]) != 'complete'
                ):
                    raise RuntimeError('Stage4 migration ledger checksum mismatch')
        missing_legacy_filings = conn.execute(
            'SELECT COUNT(*) FROM fact_sec_filing f '
            'WHERE f.company_id IS NOT NULL AND COALESCE(f.cik,\'\')<>\'\' '
            'AND NOT EXISTS (SELECT 1 FROM bridge_sec_filing_company b '
            'WHERE b.accession_number=f.accession_number '
            'AND b.issuer_company_id=f.company_id)'
        ).fetchone()[0]
        missing_legacy_documents = conn.execute(
            'SELECT COUNT(*) FROM fact_sec_filing_document d '
            'JOIN bridge_sec_filing_company b '
            'ON b.accession_number=d.accession_number AND b.issuer_ticker=d.ticker '
            'WHERE NOT EXISTS (SELECT 1 FROM bridge_sec_filing_document_company x '
            'WHERE x.accession_number=d.accession_number '
            'AND x.issuer_company_id=b.issuer_company_id '
            'AND x.source_id=d.source_id)'
        ).fetchone()[0]
        if missing_legacy_filings or missing_legacy_documents:
            raise RuntimeError('Stage4 migration backfill parity failed')
        if conn.execute('PRAGMA foreign_key_check').fetchone() is not None:
            raise RuntimeError('Stage4 migration failed foreign-key postcondition')
        applied_versions = {int(row[0]) for row in ledger}
        conn.executemany(
            '''INSERT INTO consumer_defensive_stage4_schema_migration(
                   migration_version,migration_name,migration_sha256,status,applied_at)
               VALUES(?,?,?,'complete',?)''',
            [
                (version, name, checksum, utc_now())
                for version, name, checksum in STAGE4_MIGRATION_HISTORY
                if version not in applied_versions
            ],
        )
        if nested:
            conn.execute('RELEASE SAVEPOINT stage4_schema_migration')
        else:
            conn.commit()
    except BaseException:
        if nested:
            conn.execute('ROLLBACK TO SAVEPOINT stage4_schema_migration')
            conn.execute('RELEASE SAVEPOINT stage4_schema_migration')
        else:
            conn.rollback()
        raise


# Ordered migration units supersede the pre-v5 mutable-schema migrator above.
# Versions 2-4 retain their published ledger identities; v5 is additive.


def _stage4_add_column(
    conn: sqlite3.Connection, table: str, column: str, declaration: str
) -> None:
    columns = {str(row[1]) for row in conn.execute(f'PRAGMA table_info({table})')}
    if column not in columns:
        conn.execute(f'ALTER TABLE {table} ADD COLUMN {column} {declaration}')


def _stage4_execute_sql(conn: sqlite3.Connection, sql: str) -> None:
    for statement in _schema_statements(sql):
        conn.execute(statement)


def _stage4_migration_v2(conn: sqlite3.Connection) -> None:
    """Frozen shared-accession foundation; safe on populated legacy databases."""
    _stage4_execute_sql(conn, '''
    CREATE TABLE IF NOT EXISTS dim_issuer_reporting_profile (
        ticker TEXT PRIMARY KEY, cik TEXT, primary_annual_form TEXT,
        foreign_issuer_flag INTEGER NOT NULL DEFAULT 0,
        us_gaap_flag INTEGER NOT NULL DEFAULT 0,
        ifrs_flag INTEGER NOT NULL DEFAULT 0,
        latest_filing_accepted_at TEXT,
        latest_companyfacts_accepted_at TEXT,
        companyfacts_lag_days INTEGER,
        inline_xbrl_fallback_required INTEGER NOT NULL DEFAULT 0,
        coverage_status TEXT NOT NULL, review_reason TEXT, updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS fact_sec_filing_document (
        accession_number TEXT NOT NULL, ticker TEXT NOT NULL,
        form_type TEXT NOT NULL, accepted_at TEXT, primary_document TEXT,
        source_url TEXT NOT NULL, content_sha256 TEXT, cache_path TEXT,
        hydration_status TEXT NOT NULL, source_id TEXT NOT NULL,
        updated_at TEXT NOT NULL, PRIMARY KEY(accession_number,source_id),
        FOREIGN KEY(accession_number) REFERENCES fact_sec_filing(accession_number)
            ON DELETE CASCADE,
        FOREIGN KEY(source_id) REFERENCES source_registry(source_id) ON DELETE RESTRICT
    );
    CREATE TABLE IF NOT EXISTS bridge_sec_filing_document_company (
        accession_number TEXT NOT NULL, issuer_company_id INTEGER NOT NULL,
        source_id TEXT NOT NULL, issuer_ticker TEXT NOT NULL,
        issuer_cik TEXT NOT NULL, accepted_at TEXT, primary_document TEXT,
        source_url TEXT NOT NULL, content_sha256 TEXT, cache_path TEXT,
        hydration_status TEXT NOT NULL, updated_at TEXT NOT NULL,
        PRIMARY KEY(accession_number,issuer_company_id,source_id),
        FOREIGN KEY(accession_number,issuer_company_id)
            REFERENCES bridge_sec_filing_company(accession_number,issuer_company_id)
            ON DELETE CASCADE,
        FOREIGN KEY(source_id) REFERENCES source_registry(source_id) ON DELETE RESTRICT
    );
    CREATE TABLE IF NOT EXISTS consumer_defensive_sec_reconciliation_state (
        asof_date TEXT PRIMARY KEY, cutoff TEXT NOT NULL,
        scope_issuer_count INTEGER NOT NULL, association_count INTEGER NOT NULL,
        accession_count INTEGER NOT NULL, shared_accession_count INTEGER NOT NULL,
        association_sha256 TEXT NOT NULL, cache_manifest_sha256 TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status='complete'), completed_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS fact_specialized_metric_disclosure_census (
        ticker TEXT NOT NULL, accession_number TEXT NOT NULL, metric_id TEXT NOT NULL,
        calibration_cohort_id TEXT NOT NULL, applicability_subtype TEXT NOT NULL,
        accepted_at TEXT, form_type TEXT NOT NULL, hit_count INTEGER NOT NULL,
        matched_terms_json TEXT NOT NULL, evidence_json TEXT NOT NULL,
        parser_version TEXT NOT NULL, source_id TEXT NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY(ticker,accession_number,metric_id,parser_version),
        FOREIGN KEY(metric_id) REFERENCES dim_specialized_metric(metric_id)
            ON DELETE RESTRICT,
        FOREIGN KEY(source_id) REFERENCES source_registry(source_id) ON DELETE RESTRICT
    );
    CREATE TABLE IF NOT EXISTS fact_specialized_metric_disclosure_summary (
        ticker TEXT NOT NULL, metric_id TEXT NOT NULL,
        calibration_cohort_id TEXT NOT NULL, applicability_subtype TEXT NOT NULL,
        asof_date TEXT NOT NULL, applicability_status TEXT NOT NULL,
        filings_searched INTEGER NOT NULL, filings_with_hits INTEGER NOT NULL,
        disclosure_status TEXT NOT NULL, first_disclosure_accepted_at TEXT,
        last_disclosure_accepted_at TEXT, parser_version TEXT NOT NULL,
        source_id TEXT NOT NULL, updated_at TEXT NOT NULL,
        PRIMARY KEY(ticker,metric_id,parser_version,asof_date),
        FOREIGN KEY(metric_id) REFERENCES dim_specialized_metric(metric_id)
            ON DELETE RESTRICT,
        FOREIGN KEY(source_id) REFERENCES source_registry(source_id) ON DELETE RESTRICT
    );
    CREATE INDEX IF NOT EXISTS idx_stage4_raw_ticker_source_accepted
        ON fact_sec_xbrl_fact_raw(ticker,source_id,accepted_at);
    CREATE INDEX IF NOT EXISTS idx_stage4_canonical_ticker_accepted
        ON fact_financial_statement_canonical(ticker,accepted_at,canonical_metric);
    CREATE INDEX IF NOT EXISTS idx_stage4_canonical_raw_fact_id
        ON fact_financial_statement_canonical(source_raw_fact_id);
    ''')
    _stage4_add_column(
        conn, 'fact_sec_filing', 'metadata_quality_flags_json',
        "TEXT NOT NULL DEFAULT '[]'",
    )
    conn.execute('DROP INDEX IF EXISTS idx_stage4_raw_ticker_accepted')
    conn.execute('''
        INSERT OR IGNORE INTO bridge_sec_filing_company(
            accession_number,issuer_company_id,issuer_ticker,issuer_cik,
            relationship,relationship_evidence,form_type,filing_date,accepted_at,
            report_date,primary_document,source_id,source_url,created_at,updated_at
        )
        SELECT f.accession_number,f.company_id,f.ticker,
               printf('%010d',CAST(f.cik AS INTEGER)),
               'associated_via_submissions','legacy_fact_sec_filing_backfill',
               f.form_type,f.filing_date,f.accepted_at,f.report_date,
               f.primary_document,f.source_id,
               'https://www.sec.gov/Archives/edgar/data/' ||
               CAST(CAST(f.cik AS INTEGER) AS TEXT) || '/' ||
               REPLACE(f.accession_number,'-','') || '/' ||
               COALESCE(f.primary_document,''),f.created_at,f.updated_at
        FROM fact_sec_filing f
        WHERE f.company_id IS NOT NULL AND COALESCE(f.cik,'')<>''
    ''')
    conn.execute('''
        INSERT OR IGNORE INTO bridge_sec_filing_document_company(
            accession_number,issuer_company_id,source_id,issuer_ticker,issuer_cik,
            accepted_at,primary_document,source_url,content_sha256,cache_path,
            hydration_status,updated_at
        )
        SELECT d.accession_number,b.issuer_company_id,d.source_id,b.issuer_ticker,
               b.issuer_cik,f.accepted_at,d.primary_document,
               COALESCE(NULLIF(b.source_url,''),d.source_url),d.content_sha256,
               d.cache_path,d.hydration_status,d.updated_at
        FROM fact_sec_filing_document d
        JOIN bridge_sec_filing_company b
          ON b.accession_number=d.accession_number AND b.issuer_ticker=d.ticker
        JOIN fact_sec_filing f ON f.accession_number=d.accession_number
    ''')


def _stage4_migration_v3(conn: sqlite3.Connection) -> None:
    """Frozen immutable cache-seal storage migration."""
    _stage4_add_column(
        conn, 'consumer_defensive_sec_reconciliation_state',
        'cache_manifest_json', "TEXT NOT NULL DEFAULT '[]'",
    )
    _stage4_add_column(
        conn, 'consumer_defensive_sec_reconciliation_state',
        'cache_root', "TEXT NOT NULL DEFAULT ''",
    )
    conn.execute('''CREATE TABLE IF NOT EXISTS consumer_defensive_sec_cache_snapshot(
        asof_date TEXT PRIMARY KEY, seal_relative_path TEXT NOT NULL,
        cache_manifest_sha256 TEXT NOT NULL, cache_manifest_json TEXT NOT NULL,
        created_at TEXT NOT NULL)''')


def _stage4_recreate_parser_view(conn: sqlite3.Connection) -> None:
    conn.execute('DROP VIEW IF EXISTS consumer_defensive_sec_parser_filing_input')
    conn.execute('''CREATE VIEW consumer_defensive_sec_parser_filing_input AS
        SELECT b.issuer_ticker AS ticker,b.issuer_cik AS cik,
               b.issuer_cik AS archive_cik,f.accession_number,b.form_type,
               b.filing_date,f.accepted_at AS accepted_at,
               b.accepted_at AS observed_accepted_at,b.report_date,
               b.primary_document,b.source_id,b.relationship,b.issuer_company_id,
               b.association_status,b.retirement_effective_asof,
               NULL AS fiscal_year,NULL AS fiscal_period,
               COALESCE(NULLIF(UPPER(TRIM(c.reporting_currency)),''),'USD')
                   AS company_currency
        FROM bridge_sec_filing_company b
        JOIN fact_sec_filing f ON f.accession_number=b.accession_number
        JOIN dim_company c ON c.company_id=b.issuer_company_id''')


def _stage4_migration_v4(conn: sqlite3.Connection) -> None:
    """Frozen non-destructive current-projection retirement migration."""
    _stage4_add_column(
        conn, 'bridge_sec_filing_company', 'association_status',
        "TEXT NOT NULL DEFAULT 'active'",
    )
    _stage4_add_column(
        conn, 'bridge_sec_filing_company', 'retirement_effective_asof', 'TEXT'
    )
    _stage4_add_column(
        conn, 'bridge_sec_filing_company', 'retirement_reason', 'TEXT'
    )
    _stage4_add_column(
        conn, 'bridge_sec_filing_document_company', 'inline_xbrl_verified',
        'INTEGER NOT NULL DEFAULT 0 CHECK(inline_xbrl_verified IN (0,1))',
    )
    _stage4_execute_sql(conn, '''
    DROP TRIGGER IF EXISTS trg_sec_bridge_invalidate_reconciliation_insert;
    CREATE TRIGGER trg_sec_bridge_invalidate_reconciliation_insert
    AFTER INSERT ON bridge_sec_filing_company BEGIN
        DELETE FROM consumer_defensive_sec_reconciliation_state;
    END;
    DROP TRIGGER IF EXISTS trg_sec_bridge_invalidate_reconciliation_delete;
    CREATE TRIGGER trg_sec_bridge_invalidate_reconciliation_delete
    AFTER DELETE ON bridge_sec_filing_company BEGIN
        DELETE FROM consumer_defensive_sec_reconciliation_state;
    END;
    DROP TRIGGER IF EXISTS trg_sec_bridge_invalidate_reconciliation_update;
    CREATE TRIGGER trg_sec_bridge_invalidate_reconciliation_update
    AFTER UPDATE ON bridge_sec_filing_company BEGIN
        DELETE FROM consumer_defensive_sec_reconciliation_state;
    END;
    ''')
    _stage4_recreate_parser_view(conn)


_RAW_OBSERVATION_FIELDS = (
    'ticker','cik','accession_number','taxonomy','concept','value_text',
    'numeric_value','unit','period_start','period_end','filed_date','accepted_at',
    'form_type','frame','dimensions_json','source_id','source_detail',
)


def _source_observation_id(values: Iterable[Any]) -> str:
    encoded = json.dumps(
        list(values), ensure_ascii=True, separators=(',', ':'), allow_nan=False
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _association_event_sha256(
    accession: str, company_id: int, ticker: str, cik: str,
    effective_asof: str, event_type: str, reason: str,
) -> str:
    return hashlib.sha256(json.dumps(
        [accession,company_id,ticker,cik,effective_asof,event_type,reason],
        ensure_ascii=True,separators=(',', ':'),
    ).encode()).hexdigest()


def _append_association_event(
    conn: sqlite3.Connection, *, accession: str, company_id: int,
    ticker: str, cik: str, effective_asof: str, event_type: str, reason: str,
) -> None:
    digest = _association_event_sha256(
        accession,company_id,ticker,cik,effective_asof,event_type,reason
    )
    conn.execute('''INSERT OR IGNORE INTO sec_filing_company_association_event(
        accession_number,issuer_company_id,issuer_ticker,issuer_cik,effective_asof,
        event_type,reason,event_sha256,created_at) VALUES(?,?,?,?,?,?,?,?,?)''',
        (accession,company_id,ticker,cik,effective_asof,event_type,reason,digest,utc_now()))


def _backfill_source_observation_ids(conn: sqlite3.Connection, *, exact: bool = False, batch_size: int = 2_048) -> None:
    '''Repair raw identities with bounded keyset batches.'''
    if batch_size < 1:
        raise ValueError('source-observation backfill batch_size must be positive')
    fields = ','.join(_RAW_OBSERVATION_FIELDS)
    predicates = (
        ('',) if exact else (
            'AND source_observation_id IS NULL',
            "AND source_observation_id=''",
        )
    )
    for predicate in predicates:
        last_id = 0
        while True:
            rows = conn.execute(
                f'''SELECT raw_fact_id,{fields},source_observation_id
                    FROM fact_sec_xbrl_fact_raw
                    WHERE raw_fact_id>? {predicate}
                    ORDER BY raw_fact_id LIMIT ?''',
                (last_id, batch_size),
            ).fetchall()
            if not rows:
                break
            updates: list[tuple[str, int]] = []
            for row in rows:
                last_id = int(row[0])
                digest = _source_observation_id(row[1:-1])
                if str(row[-1] or '') != digest:
                    updates.append((digest, last_id))
            if updates:
                conn.executemany(
                    'UPDATE fact_sec_xbrl_fact_raw '
                    'SET source_observation_id=? WHERE raw_fact_id=?', updates,
                )
                conn.executemany('''UPDATE fact_financial_statement_canonical
                    SET source_observation_id=? WHERE source_raw_fact_id=?
                      AND source_observation_id IS NOT ?''',
                    [(digest, raw_id, digest) for digest, raw_id in updates],
                )
    if exact:
        conn.execute('''UPDATE fact_financial_statement_canonical
            SET source_observation_id=(SELECT r.source_observation_id
                FROM fact_sec_xbrl_fact_raw r
                WHERE r.raw_fact_id=
                    fact_financial_statement_canonical.source_raw_fact_id)
            WHERE source_raw_fact_id IS NOT NULL
              AND source_observation_id IS NOT (SELECT r.source_observation_id
                  FROM fact_sec_xbrl_fact_raw r WHERE r.raw_fact_id=
                      fact_financial_statement_canonical.source_raw_fact_id)''')
        return
    for predicate in (
        'c.source_observation_id IS NULL', "c.source_observation_id=''",
    ):
        last_canonical_id = 0
        while True:
            canonical_rows = conn.execute(f'''SELECT c.canonical_fact_id,
                r.source_observation_id
                FROM fact_financial_statement_canonical c
                JOIN fact_sec_xbrl_fact_raw r
                  ON r.raw_fact_id=c.source_raw_fact_id
                WHERE c.canonical_fact_id>? AND {predicate}
                  AND r.source_observation_id IS NOT NULL
                  AND r.source_observation_id<>''
                ORDER BY c.canonical_fact_id LIMIT ?''',
                (last_canonical_id, batch_size),
            ).fetchall()
            if not canonical_rows:
                break
            last_canonical_id = int(canonical_rows[-1][0])
            conn.executemany('''UPDATE fact_financial_statement_canonical
                SET source_observation_id=? WHERE canonical_fact_id=?''',
                [(str(row[1]), int(row[0])) for row in canonical_rows],
            )


def _backfill_association_events(
    conn: sqlite3.Connection, *, batch_size: int = 2_048
) -> None:
    '''Backfill association history with bounded composite-key batches.'''
    if batch_size < 1:
        raise ValueError('association-event backfill batch_size must be positive')
    last_accession = ''
    last_company_id = -1
    while True:
        rows = conn.execute('''SELECT b.accession_number,b.issuer_company_id,
            b.issuer_ticker,b.issuer_cik,b.accepted_at,b.filing_date,b.created_at,
            b.association_status,b.retirement_effective_asof,b.retirement_reason
            FROM bridge_sec_filing_company b
            WHERE (b.accession_number>? OR
                  (b.accession_number=? AND b.issuer_company_id>?))
              AND NOT EXISTS(SELECT 1 FROM sec_filing_company_association_event e
                WHERE e.accession_number=b.accession_number
                  AND e.issuer_company_id=b.issuer_company_id)
            ORDER BY b.accession_number,b.issuer_company_id LIMIT ?''',
            (last_accession,last_accession,last_company_id,batch_size),
        ).fetchall()
        if not rows:
            break
        for row in rows:
            last_accession = str(row[0])
            last_company_id = int(row[1])
            observed = str(row[4] or row[5] or row[6] or '1900-01-01')
            _append_association_event(
                conn, accession=last_accession, company_id=last_company_id,
                ticker=str(row[2]), cik=str(row[3]), effective_asof=observed,
                event_type='observed', reason='v5_legacy_projection_backfill',
            )
            if str(row[7]) == 'retired' and row[8]:
                _append_association_event(
                    conn, accession=last_accession, company_id=last_company_id,
                    ticker=str(row[2]), cik=str(row[3]),
                    effective_asof=str(row[8])[:10] + 'T00:00:00Z',
                    event_type='retired',
                    reason=str(row[9] or 'v5_legacy_retirement_backfill'),
                )


def _refresh_source_observation_ids_for_accession(
    conn: sqlite3.Connection, accession: str, *, batch_size: int = 2_048
) -> None:
    fields = ','.join(_RAW_OBSERVATION_FIELDS)
    last_id = 0
    while True:
        rows = conn.execute(
            f'''SELECT raw_fact_id,{fields},source_observation_id
                FROM fact_sec_xbrl_fact_raw
                WHERE accession_number=? AND raw_fact_id>?
                ORDER BY raw_fact_id LIMIT ?''',(accession,last_id,batch_size),
        ).fetchall()
        if not rows:
            break
        updates: list[tuple[str,int]] = []
        for row in rows:
            last_id = int(row[0])
            digest = _source_observation_id(row[1:-1])
            if digest != str(row[-1] or ''):
                updates.append((digest,last_id))
        if updates:
            conn.executemany('''UPDATE fact_sec_xbrl_fact_raw
                SET source_observation_id=? WHERE raw_fact_id=?''',updates)
    conn.execute('''UPDATE fact_financial_statement_canonical
        SET source_observation_id=(SELECT r.source_observation_id
            FROM fact_sec_xbrl_fact_raw r
            WHERE r.raw_fact_id=fact_financial_statement_canonical.source_raw_fact_id)
        WHERE accession_number=? AND source_raw_fact_id IS NOT NULL''',(accession,))


def _count_raw_observation_identity_mismatches(
    conn: sqlite3.Connection, *, cutoff: str, batch_size: int = 2_048
) -> int:
    fields = ','.join(_RAW_OBSERVATION_FIELDS)
    last_id = 0
    mismatches = 0
    while True:
        rows = conn.execute(
            f'''SELECT raw_fact_id,{fields},source_observation_id
                FROM fact_sec_xbrl_fact_raw
                WHERE accepted_at<=? AND raw_fact_id>?
                ORDER BY raw_fact_id LIMIT ?''',(cutoff,last_id,batch_size),
        ).fetchall()
        if not rows:
            break
        for row in rows:
            last_id = int(row[0])
            mismatches += int(
                str(row[-1] or '') != _source_observation_id(row[1:-1])
            )
    return mismatches


def _count_association_event_identity_mismatches(
    conn: sqlite3.Connection, *, cutoff: str, batch_size: int = 2_048
) -> int:
    last_id = 0
    mismatches = 0
    while True:
        rows = conn.execute('''SELECT event_id,accession_number,
            issuer_company_id,issuer_ticker,issuer_cik,effective_asof,
            event_type,reason,event_sha256
            FROM sec_filing_company_association_event
            WHERE effective_asof<=? AND event_id>?
            ORDER BY event_id LIMIT ?''',(cutoff,last_id,batch_size)).fetchall()
        if not rows:
            break
        for row in rows:
            last_id = int(row[0])
            expected = _association_event_sha256(
                str(row[1]),int(row[2]),str(row[3]),str(row[4]),
                str(row[5]),str(row[6]),str(row[7]),
            )
            mismatches += int(str(row[8] or '') != expected)
    return mismatches


def _stage4_migration_v5(conn: sqlite3.Connection) -> None:
    """Frozen config/scope, deterministic-lineage, and lifecycle-event migration."""
    for table in (
        'consumer_defensive_sec_reconciliation_state',
        'consumer_defensive_sec_cache_snapshot',
    ):
        _stage4_add_column(
            conn, table, 'ingestion_config_sha256', "TEXT NOT NULL DEFAULT ''"
        )
        _stage4_add_column(
            conn, table, 'issuer_scope_sha256', "TEXT NOT NULL DEFAULT ''"
        )
    _stage4_add_column(
        conn, 'fact_sec_xbrl_fact_raw', 'source_observation_id', 'TEXT'
    )
    _stage4_add_column(
        conn, 'fact_financial_statement_canonical', 'source_observation_id', 'TEXT'
    )
    _stage4_execute_sql(conn, '''
    CREATE TABLE IF NOT EXISTS sec_filing_company_association_event (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        accession_number TEXT NOT NULL, issuer_company_id INTEGER NOT NULL,
        issuer_ticker TEXT NOT NULL, issuer_cik TEXT NOT NULL,
        effective_asof TEXT NOT NULL,
        event_type TEXT NOT NULL CHECK(event_type IN ('observed','retired','reactivated')),
        reason TEXT NOT NULL, event_sha256 TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL,
        FOREIGN KEY(accession_number) REFERENCES fact_sec_filing(accession_number)
            ON DELETE CASCADE,
        FOREIGN KEY(issuer_company_id) REFERENCES dim_company(company_id)
            ON DELETE CASCADE
    );
    CREATE INDEX IF NOT EXISTS idx_stage4_association_event_pit
        ON sec_filing_company_association_event(
            accession_number,issuer_company_id,effective_asof,event_id);
    CREATE INDEX IF NOT EXISTS idx_stage4_raw_source_observation_id
        ON fact_sec_xbrl_fact_raw(source_observation_id);
    CREATE INDEX IF NOT EXISTS idx_stage4_canonical_source_observation_id
        ON fact_financial_statement_canonical(source_observation_id);
    CREATE TRIGGER IF NOT EXISTS trg_stage4_association_event_no_update
    BEFORE UPDATE ON sec_filing_company_association_event BEGIN
        SELECT RAISE(ABORT,'association lifecycle events are append-only');
    END;
    CREATE TRIGGER IF NOT EXISTS trg_stage4_association_event_no_delete
    BEFORE DELETE ON sec_filing_company_association_event BEGIN
        SELECT RAISE(ABORT,'association lifecycle events are append-only');
    END;
    ''')
    _backfill_source_observation_ids(conn)
    _backfill_association_events(conn)
    _stage4_recreate_parser_view(conn)


def _stage4_migration_v6(conn: sqlite3.Connection) -> None:
    '''Frozen monotonic SEC-ingestion contract and exact lineage repair.'''
    conn.execute('''CREATE TABLE IF NOT EXISTS consumer_defensive_sec_ingestion_watermark(
        model_family TEXT PRIMARY KEY CHECK(model_family='consumer_defensive'),
        asof_date TEXT NOT NULL,
        cutoff TEXT NOT NULL,
        mutation_kind TEXT NOT NULL,
        updated_at TEXT NOT NULL)''')
    candidates: list[str] = []
    sources = (
        ('consumer_defensive_sec_reconciliation_state','asof_date'),
        ('consumer_defensive_sec_cache_snapshot','asof_date'),
        ('feature_financial_statement','asof_date'),
        ('fact_specialized_metric_disclosure_summary','asof_date'),
        ('sec_parser_run','asof_date'),
    )
    for table, column in sources:
        exists = conn.execute(
            '''SELECT 1 FROM sqlite_master WHERE type='table' AND name=?''',
            (table,),
        ).fetchone()
        columns = (
            {str(row[1]) for row in conn.execute(f'PRAGMA table_info({table})')}
            if exists else set()
        )
        if column in columns:
            value = conn.execute(
                f'''SELECT MAX(SUBSTR({column},1,10)) FROM {table}
                    WHERE COALESCE({column},'')<>'' '''
            ).fetchone()[0]
            if value:
                candidates.append(str(value))
    if candidates:
        asof_date = max(candidates)
        conn.execute('''INSERT INTO consumer_defensive_sec_ingestion_watermark(
            model_family,asof_date,cutoff,mutation_kind,updated_at)
            VALUES(?,?,?,?,?) ON CONFLICT(model_family) DO NOTHING''',
            (MODEL_FAMILY,asof_date,asof_date+'T23:59:59Z',
             'v6_conservative_backfill',utc_now()),
        )
    _backfill_source_observation_ids(conn, exact=True)
    _backfill_association_events(conn)


def _stage4_migration_v7(conn: sqlite3.Connection) -> None:
    '''Frozen invalidation contract for every mutable sealed parser input.'''
    _stage4_execute_sql(conn, '''
    CREATE TRIGGER IF NOT EXISTS trg_stage4_document_bridge_invalidate_insert
    AFTER INSERT ON bridge_sec_filing_document_company BEGIN
        DELETE FROM consumer_defensive_sec_reconciliation_state;
    END;
    CREATE TRIGGER IF NOT EXISTS trg_stage4_document_bridge_invalidate_delete
    AFTER DELETE ON bridge_sec_filing_document_company BEGIN
        DELETE FROM consumer_defensive_sec_reconciliation_state;
    END;
    CREATE TRIGGER IF NOT EXISTS trg_stage4_document_bridge_invalidate_update
    AFTER UPDATE OF accession_number,issuer_company_id,source_id,issuer_ticker,
        issuer_cik,accepted_at,primary_document,source_url,content_sha256,
        cache_path,hydration_status,inline_xbrl_verified
    ON bridge_sec_filing_document_company
    WHEN OLD.accession_number IS NOT NEW.accession_number
      OR OLD.issuer_company_id IS NOT NEW.issuer_company_id
      OR OLD.source_id IS NOT NEW.source_id
      OR OLD.issuer_ticker IS NOT NEW.issuer_ticker
      OR OLD.issuer_cik IS NOT NEW.issuer_cik
      OR OLD.accepted_at IS NOT NEW.accepted_at
      OR OLD.primary_document IS NOT NEW.primary_document
      OR OLD.source_url IS NOT NEW.source_url
      OR OLD.content_sha256 IS NOT NEW.content_sha256
      OR OLD.cache_path IS NOT NEW.cache_path
      OR OLD.hydration_status IS NOT NEW.hydration_status
      OR OLD.inline_xbrl_verified IS NOT NEW.inline_xbrl_verified
    BEGIN
        DELETE FROM consumer_defensive_sec_reconciliation_state;
    END;
    CREATE TRIGGER IF NOT EXISTS trg_stage4_association_event_invalidate_insert
    AFTER INSERT ON sec_filing_company_association_event BEGIN
        DELETE FROM consumer_defensive_sec_reconciliation_state;
    END;
    ''')
    # A pre-v7 writer could have changed either input without invalidating its
    # reconciliation. Snapshot rows are retained only for the successor
    # migration to quarantine under the new semantic identity contract.
    conn.execute('DELETE FROM consumer_defensive_sec_reconciliation_state')


def _stage4_migration_v8(conn: sqlite3.Connection) -> None:
    '''Freeze mutable parser semantics and quarantine legacy seals in place.'''
    for table in (
        'consumer_defensive_sec_reconciliation_state',
        'consumer_defensive_sec_cache_snapshot',
    ):
        _stage4_add_column(
            conn, table, 'scope_contract_version',
            'INTEGER NOT NULL DEFAULT 2',
        )
        _stage4_add_column(
            conn, table, 'trust_state',
            "TEXT NOT NULL DEFAULT 'quarantined_legacy_scope_v2'",
        )
        _stage4_add_column(conn, table, 'quarantine_reason', 'TEXT')
    _stage4_execute_sql(conn, '''
    DROP TRIGGER IF EXISTS trg_sec_bridge_invalidate_reconciliation_insert;
    CREATE TRIGGER trg_sec_bridge_invalidate_reconciliation_insert
    AFTER INSERT ON bridge_sec_filing_company BEGIN
        UPDATE consumer_defensive_sec_reconciliation_state
        SET trust_state='invalidated_by_mutation',
            quarantine_reason='bridge_sec_filing_company_insert'
        WHERE trust_state='trusted_current';
    END;
    DROP TRIGGER IF EXISTS trg_sec_bridge_invalidate_reconciliation_delete;
    CREATE TRIGGER trg_sec_bridge_invalidate_reconciliation_delete
    AFTER DELETE ON bridge_sec_filing_company BEGIN
        UPDATE consumer_defensive_sec_reconciliation_state
        SET trust_state='invalidated_by_mutation',
            quarantine_reason='bridge_sec_filing_company_delete'
        WHERE trust_state='trusted_current';
    END;
    DROP TRIGGER IF EXISTS trg_sec_bridge_invalidate_reconciliation_update;
    CREATE TRIGGER trg_sec_bridge_invalidate_reconciliation_update
    AFTER UPDATE OF issuer_company_id,issuer_ticker,issuer_cik,relationship,
        relationship_evidence,form_type,filing_date,accepted_at,report_date,
        primary_document,source_id,source_url,association_status,
        retirement_effective_asof,retirement_reason
    ON bridge_sec_filing_company
    WHEN OLD.issuer_company_id IS NOT NEW.issuer_company_id
      OR OLD.issuer_ticker IS NOT NEW.issuer_ticker
      OR OLD.issuer_cik IS NOT NEW.issuer_cik
      OR OLD.relationship IS NOT NEW.relationship
      OR OLD.relationship_evidence IS NOT NEW.relationship_evidence
      OR OLD.form_type IS NOT NEW.form_type
      OR OLD.filing_date IS NOT NEW.filing_date
      OR OLD.accepted_at IS NOT NEW.accepted_at
      OR OLD.report_date IS NOT NEW.report_date
      OR OLD.primary_document IS NOT NEW.primary_document
      OR OLD.source_id IS NOT NEW.source_id
      OR OLD.source_url IS NOT NEW.source_url
      OR OLD.association_status IS NOT NEW.association_status
      OR OLD.retirement_effective_asof IS NOT NEW.retirement_effective_asof
      OR OLD.retirement_reason IS NOT NEW.retirement_reason
    BEGIN
        UPDATE consumer_defensive_sec_reconciliation_state
        SET trust_state='invalidated_by_mutation',
            quarantine_reason='bridge_sec_filing_company_update'
        WHERE trust_state='trusted_current';
    END;
    DROP TRIGGER IF EXISTS trg_stage4_document_bridge_invalidate_insert;
    CREATE TRIGGER trg_stage4_document_bridge_invalidate_insert
    AFTER INSERT ON bridge_sec_filing_document_company BEGIN
        UPDATE consumer_defensive_sec_reconciliation_state
        SET trust_state='invalidated_by_mutation',
            quarantine_reason='document_bridge_insert'
        WHERE trust_state='trusted_current';
    END;
    DROP TRIGGER IF EXISTS trg_stage4_document_bridge_invalidate_delete;
    CREATE TRIGGER trg_stage4_document_bridge_invalidate_delete
    AFTER DELETE ON bridge_sec_filing_document_company BEGIN
        UPDATE consumer_defensive_sec_reconciliation_state
        SET trust_state='invalidated_by_mutation',
            quarantine_reason='document_bridge_delete'
        WHERE trust_state='trusted_current';
    END;
    DROP TRIGGER IF EXISTS trg_stage4_document_bridge_invalidate_update;
    CREATE TRIGGER trg_stage4_document_bridge_invalidate_update
    AFTER UPDATE OF accession_number,issuer_company_id,source_id,issuer_ticker,
        issuer_cik,accepted_at,primary_document,source_url,content_sha256,
        cache_path,hydration_status,inline_xbrl_verified
    ON bridge_sec_filing_document_company
    WHEN OLD.accession_number IS NOT NEW.accession_number
      OR OLD.issuer_company_id IS NOT NEW.issuer_company_id
      OR OLD.source_id IS NOT NEW.source_id
      OR OLD.issuer_ticker IS NOT NEW.issuer_ticker
      OR OLD.issuer_cik IS NOT NEW.issuer_cik
      OR OLD.accepted_at IS NOT NEW.accepted_at
      OR OLD.primary_document IS NOT NEW.primary_document
      OR OLD.source_url IS NOT NEW.source_url
      OR OLD.content_sha256 IS NOT NEW.content_sha256
      OR OLD.cache_path IS NOT NEW.cache_path
      OR OLD.hydration_status IS NOT NEW.hydration_status
      OR OLD.inline_xbrl_verified IS NOT NEW.inline_xbrl_verified
    BEGIN
        UPDATE consumer_defensive_sec_reconciliation_state
        SET trust_state='invalidated_by_mutation',
            quarantine_reason='document_bridge_update'
        WHERE trust_state='trusted_current';
    END;
    DROP TRIGGER IF EXISTS trg_stage4_association_event_invalidate_insert;
    CREATE TRIGGER trg_stage4_association_event_invalidate_insert
    AFTER INSERT ON sec_filing_company_association_event BEGIN
        UPDATE consumer_defensive_sec_reconciliation_state
        SET trust_state='invalidated_by_mutation',
            quarantine_reason='association_event_insert'
        WHERE trust_state='trusted_current';
    END;
    CREATE TRIGGER IF NOT EXISTS trg_stage4_fact_filing_invalidate_insert
    AFTER INSERT ON fact_sec_filing BEGIN
        UPDATE consumer_defensive_sec_reconciliation_state
        SET trust_state='invalidated_by_mutation',
            quarantine_reason='fact_sec_filing_insert'
        WHERE trust_state='trusted_current';
    END;
    CREATE TRIGGER IF NOT EXISTS trg_stage4_fact_filing_invalidate_delete
    AFTER DELETE ON fact_sec_filing BEGIN
        UPDATE consumer_defensive_sec_reconciliation_state
        SET trust_state='invalidated_by_mutation',
            quarantine_reason='fact_sec_filing_delete'
        WHERE trust_state='trusted_current';
    END;
    CREATE TRIGGER IF NOT EXISTS trg_stage4_fact_filing_invalidate_update
    AFTER UPDATE OF accession_number,company_id,ticker,cik,form_type,
        filing_date,accepted_at,report_date,primary_document,source_id,
        source_url,content_sha256,metadata_quality_flags_json
    ON fact_sec_filing
    WHEN OLD.accession_number IS NOT NEW.accession_number
      OR OLD.company_id IS NOT NEW.company_id
      OR OLD.ticker IS NOT NEW.ticker
      OR OLD.cik IS NOT NEW.cik
      OR OLD.form_type IS NOT NEW.form_type
      OR OLD.filing_date IS NOT NEW.filing_date
      OR OLD.accepted_at IS NOT NEW.accepted_at
      OR OLD.report_date IS NOT NEW.report_date
      OR OLD.primary_document IS NOT NEW.primary_document
      OR OLD.source_id IS NOT NEW.source_id
      OR OLD.source_url IS NOT NEW.source_url
      OR OLD.content_sha256 IS NOT NEW.content_sha256
      OR OLD.metadata_quality_flags_json IS NOT NEW.metadata_quality_flags_json
    BEGIN
        UPDATE consumer_defensive_sec_reconciliation_state
        SET trust_state='invalidated_by_mutation',
            quarantine_reason='fact_sec_filing_update'
        WHERE trust_state='trusted_current';
    END;
    DROP TRIGGER IF EXISTS trg_stage4_company_currency_invalidate_update;
    CREATE TRIGGER trg_stage4_company_currency_invalidate_update
    AFTER UPDATE OF primary_ticker,cik,reporting_currency ON dim_company
    WHEN OLD.primary_ticker IS NOT NEW.primary_ticker
      OR OLD.cik IS NOT NEW.cik
      OR COALESCE(NULLIF(UPPER(TRIM(OLD.reporting_currency)),''),'USD')
         IS NOT COALESCE(NULLIF(UPPER(TRIM(NEW.reporting_currency)),''),'USD')
    BEGIN
        UPDATE consumer_defensive_sec_cache_snapshot
        SET trust_state='quarantined_scope_change',
            quarantine_reason='dim_company_semantic_scope_update'
        WHERE trust_state='trusted_current';
        DELETE FROM consumer_defensive_sec_reconciliation_state;
    END;
    ''')
    _stage4_recreate_parser_view(conn)
    # v8 changes the scope/config identity. Legacy rows remain queryable for
    # audit but are never accepted by readers or cache-only replay.
    for table in (
        'consumer_defensive_sec_reconciliation_state',
        'consumer_defensive_sec_cache_snapshot',
    ):
        conn.execute(f'''UPDATE {table}
            SET scope_contract_version=2,
                trust_state='quarantined_legacy_scope_v2',
                quarantine_reason='v8_reporting_currency_not_bound'
            WHERE scope_contract_version<>3
               OR trust_state<>'trusted_current' ''')


def _stage4_migration_v9(conn: sqlite3.Connection) -> None:
    '''Index every non-key lookup performed by shared-accession reconciliation.'''
    _stage4_execute_sql(conn, '''
    CREATE INDEX IF NOT EXISTS idx_stage4_raw_accession_fact
        ON fact_sec_xbrl_fact_raw(accession_number,raw_fact_id);
    CREATE INDEX IF NOT EXISTS idx_stage4_canonical_accession
        ON fact_financial_statement_canonical(accession_number);
    CREATE INDEX IF NOT EXISTS idx_stage4_census_accession
        ON fact_specialized_metric_disclosure_census(accession_number);
    ''')


def _stage4_migration_v10(conn: sqlite3.Connection) -> None:
    """Add sealed inline-XBRL fallback provenance without changing SEC seals."""
    _stage4_add_column(
        conn, 'dim_issuer_reporting_profile', 'latest_fallback_accepted_at', 'TEXT'
    )
    _stage4_add_column(
        conn, 'dim_issuer_reporting_profile', 'fallback_document_sha256', 'TEXT'
    )
    _stage4_add_column(
        conn, 'dim_issuer_reporting_profile', 'fallback_parser_version', 'TEXT'
    )
    _stage4_execute_sql(conn, '''
    CREATE TABLE IF NOT EXISTS fact_sec_inline_xbrl_fallback_run (
        asof_date TEXT NOT NULL,
        ticker TEXT NOT NULL,
        accession_number TEXT NOT NULL,
        accepted_at TEXT NOT NULL,
        document_sha256 TEXT NOT NULL,
        parser_version TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN (
            'covered','nonfinancial_inline_xbrl','insufficient_mapped_facts'
        )),
        numeric_fact_count INTEGER NOT NULL,
        consolidated_fact_count INTEGER NOT NULL,
        mapped_fact_count INTEGER NOT NULL,
        context_count INTEGER NOT NULL,
        unit_count INTEGER NOT NULL,
        skipped_fact_count INTEGER NOT NULL,
        unsupported_transformations_json TEXT NOT NULL,
        source_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(asof_date,ticker,accession_number,parser_version),
        FOREIGN KEY(accession_number) REFERENCES fact_sec_filing(accession_number)
            ON DELETE CASCADE,
        FOREIGN KEY(source_id) REFERENCES source_registry(source_id)
            ON DELETE RESTRICT
    );
    CREATE INDEX IF NOT EXISTS idx_stage4_inline_fallback_ticker_accepted
        ON fact_sec_inline_xbrl_fallback_run(ticker,accepted_at,status);
    ''')


_STAGE4_MIGRATION_UNITS = {
    2: _stage4_migration_v2,
    3: _stage4_migration_v3,
    4: _stage4_migration_v4,
    5: _stage4_migration_v5,
    6: _stage4_migration_v6,
    7: _stage4_migration_v7,
    8: _stage4_migration_v8,
    9: _stage4_migration_v9,
    10: _stage4_migration_v10,
}


def _reconcile_stage4_legacy_backfills(conn: sqlite3.Connection) -> None:
    """Repair legacy rows added by old writers after their migration stamp."""
    conn.execute('''CREATE INDEX IF NOT EXISTS
        idx_stage4_raw_missing_observation_identity
        ON fact_sec_xbrl_fact_raw(raw_fact_id)
        WHERE source_observation_id IS NULL OR source_observation_id='' ''')
    conn.execute('''CREATE INDEX IF NOT EXISTS
        idx_stage4_canonical_missing_observation_identity
        ON fact_financial_statement_canonical(canonical_fact_id)
        WHERE source_observation_id IS NULL OR source_observation_id='' ''')
    _backfill_source_observation_ids(conn)
    summary_columns = {
        str(row[1]) for row in conn.execute(
            'PRAGMA table_info(fact_specialized_metric_disclosure_summary)'
        )
    }
    if summary_columns and 'asof_date' not in summary_columns:
        if conn.execute('''SELECT 1 FROM sqlite_master WHERE type='table'
            AND name='fact_specialized_metric_disclosure_summary_legacy_backup' ''').fetchone():
            raise RuntimeError('Legacy disclosure summary backup already exists')
        conn.execute('''ALTER TABLE fact_specialized_metric_disclosure_summary
            RENAME TO fact_specialized_metric_disclosure_summary_legacy_backup''')
        conn.execute('''CREATE TABLE fact_specialized_metric_disclosure_summary(
            ticker TEXT NOT NULL,metric_id TEXT NOT NULL,
            calibration_cohort_id TEXT NOT NULL,applicability_subtype TEXT NOT NULL,
            asof_date TEXT NOT NULL,applicability_status TEXT NOT NULL,
            filings_searched INTEGER NOT NULL,filings_with_hits INTEGER NOT NULL,
            disclosure_status TEXT NOT NULL,first_disclosure_accepted_at TEXT,
            last_disclosure_accepted_at TEXT,parser_version TEXT NOT NULL,
            source_id TEXT NOT NULL,updated_at TEXT NOT NULL,
            PRIMARY KEY(ticker,metric_id,parser_version,asof_date),
            FOREIGN KEY(metric_id) REFERENCES dim_specialized_metric(metric_id)
                ON DELETE RESTRICT,
            FOREIGN KEY(source_id) REFERENCES source_registry(source_id)
                ON DELETE RESTRICT)''')
        conn.execute('''INSERT INTO fact_specialized_metric_disclosure_summary(
            ticker,metric_id,calibration_cohort_id,applicability_subtype,asof_date,
            applicability_status,filings_searched,filings_with_hits,disclosure_status,
            first_disclosure_accepted_at,last_disclosure_accepted_at,parser_version,
            source_id,updated_at)
            SELECT ticker,metric_id,calibration_cohort_id,applicability_subtype,
                   COALESCE(NULLIF(substr(updated_at,1,10),''),'1900-01-01'),
                   applicability_status,filings_searched,filings_with_hits,
                   disclosure_status,first_disclosure_accepted_at,
                   last_disclosure_accepted_at,parser_version,source_id,updated_at
            FROM fact_specialized_metric_disclosure_summary_legacy_backup''')
    conn.execute('''UPDATE fact_specialized_metric_disclosure_summary
        SET disclosure_status=CASE disclosure_status
            WHEN 'applicable_and_disclosed' THEN 'applicable_term_hit'
            WHEN 'applicable_not_disclosed' THEN 'applicable_no_term_hit'
            ELSE disclosure_status END''')
    conn.execute('''
        INSERT OR IGNORE INTO bridge_sec_filing_company(
            accession_number,issuer_company_id,issuer_ticker,issuer_cik,
            relationship,relationship_evidence,form_type,filing_date,accepted_at,
            report_date,primary_document,source_id,source_url,created_at,updated_at
        )
        SELECT f.accession_number,f.company_id,f.ticker,
               printf('%010d',CAST(f.cik AS INTEGER)),
               'associated_via_submissions','legacy_fact_sec_filing_backfill',
               f.form_type,f.filing_date,f.accepted_at,f.report_date,
               f.primary_document,f.source_id,
               'https://www.sec.gov/Archives/edgar/data/' ||
               CAST(CAST(f.cik AS INTEGER) AS TEXT) || '/' ||
               REPLACE(f.accession_number,'-','') || '/' ||
               COALESCE(f.primary_document,''),f.created_at,f.updated_at
        FROM fact_sec_filing f
        WHERE f.company_id IS NOT NULL AND COALESCE(f.cik,'')<>''
    ''')
    conn.execute('''
        INSERT OR IGNORE INTO bridge_sec_filing_document_company(
            accession_number,issuer_company_id,source_id,issuer_ticker,issuer_cik,
            accepted_at,primary_document,source_url,content_sha256,cache_path,
            hydration_status,updated_at
        )
        SELECT d.accession_number,b.issuer_company_id,d.source_id,b.issuer_ticker,
               b.issuer_cik,f.accepted_at,d.primary_document,
               COALESCE(NULLIF(b.source_url,''),d.source_url),d.content_sha256,
               d.cache_path,d.hydration_status,d.updated_at
        FROM fact_sec_filing_document d
        JOIN bridge_sec_filing_company b
          ON b.accession_number=d.accession_number AND b.issuer_ticker=d.ticker
        JOIN fact_sec_filing f ON f.accession_number=d.accession_number
    ''')
    _backfill_association_events(conn)


def _validate_stage4_migration_ledger(
    rows: Iterable[sqlite3.Row | tuple[Any, ...]],
) -> set[int]:
    ledger = list(rows)
    versions = [int(row[0]) for row in ledger]
    expected_versions = [row[0] for row in STAGE4_MIGRATION_HISTORY[:len(ledger)]]
    if versions != expected_versions:
        raise RuntimeError('Stage4 migration ledger has a gap or future version')
    for actual, expected in zip(
        ledger, STAGE4_MIGRATION_HISTORY[:len(ledger)], strict=True
    ):
        version, name, checksum = expected
        actual_checksum = str(actual[2])
        valid_checksum = (
            actual_checksum == checksum
            or actual_checksum in LEGACY_MIGRATION_CHECKSUMS.get(version,set())
        )
        if str(actual[1]) != name or not valid_checksum or str(actual[3]) != 'complete':
            raise RuntimeError('Stage4 migration ledger checksum mismatch')
    return set(versions)


def ensure_stage4_schema(conn: sqlite3.Connection) -> None:
    """Apply immutable Stage4 migration units atomically and in ledger order."""
    nested = conn.in_transaction
    conn.execute('SAVEPOINT stage4_schema_v5' if nested else 'BEGIN IMMEDIATE')
    try:
        conn.execute('''CREATE TABLE IF NOT EXISTS consumer_defensive_stage4_schema_migration(
            migration_version INTEGER PRIMARY KEY,migration_name TEXT NOT NULL DEFAULT '',
            migration_sha256 TEXT NOT NULL DEFAULT '',status TEXT NOT NULL CHECK(status='complete'),
            applied_at TEXT NOT NULL)''')
        _stage4_add_column(
            conn, 'consumer_defensive_stage4_schema_migration',
            'migration_name', "TEXT NOT NULL DEFAULT ''",
        )
        _stage4_add_column(
            conn, 'consumer_defensive_stage4_schema_migration',
            'migration_sha256', "TEXT NOT NULL DEFAULT ''",
        )
        ledger = conn.execute('''SELECT migration_version,migration_name,
            migration_sha256,status FROM consumer_defensive_stage4_schema_migration
            ORDER BY migration_version''').fetchall()
        applied = _validate_stage4_migration_ledger(ledger)
        for version, name, checksum in STAGE4_MIGRATION_HISTORY:
            if version in applied:
                continue
            _STAGE4_MIGRATION_UNITS[version](conn)
            conn.execute('''INSERT INTO consumer_defensive_stage4_schema_migration(
                migration_version,migration_name,migration_sha256,status,applied_at)
                VALUES(?,?,?,'complete',?)''',(version,name,checksum,utc_now()))
        _reconcile_stage4_legacy_backfills(conn)
        missing_filings = int(conn.execute('''SELECT COUNT(*) FROM fact_sec_filing f
            WHERE f.company_id IS NOT NULL AND COALESCE(f.cik,'')<>''
              AND NOT EXISTS(SELECT 1 FROM bridge_sec_filing_company b
                WHERE b.accession_number=f.accession_number
                  AND b.issuer_company_id=f.company_id)''').fetchone()[0])
        missing_documents = int(conn.execute('''SELECT COUNT(*)
            FROM fact_sec_filing_document d JOIN bridge_sec_filing_company b
              ON b.accession_number=d.accession_number AND b.issuer_ticker=d.ticker
            WHERE NOT EXISTS(SELECT 1 FROM bridge_sec_filing_document_company x
              WHERE x.accession_number=d.accession_number
                AND x.issuer_company_id=b.issuer_company_id
                AND x.source_id=d.source_id)''').fetchone()[0])
        if missing_filings or missing_documents:
            raise RuntimeError('Stage4 migration backfill parity failed')
        if conn.execute('PRAGMA foreign_key_check').fetchone() is not None:
            raise RuntimeError('Stage4 migration failed foreign-key postcondition')
        if nested:
            conn.execute('RELEASE SAVEPOINT stage4_schema_v5')
        else:
            conn.commit()
    except BaseException:
        if nested:
            conn.execute('ROLLBACK TO SAVEPOINT stage4_schema_v5')
            conn.execute('RELEASE SAVEPOINT stage4_schema_v5')
        else:
            conn.rollback()
        raise


def _reconcile_filing_accession(conn: sqlite3.Connection, accession: str) -> str:
    associations = conn.execute(
        '''SELECT * FROM bridge_sec_filing_company
           WHERE accession_number=? ORDER BY issuer_cik,issuer_company_id''',
        (accession,),
    ).fetchall()
    if not associations:
        raise RuntimeError(f'Filing {accession} has no issuer association')
    preferred = associations[0]
    accepted = max(str(row['accepted_at'] or '') for row in associations)
    previous_accepted_row = conn.execute(
        'SELECT accepted_at FROM fact_sec_filing WHERE accession_number=?',
        (accession,),
    ).fetchone()
    previous_accepted = str(previous_accepted_row[0] or '') if previous_accepted_row else ''
    metadata = {
        (
            str(row['form_type'] or ''), str(row['filing_date'] or ''),
            str(row['report_date'] or ''), str(row['primary_document'] or ''),
            str(row['source_id'] or ''),
        )
        for row in associations
    }
    observed_acceptances = {str(row['accepted_at'] or '') for row in associations}
    flags: list[str] = []
    if len(metadata) > 1:
        flags.append('association_metadata_conflict')
    if len(observed_acceptances) > 1:
        flags.append('association_accepted_at_conflict')
    conn.execute(
        '''UPDATE fact_sec_filing
           SET company_id=NULL,ticker='ACCESSION_NEUTRAL',cik=NULL,
               form_type=?,filing_date=?,accepted_at=?,report_date=?,
               primary_document=?,source_id=?,source_url=NULL,
               metadata_quality_flags_json=?,updated_at=?
           WHERE accession_number=?''',
        (
            preferred['form_type'], preferred['filing_date'], accepted,
            preferred['report_date'], preferred['primary_document'],
            preferred['source_id'], json.dumps(flags, separators=(',', ':')),
            utc_now(), accession,
        ),
    )
    conn.execute(
        'UPDATE fact_sec_xbrl_fact_raw SET accepted_at=? WHERE accession_number=?',
        (accepted, accession),
    )
    _refresh_source_observation_ids_for_accession(conn, accession)
    conn.execute(
        'UPDATE bridge_sec_filing_document_company SET accepted_at=? WHERE accession_number=?',
        (accepted, accession),
    )
    if previous_accepted and previous_accepted != accepted:
        affected_date = min(previous_accepted, accepted)[:10]
        conn.execute(
            'DELETE FROM fact_financial_statement_canonical WHERE accession_number=?',
            (accession,),
        )
        conn.execute(
            'DELETE FROM fact_specialized_metric_disclosure_census WHERE accession_number=?',
            (accession,),
        )
        for association in associations:
            ticker = str(association['issuer_ticker'])
            conn.execute(
                'DELETE FROM feature_financial_statement '
                'WHERE model_family=? AND ticker=? AND asof_date>=?',
                (MODEL_FAMILY, ticker, affected_date),
            )
            conn.execute(
                'DELETE FROM fact_specialized_metric_disclosure_summary '
                'WHERE ticker=? AND asof_date>=?',
                (ticker, affected_date),
            )
    return accepted


def _reconcile_reporting_profiles_for_accession(
    conn: sqlite3.Connection, accession: str, *, companyfacts_lag_days: int
) -> None:
    rows = conn.execute(
        '''SELECT p.*,b.form_type AS association_form,f.accepted_at AS canonical_accepted
           FROM dim_issuer_reporting_profile p
           JOIN bridge_sec_filing_company b ON b.issuer_ticker=p.ticker
           JOIN fact_sec_filing f ON f.accession_number=b.accession_number
           WHERE b.accession_number=? AND b.association_status='active' ''',
        (accession,),
    ).fetchall()
    for profile in rows:
        ticker = str(profile['ticker'])
        form = str(profile['association_form'] or '').upper()
        accepted = str(profile['canonical_accepted'] or '')
        has_facts = bool(conn.execute(
            '''SELECT 1 FROM fact_sec_xbrl_fact_raw
               WHERE ticker=? AND accession_number=? LIMIT 1''',
            (ticker, accession),
        ).fetchone())
        eligible = form in PROFILE_FINANCIAL_FORMS or (
            form in PROFILE_CONDITIONAL_XBRL_FORMS and has_facts
        )
        if not eligible:
            continue
        latest_filing = max(str(profile['latest_filing_accepted_at'] or ''), accepted)
        latest_fact_row = conn.execute(
            '''SELECT MAX(accepted_at) FROM fact_sec_xbrl_fact_raw
               WHERE ticker=? AND accepted_at<>'' ''',
            (ticker,),
        ).fetchone()
        latest_fact = str(latest_fact_row[0] or '') if latest_fact_row else ''
        lag = None
        if latest_filing and latest_fact:
            lag = (
                date.fromisoformat(latest_filing[:10])
                - date.fromisoformat(latest_fact[:10])
            ).days
        annual = str(profile['primary_annual_form'] or '')
        canonical_form = _canonical_financial_form(form)
        if canonical_form in PROFILE_ANNUAL_FORMS and accepted >= latest_filing:
            annual = canonical_form
        fallback = int(
            annual in {'20-F', '40-F'}
            and (lag is None or lag > companyfacts_lag_days)
        )
        status = (
            'inline_fallback_required'
            if fallback
            else ('covered' if latest_fact else 'filings_only')
        )
        conn.execute(
            '''UPDATE dim_issuer_reporting_profile
               SET primary_annual_form=?,latest_filing_accepted_at=?,
                   latest_companyfacts_accepted_at=?,companyfacts_lag_days=?,
                   inline_xbrl_fallback_required=?,coverage_status=?,
                   review_reason=?,updated_at=? WHERE ticker=?''',
            (
                annual or None, latest_filing or None, latest_fact or None, lag,
                fallback, status, 'inline_xbrl_required' if fallback else None,
                utc_now(), ticker,
            ),
        )


def bootstrap_stage4(conn: sqlite3.Connection, bundle: ConfigBundle) -> None:
    init_db(conn)
    source_path = resolve_path(cfg_get(bundle.payload, "source_registry.path"), base_dir=bundle.base_dir)
    metric_path = resolve_path(cfg_get(bundle.payload, "specialized_metrics.registry_path"), base_dir=bundle.base_dir)
    upsert_source_registry(conn, load_source_registry(source_path))
    stage2_sources = resolve_path(
        cfg_get(bundle.payload, "source_registry.stage2_path"), base_dir=bundle.base_dir
    )
    if stage2_sources.exists():
        upsert_stage2_sources(conn, load_source_registry(stage2_sources))
    version, metrics = load_metric_registry(metric_path)
    upsert_metric_registry(conn, registry_version=version, metrics=metrics)
    ensure_stage4_schema(conn)


def _http_policy(config: dict[str, Any], root: str) -> HttpPolicy:
    return HttpPolicy(
        user_agent=str(cfg_get(config, f"{root}.user_agent")),
        timeout_sec=float(cfg_get(config, f"{root}.timeout_sec", 30)),
        retries=int(cfg_get(config, f"{root}.retries", 3)),
        sleep_sec=float(cfg_get(config, f"{root}.sleep_sec", 0.1)),
    )


def http_fetcher(policy: HttpPolicy) -> Fetcher:
    transient_statuses = {408, 425, 429, 500, 502, 503, 504}
    throttle_lock = threading.Lock()
    next_request_at = 0.0

    def wait_for_request_slot() -> None:
        nonlocal next_request_at
        if policy.sleep_sec <= 0:
            return
        with throttle_lock:
            now = time.monotonic()
            delay = max(0.0, next_request_at - now)
            next_request_at = max(now, next_request_at) + policy.sleep_sec
        if delay:
            time.sleep(delay)

    def fetch(url: str) -> bytes:
        request = urllib.request.Request(url, headers={"User-Agent": policy.user_agent, "Accept-Encoding": "identity"})
        last: Exception | None = None
        for attempt in range(policy.retries):
            try:
                wait_for_request_slot()
                with urllib.request.urlopen(request, timeout=policy.timeout_sec) as response:
                    payload = response.read()
                return payload
            except urllib.error.HTTPError as exc:
                last = exc
                if exc.code not in transient_statuses:
                    break
                if attempt + 1 < policy.retries:
                    retry_after = 0.0
                    raw_retry_after = exc.headers.get("Retry-After") if exc.headers else None
                    if raw_retry_after:
                        try:
                            retry_after = max(0.0, float(raw_retry_after))
                        except ValueError:
                            retry_after = 0.0
                    time.sleep(min(max(retry_after, float(2 ** attempt)), 30.0))
            except (OSError, urllib.error.URLError) as exc:
                last = exc
                if attempt + 1 < policy.retries:
                    time.sleep(min(float(2 ** attempt), 16.0))
        raise RuntimeError(f"HTTP fetch failed for {url}: {last}")
    return fetch


def _cached(
    fetch: Fetcher, url: str, path: Path, force: bool = False, *,
    cache_root: Path | None = None,
    sealed_lookup: dict[str, Path] | None = None,
) -> bytes:
    if cache_root is not None:
        path = _safe_cache_write_target(
            cache_root, path, context='SEC mutable cache alias'
        )
    if sealed_lookup is not None:
        if cache_root is None:
            raise RuntimeError('Sealed cache lookup requires cache_root')
        logical = resolve_filesystem_path(path).relative_to(
            resolve_filesystem_path(cache_root)
        ).as_posix()
        sealed = sealed_lookup.get(logical)
        if sealed is None:
            raise FileNotFoundError(f'Sealed SEC cache entry missing: {logical}')
        return read_bytes(sealed)
    if path_exists(path) and not force:
        return read_bytes(path)
    if os.environ.get("CONSUMER_DEFENSIVE_CACHE_ONLY", "").strip().casefold() in {
        "1", "true", "yes", "on",
    }:
        reason = "force-refresh requested" if force else "cache entry missing"
        raise FileNotFoundError(f"Consumer Defensive cache-only replay: {reason}: {path}")
    payload = fetch(url)
    _atomic_promote_bytes(path, payload, cache_root=cache_root)
    return payload


def _safe_cache_write_target(
    cache_root: Path,
    path: Path,
    *,
    context: str,
) -> Path:
    '''Create safe parents and return a cache target with canonical identity.'''
    lexical_root = Path(os.path.abspath(cache_root))
    mkdir_path(lexical_root, parents=True, exist_ok=True)
    resolved_root = resolve_filesystem_path(lexical_root, strict=True)
    lexical_target = Path(os.path.abspath(path))
    try:
        relative = lexical_target.relative_to(lexical_root)
    except ValueError as exc:
        raise RuntimeError(f'{context} is outside the configured cache root') from exc
    if not relative.parts or any(part in {'', '.', '..'} for part in relative.parts):
        raise RuntimeError(f'{context} has an unsafe relative path')

    current = lexical_root
    expected = resolved_root
    for part in relative.parts[:-1]:
        current /= part
        expected /= part
        if lexists_path(current):
            try:
                actual = resolve_filesystem_path(current, strict=True)
            except OSError as exc:
                raise RuntimeError(f'{context} has an unresolvable parent') from exc
            if actual != expected or not is_dir_path(actual):
                raise RuntimeError(
                    f'{context} has a symlinked or non-directory cache parent'
                )
        else:
            try:
                mkdir_path(current)
            except FileExistsError:
                # A sibling hydration worker may have created the same safe
                # accession parent after the lexical check.  Re-resolve it
                # below so a symlink or non-directory race still fails closed.
                pass
            actual = resolve_filesystem_path(current, strict=True)
            if actual != expected or not is_dir_path(actual):
                raise RuntimeError(f'{context} cache parent identity changed')

    expected_target = expected / relative.parts[-1]
    if lexists_path(lexical_target):
        try:
            actual_target = resolve_filesystem_path(lexical_target, strict=True)
        except OSError as exc:
            raise RuntimeError(f'{context} target cannot be resolved') from exc
        if actual_target != expected_target or not is_file_path(actual_target):
            raise RuntimeError(f'{context} is a symlink or non-regular file')
    return lexical_target


def _atomic_promote_bytes(
    path: Path, payload: bytes, *, cache_root: Path | None = None
) -> None:
    """Publish validated bytes without exposing a partial cache alias."""

    if cache_root is not None:
        path = _safe_cache_write_target(
            cache_root, path, context='SEC mutable cache alias'
        )
    else:
        mkdir_path(path.parent, parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=filesystem_path(path.parent), prefix=f'.{path.name}.', suffix='.tmp'
    )
    temporary = resolve_filesystem_path(Path(temporary_name), strict=True)
    try:
        with os.fdopen(descriptor, 'wb') as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if cache_root is not None:
            temporary = _safe_cache_write_target(
                cache_root, temporary, context='SEC mutable cache temporary'
            )
            path = _safe_cache_write_target(
                cache_root, path, context='SEC mutable cache alias'
            )
        replace_path(temporary, path)
        if cache_root is not None:
            _safe_cache_write_target(
                cache_root, path, context='SEC mutable cache alias'
            )
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        unlink_path(temporary, missing_ok=True)


def _validated_mutable_json(
    fetch: Fetcher,
    url: str,
    path: Path,
    *,
    validate: Callable[[object], None],
    cache_root: Path | None = None,
    sealed_lookup: dict[str, Path] | None = None,
    force: bool = False,
    reuse_valid_cache: bool = False,
    promote: bool = True,
) -> tuple[bytes, dict[str, Any]]:
    if cache_root is not None:
        path = _safe_cache_write_target(
            cache_root, path, context='SEC mutable cache alias'
        )
    cache_only = os.environ.get(
        'CONSUMER_DEFENSIVE_CACHE_ONLY', ''
    ).strip().casefold() in {'1', 'true', 'yes', 'on'}
    if cache_only or sealed_lookup is not None:
        raw = _cached(
            fetch, url, path, False,
            cache_root=cache_root, sealed_lookup=sealed_lookup,
        )
        payload = json.loads(raw)
        validate(payload)
        return raw, payload
    if reuse_valid_cache and path_exists(path) and not force:
        try:
            raw = read_bytes(path)
            payload = json.loads(raw)
            validate(payload)
            return raw, payload
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            # An online run may repair a corrupt alias, but it must never consume it.
            pass
    raw = fetch(url)
    payload = json.loads(raw)
    validate(payload)
    if promote:
        _atomic_promote_bytes(path, raw, cache_root=cache_root)
    return raw, payload


def _cache_manifest_record(cache_root: Path, path: Path, payload: bytes) -> dict[str, Any]:
    path = _safe_cache_write_target(
        cache_root, path, context='SEC cache manifest alias'
    )
    return {
        "path": resolve_filesystem_path(path).relative_to(
            resolve_filesystem_path(cache_root)
        ).as_posix(),
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def _cache_manifest_summary(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    by_path: dict[str, dict[str, Any]] = {}
    for record in records:
        normalized_record = {
            "path": str(record["path"]),
            "bytes": int(record["bytes"]),
            "sha256": str(record["sha256"]),
        }
        previous = by_path.get(normalized_record["path"])
        if previous is not None and previous != normalized_record:
            raise ValueError(
                "Conflicting cache-manifest observations for "
                f"{normalized_record['path']}: {previous} != {normalized_record}"
            )
        by_path[normalized_record["path"]] = normalized_record
    normalized = sorted(by_path.values(), key=lambda row: row["path"])
    encoded = json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()
    return {
        "files": len(normalized),
        "bytes": sum(int(row["bytes"]) for row in normalized),
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "entries": normalized,
    }


def _sealed_manifest_projection(
    entries: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    '''Project staged logical bytes to their deterministic date-seal identity.'''

    sealed_records: list[dict[str, Any]] = []
    for entry in entries:
        logical_path = str(entry['path'])
        logical = Path(logical_path)
        if logical.is_absolute() or not logical.parts or '..' in logical.parts:
            raise RuntimeError(f'Unsafe logical path in SEC cache seal: {logical_path}')
        digest = str(entry['sha256'])
        if not re.fullmatch(r'[0-9a-f]{64}', digest):
            raise RuntimeError(f'Invalid SHA-256 in SEC cache seal: {digest!r}')
        sealed_records.append({
            'logical_path': logical_path,
            'object_path': f'objects/sha256/{digest}',
            'bytes': int(entry['bytes']),
            'sha256': digest,
        })
    sealed_records.sort(key=lambda row: row['logical_path'])
    if len({row['logical_path'] for row in sealed_records}) != len(sealed_records):
        raise RuntimeError('Duplicate logical paths in SEC cache seal')
    encoded = json.dumps(
        sealed_records, sort_keys=True, separators=(',', ':')
    ).encode()
    return {
        'files': len(sealed_records),
        'bytes': sum(int(row['bytes']) for row in sealed_records),
        'sha256': hashlib.sha256(encoded).hexdigest(),
        'entries': sealed_records,
    }


def _seal_cache_manifest(
    cache_root: Path, asof_date: str, entries: Iterable[dict[str, Any]]
) -> tuple[Path, dict[str, Any]]:
    entries = list(entries)
    cache_root = resolve_filesystem_path(cache_root, strict=True)
    sealed_root = cache_root / 'sealed' / asof_date
    sealed_records: list[dict[str, Any]] = []
    for entry in entries:
        logical_path = str(entry['path'])
        if Path(logical_path).is_absolute() or '..' in Path(logical_path).parts:
            raise RuntimeError(f'Unsafe logical path in SEC cache seal: {logical_path}')
        source = resolve_filesystem_path(cache_root / logical_path, strict=True)
        source.relative_to(cache_root)
        source_payload = read_bytes(source)
        digest = str(entry['sha256'])
        if (
            hashlib.sha256(source_payload).hexdigest() != digest
            or len(source_payload) != int(entry['bytes'])
        ):
            raise RuntimeError(f'SEC cache source changed before sealing: {logical_path}')
        if not re.fullmatch(r'[0-9a-f]{64}', digest):
            raise RuntimeError(f'Invalid SHA-256 in SEC cache seal: {digest!r}')
        target = _safe_cache_write_target(
            cache_root, sealed_root / 'objects' / 'sha256' / digest,
            context='SEC date-local immutable object',
        )
        global_target = _safe_cache_write_target(
            cache_root, cache_root / 'objects' / 'sha256' / digest,
            context='SEC global immutable object',
        )
        if path_exists(global_target):
            global_payload = read_bytes(global_target)
            if (
                hashlib.sha256(global_payload).hexdigest() != digest
                or len(global_payload) != int(entry['bytes'])
            ):
                raise RuntimeError(
                    f'Existing global SEC cache object mismatch: {global_target}'
                )
        else:
            descriptor, temporary_name = tempfile.mkstemp(
                dir=filesystem_path(global_target.parent),
                prefix='.g.', suffix='.tmp',
            )
            global_temporary = resolve_filesystem_path(
                Path(temporary_name), strict=True
            )
            global_temporary = _safe_cache_write_target(
                cache_root, global_temporary,
                context='SEC global immutable object temporary',
            )
            try:
                with open_path(source, 'rb') as source_handle, os.fdopen(
                    descriptor, 'wb'
                ) as sealed_handle:
                    descriptor = -1
                    shutil.copyfileobj(source_handle, sealed_handle)
                    sealed_handle.flush()
                    os.fsync(sealed_handle.fileno())
                global_payload = read_bytes(global_temporary)
                if (
                    hashlib.sha256(global_payload).hexdigest() != digest
                    or len(global_payload) != int(entry['bytes'])
                ):
                    raise RuntimeError(
                        f'SEC cache object changed while sealing: {logical_path}'
                    )
                replace_path(global_temporary, global_target)
                _safe_cache_write_target(
                    cache_root, global_target,
                    context='SEC global immutable object',
                )
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
                unlink_path(global_temporary, missing_ok=True)
        if path_exists(target):
            if hashlib.sha256(read_bytes(target)).hexdigest() != digest:
                raise RuntimeError(f'Existing cache seal object mismatch: {target}')
        else:
            # Date-local objects may share an inode only with the immutable
            # global CAS object, never with a mutable acquisition alias.
            try:
                # A hardlink publish creates the final immutable name in one
                # operation and avoids repeating the 64-byte digest in an
                # already long Windows temporary path.
                link_path(global_target, target)
            except FileExistsError:
                pass
            except OSError:
                # Cross-volume or hardlink-restricted filesystems use an
                # exclusive short temporary followed by same-directory replace.
                descriptor, temporary_name = tempfile.mkstemp(
                    dir=filesystem_path(target.parent), prefix='.d.', suffix='.tmp',
                )
                temporary = resolve_filesystem_path(
                    Path(temporary_name), strict=True
                )
                try:
                    with open_path(global_target, 'rb') as source_handle, os.fdopen(
                        descriptor, 'wb'
                    ) as sealed_handle:
                        descriptor = -1
                        shutil.copyfileobj(source_handle, sealed_handle)
                        sealed_handle.flush()
                        os.fsync(sealed_handle.fileno())
                    copied = read_bytes(temporary)
                    if (
                        hashlib.sha256(copied).hexdigest() != digest
                        or len(copied) != int(entry['bytes'])
                    ):
                        raise RuntimeError(
                            'SEC cache object changed while sealing: '
                            f'{logical_path}'
                        )
                    replace_path(temporary, target)
                finally:
                    if descriptor >= 0:
                        os.close(descriptor)
                    unlink_path(temporary, missing_ok=True)
            _safe_cache_write_target(
                cache_root, target,
                context='SEC date-local immutable object',
            )
            copied = read_bytes(target)
            if (
                hashlib.sha256(copied).hexdigest() != digest
                or len(copied) != int(entry['bytes'])
            ):
                raise RuntimeError(
                    f'SEC cache object changed while sealing: {logical_path}'
                )
        sealed_records.append({
            'logical_path': logical_path,
            'object_path': target.relative_to(sealed_root).as_posix(),
            'bytes': int(entry['bytes']), 'sha256': digest,
        })
    projected = _sealed_manifest_projection(entries)
    if sealed_records != projected['entries']:
        raise RuntimeError('SEC cache seal projection changed during publication')
    return sealed_root, projected


def _verify_cache_manifest(
    cache_root: str | Path, manifest_json: str, expected_sha256: str
) -> bool:
    try:
        root = resolve_filesystem_path(Path(cache_root), strict=True)
        entries = json.loads(manifest_json)
        verified: list[dict[str, Any]] = []
        if not is_dir_path(root) or not isinstance(entries, list) or not entries:
            return False
        for entry in entries:
            logical_path = str(entry['logical_path'])
            if Path(logical_path).is_absolute() or '..' in Path(logical_path).parts:
                return False
            path = resolve_filesystem_path(
                root / str(entry['object_path']), strict=True
            )
            path.relative_to(root)
            payload = read_bytes(path)
            digest = hashlib.sha256(payload).hexdigest()
            if digest != str(entry['sha256']) or len(payload) != int(entry['bytes']):
                return False
            verified.append({
                'logical_path': logical_path,
                'object_path': str(entry['object_path']),
                'bytes': len(payload), 'sha256': digest,
            })
        verified.sort(key=lambda row: row['logical_path'])
        if len({row['logical_path'] for row in verified}) != len(verified):
            return False
        encoded = json.dumps(verified, sort_keys=True, separators=(',', ':')).encode()
        return hashlib.sha256(encoded).hexdigest() == expected_sha256
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False


def _sealed_cache_lookup(
    conn: sqlite3.Connection, cache_root: Path, asof_date: str, *,
    allow_quarantined: bool = False,
) -> dict[str, Path]:
    trust_clause = '' if allow_quarantined else (
        " AND scope_contract_version=3 AND trust_state='trusted_current'"
    )
    row = conn.execute(
        '''SELECT seal_relative_path,cache_manifest_json,cache_manifest_sha256
           FROM consumer_defensive_sec_cache_snapshot WHERE asof_date=?'''
        + trust_clause,
        (asof_date,),
    ).fetchone()
    if row is None:
        raise RuntimeError(
            f'Cache-only SEC replay requires an exact full-scope seal for {asof_date}'
        )
    sealed_root = resolve_sec_seal_root(
        cache_root, str(row[0]), expected_asof=asof_date
    )
    if not _verify_cache_manifest(sealed_root, str(row[1]), str(row[2])):
        raise RuntimeError(f'Cache-only SEC replay seal failed verification: {asof_date}')
    entries = json.loads(str(row[1]))
    return {
        str(entry['logical_path']): filesystem_path(
            sealed_root / str(entry['object_path'])
        )
        for entry in entries
    }


def _read_yaml(path: Path) -> dict[str, Any]:
    import yaml  # type: ignore
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"YAML root must be a mapping: {path}")
    return payload


def load_applicability(path: Path) -> dict[str, tuple[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    result: dict[str, tuple[str, str]] = {}
    for row in rows:
        ticker = str(row.get("ticker") or "").strip().upper()
        cohort = str(row.get("calibration_cohort_id") or "").strip()
        subtype = str(row.get("applicability_subtype") or "").strip()
        if not ticker or not cohort or not subtype or ticker in result:
            raise ValueError(f"Invalid or duplicate applicability row: {row}")
        result[ticker] = (cohort, subtype)
    return result


def apply_applicability(conn: sqlite3.Connection, path: Path) -> dict[str, int]:
    mapping = load_applicability(path)
    db_rows = conn.execute(
        "SELECT ticker, calibration_cohort_id FROM dim_consumer_defensive_taxonomy WHERE model_family=?",
        (MODEL_FAMILY,),
    ).fetchall()
    database_tickers = {str(row[0]) for row in db_rows}
    missing = sorted(database_tickers - set(mapping))
    mismatch = sorted(
        str(row[0]) for row in db_rows
        if str(row[0]) in mapping and str(row[1]) != mapping[str(row[0])][0]
    )
    if missing or mismatch:
        raise ValueError(
            "Applicability mapping mismatch: "
            f"missing={missing} cohort_mismatch={mismatch}"
        )
    with conn:
        for ticker, (_, subtype) in mapping.items():
            conn.execute(
                "UPDATE dim_consumer_defensive_taxonomy SET applicability_subtype=?, updated_at=? WHERE ticker=? AND model_family=?",
                (subtype, utc_now(), ticker, MODEL_FAMILY),
            )
    loaded = conn.execute(
        "SELECT COUNT(*) FROM dim_consumer_defensive_taxonomy WHERE model_family=? AND applicability_subtype<>''",
        (MODEL_FAMILY,),
    ).fetchone()[0]
    return {"mapping_rows": len(mapping), "taxonomy_rows_updated": int(loaded)}


def _normalize_accepted(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    text = text.replace(" ", "T")
    if len(text) == 19:
        text += "Z"
    return text


def _sec_archive_url(archive_cik: str, accession: str, primary_document: str) -> str:
    if not re.fullmatch(r'\d{1,10}', str(archive_cik)):
        raise ValueError(f'Invalid SEC archive CIK: {archive_cik!r}')
    if not re.fullmatch(r'\d{10}-\d{2}-\d{6}', accession):
        raise ValueError(f'Invalid SEC accession number: {accession!r}')
    quoted_document = quote_sec_relative_document_path(
        primary_document, allowed_suffixes=SEC_PRIMARY_DOCUMENT_SUFFIXES,
        context='SEC primaryDocument URL path',
    )
    return (
        "https://www.sec.gov/Archives/edgar/data/"
        f"{int(archive_cik)}/{accession.replace('-', '')}/{quoted_document}"
    )


def _filing_bridge_bad_url_count(
    conn: sqlite3.Connection, *, cutoff: str | None = None
) -> int:
    sql = '''SELECT b.issuer_cik,b.accession_number,b.primary_document,b.source_url
             FROM bridge_sec_filing_company b'''
    params: tuple[Any, ...] = ()
    if cutoff is not None:
        sql += ''' JOIN fact_sec_filing f ON f.accession_number=b.accession_number
                   WHERE f.accepted_at<=?'''
        params = (cutoff,)
    bad = 0
    for row in conn.execute(sql, params):
        primary = str(row[2] or '')
        try:
            expected = (
                _sec_archive_url(str(row[0]), str(row[1]), primary)
                if primary else None
            )
        except (TypeError, ValueError):
            bad += 1
            continue
        actual = str(row[3]) if row[3] is not None else None
        bad += int(actual != expected)
    return bad


def _filing_rows(payload: dict[str, Any]) -> Iterable[dict[str, Any]]:
    recent = payload.get("filings", {}).get("recent", {})
    if not isinstance(recent, dict):
        return []
    keys = ["accessionNumber", "filingDate", "acceptanceDateTime", "reportDate", "form", "primaryDocument"]
    count = max((len(recent.get(key) or []) for key in keys), default=0)
    return [
        {key: (recent.get(key) or [])[i] if i < len(recent.get(key) or []) else "" for key in keys}
        for i in range(count)
    ]


def _validated_issuer_filing_projection(
    rows: Iterable[dict[str, Any]], companyfacts: dict[str, Any],
) -> list[dict[str, Any]]:
    '''Validate and deduplicate one issuer's complete staged SEC projection.'''
    by_accession: dict[str, tuple[str, ...]] = {}
    projected: list[dict[str, Any]] = []
    for row in rows:
        accession = str(row.get('accessionNumber') or '')
        identity = (
            str(row.get('filingDate') or ''),
            str(_normalize_accepted(row.get('acceptanceDateTime')) or ''),
            str(row.get('reportDate') or ''),
            str(row.get('form') or ''),
            str(row.get('primaryDocument') or ''),
        )
        previous = by_accession.get(accession)
        if previous is not None:
            if previous != identity:
                raise ValueError(
                    'Conflicting staged SEC submissions metadata for '
                    f'accession {accession}'
                )
            continue
        by_accession[accession] = identity
        projected.append(row)
    for concepts in (companyfacts.get('facts') or {}).values():
        for definition in concepts.values():
            for observations in (definition.get('units') or {}).values():
                for observation in observations:
                    accession = str(observation.get('accn') or '')
                    filing = by_accession.get(accession)
                    if filing is None:
                        continue
                    observed_form = str(observation.get('form') or '')
                    if not _companyfacts_form_matches_submission(
                        observed_form, filing[3],
                    ):
                        raise ValueError(
                            'SEC Companyfacts form conflicts with staged '
                            'submissions metadata for accession '
                            f'{accession}: Companyfacts={observed_form!r}, '
                            f'submissions={filing[3]!r}'
                        )
    return projected
def _validate_submissions_payload(
    payload: object, *, expected_cik: str, require_nonempty: bool,
    require_cik: bool = True,
) -> None:
    if not isinstance(payload, dict):
        raise ValueError('SEC submissions payload must be an object')
    payload_cik = str(payload.get('cik') or '').strip()
    if require_cik and not payload_cik:
        raise ValueError('SEC submissions payload is missing required root CIK')
    if payload_cik and payload_cik.zfill(10) != expected_cik:
        raise ValueError(
            f'SEC submissions CIK mismatch: expected {expected_cik}, got {payload_cik}'
        )
    recent = (payload.get('filings') or {}).get('recent')
    if not isinstance(recent, dict):
        raise ValueError('SEC submissions filings.recent must be an object')
    keys = (
        'accessionNumber','filingDate','acceptanceDateTime',
        'reportDate','form','primaryDocument',
    )
    values = [recent.get(key) for key in keys]
    if any(not isinstance(value, list) for value in values):
        raise ValueError('SEC submissions required recent fields must be arrays')
    lengths = {len(value) for value in values}
    if len(lengths) != 1:
        raise ValueError('SEC submissions required recent arrays have unequal lengths')
    row_count = next(iter(lengths), 0)
    seen_accessions: set[str] = set()
    for position in range(row_count):
        context = f'SEC submissions recent row {position}'
        accession = values[0][position]
        if (
            not isinstance(accession, str)
            or accession != accession.strip()
            or not re.fullmatch(r'\d{10}-\d{2}-\d{6}', accession)
        ):
            raise ValueError(f'{context} has invalid accessionNumber')
        if accession in seen_accessions:
            raise ValueError(f'{context} duplicates accessionNumber {accession}')
        seen_accessions.add(accession)
        for key, value, required in (
            ('filingDate', values[1][position], True),
            ('reportDate', values[3][position], False),
        ):
            if not isinstance(value, str) or (required and not value.strip()):
                raise ValueError(f'{context} has invalid {key}')
            if not value:
                continue
            try:
                date.fromisoformat(value)
            except ValueError as exc:
                raise ValueError(f'{context} has invalid {key}') from exc
        accepted = values[2][position]
        if not isinstance(accepted, str) or not accepted.strip():
            raise ValueError(f'{context} has invalid acceptanceDateTime')
        try:
            datetime.fromisoformat(accepted.replace('Z', '+00:00'))
        except ValueError as exc:
            raise ValueError(f'{context} has invalid acceptanceDateTime') from exc
        form = values[4][position]
        primary_document = values[5][position]
        if not isinstance(form, str) or not form.strip():
            raise ValueError(f'{context} has invalid form')
        validate_sec_relative_document_path(
            primary_document, allow_blank=True,
            allowed_suffixes=SEC_PRIMARY_DOCUMENT_SUFFIXES,
            context=f'{context} primaryDocument',
        )
    archive_files = (payload.get('filings') or {}).get('files', [])
    if not isinstance(archive_files, list):
        raise ValueError('SEC submissions filings.files must be an array')
    archive_names: set[str] = set()
    for descriptor in archive_files:
        if not isinstance(descriptor, dict):
            raise ValueError('SEC submissions archive descriptor must be an object')
        name = validate_sec_document_basename(
            descriptor.get('name'),
            allowed_suffixes=SEC_SUBMISSIONS_ARCHIVE_SUFFIXES,
            context='SEC submissions archive descriptor name',
        )
        normalized = name.casefold()
        if normalized in archive_names:
            raise ValueError(f'Duplicate SEC submissions archive descriptor: {name!r}')
        archive_names.add(normalized)
    if require_nonempty and not next(iter(lengths), 0) and not archive_files:
        raise ValueError('SEC submissions issuer feed is unexpectedly empty')


def _sec_ingestion_config_sha256(settings: dict[str, Any]) -> str:
    """Hash every input that can change canonical SEC ingestion artifacts."""
    payload = {
        'settings': {
            key: settings.get(key)
            for key in (
                'submissions_url_template','submissions_archive_url_template',
                'companyfacts_url_template','include_submission_archives',
                'documents_per_issuer','hydrate_documents','companyfacts_lag_days',
            )
        },
        'allowed_fact_forms': sorted(ALLOWED_FACT_FORMS),
        'document_forms': sorted(DOCUMENT_FORMS),
        'profile_financial_forms': sorted(PROFILE_FINANCIAL_FORMS),
        'profile_conditional_xbrl_forms': sorted(PROFILE_CONDITIONAL_XBRL_FORMS),
        'financial_form_families': sorted(FINANCIAL_FORM_FAMILIES.items()),
        'schema_version': SEC_INGESTION_CONFIG_VERSION,
    }
    return hashlib.sha256(json.dumps(
        payload,sort_keys=True,separators=(',', ':'),ensure_ascii=True,
    ).encode()).hexdigest()


def _issuer_scope_sha256(rows: Iterable[sqlite3.Row]) -> str:
    scope = sorted(
        [
            str(row[0]), int(row[2]), str(row[1] or '').zfill(10),
            str(row[3] or 'USD').strip().upper() or 'USD',
        ]
        for row in rows
    )
    return hashlib.sha256(json.dumps(
        scope,sort_keys=True,separators=(',', ':'),ensure_ascii=True,
    ).encode()).hexdigest()


def _sec_ingestion_watermark(conn: sqlite3.Connection) -> str:
    row = conn.execute('''SELECT asof_date
        FROM consumer_defensive_sec_ingestion_watermark
        WHERE model_family=?''',(MODEL_FAMILY,)).fetchone()
    return str(row[0]) if row else ''


def _assert_sec_ingestion_not_reverse(
    conn: sqlite3.Connection, *, asof_date: str
) -> None:
    watermark = _sec_ingestion_watermark(conn)
    if watermark and asof_date < watermark:
        raise RuntimeError(
            'SEC ingestion reverse replay rejected before mutation: '
            f'requested={asof_date} watermark={watermark}'
        )


def _advance_sec_ingestion_watermark(
    conn: sqlite3.Connection, *, asof_date: str, mutation_kind: str
) -> None:
    '''Advance the singleton watermark inside the caller transaction.'''
    watermark = _sec_ingestion_watermark(conn)
    if watermark and asof_date < watermark:
        raise RuntimeError(
            f'SEC ingestion watermark regression: {asof_date} < {watermark}'
        )
    conn.execute('''INSERT INTO consumer_defensive_sec_ingestion_watermark(
        model_family,asof_date,cutoff,mutation_kind,updated_at)
        VALUES(?,?,?,?,?) ON CONFLICT(model_family) DO UPDATE SET
        asof_date=excluded.asof_date,cutoff=excluded.cutoff,
        mutation_kind=excluded.mutation_kind,updated_at=excluded.updated_at''',
        (MODEL_FAMILY,asof_date,asof_date+'T23:59:59Z',mutation_kind,utc_now()),
    )


def _cache_only_sec_preflight(
    conn: sqlite3.Connection, *, asof_date: str,
    ingestion_config_sha256: str, issuer_scope_sha256: str,
    scope_issuer_count: int,
) -> None:
    row = conn.execute('''SELECT r.scope_issuer_count,
        r.ingestion_config_sha256,r.issuer_scope_sha256,
        s.ingestion_config_sha256,s.issuer_scope_sha256
        FROM consumer_defensive_sec_reconciliation_state r
        JOIN consumer_defensive_sec_cache_snapshot s USING(asof_date)
        WHERE r.asof_date=? AND r.status='complete'
          AND r.scope_contract_version=3
          AND s.scope_contract_version=3
          AND r.trust_state='trusted_current'
          AND s.trust_state='trusted_current'
          AND r.cache_manifest_sha256=s.cache_manifest_sha256
          AND r.cache_manifest_json=s.cache_manifest_json''',(asof_date,)).fetchone()
    if row is None or (
        int(row[0]) != scope_issuer_count
        or any(str(row[index]) != ingestion_config_sha256 for index in (1,3))
        or any(str(row[index]) != issuer_scope_sha256 for index in (2,4))
    ):
        raise RuntimeError(
            'Cache-only SEC replay config/scope does not exactly match the '
            f'immutable snapshot and reconciliation for {asof_date}'
        )


def _sec_ingestion_state_is_empty(conn: sqlite3.Connection) -> bool:
    tables = (
        'fact_sec_filing',
        'bridge_sec_filing_company',
        'fact_sec_filing_document',
        'bridge_sec_filing_document_company',
        'fact_sec_xbrl_fact_raw',
        'fact_financial_statement_canonical',
        'feature_financial_statement',
        'dim_issuer_reporting_profile',
        'fact_sec_inline_xbrl_fallback_run',
        'fact_specialized_metric_disclosure_census',
        'fact_specialized_metric_disclosure_summary',
        'consumer_defensive_sec_reconciliation_state',
        'consumer_defensive_sec_cache_snapshot',
        'consumer_defensive_sec_ingestion_watermark',
    )
    return not any(
        conn.execute(f'SELECT 1 FROM {table} LIMIT 1').fetchone()
        for table in tables
    )


def _validate_companyfacts_payload(payload: object, *, expected_cik: str) -> None:
    if not isinstance(payload, dict):
        raise ValueError('SEC Companyfacts payload must be an object')
    payload_cik = str(payload.get('cik') or '').strip()
    if not payload_cik:
        raise ValueError('SEC Companyfacts payload is missing required root CIK')
    if payload_cik and payload_cik.zfill(10) != expected_cik:
        raise ValueError(
            f'SEC Companyfacts CIK mismatch: expected {expected_cik}, got {payload_cik}'
        )
    facts = payload.get('facts', {})
    if not isinstance(facts, dict):
        raise ValueError('SEC Companyfacts facts must be an object')
    for taxonomy, concepts in facts.items():
        if not isinstance(taxonomy, str) or not taxonomy.strip():
            raise ValueError('SEC Companyfacts taxonomy names must be non-empty strings')
        if not isinstance(concepts, dict):
            raise ValueError(
                f'SEC Companyfacts taxonomy {taxonomy!r} must contain an object'
            )
        for concept, definition in concepts.items():
            if not isinstance(concept, str) or not concept.strip():
                raise ValueError('SEC Companyfacts concept names must be non-empty strings')
            if not isinstance(definition, dict):
                raise ValueError(
                    f'SEC Companyfacts concept {taxonomy}:{concept} must be an object'
                )
            units = definition.get('units')
            if not isinstance(units, dict):
                raise ValueError(
                    f'SEC Companyfacts concept {taxonomy}:{concept} units must be an object'
                )
            for unit, observations in units.items():
                if not isinstance(unit, str) or not unit.strip():
                    raise ValueError(
                        f'SEC Companyfacts concept {taxonomy}:{concept} has an invalid unit'
                    )
                if not isinstance(observations, list):
                    raise ValueError(
                        f'SEC Companyfacts observations for {taxonomy}:{concept}:{unit} '
                        'must be an array'
                    )
                for position, observation in enumerate(observations):
                    context = f'{taxonomy}:{concept}:{unit}[{position}]'
                    if not isinstance(observation, dict):
                        raise ValueError(
                            f'SEC Companyfacts observation {context} must be an object'
                        )
                    for key in ('accn', 'form', 'filed', 'end'):
                        value = observation.get(key)
                        if not isinstance(value, str) or not value.strip():
                            raise ValueError(
                                f'SEC Companyfacts observation {context} has invalid {key}'
                            )
                    accession = str(observation['accn']).strip()
                    if not re.fullmatch(r'\d{10}-\d{2}-\d{6}', accession):
                        raise ValueError(
                            f'SEC Companyfacts observation {context} has invalid accession'
                        )
                    for key in ('filed', 'end'):
                        try:
                            date.fromisoformat(str(observation[key]))
                        except ValueError as exc:
                            raise ValueError(
                                f'SEC Companyfacts observation {context} has invalid {key}'
                            ) from exc
                    start = observation.get('start')
                    if start not in (None, ''):
                        if not isinstance(start, str):
                            raise ValueError(
                                f'SEC Companyfacts observation {context} has invalid start'
                            )
                        try:
                            date.fromisoformat(start)
                        except ValueError as exc:
                            raise ValueError(
                                f'SEC Companyfacts observation {context} has invalid start'
                            ) from exc
                    value = observation.get('val')
                    if (
                        isinstance(value, bool)
                        or not isinstance(value, (int, float))
                        or not math.isfinite(float(value))
                    ):
                        raise ValueError(
                            f'SEC Companyfacts observation {context} has invalid numeric val'
                        )


def _validate_fx_chart_payload(
    payload: object, *, expected_symbol: str,
    start_date: date, end_date: date,
) -> list[FxRateObservation]:
    """Validate one Yahoo daily FX chart and return usable requested-window rows.

    Yahoo keeps the timestamp and close arrays positionally aligned, representing
    a missing daily quote with a JSON ``null`` close.  It can also return a small
    number of observations immediately outside the requested UTC-date boundary
    (notably an in-progress next-day FX quote).  Missing closes are therefore
    skipped and near-boundary observations are validated but filtered.  A payload
    whose timestamps stray materially beyond the requested window is treated as
    the wrong response/cache object and rejected.
    """

    if start_date > end_date:
        raise ValueError(
            f'Invalid FX validation window: {start_date.isoformat()} is after '
            f'{end_date.isoformat()}'
        )
    if not isinstance(payload, dict) or not isinstance(payload.get('chart'), dict):
        raise ValueError('FX chart payload must contain a chart object')
    chart = payload['chart']
    if chart.get('error') not in (None, {}):
        raise ValueError(f'FX chart provider returned an error: {chart.get("error")!r}')
    results = chart.get('result')
    if not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], dict):
        raise ValueError('FX chart result must contain exactly one object')
    result = results[0]
    meta = result.get('meta')
    if not isinstance(meta, dict):
        raise ValueError('FX chart result meta must be an object')
    symbol = str(meta.get('symbol') or '').strip()
    if symbol != expected_symbol:
        raise ValueError(
            f'FX chart symbol mismatch: expected {expected_symbol}, got {symbol or "<missing>"}'
        )
    timestamps = result.get('timestamp')
    indicators = result.get('indicators')
    quotes = indicators.get('quote') if isinstance(indicators, dict) else None
    if (
        not isinstance(timestamps, list)
        or not isinstance(quotes, list)
        or len(quotes) != 1
        or not isinstance(quotes[0], dict)
        or not isinstance(quotes[0].get('close'), list)
    ):
        raise ValueError('FX chart timestamps and quote-close arrays are required')
    closes = quotes[0]['close']
    if len(timestamps) != len(closes) or not timestamps:
        raise ValueError('FX chart timestamp and close arrays must be non-empty and equal length')
    observations: list[FxRateObservation] = []
    previous_stamp: int | None = None
    previous_rate_date: date | None = None
    tolerance = timedelta(days=FX_PROVIDER_BOUNDARY_TOLERANCE_DAYS)
    earliest_allowed = (
        start_date - tolerance if start_date >= date.min + tolerance else date.min
    )
    latest_allowed = end_date + tolerance if end_date <= date.max - tolerance else date.max
    outside_window_count = 0
    for position, (raw_stamp, raw_value) in enumerate(zip(timestamps, closes, strict=True)):
        if isinstance(raw_stamp, bool) or not isinstance(raw_stamp, (int, float)):
            raise ValueError(f'FX chart timestamp {position} must be a finite integer')
        try:
            numeric_stamp = float(raw_stamp)
        except (OverflowError, TypeError, ValueError) as exc:
            raise ValueError(
                f'FX chart timestamp {position} must be a finite integer'
            ) from exc
        if not math.isfinite(numeric_stamp) or not numeric_stamp.is_integer():
            raise ValueError(f'FX chart timestamp {position} must be a finite integer')
        stamp = int(raw_stamp)
        if previous_stamp is not None and stamp <= previous_stamp:
            raise ValueError('FX chart timestamps must be unique and strictly increasing')
        previous_stamp = stamp
        try:
            rate_date = datetime.fromtimestamp(stamp, tz=timezone.utc).date()
        except (OSError, OverflowError, ValueError) as exc:
            raise ValueError(f'FX chart timestamp {position} is outside the supported range') from exc
        if previous_rate_date is not None and rate_date <= previous_rate_date:
            raise ValueError('FX chart daily observations must have unique increasing UTC dates')
        previous_rate_date = rate_date
        if rate_date < earliest_allowed or rate_date > latest_allowed:
            raise ValueError(
                f'FX chart timestamp {rate_date.isoformat()} is materially outside requested '
                f'range {start_date.isoformat()}..{end_date.isoformat()}'
            )
        if rate_date < start_date or rate_date > end_date:
            outside_window_count += 1
            if outside_window_count > FX_PROVIDER_BOUNDARY_MAX_OBSERVATIONS:
                raise ValueError(
                    'FX chart payload contains too many observations outside requested range '
                    f'{start_date.isoformat()}..{end_date.isoformat()}'
                )
        if raw_value is None:
            continue
        if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
            raise ValueError(f'FX chart close {position} must be finite and positive')
        try:
            rate = float(raw_value)
        except (OverflowError, TypeError, ValueError) as exc:
            raise ValueError(
                f'FX chart close {position} must be finite and positive'
            ) from exc
        if not math.isfinite(rate) or rate <= 0:
            raise ValueError(f'FX chart close {position} must be finite and positive')
        if rate_date < start_date or rate_date > end_date:
            continue
        observations.append(FxRateObservation(
            expected_symbol.removesuffix('USD=X'), rate_date.isoformat(), rate
        ))
    if not observations:
        raise ValueError(
            'FX chart payload has no usable observations in requested range '
            f'{start_date.isoformat()}..{end_date.isoformat()}'
        )
    return observations


def _contains_inline_xbrl_markup(raw: bytes) -> bool:
    """Require both the standard inline-XBRL namespace and an inline fact element."""

    return bool(INLINE_XBRL_NAMESPACE.search(raw) and INLINE_XBRL_ELEMENT.search(raw))


def _contains_financial_inline_xbrl_markup(raw: bytes) -> bool:
    """Require a non-DEI numeric inline fact before treating a 6-K as financial."""

    return bool(
        INLINE_XBRL_NAMESPACE.search(raw) and INLINE_XBRL_NUMERIC_FACT.search(raw)
    )


def _reporting_profile_anchor(
    filing_rows: Iterable[dict[str, Any]],
    *,
    cutoff: str,
    companyfacts_xbrl_accessions: set[str],
    inline_xbrl_accessions: set[str],
) -> tuple[str, str]:
    """Return latest financial acceptance and normalized primary annual form.

    A 6-K is financial-profile evidence only when its exact accession has XBRL
    facts in Companyfacts or verified inline-XBRL markup.  Ordinary submissions,
    ownership reports, and non-XBRL 6-K filings cannot move the profile anchor.
    """

    xbrl_accessions = companyfacts_xbrl_accessions | inline_xbrl_accessions
    eligible: list[tuple[str, str]] = []
    for row in filing_rows:
        accession = str(row.get("accessionNumber") or "").strip()
        form = str(row.get("form") or "").strip().upper()
        accepted = _normalize_accepted(row.get("acceptanceDateTime"))
        if not accession or not accepted or accepted > cutoff:
            continue
        if form not in PROFILE_FINANCIAL_FORMS and not (
            form in PROFILE_CONDITIONAL_XBRL_FORMS and accession in xbrl_accessions
        ):
            continue
        eligible.append((accepted, form))
    eligible.sort(reverse=True)
    latest_filing = eligible[0][0] if eligible else ""
    annual = ""
    for _, form in eligible:
        base_form = _canonical_financial_form(form)
        if base_form in PROFILE_ANNUAL_FORMS:
            annual = base_form
            break
    return latest_filing, annual


def _pit_inline_fallback_required(
    conn: sqlite3.Connection, *, ticker: str, cutoff: str, lag_days: int
) -> bool:
    evidence_rows = conn.execute(
        '''SELECT b.accession_number,b.form_type,f.accepted_at
           FROM bridge_sec_filing_company b
           JOIN fact_sec_filing f ON f.accession_number=b.accession_number
           WHERE b.issuer_ticker=? AND f.accepted_at<=?
             AND COALESCE((SELECT e.event_type
                 FROM sec_filing_company_association_event e
                 WHERE e.accession_number=b.accession_number
                   AND e.issuer_company_id=b.issuer_company_id
                   AND e.effective_asof<=?
                 ORDER BY e.effective_asof DESC,e.event_id DESC LIMIT 1),
                 CASE WHEN b.association_status='active' THEN 'observed' ELSE 'retired' END)
                 IN ('observed','reactivated')''',
        (ticker, cutoff, cutoff),
    ).fetchall()
    eligible: list[tuple[str, str]] = []
    for row in evidence_rows:
        form = str(row['form_type'] or '').upper()
        accession = str(row['accession_number'])
        accepted = str(row['accepted_at'] or '')
        conditional = form in PROFILE_CONDITIONAL_XBRL_FORMS and bool(
            conn.execute(
                '''SELECT 1 FROM fact_sec_xbrl_fact_raw
                   WHERE ticker=? AND accession_number=? AND accepted_at<=?
                   LIMIT 1''',
                (ticker, accession, cutoff),
            ).fetchone()
        )
        if form in PROFILE_FINANCIAL_FORMS or conditional:
            eligible.append((accepted, form))
    eligible.sort(reverse=True)
    latest_filing = eligible[0][0] if eligible else ''
    annual = next(
        (
            _canonical_financial_form(form) for _, form in eligible
            if _canonical_financial_form(form) in PROFILE_ANNUAL_FORMS
        ),
        '',
    )
    latest_fact = str(conn.execute(
        '''SELECT MAX(r.accepted_at) FROM fact_sec_xbrl_fact_raw r
           JOIN bridge_sec_filing_company b
             ON b.accession_number=r.accession_number
            AND b.issuer_ticker=r.ticker
           WHERE r.ticker=? AND r.accepted_at<=?
             AND COALESCE((SELECT e.event_type
                 FROM sec_filing_company_association_event e
                 WHERE e.accession_number=b.accession_number
                   AND e.issuer_company_id=b.issuer_company_id
                   AND e.effective_asof<=?
                 ORDER BY e.effective_asof DESC,e.event_id DESC LIMIT 1),
                 CASE WHEN b.association_status='active' THEN 'observed' ELSE 'retired' END)
                 IN ('observed','reactivated')''',
        (ticker, cutoff, cutoff),
    ).fetchone()[0] or '')
    lag = None
    if latest_filing and latest_fact:
        lag = (
            date.fromisoformat(latest_filing[:10])
            - date.fromisoformat(latest_fact[:10])
        ).days
    return bool(
        annual in {'20-F', '40-F'} and (lag is None or lag > lag_days)
    )


def _issuer_rows(conn: sqlite3.Connection, tickers: list[str] | None = None) -> list[sqlite3.Row]:
    query = """
        SELECT t.ticker, c.cik, c.company_id,
               COALESCE(NULLIF(UPPER(TRIM(c.reporting_currency)),''),'USD')
                   AS reporting_currency
        FROM dim_consumer_defensive_taxonomy t
        JOIN dim_company c ON c.company_id=t.company_id
        WHERE t.model_family=?
    """
    params: list[Any] = [MODEL_FAMILY]
    if tickers is not None:
        if not tickers:
            return []
        query += f" AND t.ticker IN ({','.join('?' for _ in tickers)})"
        params.extend(tickers)
    query += " ORDER BY t.ticker"
    return conn.execute(query, params).fetchall()


def _association_manifest_payload(payload: Iterable[Iterable[Any]]) -> dict[str, Any]:
    digest = hashlib.sha256()
    association_count = 0
    accession_count = 0
    shared_accession_count = 0
    previous_accession = ''
    current_accession_rows = 0
    for raw_row in payload:
        row = list(raw_row)
        accession = str(row[0])
        if accession != previous_accession:
            if previous_accession:
                accession_count += 1
                shared_accession_count += int(current_accession_rows > 1)
            previous_accession = accession
            current_accession_rows = 0
        current_accession_rows += 1
        association_count += 1
        digest.update(json.dumps(row, ensure_ascii=True, separators=(',', ':')).encode())
        digest.update(b'\n')
    if previous_accession:
        accession_count += 1
        shared_accession_count += int(current_accession_rows > 1)
    return {
        'association_count': association_count,
        'accession_count': accession_count,
        'shared_accession_count': shared_accession_count,
        'association_sha256': digest.hexdigest(),
    }


def _raw_fact_semantics(rows: Iterable[Iterable[Any]]) -> list[str]:
    return sorted(
        json.dumps(list(row), ensure_ascii=True, separators=(',', ':'))
        for row in rows
    )


def _additive_raw_fact_rows(
    existing_rows: Iterable[Iterable[Any]],
    staged_rows: Iterable[tuple[Any, ...]],
) -> list[tuple[Any, ...]] | None:
    '''Return only new rows when a staged SEC slice is a strict safe superset.'''
    existing_by_id: dict[str, tuple[Any, ...]] = {}
    for raw_row in existing_rows:
        row = tuple(raw_row)
        observation_id = str(row[-1] or '')
        if not observation_id or observation_id in existing_by_id:
            return None
        existing_by_id[observation_id] = row[:-1]
    staged_by_id: dict[str, tuple[Any, ...]] = {}
    materialized: list[tuple[Any, ...]] = []
    for raw_row in staged_rows:
        row = tuple(raw_row)
        observation_id = str(row[-2] or '')
        if not observation_id or observation_id in staged_by_id:
            return None
        staged_by_id[observation_id] = row[:-2]
        materialized.append(row)
    if not set(existing_by_id).issubset(staged_by_id):
        return None
    if any(
        staged_by_id[observation_id] != semantic
        for observation_id, semantic in existing_by_id.items()
    ):
        return None
    return [
        row for row in materialized
        if str(row[-2]) not in existing_by_id
    ]


def _association_manifest(
    conn: sqlite3.Connection, *, cutoff: str, tickers: list[str]
) -> dict[str, Any]:
    if not tickers:
        payload: list[list[Any]] = []
    else:
        placeholders = ','.join('?' for _ in tickers)
        payload = conn.execute(
            f'''SELECT b.accession_number,b.issuer_company_id,b.issuer_ticker,b.issuer_cik,
                       b.relationship,b.form_type,b.filing_date,b.accepted_at,
                       COALESCE(b.report_date,''),COALESCE(b.primary_document,''),
                       b.source_id,COALESCE(b.source_url,'')
                FROM bridge_sec_filing_company b
                JOIN fact_sec_filing f ON f.accession_number=b.accession_number
                WHERE b.issuer_ticker IN ({placeholders}) AND f.accepted_at<=?
                  AND COALESCE((SELECT e.event_type
                      FROM sec_filing_company_association_event e
                      WHERE e.accession_number=b.accession_number
                        AND e.issuer_company_id=b.issuer_company_id
                        AND e.effective_asof<=?
                      ORDER BY e.effective_asof DESC,e.event_id DESC LIMIT 1),
                      CASE WHEN b.association_status='active' THEN 'observed' ELSE 'retired' END)
                      IN ('observed','reactivated')
                ORDER BY b.accession_number,b.issuer_company_id''',
            [*tickers, cutoff, cutoff],
        )
    return _association_manifest_payload(payload)


def _retire_absent_associations(
    conn: sqlite3.Connection,
    *,
    cutoff: str,
    scope: list[str],
    parsed_keys: set[tuple[str, int]],
) -> int:
    '''Non-destructively retire associations absent from a clean full parse.'''
    if not scope:
        return 0
    placeholders = ','.join('?' for _ in scope)
    rows = conn.execute(
        f'''SELECT accession_number,issuer_company_id,issuer_ticker,issuer_cik
            FROM bridge_sec_filing_company
            WHERE issuer_ticker IN ({placeholders}) AND accepted_at<=?
              AND association_status='active' ''',
        [*scope, cutoff],
    ).fetchall()
    stale = [
        (str(row[0]), int(row[1]), str(row[2]), str(row[3])) for row in rows
        if (str(row[0]), int(row[1])) not in parsed_keys
    ]
    conn.executemany(
        '''UPDATE bridge_sec_filing_company
           SET association_status='retired',retirement_effective_asof=?,
               retirement_reason='absent_from_complete_submissions_reconciliation',
               updated_at=?
           WHERE accession_number=? AND issuer_company_id=?''',
        [(cutoff[:10], utc_now(), accession, company_id)
         for accession, company_id, _, _ in stale],
    )
    for accession, company_id, ticker, cik in stale:
        _append_association_event(
            conn,accession=accession,company_id=company_id,ticker=ticker,cik=cik,
            effective_asof=cutoff,event_type='retired',
            reason='absent_from_complete_submissions_reconciliation',
        )
    return len(stale)


def sync_sec_fundamentals(
    conn: sqlite3.Connection,
    bundle: ConfigBundle,
    *,
    tickers: list[str] | None = None,
    as_of: str | None = None,
    force_refresh: bool = False,
    fetch: Fetcher | None = None,
    _rehabilitation_preflight: bool = False,
    incremental_from_asof: str | None = None,
) -> dict[str, Any]:
    asof_date = (as_of or date.today().isoformat())[:10]
    date.fromisoformat(asof_date)
    _assert_sec_ingestion_not_reverse(conn, asof_date=asof_date)
    config = bundle.payload
    settings = cfg_get(config, "sec_fundamentals")
    cache = resolve_path(settings["cache_dir"], base_dir=bundle.base_dir)
    fetch = fetch or http_fetcher(_http_policy(config, "sec_fundamentals"))
    cutoff = asof_date + "T23:59:59Z"
    incremental_cutoff = ''
    incremental_input_hashes: dict[str, str] = {}
    if incremental_from_asof is not None:
        incremental_date = incremental_from_asof[:10]
        date.fromisoformat(incremental_date)
        if incremental_date >= asof_date:
            raise ValueError(
                'Incremental SEC history base must precede the requested cutoff.'
            )
        trusted_base = conn.execute(
            '''SELECT cache_manifest_json
               FROM consumer_defensive_sec_cache_snapshot
               WHERE asof_date=? AND scope_contract_version=3
                 AND trust_state='trusted_current' ''',
            (incremental_date,),
        ).fetchone()
        if trusted_base is None:
            raise RuntimeError(
                'Incremental SEC history base lacks an exact trusted snapshot: '
                f'{incremental_date}'
            )
        incremental_cutoff = incremental_date + 'T23:59:59Z'
        for entry in json.loads(str(trusted_base[0])):
            logical_path = str(entry.get('path') or '')
            if logical_path.startswith(('submissions/', 'companyfacts/')):
                incremental_input_hashes[logical_path] = str(
                    entry.get('sha256') or ''
                )
    expected_issuers = _issuer_rows(conn, tickers)
    ingestion_config_sha256 = _sec_ingestion_config_sha256(settings)
    issuer_scope_sha256 = _issuer_scope_sha256(expected_issuers)
    cache_only = os.environ.get(
        'CONSUMER_DEFENSIVE_CACHE_ONLY', ''
    ).strip().casefold() in {'1', 'true', 'yes', 'on'}
    snapshot_exists = bool(conn.execute(
        '''SELECT 1 FROM consumer_defensive_sec_cache_snapshot
           WHERE asof_date=? AND scope_contract_version=3
             AND trust_state='trusted_current' ''',
        (cutoff[:10],),
    ).fetchone())
    quarantined_snapshot_exists = bool(conn.execute(
        '''SELECT 1 FROM consumer_defensive_sec_cache_snapshot
           WHERE asof_date=? AND (
               scope_contract_version<>3 OR trust_state<>'trusted_current'
           )''',(cutoff[:10],),
    ).fetchone())
    rehabilitation_lookup: dict[str, Path] | None = None
    if quarantined_snapshot_exists:
        watermark = _sec_ingestion_watermark(conn)
        if tickers is not None or asof_date != watermark:
            raise RuntimeError(
                'Quarantined SEC snapshot requires a full exact-seal '
                'rehabilitation at the current ingestion watermark'
            )
        rehabilitation_lookup = _sealed_cache_lookup(
            conn, cache, cutoff[:10], allow_quarantined=True
        )
    empty_cache_only_bootstrap = (
        cache_only
        and rehabilitation_lookup is None
        and not snapshot_exists
        and not quarantined_snapshot_exists
        and _sec_ingestion_state_is_empty(conn)
    )
    if cache_only and rehabilitation_lookup is None and not empty_cache_only_bootstrap:
        _cache_only_sec_preflight(
            conn,asof_date=cutoff[:10],
            ingestion_config_sha256=ingestion_config_sha256,
            issuer_scope_sha256=issuer_scope_sha256,
            scope_issuer_count=len(expected_issuers),
        )
    if snapshot_exists and tickers is None and not cache_only:
        snapshot = conn.execute('''SELECT ingestion_config_sha256,
            issuer_scope_sha256 FROM consumer_defensive_sec_cache_snapshot
            WHERE asof_date=? AND scope_contract_version=3
              AND trust_state='trusted_current' ''',(cutoff[:10],)).fetchone()
        prior = conn.execute('''SELECT r.scope_issuer_count,r.association_count,
            r.accession_count,r.shared_accession_count,r.association_sha256,
            r.ingestion_config_sha256,r.issuer_scope_sha256,
            s.ingestion_config_sha256,s.issuer_scope_sha256
            FROM consumer_defensive_sec_reconciliation_state r
            JOIN consumer_defensive_sec_cache_snapshot s USING(asof_date)
            WHERE r.asof_date=? AND r.status='complete'
              AND r.scope_contract_version=3
              AND s.scope_contract_version=3
              AND r.trust_state='trusted_current'
              AND s.trust_state='trusted_current' ''',(cutoff[:10],)).fetchone()
        if prior is None:
            if (
                snapshot is None
                or str(snapshot[0]) != ingestion_config_sha256
                or str(snapshot[1]) != issuer_scope_sha256
            ):
                raise RuntimeError(
                    'Immutable SEC snapshot lacks compatible reconciliation state'
                )
            # An invalidated v8 reconciliation retains its exact trusted
            # snapshot. Verify those immutable bytes before a live same-date
            # acquisition; final resealing must still match them exactly.
            rehabilitation_lookup = _sealed_cache_lookup(
                conn, cache, cutoff[:10]
            )
        else:
            if (
                str(prior[5]) != ingestion_config_sha256
                or str(prior[6]) != issuer_scope_sha256
                or str(prior[7]) != ingestion_config_sha256
                or str(prior[8]) != issuer_scope_sha256
                or int(prior[0]) != len(expected_issuers)
            ):
                raise RuntimeError(
                    f'Immutable SEC snapshot config/scope conflict for {cutoff[:10]}'
                )
            _sealed_cache_lookup(conn, cache, cutoff[:10])
            manifest = {
                'association_count': int(prior[1]),
                'accession_count': int(prior[2]),
                'shared_accession_count': int(prior[3]),
                'association_sha256': str(prior[4]),
            }
            return {
                'issuers': int(prior[0]),'filings_processed': 0,
                'filings_stored_unique': int(prior[2]),'raw_facts': 0,
                'documents': 0,'retired_associations': 0,
                'cache_manifest': {'immutable_replay': True},
                'association_manifest': manifest,
                'parsed_association_manifest': dict(manifest),
                'full_scope_reconciled': True,'failures': [],
            }
    if snapshot_exists and tickers is not None and not cache_only:
        raise RuntimeError(
            f'Cannot run a live targeted refresh against immutable SEC snapshot '
            f'{cutoff[:10]}'
        )
    if rehabilitation_lookup is not None and not _rehabilitation_preflight:
        scratch = sqlite3.connect(':memory:')
        scratch.row_factory = sqlite3.Row
        scratch.execute('PRAGMA foreign_keys=ON')
        try:
            conn.backup(scratch)
            checked = sync_sec_fundamentals(
                scratch, bundle, tickers=tickers, as_of=asof_date,
                force_refresh=force_refresh, fetch=fetch,
                _rehabilitation_preflight=True,
            )
            if checked['failures'] or not checked['full_scope_reconciled']:
                raise RuntimeError(
                    'Immutable SEC snapshot rehabilitation preflight failed: '
                    f"{checked['failures']}"
                )
        finally:
            scratch.close()
    sealed_lookup = rehabilitation_lookup or (
        _sealed_cache_lookup(conn, cache, cutoff[:10])
        if cache_only and snapshot_exists else None
    )
    failures: list[dict[str, str]] = []
    filing_count = fact_count = document_count = 0
    cache_records: list[dict[str, Any]] = []
    parsed_associations: dict[tuple[str, int], list[Any]] = {}
    if tickers is None and not cache_only:
        with conn:
            mutation_changes_before = conn.total_changes
            conn.execute('''UPDATE consumer_defensive_sec_reconciliation_state
                SET trust_state='invalidated_by_refresh',
                    quarantine_reason='full_live_refresh_started'
                WHERE asof_date=? AND trust_state='trusted_current' ''',
                (cutoff[:10],))
            if conn.total_changes > mutation_changes_before:
                _advance_sec_ingestion_watermark(
                    conn,asof_date=asof_date,
                    mutation_kind='full_reconciliation_invalidated',
                )
    for issuer in expected_issuers:
        ticker, cik, company_id = str(issuer[0]), str(issuer[1] or ""), int(issuer[2])
        if not cik:
            failures.append({"ticker": ticker, "error": "missing_cik"})
            continue
        cik10 = cik.zfill(10)
        try:
            submissions_url = settings["submissions_url_template"].format(cik=cik10)
            submissions_path = cache / "submissions" / f"CIK{cik10}.json"
            submissions_raw, submissions_payload = _validated_mutable_json(
                fetch,
                submissions_url,
                submissions_path,
                validate=lambda payload: _validate_submissions_payload(
                    payload, expected_cik=cik10, require_nonempty=True
                ), cache_root=cache, sealed_lookup=sealed_lookup,
                force=force_refresh, promote=False,
            )
            issuer_cache_payloads = [(submissions_path, submissions_raw)]
            filing_rows = list(_filing_rows(submissions_payload))
            for archive in submissions_payload.get("filings", {}).get("files", []) if settings.get("include_submission_archives", True) else []:
                name = str(archive.get("name") or "")
                if name:
                    quoted_name = quote_sec_document_basename(
                        name,
                        allowed_suffixes=SEC_SUBMISSIONS_ARCHIVE_SUFFIXES,
                        context='SEC submissions archive descriptor name',
                    )
                    url = settings["submissions_archive_url_template"].format(
                        file_name=quoted_name
                    )
                    archive_path = cache / "submissions" / name
                    archive_raw, archive_payload = _validated_mutable_json(
                        fetch, url, archive_path,
                        validate=lambda payload: _validate_submissions_payload(
                            {"filings": {"recent": payload, "files": []}},
                            expected_cik=cik10, require_nonempty=False,
                            require_cik=False,
                        ),
                        cache_root=cache, sealed_lookup=sealed_lookup,
                        force=force_refresh, reuse_valid_cache=True, promote=False,
                    )
                    issuer_cache_payloads.append((archive_path, archive_raw))
                    filing_rows.extend(_filing_rows({"filings": {"recent": archive_payload}}))
            facts_url = settings["companyfacts_url_template"].format(cik=cik10)
            companyfacts_path = cache / "companyfacts" / f"CIK{cik10}.json"
            companyfacts_raw, companyfacts = _validated_mutable_json(
                fetch,
                facts_url,
                companyfacts_path,
                validate=lambda payload: _validate_companyfacts_payload(
                    payload, expected_cik=cik10
                ), cache_root=cache, sealed_lookup=sealed_lookup,
                force=force_refresh, promote=False,
            )
            issuer_cache_payloads.append((companyfacts_path, companyfacts_raw))
            filing_rows = _validated_issuer_filing_projection(
                filing_rows, companyfacts
            )
            hydration_candidates = [
                row for row in filing_rows
                if str(row.get('form') or '') in DOCUMENT_FORMS
                and _normalize_accepted(row.get('acceptanceDateTime'))
                and str(_normalize_accepted(row.get('acceptanceDateTime'))) <= cutoff
                and str(row.get('primaryDocument') or '')
                and Path(str(row.get('primaryDocument') or '')).suffix.casefold()
                    in SEC_DOCUMENT_SUFFIXES
            ]
            hydration_candidates.sort(
                key=lambda row: (
                    _normalize_accepted(row.get('acceptanceDateTime')) or ''
                ),
                reverse=True,
            )
            hydration_candidates = hydration_candidates[
                : int(settings.get('documents_per_issuer', 8))
            ]
            hydrated_document_paths: dict[tuple[str, str], Path] = {}
            for filing_row in hydration_candidates:
                accession = str(filing_row['accessionNumber'])
                primary = str(filing_row['primaryDocument'])
                hydrated_document_paths[(accession, primary)] = (
                    resolve_sec_relative_document_path(
                        cache / 'filings' / cik10 / accession,
                        primary, allowed_suffixes=SEC_DOCUMENT_SUFFIXES,
                        containment_root=cache,
                        context=(
                            f'SEC hydrated primaryDocument for {ticker} {accession}'
                        ),
                    )
                )
            if not cache_only and sealed_lookup is None:
                for staged_path, staged_raw in issuer_cache_payloads:
                    _atomic_promote_bytes(
                        staged_path, staged_raw, cache_root=cache
                    )
            issuer_input_records = [
                _cache_manifest_record(cache, staged_path, staged_raw)
                for staged_path, staged_raw in issuer_cache_payloads
            ]
            cache_records.extend(issuer_input_records)
            issuer_incremental_cutoff = ''
            if incremental_cutoff and all(
                incremental_input_hashes.get(str(record['path']))
                == str(record['sha256'])
                for record in issuer_input_records
            ):
                issuer_incremental_cutoff = incremental_cutoff
            accession_lookup: dict[str, str] = {}
            associated_rows: list[dict[str, Any]] = []
            shared_accessions_to_reconcile: set[str] = set()
            prior_projection: dict[str, tuple[list[Any], str]] = {}
            if issuer_incremental_cutoff:
                for stored in conn.execute(
                    '''SELECT b.accession_number,b.issuer_company_id,
                              b.issuer_ticker,b.issuer_cik,b.relationship,
                              b.form_type,b.filing_date,b.accepted_at,
                              COALESCE(b.report_date,''),
                              COALESCE(b.primary_document,''),b.source_id,
                              COALESCE(b.source_url,''),f.accepted_at
                       FROM bridge_sec_filing_company b
                       JOIN fact_sec_filing f
                         ON f.accession_number=b.accession_number
                       WHERE b.issuer_company_id=? AND b.accepted_at<=?''',
                    (company_id, issuer_incremental_cutoff),
                ):
                    prior_projection[str(stored[0])] = (
                        list(stored[:12]), str(stored[12]),
                    )
            with conn:
                mutation_changes_before = conn.total_changes
                for row in filing_rows:
                    accession = str(row["accessionNumber"] or "")
                    accepted = _normalize_accepted(row["acceptanceDateTime"])
                    form = str(row["form"] or "")
                    filed = str(row["filingDate"] or "")
                    if not accession or not filed or not accepted or accepted > cutoff:
                        continue
                    primary_document = str(row["primaryDocument"] or "")
                    association_source_url = (
                        _sec_archive_url(cik10, accession, primary_document)
                        if primary_document else None
                    )
                    parsed_row = [
                        accession, company_id, ticker, cik10,
                        "associated_via_submissions", form, filed, accepted,
                        str(row["reportDate"] or ""), primary_document,
                        SEC_SUBMISSIONS, association_source_url or '',
                    ]
                    association_key = (accession, company_id)
                    previous = parsed_associations.get(association_key)
                    if previous is not None and previous != parsed_row:
                        raise ValueError(
                            "Conflicting submissions metadata for issuer association "
                            f"{ticker} {accession}"
                        )
                    parsed_associations[association_key] = parsed_row
                    prior = prior_projection.get(accession)
                    if accepted <= issuer_incremental_cutoff and prior is not None:
                        stored_projection, canonical_accepted = prior
                        if stored_projection == parsed_row:
                            accession_lookup[accession] = canonical_accepted
                            canonical_row = dict(row)
                            canonical_row['acceptanceDateTime'] = canonical_accepted
                            associated_rows.append(canonical_row)
                            filing_count += 1
                            continue
                    projection = conn.execute('''SELECT association_status
                        FROM bridge_sec_filing_company
                        WHERE accession_number=? AND issuer_company_id=?''',
                        (accession,company_id)).fetchone()
                    prior_status = str(projection[0]) if projection else ''
                    conn.execute(
                        """INSERT INTO fact_sec_filing(accession_number,company_id,ticker,cik,form_type,filing_date,accepted_at,report_date,primary_document,source_id,source_url,content_sha256,created_at,updated_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL,?,?)
                           ON CONFLICT(accession_number) DO UPDATE SET
                               company_id=NULL,ticker='ACCESSION_NEUTRAL',cik=NULL,
                               source_url=NULL,
                               updated_at=excluded.updated_at""",
                        (accession, None, "ACCESSION_NEUTRAL", None, form, filed, accepted, str(row["reportDate"] or "") or None, primary_document or None, SEC_SUBMISSIONS, None, utc_now(), utc_now()),
                    )
                    conn.execute(
                        """INSERT INTO bridge_sec_filing_company(
                               accession_number,issuer_company_id,issuer_ticker,issuer_cik,
                               relationship,relationship_evidence,form_type,filing_date,accepted_at,
                               report_date,primary_document,source_id,source_url,created_at,updated_at
                           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(accession_number,issuer_company_id) DO UPDATE SET
                               issuer_ticker=excluded.issuer_ticker,issuer_cik=excluded.issuer_cik,
                               relationship=excluded.relationship,
                               relationship_evidence=excluded.relationship_evidence,
                               form_type=excluded.form_type,filing_date=excluded.filing_date,
                               accepted_at=excluded.accepted_at,report_date=excluded.report_date,
                               primary_document=excluded.primary_document,source_id=excluded.source_id,
                               source_url=excluded.source_url,updated_at=excluded.updated_at""",
                        (accession, company_id, ticker, cik10, "associated_via_submissions", "observed_in_issuer_submissions_feed", form, filed, accepted, str(row["reportDate"] or "") or None, primary_document or None, SEC_SUBMISSIONS, association_source_url, utc_now(), utc_now()),
                    )
                    conn.execute(
                        '''UPDATE bridge_sec_filing_company SET
                           association_status='active',
                           retirement_effective_asof=NULL,
                           retirement_reason=NULL
                           WHERE accession_number=? AND issuer_company_id=?''',
                        (accession, company_id),
                    )
                    if not prior_status:
                        _append_association_event(
                            conn,accession=accession,company_id=company_id,
                            ticker=ticker,cik=cik10,effective_asof=accepted,
                            event_type='observed',
                            reason='observed_in_issuer_submissions_feed',
                        )
                    elif prior_status == 'retired':
                        _append_association_event(
                            conn,accession=accession,company_id=company_id,
                            ticker=ticker,cik=cik10,effective_asof=cutoff,
                            event_type='reactivated',
                            reason='reobserved_in_complete_submissions_feed',
                        )
                    association_count = int(conn.execute(
                        'SELECT COUNT(*) FROM bridge_sec_filing_company WHERE accession_number=?',
                        (accession,),
                    ).fetchone()[0])
                    base_accepted = str(conn.execute(
                        'SELECT accepted_at FROM fact_sec_filing WHERE accession_number=?',
                        (accession,),
                    ).fetchone()[0] or '')
                    canonical_accepted = (
                        _reconcile_filing_accession(conn, accession)
                        if association_count > 1 or base_accepted != accepted
                        else base_accepted
                    )
                    if association_count > 1:
                        shared_accessions_to_reconcile.add(accession)
                    accession_lookup[accession] = canonical_accepted
                    canonical_row = dict(row)
                    canonical_row['acceptanceDateTime'] = canonical_accepted
                    associated_rows.append(canonical_row)
                    filing_count += 1
                if conn.total_changes > mutation_changes_before:
                    _advance_sec_ingestion_watermark(
                        conn,asof_date=asof_date,
                        mutation_kind=(
                            'targeted_filing_projection'
                            if tickers is not None
                            else 'full_filing_projection'
                        ),
                    )
            issuer_fact_count = 0
            issuer_document_count = 0
            with conn:
                mutation_changes_before = conn.total_changes
                taxonomies: set[str] = set()
                companyfacts_xbrl_accessions: set[str] = set()
                issuer_fact_rows: list[tuple[Any, ...]] = []
                facts_created_at = utc_now()
                latest_fact = ""
                if issuer_incremental_cutoff:
                    taxonomies.update(
                        str(row[0]) for row in conn.execute(
                            '''SELECT DISTINCT taxonomy
                               FROM fact_sec_xbrl_fact_raw
                               WHERE ticker=? AND source_id=?
                                 AND accepted_at<=?''',
                            (
                                ticker, SEC_COMPANYFACTS,
                                issuer_incremental_cutoff,
                            ),
                        )
                    )
                    companyfacts_xbrl_accessions.update(
                        str(row[0]) for row in conn.execute(
                            '''SELECT DISTINCT accession_number
                               FROM fact_sec_xbrl_fact_raw
                               WHERE ticker=? AND source_id=?
                                 AND accepted_at<=?
                                 AND COALESCE(accession_number,'')<>'' ''',
                            (
                                ticker, SEC_COMPANYFACTS,
                                issuer_incremental_cutoff,
                            ),
                        )
                    )
                    prior_latest = conn.execute(
                        '''SELECT MAX(accepted_at)
                           FROM fact_sec_xbrl_fact_raw
                           WHERE ticker=? AND source_id=? AND accepted_at<=?''',
                        (
                            ticker, SEC_COMPANYFACTS,
                            issuer_incremental_cutoff,
                        ),
                    ).fetchone()[0]
                    latest_fact = str(prior_latest or '')
                for taxonomy, concepts in (companyfacts.get("facts") or {}).items():
                    if taxonomy not in {"us-gaap", "ifrs-full", "dei"} or not isinstance(concepts, dict):
                        continue
                    taxonomies.add(taxonomy)
                    for concept, definition in concepts.items():
                        for unit, observations in (definition.get("units") or {}).items():
                            for obs in observations or []:
                                form = str(obs.get("form") or "")
                                accession = str(obs.get("accn") or "")
                                accepted = accession_lookup.get(accession)
                                if form not in ALLOWED_FACT_FORMS or not accepted or accepted > cutoff:
                                    continue
                                companyfacts_xbrl_accessions.add(accession)
                                latest_fact = max(latest_fact, accepted)
                                if (
                                    issuer_incremental_cutoff
                                    and accepted <= issuer_incremental_cutoff
                                ):
                                    continue
                                value = obs.get("val")
                                numeric = float(value) if isinstance(value, (int, float)) and math.isfinite(float(value)) else None
                                semantic_fact = (
                                    ticker,cik,accession or None,taxonomy,concept,
                                    str(value) if value is not None else None,numeric,unit,
                                    obs.get('start'),obs.get('end'),obs.get('filed'),
                                    accepted,form,obs.get('frame'),'{}',SEC_COMPANYFACTS,
                                    f'companyfacts:{taxonomy}:{concept}',
                                )
                                issuer_fact_rows.append((
                                    *semantic_fact,
                                    _source_observation_id(semantic_fact),facts_created_at,
                                ))
                if issuer_incremental_cutoff:
                    conn.executemany(
                        """INSERT OR IGNORE INTO fact_sec_xbrl_fact_raw(ticker,cik,accession_number,taxonomy,concept,value_text,numeric_value,unit,period_start,period_end,filed_date,accepted_at,form_type,frame,dimensions_json,source_id,source_detail,source_observation_id,created_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        issuer_fact_rows,
                    )
                    issuer_fact_count = int(conn.execute(
                        '''SELECT COUNT(*) FROM fact_sec_xbrl_fact_raw
                           WHERE ticker=? AND source_id=? AND accepted_at<=?''',
                        (ticker, SEC_COMPANYFACTS, cutoff),
                    ).fetchone()[0])
                else:
                    existing_fact_rows = conn.execute(
                        """SELECT ticker,cik,accession_number,taxonomy,concept,
                                  value_text,numeric_value,unit,period_start,period_end,
                                  filed_date,accepted_at,form_type,frame,dimensions_json,
                                  source_id,source_detail,source_observation_id
                           FROM fact_sec_xbrl_fact_raw
                           WHERE ticker=? AND source_id=? AND accepted_at<=?""",
                        (ticker, SEC_COMPANYFACTS, cutoff),
                    ).fetchall()
                    additive_rows = _additive_raw_fact_rows(
                        existing_fact_rows, issuer_fact_rows
                    )
                if not issuer_incremental_cutoff and additive_rows is None:
                    conn.execute(
                        """DELETE FROM fact_sec_xbrl_fact_raw
                           WHERE ticker=? AND source_id=? AND accepted_at<=?""",
                        (ticker, SEC_COMPANYFACTS, cutoff),
                    )
                    conn.executemany(
                        """INSERT INTO fact_sec_xbrl_fact_raw(ticker,cik,accession_number,taxonomy,concept,value_text,numeric_value,unit,period_start,period_end,filed_date,accepted_at,form_type,frame,dimensions_json,source_id,source_detail,source_observation_id,created_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        issuer_fact_rows,
                    )
                elif not issuer_incremental_cutoff and additive_rows:
                    conn.executemany(
                        """INSERT INTO fact_sec_xbrl_fact_raw(ticker,cik,accession_number,taxonomy,concept,value_text,numeric_value,unit,period_start,period_end,filed_date,accepted_at,form_type,frame,dimensions_json,source_id,source_detail,source_observation_id,created_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        additive_rows,
                    )
                if not issuer_incremental_cutoff:
                    issuer_fact_count = len(issuer_fact_rows)
                inline_xbrl_accessions: set[str] = set()
                relevant_accessions = {
                    (str(row['accessionNumber']), str(row['primaryDocument']))
                    for row in hydration_candidates
                }
                relevant = [
                    row for row in associated_rows
                    if (
                        str(row['accessionNumber']),
                        str(row['primaryDocument']),
                    ) in relevant_accessions
                ]
                relevant.sort(key=lambda r: _normalize_accepted(r.get("acceptanceDateTime")) or "", reverse=True)
                for row in relevant:
                    accession = str(row["accessionNumber"])
                    primary = str(row["primaryDocument"] or "")
                    if not primary:
                        continue
                    doc_url = _sec_archive_url(cik10, accession, primary)
                    doc_path = hydrated_document_paths[(accession, primary)]
                    status, digest = "not_requested", None
                    inline_verified = 0
                    if settings.get("hydrate_documents", True):
                        try:
                            raw = _cached(
                                fetch, doc_url, doc_path, force_refresh,
                                cache_root=cache, sealed_lookup=sealed_lookup,
                            )
                            cache_records.append(_cache_manifest_record(cache, doc_path, raw))
                            digest, status = hashlib.sha256(raw).hexdigest(), "hydrated"
                            if _contains_inline_xbrl_markup(raw):
                                inline_verified = 1
                            if _contains_financial_inline_xbrl_markup(raw):
                                inline_xbrl_accessions.add(accession)
                            issuer_document_count += 1
                        except Exception as exc:  # retain explicit coverage failure
                            status = f"fetch_failed:{type(exc).__name__}"
                            failures.append(
                                {
                                    "ticker": ticker,
                                    "error": (
                                        f"filing_document:{accession}:"
                                        f"{type(exc).__name__}: {exc}"
                                    ),
                                }
                            )
                    conn.execute(
                        """INSERT INTO fact_sec_filing_document(accession_number,ticker,form_type,accepted_at,primary_document,source_url,content_sha256,cache_path,hydration_status,source_id,updated_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(accession_number,source_id) DO UPDATE SET content_sha256=excluded.content_sha256,cache_path=excluded.cache_path,hydration_status=excluded.hydration_status,updated_at=excluded.updated_at""",
                        (accession, ticker, str(row["form"]), _normalize_accepted(row["acceptanceDateTime"]), primary, doc_url, digest, str(doc_path), status, SEC_INLINE, utc_now()),
                    )
                    conn.execute(
                        """INSERT INTO bridge_sec_filing_document_company(
                               accession_number,issuer_company_id,source_id,issuer_ticker,
                               issuer_cik,accepted_at,primary_document,source_url,
                               content_sha256,cache_path,hydration_status,
                               inline_xbrl_verified,updated_at
                           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(accession_number,issuer_company_id,source_id) DO UPDATE SET
                               issuer_ticker=excluded.issuer_ticker,issuer_cik=excluded.issuer_cik,
                               accepted_at=excluded.accepted_at,
                               primary_document=excluded.primary_document,
                               source_url=excluded.source_url,
                               content_sha256=excluded.content_sha256,
                               cache_path=excluded.cache_path,
                               hydration_status=excluded.hydration_status,
                               inline_xbrl_verified=excluded.inline_xbrl_verified,
                               updated_at=excluded.updated_at""",
                        (accession, company_id, SEC_INLINE, ticker, cik10, _normalize_accepted(row["acceptanceDateTime"]), primary, doc_url, digest, str(doc_path), status, inline_verified, utc_now()),
                    )
                latest_filing, annual = _reporting_profile_anchor(
                    associated_rows,
                    cutoff=cutoff,
                    companyfacts_xbrl_accessions=companyfacts_xbrl_accessions,
                    inline_xbrl_accessions=inline_xbrl_accessions,
                )
                lag = None
                if latest_filing and latest_fact:
                    lag = (datetime.fromisoformat(latest_filing[:10]) - datetime.fromisoformat(latest_fact[:10])).days
                fallback = int(bool(annual in {"20-F", "40-F"} and (lag is None or lag > int(settings.get("companyfacts_lag_days", 120)))))
                status = (
                    "inline_fallback_required"
                    if fallback
                    else ("covered" if issuer_fact_count else "filings_only")
                )
                conn.execute(
                    """INSERT INTO dim_issuer_reporting_profile(ticker,cik,primary_annual_form,foreign_issuer_flag,us_gaap_flag,ifrs_flag,latest_filing_accepted_at,latest_companyfacts_accepted_at,companyfacts_lag_days,inline_xbrl_fallback_required,coverage_status,review_reason,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(ticker) DO UPDATE SET primary_annual_form=excluded.primary_annual_form,foreign_issuer_flag=excluded.foreign_issuer_flag,us_gaap_flag=excluded.us_gaap_flag,ifrs_flag=excluded.ifrs_flag,latest_filing_accepted_at=excluded.latest_filing_accepted_at,latest_companyfacts_accepted_at=excluded.latest_companyfacts_accepted_at,companyfacts_lag_days=excluded.companyfacts_lag_days,inline_xbrl_fallback_required=excluded.inline_xbrl_fallback_required,coverage_status=excluded.coverage_status,review_reason=excluded.review_reason,updated_at=excluded.updated_at
                           WHERE COALESCE(excluded.latest_filing_accepted_at,'') >= COALESCE(dim_issuer_reporting_profile.latest_filing_accepted_at,'')
                              OR (COALESCE(excluded.latest_companyfacts_accepted_at,'') <> ''
                                  AND COALESCE(excluded.latest_companyfacts_accepted_at,'') = COALESCE(dim_issuer_reporting_profile.latest_companyfacts_accepted_at,'')
                                  AND ? >= COALESCE(dim_issuer_reporting_profile.latest_filing_accepted_at,''))""",
                    (ticker, cik, annual or None, int(annual in {"20-F", "40-F"}), int("us-gaap" in taxonomies), int("ifrs-full" in taxonomies), latest_filing or None, latest_fact or None, lag, fallback, status, "inline_xbrl_required" if fallback else None, utc_now(), cutoff),
                )
                for accession in shared_accessions_to_reconcile:
                    _reconcile_filing_accession(conn, accession)
                    _reconcile_reporting_profiles_for_accession(
                        conn, accession,
                        companyfacts_lag_days=int(
                            settings.get("companyfacts_lag_days", 120)
                        ),
                    )
                if conn.total_changes > mutation_changes_before:
                    _advance_sec_ingestion_watermark(
                        conn,asof_date=asof_date,
                        mutation_kind=(
                            'targeted_financial_projection'
                            if tickers is not None
                            else 'full_financial_projection'
                        ),
                    )
            fact_count += issuer_fact_count
            document_count += issuer_document_count
        except Exception as exc:
            failures.append({"ticker": ticker, "error": f"{type(exc).__name__}: {exc}"})
    scope = [str(row[0]) for row in _issuer_rows(conn, tickers)]
    if scope:
        placeholders = ",".join("?" for _ in scope)
        stored_unique = int(conn.execute(
            f"""SELECT COUNT(DISTINCT accession_number)
                FROM bridge_sec_filing_company
                WHERE issuer_ticker IN ({placeholders}) AND accepted_at<=?""",
            [*scope, cutoff],
        ).fetchone()[0])
    else:
        stored_unique = 0
    cache_manifest = _cache_manifest_summary(cache_records)
    full_scope = tickers is None and len(scope) == len(_issuer_rows(conn, None))
    retired_associations = 0
    if full_scope and not failures:
        with conn:
            retired_associations = _retire_absent_associations(
                conn, cutoff=cutoff, scope=scope,
                parsed_keys=set(parsed_associations),
            )
            if retired_associations:
                _advance_sec_ingestion_watermark(
                    conn,asof_date=asof_date,
                    mutation_kind='full_association_reconciliation',
                )
    association_manifest = _association_manifest(conn, cutoff=cutoff, tickers=scope)
    parsed_association_manifest = _association_manifest_payload(
        (
            parsed_associations[key]
            for key in sorted(parsed_associations, key=lambda item: (item[0], item[1]))
        )
    )
    manifests_match = parsed_association_manifest == association_manifest
    if full_scope and not failures and not manifests_match:
        failures.append({
            "ticker": "*",
            "error": "parsed_submissions_associations_do_not_match_database_bridge",
        })
    if full_scope and not failures and manifests_match:
        if sealed_lookup is not None:
            prior_seal = conn.execute(
                '''SELECT cache_manifest_json,cache_manifest_sha256
                  FROM consumer_defensive_sec_cache_snapshot
                   WHERE asof_date=?''', (cutoff[:10],)
            ).fetchone()
            if prior_seal is None:
                raise RuntimeError('Exact SEC seal disappeared during replay')
            projected = _sealed_manifest_projection(cache_manifest['entries'])
            prior_entries = json.loads(str(prior_seal[0]))
            if (
                projected['entries'] != prior_entries
                or str(projected['sha256']) != str(prior_seal[1])
            ):
                raise RuntimeError(
                    f'Immutable SEC cache snapshot conflict for {cutoff[:10]}'
                )
            sealed_root = cache / 'sealed' / cutoff[:10]
            sealed_manifest = {
                'sha256': str(prior_seal[1]),
                'entries': prior_entries,
            }
        else:
            sealed_root, sealed_manifest = _seal_cache_manifest(
                cache, cutoff[:10], cache_manifest['entries']
            )
        with conn:
            manifest_json = json.dumps(
                sealed_manifest['entries'], sort_keys=True, separators=(',', ':')
            )
            existing_snapshot = conn.execute(
                '''SELECT cache_manifest_sha256,cache_manifest_json,
                          scope_contract_version,trust_state
                   FROM consumer_defensive_sec_cache_snapshot WHERE asof_date=?''',
                (cutoff[:10],),
            ).fetchone()
            if existing_snapshot and (
                str(existing_snapshot[0]) != str(sealed_manifest['sha256'])
                or str(existing_snapshot[1]) != manifest_json
            ):
                raise RuntimeError(
                    f'Immutable SEC cache snapshot conflict for {cutoff[:10]}'
                )
            conn.execute(
                '''INSERT INTO consumer_defensive_sec_cache_snapshot(
                       asof_date,seal_relative_path,cache_manifest_sha256,
                       cache_manifest_json,ingestion_config_sha256,
                       issuer_scope_sha256,scope_contract_version,trust_state,
                       quarantine_reason,created_at)
                   VALUES(?,?,?,?,?,?,3,'trusted_current',NULL,?)
                   ON CONFLICT(asof_date) DO UPDATE SET
                       seal_relative_path=excluded.seal_relative_path,
                       cache_manifest_sha256=excluded.cache_manifest_sha256,
                       cache_manifest_json=excluded.cache_manifest_json,
                       ingestion_config_sha256=excluded.ingestion_config_sha256,
                       issuer_scope_sha256=excluded.issuer_scope_sha256,
                       scope_contract_version=3,trust_state='trusted_current',
                       quarantine_reason=NULL,created_at=excluded.created_at''',
                (cutoff[:10], f'sealed/{cutoff[:10]}',
                 sealed_manifest['sha256'], manifest_json,
                 ingestion_config_sha256,issuer_scope_sha256,utc_now()),
            )
            conn.execute(
                '''INSERT INTO consumer_defensive_sec_reconciliation_state(
                       asof_date,cutoff,scope_issuer_count,association_count,
                       accession_count,shared_accession_count,association_sha256,
                       ingestion_config_sha256,issuer_scope_sha256,
                       cache_manifest_sha256,cache_manifest_json,cache_root,
                       status,completed_at,scope_contract_version,trust_state,
                       quarantine_reason
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?, 'complete',?,3,
                            'trusted_current',NULL)
                   ON CONFLICT(asof_date) DO UPDATE SET
                       cutoff=excluded.cutoff,scope_issuer_count=excluded.scope_issuer_count,
                       association_count=excluded.association_count,
                       accession_count=excluded.accession_count,
                       shared_accession_count=excluded.shared_accession_count,
                       association_sha256=excluded.association_sha256,
                       ingestion_config_sha256=excluded.ingestion_config_sha256,
                       issuer_scope_sha256=excluded.issuer_scope_sha256,
                       cache_manifest_sha256=excluded.cache_manifest_sha256,
                       cache_manifest_json=excluded.cache_manifest_json,
                       cache_root=excluded.cache_root,
                       status='complete',completed_at=excluded.completed_at,
                       scope_contract_version=3,trust_state='trusted_current',
                       quarantine_reason=NULL''',
                (
                    cutoff[:10], cutoff, len(scope),
                    association_manifest['association_count'],
                    association_manifest['accession_count'],
                    association_manifest['shared_accession_count'],
                    association_manifest['association_sha256'],
                    ingestion_config_sha256,issuer_scope_sha256,
                    sealed_manifest['sha256'],
                    manifest_json,
                    str(resolve_filesystem_path(sealed_root, strict=True)), utc_now(),
                ),
            )
            _advance_sec_ingestion_watermark(
                conn,asof_date=asof_date,
                mutation_kind='full_reconciliation_sealed',
            )
    return {
        "issuers": len(scope),
        "filings_processed": filing_count,
        "filings_stored_unique": stored_unique,
        "raw_facts": fact_count,
        "documents": document_count,
        "retired_associations": retired_associations,
        "cache_manifest": cache_manifest,
        "association_manifest": association_manifest,
        "parsed_association_manifest": parsed_association_manifest,
        "full_scope_reconciled": bool(
            full_scope and not failures and manifests_match
        ),
        "failures": failures,
    }


def _refresh_reporting_profile_from_database(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    cutoff: str,
    companyfacts_lag_days: int,
) -> None:
    rows = conn.execute(
        '''SELECT b.accession_number,b.form_type,f.accepted_at
           FROM bridge_sec_filing_company b
           JOIN fact_sec_filing f ON f.accession_number=b.accession_number
           WHERE b.issuer_ticker=? AND f.accepted_at<=?
             AND COALESCE((SELECT e.event_type
                 FROM sec_filing_company_association_event e
                 WHERE e.accession_number=b.accession_number
                   AND e.issuer_company_id=b.issuer_company_id
                   AND e.effective_asof<=?
                 ORDER BY e.effective_asof DESC,e.event_id DESC LIMIT 1),
                 CASE WHEN b.association_status='active' THEN 'observed' ELSE 'retired' END)
                 IN ('observed','reactivated')''',
        (ticker, cutoff, cutoff),
    ).fetchall()
    eligible: list[tuple[str, str]] = []
    for row in rows:
        accession = str(row['accession_number'])
        form = str(row['form_type'] or '').upper()
        accepted = str(row['accepted_at'] or '')
        has_facts = bool(conn.execute(
            '''SELECT 1 FROM fact_sec_xbrl_fact_raw
               WHERE ticker=? AND accession_number=? AND accepted_at<=? LIMIT 1''',
            (ticker, accession, cutoff),
        ).fetchone())
        if form in PROFILE_FINANCIAL_FORMS or (
            form in PROFILE_CONDITIONAL_XBRL_FORMS and has_facts
        ):
            eligible.append((accepted, form))
    eligible.sort(reverse=True)
    latest_filing = eligible[0][0] if eligible else ''
    annual = next((
        _canonical_financial_form(form) for _, form in eligible
        if _canonical_financial_form(form) in PROFILE_ANNUAL_FORMS
    ), '')
    companyfacts = str(conn.execute(
        '''SELECT MAX(accepted_at) FROM fact_sec_xbrl_fact_raw
           WHERE ticker=? AND source_id=? AND accepted_at<=?''',
        (ticker, SEC_COMPANYFACTS, cutoff),
    ).fetchone()[0] or '')
    fallback_row = conn.execute(
        '''SELECT accepted_at,document_sha256,parser_version
           FROM fact_sec_inline_xbrl_fallback_run
           WHERE ticker=? AND asof_date=? AND status='covered'
           ORDER BY accepted_at DESC,accession_number DESC LIMIT 1''',
        (ticker, cutoff[:10]),
    ).fetchone()
    fallback_accepted = str(fallback_row[0] or '') if fallback_row else ''
    latest_fact = max(companyfacts, fallback_accepted)
    companyfacts_lag = None
    if latest_filing and companyfacts:
        companyfacts_lag = (
            date.fromisoformat(latest_filing[:10])
            - date.fromisoformat(companyfacts[:10])
        ).days
    effective_lag = None
    if latest_filing and latest_fact:
        effective_lag = (
            date.fromisoformat(latest_filing[:10])
            - date.fromisoformat(latest_fact[:10])
        ).days
    required = int(
        annual in {'20-F','40-F'}
        and (effective_lag is None or effective_lag > companyfacts_lag_days)
    )
    covered_by_fallback = bool(
        fallback_row and fallback_accepted and fallback_accepted >= latest_filing
    )
    conn.execute(
        '''UPDATE dim_issuer_reporting_profile SET
               primary_annual_form=?,latest_filing_accepted_at=?,
               latest_companyfacts_accepted_at=?,companyfacts_lag_days=?,
               latest_fallback_accepted_at=?,fallback_document_sha256=?,
               fallback_parser_version=?,inline_xbrl_fallback_required=?,
               coverage_status=?,review_reason=?,updated_at=? WHERE ticker=?''',
        (
            annual or None,latest_filing or None,companyfacts or None,
            companyfacts_lag,fallback_accepted or None,
            str(fallback_row[1]) if fallback_row else None,
            str(fallback_row[2]) if fallback_row else None,required,
            'inline_fallback_required' if required else (
                'covered' if latest_fact else 'filings_only'
            ),
            'inline_xbrl_required' if required else (
                'inline_xbrl_fallback_covered' if covered_by_fallback else None
            ),
            utc_now(),ticker,
        ),
    )


def sync_inline_xbrl_fallback(
    conn: sqlite3.Connection,
    bundle: ConfigBundle,
    *,
    as_of: str | None = None,
) -> dict[str, Any]:
    """Extract model-mapped numeric facts from exact sealed Stage 4 documents."""
    asof_date = (as_of or date.today().isoformat())[:10]
    cutoff = asof_date + 'T23:59:59Z'
    settings = cfg_get(bundle.payload, 'sec_fundamentals')
    cache_root = resolve_path(settings['cache_dir'], base_dir=bundle.base_dir)
    all_issuers = _issuer_rows(conn, None)
    _cache_only_sec_preflight(
        conn, asof_date=asof_date,
        ingestion_config_sha256=_sec_ingestion_config_sha256(settings),
        issuer_scope_sha256=_issuer_scope_sha256(all_issuers),
        scope_issuer_count=len(all_issuers),
    )
    sealed_lookup = _sealed_cache_lookup(conn, cache_root, asof_date)
    concept_map = _read_yaml(resolve_path(
        cfg_get(bundle.payload, 'financial_features.concept_map'),
        base_dir=bundle.base_dir,
    ))
    mapped_concepts = set(_concept_index(concept_map))
    supported_currencies = {
        'USD', *(str(value).upper() for value in cfg_get(
            bundle.payload, 'fx_rates.supported_currencies', []
        )),
    }
    targets = conn.execute(
        '''SELECT p.ticker,p.latest_filing_accepted_at,b.issuer_company_id,
                  b.issuer_cik,b.accession_number,b.form_type,b.filing_date,
                  d.primary_document,d.content_sha256,d.hydration_status
           FROM dim_issuer_reporting_profile p
           JOIN bridge_sec_filing_company b ON b.issuer_ticker=p.ticker
           JOIN fact_sec_filing f ON f.accession_number=b.accession_number
           JOIN bridge_sec_filing_document_company d
             ON d.accession_number=b.accession_number
            AND d.issuer_company_id=b.issuer_company_id
            AND d.source_id=?
           WHERE (p.inline_xbrl_fallback_required=1 OR EXISTS(
                 SELECT 1 FROM fact_sec_inline_xbrl_fallback_run r
                 WHERE r.ticker=p.ticker AND r.asof_date=?))
             AND f.accepted_at=p.latest_filing_accepted_at
             AND f.accepted_at<=? ORDER BY p.ticker,b.accession_number''',
        (SEC_INLINE, asof_date, cutoff),
    ).fetchall()
    by_ticker: dict[str, sqlite3.Row] = {}
    for row in targets:
        ticker = str(row['ticker'])
        if ticker in by_ticker:
            raise RuntimeError(f'Ambiguous inline-XBRL fallback document for {ticker}')
        by_ticker[ticker] = row
    staged: list[dict[str, Any]] = []
    for ticker, row in sorted(by_ticker.items()):
        if str(row['hydration_status']) != 'hydrated':
            raise RuntimeError(f'Inline-XBRL fallback document is not hydrated: {ticker}')
        primary = validate_sec_relative_document_path(
            row['primary_document'], allowed_suffixes=SEC_DOCUMENT_SUFFIXES,
            context=f'inline-XBRL fallback document for {ticker}',
        )
        logical = (
            f"filings/{str(row['issuer_cik']).zfill(10)}/"
            f"{str(row['accession_number'])}/{primary}"
        )
        sealed_path = sealed_lookup.get(logical)
        if sealed_path is None:
            raise RuntimeError(f'Exact sealed fallback document is missing: {logical}')
        raw = read_bytes(sealed_path)
        digest = hashlib.sha256(raw).hexdigest()
        if digest != str(row['content_sha256'] or ''):
            raise RuntimeError(f'Fallback document hash mismatch: {ticker}')
        parsed = parse_inline_xbrl(raw)
        consolidated = [fact for fact in parsed.facts if fact.dimensions_json == '[]']
        mapped = [
            fact for fact in consolidated
            if fact.concept in mapped_concepts
            and str(fact.unit or '').upper() in supported_currencies
        ]
        status = (
            'covered' if mapped else (
                'nonfinancial_inline_xbrl' if not parsed.facts
                else 'insufficient_mapped_facts'
            )
        )
        staged.append({
            'row': row, 'digest': digest, 'parsed': parsed,
            'facts': consolidated, 'mapped_count': len(mapped), 'status': status,
        })
    changed_tickers: set[str] = set()
    with conn:
        for item in staged:
            row = item['row']
            ticker = str(row['ticker'])
            accession = str(row['accession_number'])
            accepted = str(row['latest_filing_accepted_at'])
            created_at = utc_now()
            fact_rows = []
            for fact in item['facts']:
                semantic = (
                    ticker, str(row['issuer_cik']), accession, fact.taxonomy,
                    fact.concept, fact.value_text, fact.numeric_value, fact.unit,
                    fact.period_start, fact.period_end, str(row['filing_date'] or ''),
                    accepted, str(row['form_type'] or ''),
                    f'inline:{fact.context_id}', fact.dimensions_json, SEC_INLINE,
                    f'inline_xbrl:{INLINE_PARSER_VERSION}:{item["digest"]}:{fact.context_id}',
                )
                fact_rows.append((*semantic, _source_observation_id(semantic), created_at))
            existing = conn.execute(
                '''SELECT ticker,cik,accession_number,taxonomy,concept,value_text,
                          numeric_value,unit,period_start,period_end,filed_date,
                          accepted_at,form_type,frame,dimensions_json,source_id,
                          source_detail,source_observation_id
                   FROM fact_sec_xbrl_fact_raw
                   WHERE ticker=? AND accession_number=? AND source_id=?''',
                (ticker, accession, SEC_INLINE),
            ).fetchall()
            semantics = [values[:-1] for values in fact_rows]
            if _raw_fact_semantics(existing) != _raw_fact_semantics(semantics):
                conn.execute(
                    '''DELETE FROM fact_financial_statement_canonical
                       WHERE accession_number=? AND source_raw_fact_id IN (
                           SELECT raw_fact_id FROM fact_sec_xbrl_fact_raw
                           WHERE ticker=? AND accession_number=? AND source_id=?)''',
                    (accession, ticker, accession, SEC_INLINE),
                )
                conn.execute(
                    '''DELETE FROM fact_sec_xbrl_fact_raw
                       WHERE ticker=? AND accession_number=? AND source_id=?''',
                    (ticker, accession, SEC_INLINE),
                )
                if fact_rows:
                    conn.executemany(
                        '''INSERT INTO fact_sec_xbrl_fact_raw(
                               ticker,cik,accession_number,taxonomy,concept,value_text,
                               numeric_value,unit,period_start,period_end,filed_date,
                               accepted_at,form_type,frame,dimensions_json,source_id,
                               source_detail,source_observation_id,created_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                        fact_rows,
                    )
                conn.execute(
                    '''DELETE FROM feature_financial_statement
                       WHERE model_family=? AND ticker=? AND asof_date>=?''',
                    (MODEL_FAMILY, ticker, accepted[:10]),
                )
                changed_tickers.add(ticker)
            parsed = item['parsed']
            conn.execute(
                '''INSERT INTO fact_sec_inline_xbrl_fallback_run(
                       asof_date,ticker,accession_number,accepted_at,
                       document_sha256,parser_version,status,numeric_fact_count,
                       consolidated_fact_count,mapped_fact_count,context_count,
                       unit_count,skipped_fact_count,
                       unsupported_transformations_json,source_id,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(asof_date,ticker,accession_number,parser_version)
                   DO UPDATE SET document_sha256=excluded.document_sha256,
                       accepted_at=excluded.accepted_at,status=excluded.status,
                       numeric_fact_count=excluded.numeric_fact_count,
                       consolidated_fact_count=excluded.consolidated_fact_count,
                       mapped_fact_count=excluded.mapped_fact_count,
                       context_count=excluded.context_count,unit_count=excluded.unit_count,
                       skipped_fact_count=excluded.skipped_fact_count,
                       unsupported_transformations_json=excluded.unsupported_transformations_json,
                       updated_at=excluded.updated_at''',
                (
                    asof_date, ticker, accession, accepted, item['digest'],
                    INLINE_PARSER_VERSION, item['status'], len(parsed.facts),
                    len(item['facts']), item['mapped_count'], parsed.contexts,
                    parsed.units, parsed.skipped_facts,
                    json.dumps(parsed.unsupported_transformations), SEC_INLINE,
                    created_at, created_at,
                ),
            )
        for ticker in sorted(by_ticker):
            _refresh_reporting_profile_from_database(
                conn, ticker=ticker, cutoff=cutoff,
                companyfacts_lag_days=int(settings.get('companyfacts_lag_days', 120)),
            )
    audit_rows = conn.execute(
        '''SELECT ticker,accession_number,status,numeric_fact_count,
                  consolidated_fact_count,mapped_fact_count,document_sha256
           FROM fact_sec_inline_xbrl_fallback_run
           WHERE asof_date=? AND parser_version=?
           ORDER BY ticker,accession_number''',
        (asof_date, INLINE_PARSER_VERSION),
    ).fetchall()
    return {
        'as_of': asof_date,
        'parser_version': INLINE_PARSER_VERSION,
        'targets': len(audit_rows),
        'replayed_targets': len(staged),
        'covered': sum(str(row['status']) == 'covered' for row in audit_rows),
        'nonfinancial_inline_xbrl': sum(
            str(row['status']) == 'nonfinancial_inline_xbrl' for row in audit_rows
        ),
        'insufficient_mapped_facts': sum(
            str(row['status']) == 'insufficient_mapped_facts' for row in audit_rows
        ),
        'numeric_facts': sum(int(row['numeric_fact_count']) for row in audit_rows),
        'consolidated_facts': sum(
            int(row['consolidated_fact_count']) for row in audit_rows
        ),
        'mapped_facts': sum(int(row['mapped_fact_count']) for row in audit_rows),
        'changed_tickers': sorted(changed_tickers),
        'results': [
            {
                'ticker': str(row['ticker']),
                'accession_number': str(row['accession_number']),
                'status': str(row['status']),
                'numeric_facts': int(row['numeric_fact_count']),
                'consolidated_facts': int(row['consolidated_fact_count']),
                'mapped_facts': int(row['mapped_fact_count']),
                'document_sha256': str(row['document_sha256']),
            }
            for row in audit_rows
        ],
    }


def sync_fx_rates(conn: sqlite3.Connection, bundle: ConfigBundle, *, start: str | None = None, end: str | None = None, force_refresh: bool = False, fetch: Fetcher | None = None) -> dict[str, Any]:
    settings = cfg_get(bundle.payload, "fx_rates")
    fetch = fetch or http_fetcher(_http_policy(bundle.payload, "fx_rates"))
    cache = resolve_path(settings["cache_dir"], base_dir=bundle.base_dir)
    start_date, end_date = date.fromisoformat(start or settings["start_date"]), date.fromisoformat(end or date.today().isoformat())
    if start_date > end_date:
        raise ValueError(
            f"Invalid FX date window: start {start_date.isoformat()} is after end {end_date.isoformat()}."
        )
    supported = {str(value).upper() for value in settings.get("supported_currencies", [])}
    non_monetary = {str(value).upper() for value in settings.get("non_monetary_three_letter_units", [])}
    units = {
        str(row[0]).upper() for row in conn.execute(
            'SELECT DISTINCT unit FROM fact_sec_xbrl_fact_raw '
            'WHERE unit IS NOT NULL AND accepted_at<=?',
            (end_date.isoformat() + 'T23:59:59Z',),
        )
    }
    overlap = sorted(supported & non_monetary)
    if overlap:
        raise ValueError(f"FX currencies and non-monetary units overlap: {overlap}")
    currencies = sorted((units & supported) - {"USD"})
    ignored_non_monetary_units = sorted(units & non_monetary)
    unknown_three_letter_units = sorted(
        unit
        for unit in units
        if MONETARY_UNITS.fullmatch(unit)
        and unit not in supported
        and unit not in non_monetary
        and unit != "USD"
    )
    rows_written, quarantined_rows, failures = 0, 0, []
    cache_records: list[dict[str, Any]] = []
    outlier_window = int(settings.get("outlier_window", 21))
    configured_start = date.fromisoformat(str(settings["start_date"]))
    fetch_start = max(
        configured_start,
        start_date - timedelta(days=max(outlier_window * 3, 7)),
    )
    exemptions = tuple(
        RedenominationExemption(
            currency=str(row["currency"]),
            start_date=str(row["start_date"]),
            end_date=str(row["end_date"]),
            reason=str(row["reason"]),
        )
        for row in settings.get("redenomination_exemptions", [])
    )
    cache_only = os.environ.get(
        'CONSUMER_DEFENSIVE_CACHE_ONLY', ''
    ).strip().casefold() in {'1', 'true', 'yes', 'on'}
    for currency in currencies:
        symbol = f"{currency}USD=X"
        p1 = int(datetime.combine(fetch_start, datetime.min.time(), tzinfo=timezone.utc).timestamp())
        p2 = int(datetime.combine(end_date + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc).timestamp())
        url = settings["chart_url_template"].format(symbol=symbol, period1=p1, period2=p2)
        try:
            payload_path = cache / (
                symbol.replace('=', '_') + '_' + fetch_start.isoformat()
                + '_' + end_date.isoformat() + '.json'
            )
            legacy_path = _safe_cache_write_target(
                cache, cache / f'{symbol}.json', context='FX legacy cache alias'
            )
            legacy_payload: tuple[bytes, dict[str, Any]] | None = None
            if (
                not force_refresh
                and not path_exists(payload_path)
                and path_exists(legacy_path)
            ):
                try:
                    legacy_raw = read_bytes(legacy_path)
                    legacy_json = json.loads(legacy_raw)
                    _validate_fx_chart_payload(
                        legacy_json, expected_symbol=symbol,
                        start_date=fetch_start, end_date=end_date,
                    )
                    legacy_payload = (legacy_raw, legacy_json)
                    payload_path = legacy_path
                except (OSError, json.JSONDecodeError, TypeError, ValueError):
                    if cache_only:
                        raise
            if legacy_payload is None:
                payload_raw, payload = _validated_mutable_json(
                    fetch, url, payload_path,
                    validate=lambda candidate: _validate_fx_chart_payload(
                        candidate, expected_symbol=symbol,
                        start_date=fetch_start, end_date=end_date,
                    ),
                    force=force_refresh, reuse_valid_cache=True,
                )
            else:
                payload_raw, payload = legacy_payload
            cache_records.append(_cache_manifest_record(cache, payload_path, payload_raw))
            observations = _validate_fx_chart_payload(
                payload, expected_symbol=symbol,
                start_date=fetch_start, end_date=end_date,
            )
            decisions = classify_fx_daily_rates(
                observations,
                window=outlier_window,
                minimum_history=int(settings.get("outlier_minimum_history", 5)),
                robust_z_threshold=float(settings.get("outlier_robust_z_threshold", 8.0)),
                relative_deviation_threshold=float(settings.get("outlier_relative_deviation_threshold", 0.35)),
                exemptions=exemptions,
            )
            with conn:
                conn.execute(
                    """DELETE FROM fact_fx_rate WHERE base_currency=? AND quote_currency='USD'
                       AND source_id=? AND rate_date BETWEEN ? AND ?""",
                    (currency, FX_SOURCE, start_date.isoformat(), end_date.isoformat()),
                )
                for decision in decisions:
                    if str(decision.observation.rate_date) < start_date.isoformat():
                        continue
                    quality_status = "usable" if decision.is_usable else "quarantined"
                    quality_reason = json.dumps(
                        {
                            "classifier_status": decision.status,
                            "reason": decision.reason,
                            "local_median": decision.local_median,
                            "local_mad": decision.local_mad,
                            "robust_z": decision.robust_z,
                            "relative_deviation": decision.relative_deviation,
                        },
                        sort_keys=True,
                    )
                    conn.execute(
                        """INSERT INTO fact_fx_rate(
                               base_currency,quote_currency,rate_date,source_id,rate,raw_rate,
                               quality_status,quality_reason,created_at
                           ) VALUES(?,?,?,?,?,?,?,?,?)""",
                        (
                            currency,
                            "USD",
                            str(decision.observation.rate_date),
                            FX_SOURCE,
                            float(decision.observation.rate),
                            float(decision.observation.rate),
                            quality_status,
                            quality_reason,
                            utc_now(),
                        ),
                    )
                    rows_written += 1
                    quarantined_rows += int(not decision.is_usable)
        except Exception as exc:
            failures.append({"currency": currency, "error": f"{type(exc).__name__}: {exc}"})
    return {
        "currencies": currencies,
        "ignored_non_monetary_units": ignored_non_monetary_units,
        "unknown_three_letter_units": unknown_three_letter_units,
        "rows_written": rows_written,
        "quarantined_rows": quarantined_rows,
        "cache_manifest": _cache_manifest_summary(cache_records),
        "failures": failures,
    }


def _fx_rate(conn: sqlite3.Connection, currency: str, period_start: str | None, period_end: str, flow: bool) -> float | None:
    if currency == "USD":
        return 1.0
    if flow and period_start:
        row = conn.execute("SELECT AVG(rate) FROM fact_fx_rate WHERE base_currency=? AND quote_currency='USD' AND quality_status='usable' AND rate_date BETWEEN ? AND ?", (currency, period_start, period_end)).fetchone()
    else:
        row = conn.execute("SELECT rate FROM fact_fx_rate WHERE base_currency=? AND quote_currency='USD' AND quality_status='usable' AND rate_date<=? ORDER BY rate_date DESC LIMIT 1", (currency, period_end)).fetchone()
    return float(row[0]) if row and row[0] is not None else None


def _concept_index(concept_map: dict[str, Any]) -> dict[str, tuple[str, str, str, int]]:
    """Map one raw concept to a metric, additive component, and priority."""
    result: dict[str, tuple[str, str, str, int]] = {}
    for metric, spec in concept_map["metrics"].items():
        statement_type = str(spec["statement_type"])
        component_specs = spec.get("components")
        if component_specs:
            groups = component_specs.items()
        else:
            groups = (("total", {"concepts": spec["concepts"]}),)
        for component, component_spec in groups:
            for priority, concept in enumerate(component_spec["concepts"]):
                concept = str(concept)
                mapping = (str(metric), statement_type, str(component), priority)
                previous = result.get(concept)
                if previous is not None and previous[:3] != mapping[:3]:
                    raise ValueError(
                        f"Financial concept {concept!r} is mapped to multiple components: "
                        f"{previous[:3]} and {mapping[:3]}."
                    )
                result[concept] = mapping
    return result


def build_financial_features(conn: sqlite3.Connection, bundle: ConfigBundle, *, as_of: str | None = None) -> dict[str, Any]:
    with conn:
        _backfill_source_observation_ids(conn)
    settings = cfg_get(bundle.payload, "financial_features")
    concept_map = _read_yaml(resolve_path(settings["concept_map"], base_dir=bundle.base_dir))
    definition_version = str(concept_map["definition_version"])
    concept_index = _concept_index(concept_map)
    cutoff = (as_of or date.today().isoformat()) + "T23:59:59Z"
    supported_currencies = {"USD", *(str(value).upper() for value in cfg_get(bundle.payload, "fx_rates.supported_currencies", []))}
    raw = conn.execute(
        """SELECT r.raw_fact_id,r.source_observation_id,r.ticker,
                  r.accession_number,r.taxonomy,r.concept,
                  r.numeric_value,r.unit,r.period_start,r.period_end,r.accepted_at
           FROM fact_sec_xbrl_fact_raw r
           JOIN bridge_sec_filing_company b
             ON b.accession_number=r.accession_number
            AND b.issuer_ticker=r.ticker
           WHERE r.numeric_value IS NOT NULL AND r.accepted_at<=?
             AND COALESCE((SELECT e.event_type
                 FROM sec_filing_company_association_event e
                 WHERE e.accession_number=b.accession_number
                   AND e.issuer_company_id=b.issuer_company_id
                   AND e.effective_asof<=?
                 ORDER BY e.effective_asof DESC,e.event_id DESC LIMIT 1),
                 CASE WHEN b.association_status='active' THEN 'observed' ELSE 'retired' END)
                 IN ('observed','reactivated')""",
        (cutoff, cutoff),
    ).fetchall()
    selection = select_canonical_financial_facts(
        raw,
        concept_index=concept_index,
        supported_currencies=supported_currencies,
    )
    canonical_written = 0
    with conn:
        conn.execute(
            """DELETE FROM fact_financial_statement_canonical
               WHERE source_id=? AND accepted_at<=?""",
            (CANONICAL_SOURCE, cutoff),
        )
        for decision in selection.decisions:
            flow = decision.statement_type != "balance_sheet"
            rate = _fx_rate(
                conn,
                decision.reported_currency,
                decision.period_start,
                decision.period_end,
                flow,
            )
            value_usd = decision.normalized_value * rate if rate is not None else None
            conn.execute(
                """INSERT INTO fact_financial_statement_canonical(
                       ticker,canonical_metric,canonical_component,accession_number,taxonomy,
                       source_concept,statement_type,period_start,period_end,accepted_at,
                       frequency,value,reported_value,reported_currency,value_usd,fx_rate,
                       source_raw_fact_id,source_observation_id,source_id,
                       definition_version,quality_status,
                       selection_method,sign_normalization_method,quality_flags_json,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    decision.ticker,
                    decision.metric,
                    decision.component,
                    decision.accession_number,
                    decision.taxonomy,
                    decision.source_concept,
                    decision.statement_type,
                    decision.period_start,
                    decision.period_end,
                    decision.accepted_at,
                    _frequency(decision.period_start, decision.period_end, flow),
                    decision.normalized_value,
                    decision.reported_value,
                    decision.reported_currency,
                    value_usd,
                    rate,
                    decision.raw_fact_id,
                    decision.source_observation_id,
                    CANONICAL_SOURCE,
                    definition_version,
                    "complete" if rate is not None else "fx_missing",
                    decision.selection_method,
                    decision.sign_normalization_method,
                    json.dumps(decision.quality_flags, sort_keys=True),
                    utc_now(),
                ),
            )
            canonical_written += 1
        conn.execute("DELETE FROM feature_financial_statement WHERE model_family=? AND asof_date=?", (MODEL_FAMILY, cutoff[:10]))
        ticker_rows = conn.execute(
            """SELECT t.ticker,s.listing_start_date,s.listing_end_date
               FROM dim_consumer_defensive_taxonomy t
               JOIN dim_security s ON s.security_id=t.security_id
               WHERE t.model_family=? ORDER BY t.ticker""",
            (MODEL_FAMILY,),
        ).fetchall()
        quality_counts: dict[str, int] = defaultdict(int)
        for ticker, listing_start, listing_end in ticker_rows:
            fallback_required = _pit_inline_fallback_required(
                conn, ticker=str(ticker), cutoff=cutoff,
                lag_days=int(cfg_get(
                    bundle.payload, "sec_fundamentals.companyfacts_lag_days", 120
                )),
            )
            facts = conn.execute(
                """SELECT canonical_metric,canonical_component,accession_number,taxonomy,
                          source_concept,period_start,period_end,accepted_at,frequency,value_usd,
                          reported_currency,source_raw_fact_id,quality_flags_json,
                          source_observation_id
                   FROM fact_financial_statement_canonical
                   WHERE ticker=? AND accepted_at<=? AND definition_version=?
                     AND value_usd IS NOT NULL""",
                (ticker, cutoff, definition_version),
            ).fetchall()
            feature = build_financial_feature_bundle(
                facts,
                as_of=cutoff[:10],
                listing_start_date=str(listing_start or "") or None,
                listing_end_date=str(listing_end or "") or None,
                maximum_period_age_days=int(settings.get("maximum_period_age_days", 550)),
                inline_xbrl_fallback_required=bool(fallback_required),
            )
            values = feature.values
            quality_counts[feature.quality_status] += 1
            conn.execute(
                """INSERT INTO feature_financial_statement(
                       model_family,ticker,asof_date,source_id,revenue_ttm_usd,gross_margin,
                       operating_margin,free_cash_flow_margin,return_on_invested_capital,
                       net_debt_to_ebitda,inventory_turnover,basis_period_end,
                       feature_definition_version,lineage_json,financial_quality_status,
                       financial_quality_reason,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    MODEL_FAMILY,
                    ticker,
                    cutoff[:10],
                    CANONICAL_SOURCE,
                    values["revenue_ttm_usd"],
                    values["gross_margin"],
                    values["operating_margin"],
                    values["free_cash_flow_margin"],
                    values["return_on_invested_capital"],
                    values["net_debt_to_ebitda"],
                    values["inventory_turnover"],
                    feature.basis_period_end,
                    feature.feature_definition_version,
                    json.dumps(feature.lineage, sort_keys=True, separators=(",", ":")),
                    feature.quality_status,
                    json.dumps(feature.quality_reasons, sort_keys=True),
                    utc_now(),
                ),
            )
    return {
        "canonical_facts": canonical_written,
        "feature_rows": len(ticker_rows),
        "as_of": cutoff[:10],
        "definition_version": definition_version,
        "feature_definition_version": FEATURE_DEFINITION_VERSION,
        "canonical_selection_audit": dict(selection.audit_counts),
        "feature_quality_counts": dict(sorted(quality_counts.items())),
    }


def _frequency(start: str | None, end: str, flow: bool) -> str:
    if not flow or not start:
        return "instant"
    days = (date.fromisoformat(end) - date.fromisoformat(start)).days
    if 60 <= days <= 150:
        return "quarterly"
    if 151 <= days <= 329:
        return "year_to_date"
    return "annual" if 330 <= days <= 430 else "other"


def _feature_values(rows: Iterable[sqlite3.Row]) -> dict[str, float | None]:
    return legacy_feature_values(list(rows))


TAG_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"\s+")


def _document_text(raw: bytes) -> str:
    text = raw.decode("utf-8", errors="replace")
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", text)
    return SPACE_RE.sub(" ", html.unescape(TAG_RE.sub(" ", text))).strip()


def run_disclosure_census(conn: sqlite3.Connection, bundle: ConfigBundle, *, as_of: str | None = None, tickers: list[str] | None = None) -> dict[str, Any]:
    settings = cfg_get(bundle.payload, "specialized_disclosure_census")
    terms_payload = _read_yaml(resolve_path(settings["terms_path"], base_dir=bundle.base_dir))
    parser_version, terms = str(terms_payload["parser_version"]), terms_payload["metrics"]
    configured_parser_version = str(settings.get("parser_version") or "")
    if parser_version != configured_parser_version:
        raise ValueError(
            "Disclosure parser-version mismatch: "
            f"config={configured_parser_version!r} terms={parser_version!r}"
        )
    registry_rows = conn.execute("SELECT metric_id,cohorts_json,applicability_subtypes_json FROM dim_specialized_metric").fetchall()
    registry = {str(r[0]): (set(json.loads(r[1])), set(json.loads(r[2]))) for r in registry_rows}
    if set(terms) != set(registry):
        raise ValueError(f"Disclosure terms must exactly cover registry metrics; missing={sorted(set(registry)-set(terms))} extra={sorted(set(terms)-set(registry))}")
    cutoff = (as_of or date.today().isoformat()) + "T23:59:59Z"
    tax_query = "SELECT ticker,calibration_cohort_id,applicability_subtype FROM dim_consumer_defensive_taxonomy WHERE model_family=?"
    params: list[Any] = [MODEL_FAMILY]
    if tickers:
        tax_query += f" AND ticker IN ({','.join('?' for _ in tickers)})"
        params.extend(tickers)
    taxonomy = conn.execute(tax_query, params).fetchall()
    if not taxonomy:
        raise ValueError("Disclosure census ticker scope did not match any Consumer Defensive taxonomy rows.")
    taxonomy_tickers = [str(row[0]) for row in taxonomy]
    document_placeholders = ",".join("?" for _ in taxonomy_tickers)
    cache_root = resolve_path(
        cfg_get(bundle.payload, 'sec_fundamentals.cache_dir'),
        base_dir=bundle.base_dir,
    )
    all_issuers = _issuer_rows(conn, None)
    expected_config_sha256 = _sec_ingestion_config_sha256(
        cfg_get(bundle.payload, 'sec_fundamentals')
    )
    expected_scope_sha256 = _issuer_scope_sha256(all_issuers)
    _cache_only_sec_preflight(
        conn, asof_date=cutoff[:10],
        ingestion_config_sha256=expected_config_sha256,
        issuer_scope_sha256=expected_scope_sha256,
        scope_issuer_count=len(all_issuers),
    )
    reconciliation = conn.execute(
        '''SELECT association_count,accession_count,shared_accession_count,
                  association_sha256
           FROM consumer_defensive_sec_reconciliation_state
           WHERE asof_date=? AND status='complete'
             AND scope_contract_version=3 AND trust_state='trusted_current' ''',
        (cutoff[:10],),
    ).fetchone()
    current_manifest = _association_manifest(
        conn, cutoff=cutoff, tickers=[str(row[0]) for row in all_issuers]
    )
    if reconciliation is None or any((
        int(reconciliation[0]) != current_manifest['association_count'],
        int(reconciliation[1]) != current_manifest['accession_count'],
        int(reconciliation[2]) != current_manifest['shared_accession_count'],
        str(reconciliation[3]) != current_manifest['association_sha256'],
    )):
        raise RuntimeError(
            'Disclosure census requires a current exact full-scope SEC '
            'association reconciliation'
        )
    sealed_lookup = _sealed_cache_lookup(conn, cache_root, cutoff[:10])
    documents = conn.execute(
        f"""SELECT d.issuer_ticker,d.accession_number,b.form_type,d.accepted_at,
                   d.cache_path,d.hydration_status,d.issuer_cik,
                   d.primary_document,d.content_sha256
            FROM bridge_sec_filing_document_company AS d
            JOIN bridge_sec_filing_company AS b
              ON b.accession_number=d.accession_number
             AND b.issuer_company_id=d.issuer_company_id
            WHERE d.accepted_at<=?
              AND COALESCE((SELECT e.event_type
                  FROM sec_filing_company_association_event e
                  WHERE e.accession_number=b.accession_number
                    AND e.issuer_company_id=b.issuer_company_id
                    AND e.effective_asof<=?
                  ORDER BY e.effective_asof DESC,e.event_id DESC LIMIT 1),
                  CASE WHEN b.association_status='active' THEN 'observed' ELSE 'retired' END)
                  IN ('observed','reactivated')
              AND d.issuer_ticker IN ({document_placeholders})""",
        [cutoff, cutoff, *taxonomy_tickers],
    ).fetchall()
    by_ticker: dict[str, list[sqlite3.Row]] = defaultdict(list)
    document_texts: dict[tuple[str, str], str] = {}
    document_parse_failures: list[dict[str, str]] = []
    sealed_root = resolve_sec_seal_root(
        cache_root, f'sealed/{cutoff[:10]}', expected_asof=cutoff[:10]
    )
    for row in documents:
        if str(row[5]) != "hydrated":
            continue
        key = (str(row[0]), str(row[1]))
        try:
            relative_document = validate_sec_relative_document_path(
                row[7], allowed_suffixes=SEC_DOCUMENT_SUFFIXES,
                context=f'SEC census document for {key[0]} {key[1]}'
            )
            logical_path = (
                f'filings/{str(row[6]).zfill(10)}/{key[1]}/'
                f'{relative_document}'
            )
            sealed_path = sealed_lookup.get(logical_path)
            if sealed_path is None:
                # Historical bridge rows can outlive the top-N hydration set
                # for this snapshot. They are outside this exact sealed census.
                continue
            sealed_path = resolve_filesystem_path(sealed_path, strict=True)
            try:
                sealed_path.relative_to(sealed_root)
            except ValueError as exc:
                raise RuntimeError('sealed SEC cache object escapes seal root') from exc
            if not is_file_path(sealed_path):
                raise RuntimeError('sealed SEC cache object is not a regular file')
            raw = read_bytes(sealed_path)
            expected_digest = str(row[8] or '')
            actual_digest = hashlib.sha256(raw).hexdigest()
            if expected_digest != actual_digest:
                raise RuntimeError(
                    'sealed document hash differs from the document bridge: '
                    f'expected={expected_digest} actual={actual_digest}'
                )
            document_texts[key] = _document_text(raw)
        except Exception as exc:
            document_parse_failures.append(
                {"ticker": key[0], "accession_number": key[1], "error": f"{type(exc).__name__}: {exc}"}
            )
    if document_parse_failures:
        first = document_parse_failures[0]
        raise RuntimeError(
            'Disclosure census requires exact sealed document bytes; '
            f'failures={len(document_parse_failures)} first={first}'
        )

    for row in documents:
        by_ticker[str(row[0])].append(row)
    detail_rows = summary_rows = 0
    disclosure_counts: dict[str, int] = defaultdict(int)
    with conn:
        for ticker, cohort, subtype in taxonomy:
            ticker, cohort, subtype = str(ticker), str(cohort), str(subtype)
            if not subtype:
                raise ValueError(f"Blank applicability subtype for {ticker}")
            for metric_id, (cohorts, subtypes) in registry.items():
                applicable = cohort in cohorts and ("all_operating_issuers" in subtypes or subtype in subtypes)
                searched = hits = 0
                accepted_hits: list[str] = []
                conn.execute(
                    """DELETE FROM fact_specialized_metric_disclosure_census
                       WHERE ticker=? AND metric_id=? AND parser_version=? AND accepted_at<=?""",
                    (ticker, metric_id, parser_version, cutoff),
                )
                if applicable:
                    for doc in by_ticker.get(ticker, []):
                        body = document_texts.get((ticker, str(doc[1])))
                        if body is None:
                            continue
                        searched += 1
                        matched, evidence = [], []
                        lower = body.casefold()
                        for phrase in terms[metric_id]:
                            phrase_lower = str(phrase).casefold()
                            positions = [m.start() for m in re.finditer(re.escape(phrase_lower), lower)]
                            if positions:
                                matched.append(str(phrase))
                                for pos in positions[:3]:
                                    context = body[max(0, pos - 120): min(len(body), pos + len(phrase_lower) + 120)]
                                    evidence.append({"term": phrase, "context_sha256": hashlib.sha256(context.encode()).hexdigest(), "offset": pos})
                        hit_count = len(evidence)
                        if hit_count:
                            hits += 1
                            accepted_hits.append(str(doc[3] or ""))
                            conn.execute("INSERT OR REPLACE INTO fact_specialized_metric_disclosure_census VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (ticker, doc[1], metric_id, cohort, subtype, doc[3], doc[2], hit_count, json.dumps(matched), json.dumps(evidence), parser_version, DISCLOSURE_SOURCE, utc_now()))
                            detail_rows += 1
                status = "not_applicable" if not applicable else ("applicable_term_hit" if hits else ("parse_unavailable" if not searched else "applicable_no_term_hit"))
                disclosure_counts[status] += 1
                conn.execute("INSERT OR REPLACE INTO fact_specialized_metric_disclosure_summary VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (ticker, metric_id, cohort, subtype, cutoff[:10], "applicable" if applicable else "not_applicable", searched, hits, status, min(accepted_hits) if accepted_hits else None, max(accepted_hits) if accepted_hits else None, parser_version, DISCLOSURE_SOURCE, utc_now()))
                summary_rows += 1
    failed = bool(document_parse_failures or disclosure_counts.get("parse_unavailable", 0))
    return {
        "status": "FAIL" if failed else "PASS",
        "tickers": len(taxonomy),
        "metrics": len(registry),
        "documents_parsed": len(document_texts),
        "document_parse_failures": document_parse_failures,
        "summary_rows": summary_rows,
        "evidence_rows": detail_rows,
        "disclosure_status_counts": dict(sorted(disclosure_counts.items())),
        "parser_version": parser_version,
    }


def _count_stale_lifecycle_disclosure_summaries(
    conn: sqlite3.Connection,
    *,
    asof_date: str,
    parser_version: str,
) -> int:
    """Count stale summaries for the active parser contract, retaining old audits."""

    return int(conn.execute(
        '''SELECT COUNT(*) FROM (
             SELECT s.ticker,s.metric_id,s.parser_version,s.asof_date
             FROM sec_filing_company_association_event e
             CROSS JOIN fact_specialized_metric_disclosure_summary s
             WHERE e.event_type IN ('retired','reactivated')
               AND s.ticker=e.issuer_ticker
               AND s.parser_version=? AND s.source_id=?
               AND s.asof_date<=?
               AND substr(e.effective_asof,1,10)<=s.asof_date
               AND e.created_at>s.updated_at
             GROUP BY s.ticker,s.metric_id,s.parser_version,s.asof_date
           )''',
        (parser_version, DISCLOSURE_SOURCE, asof_date),
    ).fetchone()[0])


def validate_stage4(conn: sqlite3.Connection, bundle: ConfigBundle, *, as_of: str | None = None) -> dict[str, Any]:
    asof_date = (as_of or date.today().isoformat())[:10]
    cutoff = asof_date + "T23:59:59Z"
    filing_bridge_orphans = int(conn.execute(
        """SELECT COUNT(*) FROM fact_sec_filing f
           WHERE NOT EXISTS(SELECT 1 FROM bridge_sec_filing_company b
                            WHERE b.accession_number=f.accession_number)"""
    ).fetchone()[0])
    filing_bridge_identity_mismatches = int(conn.execute(
        """SELECT COUNT(*) FROM bridge_sec_filing_company b
           LEFT JOIN dim_company c ON c.company_id=b.issuer_company_id
           WHERE c.company_id IS NULL OR b.issuer_ticker<>c.primary_ticker
              OR printf('%010d',CAST(b.issuer_cik AS INTEGER))<>
                 printf('%010d',CAST(c.cik AS INTEGER))"""
    ).fetchone()[0])
    filing_bridge_acceptance_missing = int(conn.execute(
        "SELECT COUNT(*) FROM bridge_sec_filing_company WHERE accepted_at IS NULL OR accepted_at=''"
    ).fetchone()[0])
    filing_bridge_acceptance_mismatches = int(conn.execute(
        """SELECT COUNT(*) FROM fact_sec_filing f
           JOIN (SELECT accession_number,MAX(accepted_at) AS max_accepted_at
                 FROM bridge_sec_filing_company GROUP BY accession_number) b
             ON b.accession_number=f.accession_number
           WHERE COALESCE(f.accepted_at,'')<>COALESCE(b.max_accepted_at,'')"""
    ).fetchone()[0])
    filing_bridge_rows = int(conn.execute(
        """SELECT COUNT(*) FROM bridge_sec_filing_company b
           JOIN fact_sec_filing f ON f.accession_number=b.accession_number
           WHERE f.accepted_at<=?""",
        (cutoff,),
    ).fetchone()[0])
    filing_view_rows = int(conn.execute(
        "SELECT COUNT(*) FROM consumer_defensive_sec_parser_filing_input WHERE accepted_at<=?",
        (cutoff,),
    ).fetchone()[0])
    filing_metadata_conflicts = int(conn.execute(
        """SELECT COUNT(*) FROM fact_sec_filing
           WHERE metadata_quality_flags_json LIKE '%association_metadata_conflict%'"""
    ).fetchone()[0])
    filing_owner_leaks = int(conn.execute(
        """SELECT COUNT(*) FROM fact_sec_filing f
           WHERE EXISTS(SELECT 1 FROM bridge_sec_filing_company b
                        WHERE b.accession_number=f.accession_number
                          AND b.relationship_evidence='observed_in_issuer_submissions_feed')
             AND (f.company_id IS NOT NULL OR f.ticker<>'ACCESSION_NEUTRAL'
                  OR f.cik IS NOT NULL OR f.source_url IS NOT NULL)"""
    ).fetchone()[0])
    filing_bridge_bad_urls = _filing_bridge_bad_url_count(conn)
    required_view_columns = {
        'ticker','cik','archive_cik','accession_number','form_type',
        'filing_date','accepted_at','observed_accepted_at','report_date',
        'primary_document','source_id','relationship','issuer_company_id',
        'association_status','retirement_effective_asof',
        'fiscal_year','fiscal_period','company_currency',
    }
    actual_view_columns = {
        str(row[1]) for row in conn.execute(
            'PRAGMA table_info(consumer_defensive_sec_parser_filing_input)'
        )
    }
    foreign_key_violations = sum(
        1 for _row in conn.execute('PRAGMA foreign_key_check')
    )
    canonical_acceptance_mismatches = int(conn.execute(
        '''SELECT COUNT(*) FROM fact_financial_statement_canonical c
           JOIN fact_sec_xbrl_fact_raw r ON r.raw_fact_id=c.source_raw_fact_id
           WHERE c.accepted_at<=? AND c.accepted_at<>r.accepted_at''',
        (cutoff,),
    ).fetchone()[0])
    census_acceptance_mismatches = int(conn.execute(
        '''SELECT COUNT(*) FROM fact_specialized_metric_disclosure_census c
           WHERE c.accepted_at<=? AND NOT EXISTS(
               SELECT 1 FROM bridge_sec_filing_document_company d
               WHERE d.accession_number=c.accession_number
                 AND d.issuer_ticker=c.ticker AND d.accepted_at=c.accepted_at)''',
        (cutoff,),
    ).fetchone()[0])
    reconciliation_scope = [str(row[0]) for row in _issuer_rows(conn, None)]
    sealed_reconciliation = conn.execute(
       '''SELECT * FROM consumer_defensive_sec_reconciliation_state
           WHERE asof_date=? AND status='complete'
             AND scope_contract_version=3 AND trust_state='trusted_current' ''',
        (asof_date,),
    ).fetchone()
    seal_cutoff = (
        str(sealed_reconciliation['cutoff']) if sealed_reconciliation else cutoff
    )
    current_reconciliation = _association_manifest(
        conn, cutoff=seal_cutoff, tickers=reconciliation_scope
    )
    cache_snapshot = conn.execute(
       '''SELECT * FROM consumer_defensive_sec_cache_snapshot
           WHERE asof_date=? AND scope_contract_version=3
             AND trust_state='trusted_current' ''', (asof_date,)
    ).fetchone()
    configured_cache = resolve_path(
        cfg_get(bundle.payload, 'sec_fundamentals.cache_dir'),
        base_dir=bundle.base_dir,
    )
    expected_config_sha256 = _sec_ingestion_config_sha256(
        cfg_get(bundle.payload,'sec_fundamentals')
    )
    expected_scope_sha256 = _issuer_scope_sha256(_issuer_rows(conn,None))
    filing_reconciliation_complete = bool(
        sealed_reconciliation
        and int(sealed_reconciliation['scope_contract_version']) == 3
        and str(sealed_reconciliation['trust_state']) == 'trusted_current'
        and int(sealed_reconciliation['scope_issuer_count']) == len(reconciliation_scope)
        and int(sealed_reconciliation['association_count'])
            == current_reconciliation['association_count']
        and int(sealed_reconciliation['accession_count'])
            == current_reconciliation['accession_count']
        and int(sealed_reconciliation['shared_accession_count'])
            == current_reconciliation['shared_accession_count']
        and str(sealed_reconciliation['association_sha256'])
            == current_reconciliation['association_sha256']
        and cache_snapshot
        and int(cache_snapshot['scope_contract_version']) == 3
        and str(cache_snapshot['trust_state']) == 'trusted_current'
        and str(cache_snapshot['cache_manifest_sha256'])
            == str(sealed_reconciliation['cache_manifest_sha256'])
        and str(cache_snapshot['cache_manifest_json'])
            == str(sealed_reconciliation['cache_manifest_json'])
        and str(sealed_reconciliation['ingestion_config_sha256'])
            == expected_config_sha256
        and str(sealed_reconciliation['issuer_scope_sha256'])
            == expected_scope_sha256
        and str(cache_snapshot['ingestion_config_sha256'])
            == expected_config_sha256
        and str(cache_snapshot['issuer_scope_sha256'])
            == expected_scope_sha256
        and _verify_cache_manifest(
            configured_cache / str(cache_snapshot['seal_relative_path']),
            str(cache_snapshot['cache_manifest_json']),
            str(cache_snapshot['cache_manifest_sha256']),
        )
    )
    expected_active = int(cfg_get(bundle.payload, "universe.expected_current_rows"))
    active = len(active_universe_tickers(conn))
    blank_subtypes = int(conn.execute("SELECT COUNT(*) FROM dim_consumer_defensive_taxonomy WHERE applicability_subtype='' OR applicability_subtype IS NULL").fetchone()[0])
    accepted_missing = int(conn.execute("SELECT COUNT(*) FROM fact_sec_xbrl_fact_raw WHERE accepted_at IS NULL OR accepted_at='' ").fetchone()[0])
    filing_acceptance_missing = int(conn.execute("SELECT COUNT(*) FROM fact_sec_filing WHERE accepted_at IS NULL OR accepted_at=''").fetchone()[0])
    profiles = int(
        conn.execute(
            """SELECT COUNT(*) FROM dim_issuer_reporting_profile p
               JOIN dim_consumer_defensive_taxonomy t ON t.ticker=p.ticker
               WHERE t.model_family=?""",
            (MODEL_FAMILY,),
        ).fetchone()[0]
    )
    features = int(conn.execute(
        "SELECT COUNT(*) FROM feature_financial_statement WHERE model_family=? AND asof_date=?",
        (MODEL_FAMILY, asof_date),
    ).fetchone()[0])
    metric_count = int(conn.execute("SELECT COUNT(*) FROM dim_specialized_metric").fetchone()[0])
    expected_taxonomy = int(cfg_get(bundle.payload, "specialized_disclosure_census.expected_applicability_rows"))
    missing_ciks = int(conn.execute("""SELECT COUNT(*) FROM dim_consumer_defensive_taxonomy t JOIN dim_company c ON c.company_id=t.company_id WHERE t.model_family=? AND (c.cik IS NULL OR c.cik='')""", (MODEL_FAMILY,)).fetchone()[0])
    covered_profiles = int(
        conn.execute(
            """SELECT COUNT(*) FROM dim_issuer_reporting_profile p
               JOIN dim_consumer_defensive_taxonomy t ON t.ticker=p.ticker
               WHERE t.model_family=? AND p.coverage_status='covered'""",
            (MODEL_FAMILY,),
        ).fetchone()[0]
    )
    fallback_required_profiles = int(
        conn.execute(
            """SELECT COUNT(*) FROM dim_issuer_reporting_profile p
               JOIN dim_consumer_defensive_taxonomy t ON t.ticker=p.ticker
               WHERE t.model_family=? AND p.inline_xbrl_fallback_required=1""",
            (MODEL_FAMILY,),
        ).fetchone()[0]
    )
    fallback_provenance_mismatches = int(conn.execute(
        '''SELECT COUNT(*) FROM dim_issuer_reporting_profile p
           WHERE p.latest_fallback_accepted_at IS NOT NULL AND NOT EXISTS(
               SELECT 1 FROM fact_sec_inline_xbrl_fallback_run r
               WHERE r.ticker=p.ticker AND r.asof_date=? AND r.status='covered'
                 AND r.accepted_at=p.latest_fallback_accepted_at
                 AND r.document_sha256=p.fallback_document_sha256
                 AND r.parser_version=p.fallback_parser_version
                 AND r.mapped_fact_count>0
                 AND EXISTS(SELECT 1 FROM fact_sec_xbrl_fact_raw f
                    WHERE f.ticker=r.ticker
                      AND f.accession_number=r.accession_number
                      AND f.source_id=?))''',
        (asof_date, SEC_INLINE),
    ).fetchone()[0])
    document_tickers = int(
        conn.execute(
            """SELECT COUNT(DISTINCT d.issuer_ticker)
               FROM bridge_sec_filing_document_company d
               JOIN bridge_sec_filing_company b
                 ON b.accession_number=d.accession_number
                AND b.issuer_company_id=d.issuer_company_id
               JOIN dim_consumer_defensive_taxonomy t ON t.ticker=d.issuer_ticker
               WHERE t.model_family=? AND d.hydration_status='hydrated'
                 AND d.accepted_at<=?
                 AND COALESCE((SELECT e.event_type
                     FROM sec_filing_company_association_event e
                     WHERE e.accession_number=b.accession_number
                       AND e.issuer_company_id=b.issuer_company_id
                       AND e.effective_asof<=?
                     ORDER BY e.effective_asof DESC,e.event_id DESC LIMIT 1),
                     CASE WHEN b.association_status='active' THEN 'observed' ELSE 'retired' END)
                     IN ('observed','reactivated')""",
            (MODEL_FAMILY, cutoff, cutoff),
        ).fetchone()[0]
    )
    taxonomy_count = int(conn.execute("SELECT COUNT(*) FROM dim_consumer_defensive_taxonomy").fetchone()[0])
    terms_payload = _read_yaml(
        resolve_path(cfg_get(bundle.payload, "specialized_disclosure_census.terms_path"), base_dir=bundle.base_dir)
    )
    parser_version = str(terms_payload["parser_version"])
    summary = int(
        conn.execute(
            """SELECT COUNT(*) FROM fact_specialized_metric_disclosure_summary s
               JOIN dim_consumer_defensive_taxonomy t ON t.ticker=s.ticker
               WHERE t.model_family=? AND s.parser_version=? AND s.asof_date=? AND s.source_id=?""",
            (MODEL_FAMILY, parser_version, cutoff[:10], DISCLOSURE_SOURCE),
        ).fetchone()[0]
    )
    feature_quality = {str(row[0]): int(row[1]) for row in conn.execute(
        """SELECT financial_quality_status,COUNT(*) FROM feature_financial_statement
           WHERE model_family=? AND asof_date=? GROUP BY financial_quality_status""",
        (MODEL_FAMILY, asof_date),
    )}
    disclosure_status = {
        str(row[0]): int(row[1])
        for row in conn.execute(
            """SELECT disclosure_status, COUNT(*)
               FROM fact_specialized_metric_disclosure_summary s
               JOIN dim_consumer_defensive_taxonomy t ON t.ticker=s.ticker
               WHERE t.model_family=? AND s.parser_version=? AND s.asof_date=? AND s.source_id=?
               GROUP BY disclosure_status""",
            (MODEL_FAMILY, parser_version, cutoff[:10], DISCLOSURE_SOURCE),
        )
    }
    annual_forms = {
        str(row[0] or "unknown"): int(row[1])
        for row in conn.execute(
            """SELECT p.primary_annual_form, COUNT(*)
               FROM dim_issuer_reporting_profile p
               JOIN dim_consumer_defensive_taxonomy t ON t.ticker=p.ticker
               WHERE t.model_family=? GROUP BY p.primary_annual_form""",
            (MODEL_FAMILY,),
        )
    }
    definition_version = str(_read_yaml(resolve_path(cfg_get(bundle.payload, "financial_features.concept_map"), base_dir=bundle.base_dir))["definition_version"])
    semantic_validation = _validate_financial_semantics(
        conn,
        asof_date=asof_date,
        definition_version=definition_version,
    )
    canonical_current_version = int(conn.execute(
        "SELECT COUNT(*) FROM fact_financial_statement_canonical WHERE definition_version=? AND accepted_at<=?",
        (definition_version, cutoff),
    ).fetchone()[0])
    fx_missing = int(conn.execute(
        "SELECT COUNT(*) FROM fact_financial_statement_canonical WHERE definition_version=? AND accepted_at<=? AND quality_status='fx_missing'",
        (definition_version, cutoff),
    ).fetchone()[0])
    required_fx = {str(row[0]) for row in conn.execute(
        "SELECT DISTINCT reported_currency FROM fact_financial_statement_canonical WHERE definition_version=? AND accepted_at<=? AND reported_currency<>'USD'",
        (definition_version, cutoff),
    )}
    covered_fx = {str(row[0]) for row in conn.execute(
        """SELECT DISTINCT base_currency FROM fact_fx_rate
           WHERE quote_currency='USD' AND rate_date<=? AND quality_status='usable'""",
        (cutoff[:10],),
    )}
    census_source_status = conn.execute(
        "SELECT status FROM source_registry WHERE source_id=?", (DISCLOSURE_SOURCE,)
    ).fetchone()
    stage4_rows = {
        "filings": int(conn.execute("SELECT COUNT(*) FROM fact_sec_filing WHERE accepted_at<=?", (cutoff,)).fetchone()[0]),
        "raw_xbrl_facts": int(conn.execute("SELECT COUNT(*) FROM fact_sec_xbrl_fact_raw WHERE accepted_at<=?", (cutoff,)).fetchone()[0]),
        "canonical_facts": canonical_current_version,
        "fx_rates": int(conn.execute("SELECT COUNT(*) FROM fact_fx_rate WHERE rate_date<=?", (asof_date,)).fetchone()[0]),
        "usable_fx_rates": int(conn.execute("SELECT COUNT(*) FROM fact_fx_rate WHERE rate_date<=? AND quality_status='usable'", (asof_date,)).fetchone()[0]),
        "quarantined_fx_rates": int(conn.execute("SELECT COUNT(*) FROM fact_fx_rate WHERE rate_date<=? AND quality_status='quarantined'", (asof_date,)).fetchone()[0]),
        "hydrated_documents": int(conn.execute(
            """SELECT COUNT(*) FROM bridge_sec_filing_document_company d
               JOIN bridge_sec_filing_company b
                 ON b.accession_number=d.accession_number
                AND b.issuer_company_id=d.issuer_company_id
               WHERE d.hydration_status='hydrated' AND d.accepted_at<=?
                 AND COALESCE((SELECT e.event_type
                     FROM sec_filing_company_association_event e
                     WHERE e.accession_number=b.accession_number
                       AND e.issuer_company_id=b.issuer_company_id
                       AND e.effective_asof<=?
                     ORDER BY e.effective_asof DESC,e.event_id DESC LIMIT 1),
                     CASE WHEN b.association_status='active' THEN 'observed' ELSE 'retired' END)
                     IN ('observed','reactivated')""",
            (cutoff, cutoff),
        ).fetchone()[0]),
        "census_evidence": int(conn.execute(
            """SELECT COUNT(*) FROM fact_specialized_metric_disclosure_census c
               JOIN dim_consumer_defensive_taxonomy t ON t.ticker=c.ticker
               WHERE t.model_family=? AND c.parser_version=? AND c.source_id=? AND c.accepted_at<=?""",
            (MODEL_FAMILY, parser_version, DISCLOSURE_SOURCE, cutoff),
        ).fetchone()[0]),
    }
    # Recompute integrity gates in the requested PIT scope. Future rows remain
    # visible in global diagnostics but cannot change a historical verdict.
    filing_bridge_orphans = int(conn.execute(
        '''SELECT COUNT(*) FROM fact_sec_filing f WHERE f.accepted_at<=?
           AND NOT EXISTS(SELECT 1 FROM bridge_sec_filing_company b
                          WHERE b.accession_number=f.accession_number)''',
        (cutoff,),
    ).fetchone()[0])
    filing_bridge_identity_mismatches = int(conn.execute(
        '''SELECT COUNT(*) FROM bridge_sec_filing_company b
           JOIN fact_sec_filing f ON f.accession_number=b.accession_number
           LEFT JOIN dim_company c ON c.company_id=b.issuer_company_id
           WHERE f.accepted_at<=? AND (c.company_id IS NULL
              OR b.issuer_ticker<>c.primary_ticker
              OR printf('%010d',CAST(b.issuer_cik AS INTEGER))<>
                 printf('%010d',CAST(c.cik AS INTEGER)))''',
        (cutoff,),
    ).fetchone()[0])
    filing_bridge_acceptance_missing = int(conn.execute(
        '''SELECT COUNT(*) FROM bridge_sec_filing_company b
           JOIN fact_sec_filing f ON f.accession_number=b.accession_number
           WHERE f.accepted_at<=? AND (b.accepted_at IS NULL OR b.accepted_at='')''',
        (cutoff,),
    ).fetchone()[0])
    filing_bridge_acceptance_mismatches = int(conn.execute(
        '''SELECT COUNT(*) FROM fact_sec_filing f
           JOIN (
             SELECT accession_number,MAX(accepted_at) AS max_accepted_at
             FROM bridge_sec_filing_company
             WHERE accepted_at<=?
             GROUP BY accession_number
           ) b ON b.accession_number=f.accession_number
           WHERE f.accepted_at<=?
             AND COALESCE(f.accepted_at,'')<>COALESCE(b.max_accepted_at,'')''',
        (cutoff, cutoff),
    ).fetchone()[0])
    filing_metadata_conflicts = int(conn.execute(
        '''SELECT COUNT(*) FROM fact_sec_filing WHERE accepted_at<=?
           AND metadata_quality_flags_json LIKE '%association_metadata_conflict%' ''',
        (cutoff,),
    ).fetchone()[0])
    filing_bridge_bad_urls = _filing_bridge_bad_url_count(conn, cutoff=cutoff)
    filing_owner_leaks = int(conn.execute(
        '''SELECT COUNT(*) FROM fact_sec_filing f WHERE f.accepted_at<=?
           AND EXISTS(SELECT 1 FROM bridge_sec_filing_company b
                      WHERE b.accession_number=f.accession_number
                        AND b.relationship_evidence='observed_in_issuer_submissions_feed')
           AND (f.company_id IS NOT NULL OR f.ticker<>'ACCESSION_NEUTRAL'
                OR f.cik IS NOT NULL OR f.source_url IS NOT NULL)''',
        (cutoff,),
    ).fetchone()[0])
    accepted_missing = int(conn.execute(
        '''SELECT COUNT(*) FROM fact_sec_xbrl_fact_raw r
           JOIN fact_sec_filing f ON f.accession_number=r.accession_number
           WHERE f.accepted_at<=? AND (r.accepted_at IS NULL OR r.accepted_at='')''',
        (cutoff,),
    ).fetchone()[0])
    filing_acceptance_missing = int(conn.execute(
        '''SELECT COUNT(*) FROM fact_sec_filing
           WHERE filing_date<=? AND (accepted_at IS NULL OR accepted_at='')''',
        (asof_date,),
    ).fetchone()[0])
    raw_observation_id_missing = _count_raw_observation_identity_mismatches(
        conn,cutoff=cutoff,
    )
    canonical_observation_id_mismatch = int(conn.execute('''SELECT COUNT(*)
        FROM fact_financial_statement_canonical c
        LEFT JOIN fact_sec_xbrl_fact_raw r ON r.raw_fact_id=c.source_raw_fact_id
        WHERE c.accepted_at<=? AND (
          c.source_observation_id IS NULL OR length(c.source_observation_id)<>64
          OR c.source_observation_id<>r.source_observation_id)''',
        (cutoff,)).fetchone()[0])
    association_event_missing = int(conn.execute('''SELECT COUNT(*)
        FROM bridge_sec_filing_company b WHERE b.accepted_at<=?
          AND NOT EXISTS(SELECT 1 FROM sec_filing_company_association_event e
            WHERE e.accession_number=b.accession_number
              AND e.issuer_company_id=b.issuer_company_id)''',
        (cutoff,)).fetchone()[0])
    association_event_hash_mismatch = _count_association_event_identity_mismatches(
        conn,cutoff=cutoff,
    )
    stale_lifecycle_features = int(conn.execute('''SELECT COUNT(*)
        FROM feature_financial_statement f WHERE f.asof_date<=?
          AND EXISTS(SELECT 1 FROM sec_filing_company_association_event e
            WHERE e.issuer_ticker=f.ticker
              AND e.event_type IN ('retired','reactivated')
              AND substr(e.effective_asof,1,10)<=f.asof_date
              AND e.created_at>f.created_at)''',(asof_date,)).fetchone()[0])
    stale_lifecycle_disclosure_summaries = (
        _count_stale_lifecycle_disclosure_summaries(
            conn, asof_date=asof_date, parser_version=parser_version
        )
    )
    stage4_rows['global_foreign_key_violations'] = foreign_key_violations
    checks = {
        'canonical_acceptance_matches_raw_lineage': canonical_acceptance_mismatches == 0,
        'census_acceptance_matches_document_lineage': census_acceptance_mismatches == 0,
        "full_taxonomy_count": taxonomy_count == expected_taxonomy,
        "historical_and_current_ciks_complete": missing_ciks == 0,
        "reporting_profiles_complete": profiles == taxonomy_count,
        "companyfacts_coverage_complete": covered_profiles == taxonomy_count,
        "inline_xbrl_fallback_provenance_complete": (
            fallback_provenance_mismatches == 0
        ),
        "filing_document_coverage_complete": document_tickers == taxonomy_count,
        "active_universe_count": active == expected_active,
        "applicability_subtypes_complete": blank_subtypes == 0,
        "raw_facts_are_acceptance_dated": accepted_missing == 0,
        'raw_fact_observation_ids_complete': raw_observation_id_missing == 0,
        'canonical_observation_lineage_deterministic': (
            canonical_observation_id_mismatch == 0
        ),
        'filing_association_lifecycle_complete': association_event_missing == 0,
        'filing_association_lifecycle_hashes_exact': (
            association_event_hash_mismatch == 0
        ),
        'lifecycle_affected_financial_features_rebuilt': (
            stale_lifecycle_features == 0
        ),
        'lifecycle_affected_disclosure_summaries_rebuilt': (
            stale_lifecycle_disclosure_summaries == 0
        ),
        "filings_are_acceptance_dated": filing_acceptance_missing == 0,
        "filing_bridge_covers_canonical_accessions": filing_bridge_orphans == 0,
        "filing_bridge_issuer_identity_valid": filing_bridge_identity_mismatches == 0,
        "filing_bridge_acceptance_complete": filing_bridge_acceptance_missing == 0,
        "filing_bridge_acceptance_reconciled": filing_bridge_acceptance_mismatches == 0,
        "filing_parser_view_matches_bridge": filing_view_rows == filing_bridge_rows,
        "filing_metadata_context_unambiguous": filing_metadata_conflicts == 0,
        "canonical_filings_are_owner_neutral": filing_owner_leaks == 0,
        "filing_bridge_urls_are_issuer_scoped": filing_bridge_bad_urls == 0,
        "filing_parser_view_contract_exact": (
            actual_view_columns == required_view_columns
        ),
        "foreign_keys_valid": foreign_key_violations == 0,
        "filing_association_reconciliation_complete": filing_reconciliation_complete,
        "canonical_fx_conversion_complete": fx_missing == 0 and required_fx.issubset(covered_fx),
        "reporting_profiles_present": profiles == taxonomy_count,
        "financial_feature_snapshot_present": features == taxonomy_count,
        "canonical_definition_version_current": canonical_current_version > 0,
        "canonical_semantic_lineage_valid": semantic_validation["invalid_canonical_rows"] == 0,
        "canonical_reporting_context_unambiguous": semantic_validation["ambiguous_reporting_contexts"] == 0,
        "financial_feature_lineage_valid": semantic_validation["invalid_feature_rows"] == 0,
        "financial_feature_definition_current": semantic_validation["wrong_feature_definition_rows"] == 0,
        "census_matrix_complete": summary == taxonomy_count * metric_count,
        "census_source_provenance_current": bool(census_source_status and str(census_source_status[0]) == "active"),
        "specialized_metrics_remain_nonproduction": float(cfg_get(bundle.payload, "specialized_metrics.production_default_weight")) == 0.0,
    }
    return {"status": "PASS" if all(checks.values()) else "FAIL", "checks": checks, "counts": {"active": active, "taxonomy": taxonomy_count, "expected_taxonomy": expected_taxonomy, "missing_ciks": missing_ciks, "profiles": profiles, "covered_profiles": covered_profiles, "fallback_required_profiles": fallback_required_profiles, "fallback_provenance_mismatches": fallback_provenance_mismatches, "document_tickers": document_tickers, "features": features, "metrics": metric_count, "census_summary": summary, "filing_acceptance_missing": filing_acceptance_missing, "raw_observation_id_missing": raw_observation_id_missing, "canonical_observation_id_mismatch": canonical_observation_id_mismatch, "association_event_missing": association_event_missing, "association_event_hash_mismatch": association_event_hash_mismatch, "stale_lifecycle_features": stale_lifecycle_features, "stale_lifecycle_disclosure_summaries": stale_lifecycle_disclosure_summaries, "canonical_current_version": canonical_current_version, "canonical_fx_missing": fx_missing, "required_fx_currencies": len(required_fx), "covered_fx_currencies": len(required_fx & covered_fx), **semantic_validation, **stage4_rows}, "coverage": {"financial_quality_status": feature_quality, "disclosure_status": disclosure_status, "primary_annual_forms": annual_forms}}


def _validate_financial_semantics(
    conn: sqlite3.Connection,
    *,
    asof_date: str,
    definition_version: str,
) -> dict[str, int]:
    """Validate stored lineage and reject any ratio that crossed contexts."""

    feature_fields = {
        "gross_margin": "gross_margin",
        "operating_margin": "operating_margin",
        "free_cash_flow_margin": "free_cash_flow_margin",
        "return_on_invested_capital": "return_on_invested_capital",
        "net_debt_to_ebitda": "net_debt_to_ebitda",
        "inventory_turnover": "inventory_turnover",
    }
    invalid_features = 0
    wrong_feature_versions = 0
    feature_rows = conn.execute(
        """SELECT * FROM feature_financial_statement
           WHERE model_family=? AND asof_date=?""",
        (MODEL_FAMILY, asof_date),
    ).fetchall()
    for row in feature_rows:
        invalid = False
        wrong_feature_versions += int(
            str(row["feature_definition_version"] or "") != FEATURE_DEFINITION_VERSION
        )
        numeric_fields = ["revenue_ttm_usd", *feature_fields.values()]
        for field in numeric_fields:
            value = row[field]
            if value is not None and not math.isfinite(float(value)):
                invalid = True
        try:
            lineage = json.loads(str(row["lineage_json"] or ""))
            reasons = json.loads(str(row["financial_quality_reason"] or "[]"))
        except (TypeError, ValueError, json.JSONDecodeError):
            invalid_features += 1
            continue
        if not isinstance(lineage, dict) or not isinstance(reasons, list):
            invalid = True
        basis = row["basis_period_end"]
        populated = [row[field] is not None for field in numeric_fields]
        if any(populated) and not basis:
            invalid = True
        if basis:
            try:
                invalid |= date.fromisoformat(str(basis)) > date.fromisoformat(asof_date)
            except ValueError:
                invalid = True
            lineage_basis = lineage.get("basis") if isinstance(lineage, dict) else None
            if not isinstance(lineage_basis, dict) or str(lineage_basis.get("period_end")) != str(basis):
                invalid = True
        if str(row["financial_quality_status"] or "") == "complete":
            if not all(populated) or reasons:
                invalid = True
        ratio_flags = lineage.get("ratio_quality_flags", {}) if isinstance(lineage, dict) else {}
        if not isinstance(ratio_flags, dict):
            invalid = True
            ratio_flags = {}
        fatal_prefixes = (
            "missing_input:", "period_end_mismatch:", "period_start_mismatch:",
            "taxonomy_mismatch:", "currency_mismatch:", "invalid_input:",
            "invalid_arithmetic:",
        )
        for ratio_name, field in feature_fields.items():
            flags = ratio_flags.get(ratio_name, [])
            if not isinstance(flags, list):
                invalid = True
                continue
            if row[field] is not None and any(str(flag).startswith(fatal_prefixes) for flag in flags):
                invalid = True
        invalid_features += int(invalid)

    invalid_canonical = 0
    canonical_rows = conn.execute(
        """SELECT c.*,r.ticker AS raw_ticker,r.accession_number AS raw_accession,
                  r.taxonomy AS raw_taxonomy,r.concept AS raw_concept,
                  r.numeric_value AS raw_numeric_value,
                  r.source_observation_id AS raw_source_observation_id
           FROM fact_financial_statement_canonical c
           LEFT JOIN fact_sec_xbrl_fact_raw r ON r.raw_fact_id=c.source_raw_fact_id
           WHERE c.definition_version=? AND c.accepted_at<=?""",
        (definition_version, asof_date + "T23:59:59Z"),
    ).fetchall()
    for row in canonical_rows:
        invalid = False
        required_text = (
            "accession_number", "taxonomy", "source_concept", "selection_method",
            "sign_normalization_method",
        )
        if any(not str(row[field] or "").strip() for field in required_text):
            invalid = True
        if row["source_raw_fact_id"] is None or row["raw_ticker"] is None:
            invalid = True
        else:
            invalid |= not str(row['source_observation_id'] or '').strip()
            invalid |= str(row['source_observation_id'] or '') != str(
                row['raw_source_observation_id'] or ''
            )
            invalid |= str(row["ticker"]) != str(row["raw_ticker"])
            invalid |= str(row["accession_number"]) != str(row["raw_accession"])
            invalid |= str(row["taxonomy"]) != str(row["raw_taxonomy"])
            invalid |= str(row["source_concept"]) != str(row["raw_concept"])
            if row["reported_value"] is None or row["raw_numeric_value"] is None:
                invalid = True
            else:
                invalid |= not math.isclose(
                    float(row["reported_value"]),
                    float(row["raw_numeric_value"]),
                    rel_tol=1e-12,
                    abs_tol=1e-9,
                )
        try:
            flags = json.loads(str(row["quality_flags_json"] or ""))
            invalid |= not isinstance(flags, list)
        except (TypeError, ValueError, json.JSONDecodeError):
            invalid = True
        for field in ("value", "reported_value", "value_usd", "fx_rate"):
            if row[field] is not None and not math.isfinite(float(row[field])):
                invalid = True
        if str(row["quality_status"]) == "complete":
            if row["fx_rate"] is None or row["value_usd"] is None:
                invalid = True
            else:
                invalid |= not math.isclose(
                    float(row["value_usd"]),
                    float(row["value"]) * float(row["fx_rate"]),
                    rel_tol=1e-12,
                    abs_tol=1e-6,
                )
        invalid_canonical += int(invalid)

    ambiguous_contexts = int(
        conn.execute(
            """SELECT COUNT(*) FROM (
                   SELECT ticker,accession_number,COUNT(DISTINCT taxonomy) AS taxonomies,
                          COUNT(DISTINCT reported_currency) AS currencies
                   FROM fact_financial_statement_canonical
                   WHERE definition_version=? AND accepted_at<=?
                   GROUP BY ticker,accession_number
                   HAVING taxonomies>1 OR currencies>1
               )""",
            (definition_version, asof_date + "T23:59:59Z"),
        ).fetchone()[0]
    )
    return {
        "invalid_canonical_rows": invalid_canonical,
        "ambiguous_reporting_contexts": ambiguous_contexts,
        "invalid_feature_rows": invalid_features,
        "wrong_feature_definition_rows": wrong_feature_versions,
    }
